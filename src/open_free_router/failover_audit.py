"""PRD-149/T2: paired failover audit hooks (decision before, outcome after).

T1 (``docs/plans/2026-10-23-failover-audit-ledger.md``) §6 fixes the seam:
``on_failover_decision`` fires after ``classify_failure`` and before the
switch to the next candidate (the ``executor.py:_log_failover`` call sites);
``on_failover_outcome`` fires when the new candidate settles.  One decision
row pairs with exactly one outcome row via ``decision_id``.

This module owns the shared audit vocabulary (field allowlists and enums from
T1 §2.2/§2.3) plus the default implementations: ``AnalyticsAuditHook`` writes
both tables in ``usage.db`` today, so the existing failover path already
produces complete pairs before PRD-131 lands; ``NoopAuditHook`` covers tests
and disabled stores.  A factory decides which implementation runs -- call
sites never change when the implementation does.
"""
from __future__ import annotations

import logging


log = logging.getLogger(__name__)

# T1 §2.2: request-context and quota/doctor columns added to the existing
# PRD-059/T4 decision row. Written through the same allowlist + sensitive
# pattern gates as the original columns.
FAILOVER_QUOTA_BANDS = frozenset({
    "sufficient", "tight", "exhausted", "unknown", "stale",
})
FAILOVER_QUOTA_SOURCES = frozenset({
    "upstream_headers", "event", "never_measured",
})
FAILOVER_QUOTA_CLASSIFICATIONS = frozenset({
    "rate_limited", "quota_exhausted", "credits_exhausted", "credential_invalid",
})
FAILOVER_DOCTOR_VERDICTS = frozenset({"pass", "warn", "fail", "never_run"})

# T1 §2.3: outcome row values. ``no_candidate`` closes a decision whose
# ``to_candidate`` is empty; ``budget_exhausted`` closes one whose candidate
# was never attempted because the total budget ran out.
FAILOVER_OUTCOMES = frozenset({"success", "failed", "no_candidate", "budget_exhausted"})

# Layer 1 of the T1 §4.2 write gate: only these keys may ever reach the
# store. Anything else a caller passes is dropped, never stored.
DECISION_KEYS = (
    "request_id", "from_candidate", "to_candidate", "reason", "attempt",
    "elapsed_ms", "ttfb_ms", "circuit", "at",
    "requested_model", "strategy", "quota_band", "quota_source",
    "quota_classification", "doctor_verdict", "doctor_failed_checks", "doctor_at",
)
OUTCOME_KEYS = (
    "decision_id", "settled_at", "outcome", "status_code", "error_kind",
    "ttfb_ms", "total_latency_ms", "attempt",
)


class FailoverAuditHook:
    """Two-method seam fixed by PRD-149/T1 §6.

    Implementations must never raise into the inference path (T1 §6.4) and
    must write only allowlisted, desensitized fields (T1 §4).
    """

    def on_failover_decision(self, **fields) -> str | None:
        """Called after classify_failure, before switching candidates.

        Returns the ``decision_id`` of the written decision row so the
        executor can pair the outcome with it, or ``None`` when nothing was
        recorded (store disabled, gate rejection, write failure).
        """
        raise NotImplementedError

    def on_failover_outcome(self, **fields):
        """Called when the switched-to candidate settles.

        For a ``success`` outcome this may return a ``Callable[[float],
        None]`` that backfills ``total_latency_ms`` when the response
        completes (the ``RouteDecisionStore.complete`` pattern); every other
        outcome returns ``None``.
        """
        raise NotImplementedError


class NoopAuditHook(FailoverAuditHook):
    """Test and disabled-store implementation: records nothing, never raises."""

    def on_failover_decision(self, **fields) -> str | None:
        return None

    def on_failover_outcome(self, **fields):
        return None


class AnalyticsAuditHook(FailoverAuditHook):
    """Default implementation: write the paired rows into ``usage.db``.

    Layer 1 (allowlist) happens here -- unknown keys are dropped before the
    store is touched.  Layers 2 and 3 (sensitive-pattern rejection and
    never-interrupt) are enforced by the store and re-enforced here: a
    rejected or failed write is downgraded to a warning and ``None``, never
    an exception into the inference path.
    """

    def __init__(self, analytics):
        self.analytics = analytics

    def _store(self):
        analytics = self.analytics
        if analytics is not None and getattr(analytics, "enabled", False):
            return analytics
        return None

    def on_failover_decision(self, **fields) -> str | None:
        analytics = self._store()
        if analytics is None:
            return None
        picked = {key: fields[key] for key in DECISION_KEYS if key in fields}
        try:
            return analytics.record_failover_decision(**picked)
        except Exception:
            log.warning(
                "failover audit decision rejected or failed; inference continues",
                exc_info=True,
            )
            return None

    def on_failover_outcome(self, **fields):
        analytics = self._store()
        if analytics is None:
            return None
        picked = {key: fields[key] for key in OUTCOME_KEYS if key in fields}
        decision_id = picked.get("decision_id")
        try:
            analytics.record_failover_outcome(**picked)
        except Exception:
            log.warning(
                "failover audit outcome rejected or failed; inference continues",
                exc_info=True,
            )
            return None
        if picked.get("outcome") == "success" and decision_id:
            return lambda total_latency_ms: analytics.backfill_outcome_latency(
                decision_id, total_latency_ms
            )
        return None


def make_audit_hook(decisions=None, analytics=None) -> FailoverAuditHook:
    """Pick the audit implementation; executor call sites never change.

    ``analytics`` wins when given, otherwise the store attached to the
    ``RouteDecisionStore`` is used.  Anything absent or disabled yields the
    no-op hook so the failover path runs unchanged without a ledger.
    """
    store = analytics if analytics is not None else getattr(decisions, "analytics", None)
    if store is not None and getattr(store, "enabled", False):
        return AnalyticsAuditHook(store)
    return NoopAuditHook()
