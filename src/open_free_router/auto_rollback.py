"""PRD-280/T3: automatic rollback on health-check failure (objective signal).

Implements the T3 slice of the route-config-snapshot design
(``docs/plans/2026-10-29-prd-280-route-config-snapshot.md`` §4 #1, trend
issue #1403): after a config-apply action, when the health check reports
failure, roll the routing config back to the pre-change snapshot and record
one ``route_switch_failed`` audit event.

**Red line -- single objective trigger.** ``maybe_auto_rollback`` is the
ONLY automatic rollback path in this codebase, and its only decision input
is the health-check result's objective ``healthy`` flag:

- ``health_result["healthy"] is False``  -> restore + ``route_switch_failed``
  event (reason fixed to the failure class ``"health_check_failed"``);
- anything else (``healthy=True``, missing/None/unknown result) -> **no**
  rollback, **no** event, no other branch exists.

红线：学习/推荐面只出推荐、不自动执行；自动回滚只由健康检查失败这一客观
信号触发（本模块无学习网关依赖、无推荐输入、无时间窗/次数阈值等其他决
策维度）；一次失败一次回滚，回滚后不做任何二次自动动作（其余交人工，
T4 做结构断言）。

Scope discipline (T3 minimal slice):

- Zero network, zero IO: :func:`restore` returns the verified pre-change
  routes; the caller owns writing them back to the config file.
- The audit event carries the fixed placeholder-safe reason class only --
  never the raw health-result body, no provider names, no credentials
  (same discipline as ``classify_failure`` / the failover audit allowlist).
- Snapshot slot is *not* cleared by rollback (T2 semantics: rollback can
  itself be rolled back; the post-rollback inverse snapshot is T4/T5 work).
- A ``None`` snapshot with a failed health check still records the event
  (the switch objectively failed) but rolls back nothing
  (``rolled_back=False, reason="no_snapshot"``); a tampered snapshot raises
  :class:`FingerprintMismatch` instead of writing back unverified routes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from open_free_router import config_snapshot as cs

# 固定的失败类别标签（占位化）：事件 reason 只用这一个常量，
# 不透传 health_result 的任何其他字段（零原始错误正文/零 provider 名）。
REASON_HEALTH_CHECK_FAILED = "health_check_failed"


def _now() -> str:
    """UTC timestamp for the audit event (monkeypatched in tests)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def maybe_auto_rollback(snapshot: dict[str, Any] | None,
                        health_result: Any) -> dict[str, Any] | None:
    """Roll back to ``snapshot`` iff the health check objectively failed.

    Args:
        snapshot: a ``route-config-snapshot-v1`` object from
            ``config_snapshot.take_snapshot`` (or ``None`` if none exists).
        health_result: the health-check outcome; the **only** field this
            function reads is ``health_result["healthy"]``.

    Returns:
        ``None`` when there is no objective failure signal (healthy True /
        missing / unknown) -- no restore, no event, nothing else is read.

        Otherwise a result dict describing the rollback attempt::

            {"rolled_back": bool,           # True 仅当快照存在且还原成功
             "reason": "health_check_failed" | "no_snapshot",
             "at": <utc iso8601>,           # 与审计事件同一时刻
             "fingerprint": <改前(=还原后)指纹 | None>,
             "restored_routes": <还原出的 routes 深拷贝 | None>}

    Raises:
        config_snapshot.FingerprintMismatch: the snapshot was tampered with
            -- restore refuses loudly rather than writing back.
    """
    # 红线：唯一决策分支。只有 healthy is False 这一客观信号会往下走；
    # 没有任何其他决策输入（无推荐、无学习网关、无阈值/时间窗）。
    if not isinstance(health_result, dict) or health_result.get("healthy") is not False:
        return None

    at = _now()
    if snapshot is None:
        # 客观信号成立但无快照可退：如实记事件，不假装回滚成功。
        cs.record_switch_failed(REASON_HEALTH_CHECK_FAILED, at)
        return {"rolled_back": False, "reason": "no_snapshot", "at": at,
                "fingerprint": None, "restored_routes": None}

    restored = cs.restore(snapshot)
    cs.record_switch_failed(REASON_HEALTH_CHECK_FAILED, at)
    return {
        "rolled_back": True,
        "reason": REASON_HEALTH_CHECK_FAILED,
        "at": at,
        "fingerprint": snapshot.get("fingerprint"),
        "restored_routes": restored,
    }
