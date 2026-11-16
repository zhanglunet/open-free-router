# -*- coding: utf-8 -*-
"""PRD-467/T2（trend issue #2256 的 T2 环节）：按密钥额度跟踪最小实现。

按 T1 设计（docs/plans/2026-11-17-per-key-quota-design.md）§2/§3 落地：
  - 四维滑动窗口（RPM 60s×1s桶 / RPD 24h×1h桶 / TPM 60s / TPD 24h，全部
    滚动窗口——上游复位提示只是提示不是复位，§2.6 写死不实现清零）；
  - 键格式（§2.2）：{platform}:{upstream_model_id}:slot-{n}:{window}，
    键里只放槽序号永不放密钥原文（隐私线）；池键 pool:{pool_id}:{window}；
  - 在途租约（§3）：派发前领租约、落定释放；放行条件每维
    recorded + in_flight < limit（严格小于，到上限就停）；
  - 上限来源三档优先级（§2.3）：上游头观测（30 分钟新鲜度）> 已核实文档
    （documented）> 签名目录值；三档全无 → 只计数不守门（没有证据不定罪）；
  - 丢计数保守（§2.5 安全不变量）：上限未知 → 只计数；上限已知、已用
    未知 → 该键按已耗尽处理（quota_counter_lost），不静默放行；
  - token 估算（§2.4）：补全预留默认 512，落定按实际 usage 多退少补
    （只退不追，超预留记 tokens_under_reserved）；值永远标 estimated。
纯内存实现（SQLite 镜像属 T3 持久化增量）；零网络零子进程；示例全占位。
测试：tests/test_prd467_t2_quota_ledger.py
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

LEDGER_SCHEMA = "quota-ledger/v1"
WINDOWS = ("rpm", "rpd", "tpm", "tpd")
WINDOW_SECONDS = {"rpm": 60, "rpd": 86400, "tpm": 60, "tpd": 86400}
BUCKET_SECONDS = {"rpm": 1, "rpd": 3600, "tpm": 1, "tpd": 3600}
SIGNAL_MAX_AGE_SECONDS = 1800           # 上游头观测新鲜度（PRD-096/T1 §2）
COMPLETION_RESERVE_DEFAULT = 512        # 补全预留（决策点 D1）
ERROR_CODES = ("QUOTA_INCOMPLETE", "QUOTA_COUNTER_LOST", "QUOTA_EXHAUSTED")
QUOTA_COUNTER_LOST = "quota_counter_lost"


def count_key(platform: str, upstream_model_id: str, slot: int,
              window: str) -> str:
    """§2.2 键格式：只放槽序号，永不放密钥原文。"""
    return f"{platform}:{upstream_model_id}:slot-{slot}:{window}"


def pool_key(pool_id: str, window: str) -> str:
    return f"pool:{pool_id}:{window}"


class _SlidingWindow:
    """滚动窗口：桶数组，时间推进丢过期桶，计数 O(桶数)。"""

    def __init__(self, window: str) -> None:
        self.window = window
        self.sec = WINDOW_SECONDS[window]
        self.bucket_sec = BUCKET_SECONDS[window]
        self.buckets: dict[int, dict[str, int]] = defaultdict(
            lambda: {"requests": 0, "tokens_est": 0})

    def prune(self, now: float) -> None:
        for ts in list(self.buckets):
            if now - ts * self.bucket_sec >= self.sec:
                del self.buckets[ts]

    def record(self, now: float, requests: int = 0,
               tokens_est: int = 0) -> None:
        self.prune(now)
        ts = int(now // self.bucket_sec)
        self.buckets[ts]["requests"] += requests
        self.buckets[ts]["tokens_est"] += tokens_est

    def count(self, now: float) -> tuple[int, int]:
        self.prune(now)
        requests = sum(b["requests"] for b in self.buckets.values())
        tokens = sum(b["tokens_est"] for b in self.buckets.values())
        return requests, tokens


class QuotaLedger:
    """按（平台，模型，凭证槽）与池计量的额度账本（内存环；SQLite 镜像
    属 T3 持久化增量）。上限未知 → 只计数不守门；计数丢失 → 按已耗尽。"""

    def __init__(self, *, now: float | None = None,
                 completion_reserve: int = COMPLETION_RESERVE_DEFAULT) -> None:
        self._now = now if now is not None else time.time()
        self.completion_reserve = completion_reserve
        self.windows: dict[str, _SlidingWindow] = {}
        self.limits: dict[str, dict[str, Any]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self.counter_lost: set[str] = set()
        self._lease_seq = 0
        self.observations_under_reserve = 0

    # ---- 时间推进（测试注入时钟）----------------------------------------------
    def advance(self, seconds: float) -> None:
        self._now += seconds

    @property
    def now(self) -> float:
        return self._now

    # ---- 上限来源三档（§2.3，按优先级取）---------------------------------------
    def set_limit(self, platform: str, model_id: str, slot: int,
                  window: str, limit: int, *, source: str,
                  observed_at: float | None = None) -> None:
        _source_rank = {"observed_header": 0, "documented": 1,
                        "catalog": 2}
        _require(window in WINDOWS, "QUOTA_INCOMPLETE", f"window {window!r} 词表外")
        _require(source in _source_rank, "QUOTA_INCOMPLETE",
                 f"来源 {source!r} 词表外")
        key = count_key(platform, model_id, slot, window)
        existing = self.limits.get(key)
        if existing and _source_rank.get(existing["source"], 9) <= \
                _source_rank.get(source, 9):
            return                              # 已有更优来源，不覆盖
        self.limits[key] = {"limit": limit, "source": source,
                            "observed_at": observed_at}

    def get_limit(self, platform: str, model_id: str, slot: int,
                  window: str) -> dict[str, Any] | None:
        lim = self.limits.get(count_key(platform, model_id, slot, window))
        if lim is None:
            return None
        if (lim.get("source") == "observed_header"
                and lim.get("observed_at") is not None
                and self._now - lim["observed_at"] > SIGNAL_MAX_AGE_SECONDS):
            return None                          # 过期降档：视为无此档
        return lim

    # ---- 计数 ------------------------------------------------------------------
    def record_dispatch(self, *, platform: str, model_id: str, slot: int,
                        tokens_est: int = 0) -> None:
        self._record(platform, model_id, slot, requests=1,
                     tokens_est=tokens_est)

    def record_pool_dispatch(self, *, pool_id: str,
                             tokens_est: int = 0) -> None:
        for w in WINDOWS:
            self._win(pool_key(pool_id, w), w).record(
                self._now, requests=1, tokens_est=tokens_est)

    def _win(self, key: str, window: str) -> _SlidingWindow:
        """按 count_key/pool_key 惰性建窗。"""
        if key not in self.windows:
            self.windows[key] = _SlidingWindow(window)
        return self.windows[key]

    def _record(self, platform: str, model_id: str, slot: int, *,
                requests: int, tokens_est: int) -> None:
        for w in WINDOWS:
            self._win(count_key(platform, model_id, slot, w), w).record(
                self._now, requests=requests, tokens_est=tokens_est)

    def mark_counter_lost(self, platform: str, model_id: str, slot: int) -> None:
        """§2.5：上限已知、已用未知 → 该键按已耗尽（不静默放行）。"""
        self.counter_lost.add(count_key(platform, model_id, slot, "rpm"))

    def counter_is_lost(self, platform: str, model_id: str, slot: int) -> bool:
        return count_key(platform, model_id, slot, "rpm") in self.counter_lost

    # ---- 守门与租约（§3）--------------------------------------------------------
    def can_dispatch(self, *, platform: str, model_id: str, slot: int,
                     est_tokens: int = 0) -> tuple[bool, str]:
        """每维都要过：recorded + in_flight < limit（严格小于）。"""
        key_rpm = count_key(platform, model_id, slot, "rpm")
        for window, demand in (("rpm", 1), ("rpd", 1), ("tpm", est_tokens),
                               ("tpd", est_tokens)):
            lim = self.get_limit(platform, model_id, slot, window)
            if lim is None:
                continue                       # 上限未知：只计数不守门
            key = count_key(platform, model_id, slot, window)
            if key in self.counter_lost:
                # §2.5：上限已知、已用未知 → 按已耗尽（不静默放行）
                return False, QUOTA_COUNTER_LOST
            rec_r, rec_t = self._win(key, window).count(self._now)
            in_flight = sum(l["demand"].get(window, 0)
                            for l in self.leases.values()
                            if not l["released"] and l["key_base"] == key_rpm)
            used = (rec_r if window in ("rpm", "rpd") else rec_t) + in_flight
            # 放行条件（§3.1）：recorded + in_flight < limit（严格小于）
            if used >= lim["limit"]:
                return False, "QUOTA_EXHAUSTED"
        return True, "ok"

    def acquire_lease(self, *, platform: str, model_id: str, slot: int,
                      est_tokens: int | None = None) -> dict[str, Any]:
        """派发前领租约（§3.1）：进行中也算数；TPM 占补全预留。"""
        allowed, reason = self.can_dispatch(platform=platform,
                                            model_id=model_id, slot=slot,
                                            est_tokens=est_tokens or 0)
        if not allowed:
            raise QuotaLedgerError(reason, reason)
        self._lease_seq += 1
        reserve = (est_tokens if est_tokens is not None
                   else self.completion_reserve)
        lease = {"lease_id": f"lease-{self._lease_seq}",
                 "key_base": count_key(platform, model_id, slot, "rpm"),
                 "platform": platform, "model_id": model_id, "slot": slot,
                 "reserved_tokens": reserve, "at": self._now,
                 "demand": {"rpm": 1, "rpd": 1,
                            "tpm": reserve, "tpd": reserve},
                 "released": False}
        self.leases[lease["lease_id"]] = lease
        return lease

    def release_lease(self, *, lease_id: str, actual_tokens: int) -> dict[str, Any]:
        """落定释放：按实际 usage 多退少补（只退不追，超预留记观测）。"""
        lease = self.leases.get(lease_id)
        _require(lease is not None and not lease["released"],
                 "QUOTA_INCOMPLETE", f"租约 {lease_id} 不存在或已释放")
        lease["released"] = True
        lease["actual_tokens"] = actual_tokens
        if actual_tokens > lease["reserved_tokens"]:
            self.observations_under_reserve += 1    # 超预留不补扣，记观测
        self.record_dispatch(platform=lease["platform"],
                             model_id=lease["model_id"],
                             slot=lease["slot"], tokens_est=actual_tokens)
        return dict(lease)

    # ---- 池守门（§5 共享池）------------------------------------------------------
    def can_dispatch_pool(self, *, pool_id: str, limit: int) -> tuple[bool, str]:
        rec_r, _ = self._win(pool_key(pool_id, "rpm"), "rpm").count(self._now)
        if rec_r >= limit:
            return False, "QUOTA_EXHAUSTED"
        return True, "ok"


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise QuotaLedgerError(code, detail)


class QuotaLedgerError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}：{detail}")
        self.code = code
