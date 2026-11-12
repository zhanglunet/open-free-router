# -*- coding: utf-8 -*-
"""PRD-378/T2（trend issue #1882 的 T2 环节）：后端异常验证台最小实现。

按 T1 设计（docs/plans/2026-11-12-prd-378-backend-verification-bench-design.md）
§4 落地：

  - verify_alert(alert, probe_data, recheck_fn)：逐条告警拨测复现验证
    ——recheck_fn 注入式重跑同一拨测判定（探针快照只读深拷贝，零新增
    检测逻辑 R2，拨测=对既有快照复算零外呼）；复现结果包含同 backend+
    异常类型 → VERIFIED，否则 UNVERIFIED（词表封闭）；探针快照指纹与
    PRD-350 routing_replay.snapshots_digest 同族口径（sha16: 前缀规范化
    摘要）；
  - verify_alerts(alerts, probe_data, recheck_fn)：换路推荐来源包装
    （接入点：availability 告警之后、reroute_inbox.collect
    (recommendations) 之前）——UNVERIFIED 拦下不进推荐来源（R1）；
  - BackendBenchLedger：机器可读 JSONL 落账（路径注入，append-only
    R3/R4）；八件套缺一 BENCH_INCOMPLETE 拒绝入账；
  - reverify：探针快照变更二次验证——指纹不符 → BENCH_STALE 拒绝复用
    旧验证（R4 关闭以最新 VERIFIED 为准）。

零网络零子进程零配置写零路由切换（学习网关只出推荐红线不变）；
示例全虚构占位。
测试：tests/test_prd378_t2_backend_verify_bench.py
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

BENCH_SCHEMA = "backend-verify-bench/v1"
VERDICTS = ("VERIFIED", "UNVERIFIED")
ERROR_CODES = ("BENCH_INCOMPLETE", "BENCH_REPRO_MISMATCH", "BENCH_STALE")


class BackendBenchError(ValueError):
    """验证台违规——显形拒绝。"""


def probe_fingerprint(probe_data: Any) -> str:
    """探针快照指纹（sha16: 前缀，与 350 snapshots_digest 同族口径）。"""
    canon = json.dumps(probe_data, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
    return "sha16:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _recusal(code: str, detail: str) -> dict[str, Any]:
    return {"schema": BENCH_SCHEMA, "refused": code, "detail": detail,
            "verdict": None}


def _reproduced(alert: dict[str, Any],
                rerun: list[dict[str, Any]]) -> bool:
    """复现判定：重跑结果中存在同 backend+异常类型的告警。"""
    for r in rerun:
        if (r.get("backend") == alert.get("backend")
                and r.get("status") == alert.get("status")):
            return True
    return False


def verify_alert(alert: dict[str, Any], probe_data: Any,
                 recheck_fn: Callable[[Any], list[dict[str, Any]]],
                 *, at: str = "") -> dict[str, Any]:
    """逐条告警拨测复现验证（R1/R2）：重跑同一拨测判定，复现 → VERIFIED。"""
    if not isinstance(alert, dict):
        return _recusal("BENCH_INCOMPLETE", "alert 非对象")
    missing = [k for k in ("alert_id", "backend", "kind", "source_ref",
                           "status") if not alert.get(k)]
    if missing:
        return _recusal("BENCH_INCOMPLETE", f"八件套缺 {missing}")
    if not callable(recheck_fn):
        return _recusal("BENCH_INCOMPLETE", "recheck_fn 未注入")
    rerun = recheck_fn(copy.deepcopy(probe_data)) or []
    match = _reproduced(alert, rerun)
    return {
        "schema": BENCH_SCHEMA,
        "alert_id": alert["alert_id"],
        "backend": alert["backend"],
        "kind": alert["kind"],
        "source_ref": alert["source_ref"],
        "probe_fingerprint": probe_fingerprint(probe_data),
        "alert": copy.deepcopy(alert),
        "reproduce": {"match": match,
                      "detail": f"重跑拨测判定 {len(rerun)} 条（占位）"},
        "verdict": "VERIFIED" if match else "UNVERIFIED",
        "at": at,
    }


def verify_alerts(alerts: list[dict[str, Any]], probe_data: Any,
                  recheck_fn: Callable[[Any], list[dict[str, Any]]],
                  *, at: str = "") -> dict[str, Any]:
    """换路推荐来源包装（接入点）：UNVERIFIED 拦下不进推荐来源（R1）。"""
    verified: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for a in alerts or []:
        out = verify_alert(a, probe_data, recheck_fn, at=at)
        if out.get("refused") or out["verdict"] == "UNVERIFIED":
            unverified.append(out)
        else:
            verified.append(out)
    return {"schema": BENCH_SCHEMA, "verified": verified,
            "unverified": unverified, "unverified_count": len(unverified)}


class BackendBenchLedger:
    """验证账本（R3/R4）：JSONL append-only；八件套缺一拒绝入账。"""

    _REQUIRED = ("alert_id", "backend", "kind", "source_ref",
                 "probe_fingerprint", "alert", "verdict")

    def __init__(self, path: str | Path | None = None) -> None:
        self._records: list[dict[str, Any]] = []
        self._path = Path(path) if path else None

    def record(self, out: dict[str, Any], *, at: str = "") -> dict[str, Any]:
        if out.get("refused"):
            raise BackendBenchError(f"{out['refused']}：{out['detail']}")
        rec = dict(out, at=at)
        missing = [k for k in self._REQUIRED if rec.get(k) in (None, "")]
        if missing:
            raise BackendBenchError(f"BENCH_INCOMPLETE：账目缺 {missing}")
        if rec["verdict"] not in VERDICTS:
            raise BackendBenchError(f"BENCH_INCOMPLETE：verdict 词表外 "
                                    f"{rec['verdict']!r}")
        if rec["verdict"] == "VERIFIED" and not rec["reproduce"]["match"]:
            raise BackendBenchError("BENCH_REPRO_MISMATCH：声称 VERIFIED 但"
                                    "复现 match=false——验证结论与复现证据"
                                    "不一致")
        self._records.append(copy.deepcopy(rec))
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False,
                                    sort_keys=True) + "\n")
        return dict(rec)

    def entries(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._records]

    def latest_for(self, alert_id: str) -> dict[str, Any] | None:
        found = [r for r in self._records if r["alert_id"] == alert_id]
        return dict(found[-1]) if found else None

    def reverify(self, alert_id: str, probe_data: Any,
                 recheck_fn: Callable[[Any], list[dict[str, Any]]],
                 *, at: str = "") -> dict[str, Any]:
        """探针快照变更二次验证（R4）：指纹不符 → BENCH_STALE 拒绝复用。"""
        prior = self.latest_for(alert_id)
        fp_now = probe_fingerprint(probe_data)
        if prior and prior["probe_fingerprint"] != fp_now:
            return _recusal("BENCH_STALE",
                            f"{alert_id} 旧验证探针指纹与当前不符——快照变更"
                            "后须重新验证，旧 VERIFIED 不复用")
        base = {"alert_id": alert_id,
                "backend": (prior or {}).get("backend") or "",
                "kind": (prior or {}).get("kind") or "",
                "source_ref": (prior or {}).get("source_ref") or "",
                "status": ((prior or {}).get("alert") or {}).get("status")}
        return self.record(verify_alert(base, probe_data, recheck_fn,
                                        at=at), at=at)
