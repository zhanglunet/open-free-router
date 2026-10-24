"""Shared pre-first-byte upstream execution with policy-driven fallback.

PRD-059/T4: the failover loop is driven by docs/plans/failover-routing-policy.md
(the "constitution"). Which failures trigger a transfer, how many times to
retry, the backoff shape, circuit-breaker thresholds and the three timeouts all
come from ``routing.policy`` / the ``failover:`` YAML section -- this module
only executes the policy, it does not invent it.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable
from urllib.parse import urlsplit

from open_free_router.failover import (
    TimeoutPhase,
    TimeoutPolicy,
    compute_backoff,
)
from open_free_router.resilience import (
    FailureDecision,
    ResilienceManager,
    classify_failure,
    parse_retry_after,
)
from open_free_router import translate
from open_free_router.failover_audit import (
    FAILOVER_QUOTA_BANDS,
    FAILOVER_QUOTA_CLASSIFICATIONS,
    FAILOVER_QUOTA_SOURCES,
    FailoverAuditHook,
    make_audit_hook,
)
from open_free_router.latency import LatencyConfig, LatencyStore
from open_free_router.quota_view import quota_view
from open_free_router.routing import RoutePlanner, RouteTarget
from open_free_router.telemetry import RouteDecisionStore
from open_free_router.quota import parse_quota_headers


log = logging.getLogger(__name__)

MAX_ERROR_BODY = 64 * 1024
# Ceiling for a reset an upstream declares in its headers. Daily budgets are the
# common case and fit well under this; a monthly one simply gets re-tested once a
# day, which is far cheaper than letting one bad header park a credential.
DECLARED_RESET_CAP = 24 * 60 * 60.0
# Legacy default for the ``timeout`` constructor argument (config.yaml
# ``upstream_timeout``). A non-default value keeps overriding the policy value.
LEGACY_TIMEOUT_DEFAULT = 120


@dataclass
class OpenedRoute:
    request_id: str
    target: RouteTarget
    credential_slot: int
    attempts: int
    connection: http.client.HTTPConnection
    response: http.client.HTTPResponse
    ttfb_ms: float | None = None
    started_at: float = 0.0
    completion_callback: Callable[[str, float], None] | None = None

    @property
    def fallback_attempts(self) -> int:
        return max(0, self.attempts - 1)

    def close(self) -> None:
        self.connection.close()
        if self.completion_callback and self.started_at:
            self.completion_callback(
                self.request_id, (time.monotonic() - self.started_at) * 1000
            )
            self.completion_callback = None


@dataclass(frozen=True)
class RouteFailure:
    request_id: str
    status: int
    body: bytes
    content_type: str
    retry_after: str | None
    attempts: int
    rejected: tuple[dict[str, str], ...]
    last_target: RouteTarget | None = None
    ttfb_ms: float | None = None
    total_latency_ms: float | None = None
    # PRD-059/T4: which timeout phase blew the budget, if any
    # ("connect" | "ttfb"). Consumed by PRD-060/T4's TIMEOUT mapping.
    timeout_phase: str | None = None
    # PRD-059/T4: the FailureDecision.kind that produced this failure, so
    # telemetry does not have to re-derive it from the status code (a 502
    # from a transport error is not a provider_unavailable).
    decision_kind: str | None = None
    # PRD-060/T4: internal normalized error (translate.NormalizedError).
    # Client-facing fields (status/body/content_type) are untouched.
    normalized: "translate.NormalizedError | None" = None

    @property
    def fallback_attempts(self) -> int:
        return max(0, self.attempts - 1)


def _credentials(target: RouteTarget) -> list[tuple[int, str]]:
    """Return configured credential slots without exposing identifiers."""
    provider = target.provider
    if provider.auth_mode == "none":
        return [(0, "")]
    if provider.api_keys:
        slots = [(index, value) for index, value in enumerate(provider.api_keys) if value]
        if slots:
            return slots
    effective = provider.effective_key
    return [(0, effective)] if effective else []


def _error_body(message: str) -> bytes:
    safe = (message or "upstream request failed")[:2048]
    return json.dumps({"error": {"message": safe, "type": "upstream_error"}}).encode()


def _unavailable_body(requested_model: str, rejected: list[dict[str, str]]) -> bytes:
    """Explain why nothing was attempted, so unconfigured never looks like broken.

    The router already knows the difference between a provider with no key and
    one that is genuinely failing; both used to return the same opaque 503.
    Reasons are bounded internal tokens and carry no provider identity, and the
    proxy is already token-authenticated, so naming them is safe here.
    """
    reasons = sorted({str(item.get("reason", "")) for item in rejected} - {""})
    if reasons == ["credential_missing"]:
        return json.dumps({"error": {
            "message": (
                f"No API key is configured for any provider serving model "
                f"'{requested_model}'. Run 'open-free-router setup' to add one."
            ),
            "type": "configuration_error",
            "reason": "credential_missing",
        }}).encode()
    body = {
        "message": f"All routes for model '{requested_model}' are temporarily unavailable.",
        "type": "upstream_error",
    }
    if reasons:
        body["reasons"] = reasons[:8]
        if "credential_missing" in reasons:
            body["message"] += (
                " Some candidates have no API key configured; run"
                " 'open-free-router setup' to add one."
            )
    return json.dumps({"error": body}).encode()


def _circuit_for(decision: FailureDecision | None) -> str | None:
    """Map a failure decision onto the T1 §5 ``circuit`` column."""
    if decision is None:
        return None
    if decision.provider_failure:
        return "provider"
    if decision.credential_terminal or decision.credential_cooldown:
        return "credential"
    if decision.model_lockout:
        return "model"
    return None


class _FailoverAuditTrail:
    """Per-request paired audit state (PRD-149/T2, T1 §2.1/§2.4).

    One decision row pairs with exactly one outcome row, keyed by
    ``decision_id``.  The trail carries the unsettled pair between the
    pre-switch decision write and the moment the switched-to candidate
    settles:

    * ``success`` / ``failed`` -- the new candidate was attempted.  Only an
      attempt on the very candidate the decision switched to may settle the
      pair; a candidate skipped by the resilience gate settles nothing.
    * ``no_candidate`` -- written immediately when the decision row carries
      no ``to_candidate``.
    * ``budget_exhausted`` -- the total budget stopped the request before
      the switched-to candidate was ever attempted.
    * a pair whose destination was gated away stays deliberately unsettled:
      the summary view judges it ``incomplete`` (T1 §2.4 -- 不回填、不编造).

    Hook failures of any kind are downgraded to warnings here as well; the
    audit can never interrupt inference (T1 §4.2 layer 3, §6.4).
    """

    def __init__(self, hook: FailoverAuditHook):
        self.hook = hook
        self.pending_decision_id: str | None = None
        self.pending_candidate: str | None = None

    def decision(self, *, to_candidate_id: str | None, **fields) -> None:
        """Write the pre-switch decision row (T1 §6.1 hook point)."""
        try:
            decision_id = self.hook.on_failover_decision(
                to_candidate=to_candidate_id, **fields
            )
        except Exception:
            log.warning(
                "failover audit decision hook failed; inference continues",
                exc_info=True,
            )
            decision_id = None
        if not decision_id:
            self.pending_decision_id = None
            self.pending_candidate = None
            return
        if to_candidate_id is None:
            # T1 §2.4: no route to switch to -- the pair closes immediately.
            self._outcome(decision_id, outcome="no_candidate")
            return
        self.pending_decision_id = decision_id
        self.pending_candidate = to_candidate_id

    def settle(
        self,
        *,
        outcome: str,
        candidate_id: str | None = None,
        **fields,
    ):
        """Settle the pending pair; returns the success latency backfill."""
        if not self.pending_decision_id:
            return None
        if (
            candidate_id is not None
            and candidate_id != self.pending_candidate
        ):
            return None
        decision_id = self.pending_decision_id
        self.pending_decision_id = None
        self.pending_candidate = None
        return self._outcome(decision_id, outcome=outcome, **fields)

    def _outcome(self, decision_id: str, *, outcome: str, **fields):
        try:
            return self.hook.on_failover_outcome(
                decision_id=decision_id, outcome=outcome, **fields
            )
        except Exception:
            log.warning(
                "failover audit outcome hook failed; inference continues",
                exc_info=True,
            )
            return None


class UpstreamExecutor:
    """Open one upstream response, trying safe alternatives before returning."""

    def __init__(
        self,
        planner: RoutePlanner,
        resilience: ResilienceManager,
        timeout: int = LEGACY_TIMEOUT_DEFAULT,
        decisions: RouteDecisionStore | None = None,
        sleeper: Callable[[float], None] | None = None,
        audit_hook: FailoverAuditHook | None = None,
    ):
        self.planner = planner
        self.resilience = resilience
        self.timeout = timeout
        self.decisions = decisions
        # Test seam: real time.sleep in production, a recorder in tests.
        self._sleeper = sleeper or time.sleep
        # PRD-149/T2 (T1 §6.2): the audit hook is built by the factory so
        # call sites never change with the implementation; tests and closed
        # stores get the no-op hook.
        self.audit_hook = audit_hook if audit_hook is not None else make_audit_hook(decisions)
        policy = planner.config
        # Legacy precedence: an explicit non-default ``timeout`` (config.yaml
        # ``upstream_timeout``) still wins; otherwise the policy value applies.
        request_timeout = (
            timeout if timeout != LEGACY_TIMEOUT_DEFAULT
            else policy.request_timeout_seconds
        )
        self.timeouts = TimeoutPolicy(
            connect=policy.connect_timeout_seconds,
            ttfb=policy.ttfb_timeout_seconds,
            request=request_timeout,
            total=policy.total_budget_seconds,
        )
        # PRD-059/T3: in-memory TTFB sliding windows for the latency
        # dimension.  None when sampling is disabled (routing falls back to
        # the probe-snapshot latency factor).
        latency_config = LatencyConfig.from_routing(self.planner.config)
        self.latency_store = (
            LatencyStore(window_size=latency_config.window_size)
            if latency_config.enabled
            else None
        )

    def _record_latency(self, target: RouteTarget, ttfb_ms: float | None) -> None:
        """Feed one first-byte observation to the latency store (PRD-059/T3).

        Only called when a first byte was actually received.  Connection
        failures have no TTFB; the resilience manager already punishes those.
        """
        if self.latency_store is None or ttfb_ms is None:
            return
        self.latency_store.record(
            target.provider_name, target.upstream_model_id, ttfb_ms
        )

    def _quota_context(
        self, provider_name: str | None, slot: int | None
    ) -> tuple[str | None, str | None, str | None]:
        """PRD-149/T1 §3.2: the failing credential's quota band/source/classification.

        Read from the resilience snapshot through ``quota_view`` so the
        ledger and the local quota panel can never disagree about the band.
        An unseen credential reports ``unknown`` / ``never_measured``.
        """
        if not provider_name or slot is None:
            return "unknown", "never_measured", None
        try:
            snapshot = self.resilience.snapshot()
            key = f"{provider_name}:slot-{slot}"
            state = (snapshot.get("credentials") or {}).get(key)
            if not isinstance(state, dict):
                return "unknown", "never_measured", None
            row = quota_view({"credentials": {key: state}})["items"][0]
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            return "unknown", "never_measured", None
        band = row.get("band")
        source = row.get("source")
        classification = row.get("event_classification")
        return (
            band if band in FAILOVER_QUOTA_BANDS else "unknown",
            source if source in FAILOVER_QUOTA_SOURCES else None,
            classification if classification in FAILOVER_QUOTA_CLASSIFICATIONS else None,
        )

    def _log_failover(
        self,
        audit: _FailoverAuditTrail,
        *,
        request_id: str,
        from_candidate: RouteTarget | None,
        to_candidate: RouteTarget | None,
        reason: str,
        attempt: int,
        elapsed_ms: float | None,
        ttfb_ms: float | None,
        circuit: str | None,
        requested_model: str = "",
        strategy: str = "",
        quota_provider: str | None = None,
        quota_slot: int | None = None,
    ) -> None:
        """Write one pre-switch decision row (T1 §5 + PRD-149/T2 §2.2).

        Called after ``classify_failure`` and before switching to the next
        candidate.  Only enum kinds, candidate IDs and timings are recorded
        -- never headers, keys, prompts or bodies.  The hook (never this
        method) owns rejection and failure handling: audit problems are
        warnings, never inference failures.
        """
        quota_band, quota_source, quota_classification = self._quota_context(
            quota_provider, quota_slot
        )
        audit.decision(
            to_candidate_id=to_candidate.canonical_id if to_candidate else None,
            request_id=request_id,
            from_candidate=from_candidate.canonical_id if from_candidate else None,
            # ``to_candidate`` is added by the trail itself (see decision()).
            reason=reason,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            ttfb_ms=ttfb_ms,
            circuit=circuit,
            requested_model=requested_model,
            strategy=strategy,
            quota_band=quota_band,
            quota_source=quota_source,
            quota_classification=quota_classification,
        )

    def _route_completion_callback(self, audit_backfill):
        """Chain the audit latency backfill onto the telemetry completion hook."""
        base = self.decisions.complete if self.decisions else None
        if audit_backfill is None:
            return base
        if base is None:
            return lambda request_id, elapsed_ms: audit_backfill(elapsed_ms)

        def chained(request_id, elapsed_ms):
            base(request_id, elapsed_ms)
            audit_backfill(elapsed_ms)

        return chained

    def _record(
        self,
        result: OpenedRoute | RouteFailure,
        requested_model: str,
        strategy: str,
        rejected: list[dict[str, str]],
        scores=(),
        attempt_results=(),
    ) -> None:
        if not self.decisions:
            return
        target = result.target if isinstance(result, OpenedRoute) else result.last_target
        if isinstance(result, RouteFailure):
            # Prefer the kind the executor actually classified with; the
            # status-code re-derivation below cannot tell a transport 502
            # from a provider 502.
            error_kind = result.decision_kind or (
                "route_unavailable" if result.attempts == 0
                else classify_failure(
                    result.status, result.body.decode("utf-8", errors="replace")
                ).kind
            )
        else:
            error_kind = ""
        self.decisions.record(
            request_id=result.request_id,
            requested_model=requested_model,
            strategy=strategy,
            status="success" if isinstance(result, OpenedRoute) else "failed",
            status_code=result.response.status if isinstance(result, OpenedRoute) else result.status,
            provider=target.provider_name if target else "",
            model=target.canonical_id if target else "",
            attempts=result.attempts,
            rejected=rejected,
            ttfb_ms=result.ttfb_ms,
            total_latency_ms=(result.total_latency_ms
                              if isinstance(result, RouteFailure) else None),
            scores=scores,
            attempt_results=attempt_results,
            error_kind=error_kind,
        )

    @staticmethod
    def _open(
        target: RouteTarget,
        credential: str,
        endpoint_suffix: str,
        payload: dict,
        timeouts: TimeoutPolicy,
        request_id: str = "",
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        # All upstream request assembly goes through the translation layer
        # (PRD-060/T2): table-driven auth, model field, URL and field mapping.
        table = translate.get_table(target.provider_name)
        outbound = translate.build_outbound_request(
            table,
            target=target,
            payload=payload,
            credential=credential,
            endpoint_suffix=endpoint_suffix,
            request_id=request_id or "unknown",
        )
        parts = urlsplit(outbound.url)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port, timeout=timeouts.connect)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        data = json.dumps(outbound.body).encode()
        try:
            conn.connect()
        except socket.timeout:
            conn.close()
            raise TimeoutPhase("connect")
        except OSError:
            conn.close()
            raise
        if conn.sock:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # From here the socket budget is the first-byte timeout (T1 §2.3):
            # getresponse() returns once the response headers arrive.
            conn.sock.settimeout(timeouts.ttfb)
        try:
            conn.request("POST", path, body=data, headers=outbound.headers)
            return conn, conn.getresponse()
        except socket.timeout:
            conn.close()
            raise TimeoutPhase("ttfb")
        except Exception:
            conn.close()
            raise

    def execute(self, requested_model: str, endpoint_suffix: str, payload: dict) -> OpenedRoute | RouteFailure:
        request_id = f"ofr_{uuid.uuid4().hex}"
        request_started = time.monotonic()
        policy = self.planner.config
        timeouts = self.timeouts
        signals = self.resilience.scoring_signals()
        for provider_name in self.planner.registry.providers:
            signals.setdefault(provider_name, {}).setdefault("health", 1.0)
        if self.decisions:
            for model, values in self.decisions.scoring_signals().items():
                signals.setdefault(model, {}).update(values)
        plan = self.planner.plan(
            requested_model, signals, self.resilience, self.latency_store
        )
        rejected = list(plan.rejected)
        if not plan.candidates:
            status = 403 if plan.strategy == "explicit" else 503
            result = RouteFailure(
                request_id,
                status,
                _error_body(f"No eligible route for model '{requested_model}'."),
                "application/json",
                None,
                0,
                tuple(rejected),
            )
            self._record(result, requested_model, plan.strategy, rejected, plan.scores)
            return result

        allow_model_fallback = (
            (plan.strategy != "explicit" and policy.fallback_enabled)
            or (plan.strategy == "explicit" and policy.explicit_model_fallback)
        )
        attempts = 0
        attempt_results = []
        last_failure: RouteFailure | None = None
        last_decision: FailureDecision | None = None
        backoff_switches = 0
        stop_request = False
        # PRD-149/T2: per-request paired audit trail (decision before the
        # switch, outcome when the switched-to candidate settles).
        audit = _FailoverAuditTrail(self.audit_hook)
        # The credential slot that produced last_failure, for the decision
        # row's quota context (T1 §3.2).
        failed_slot: int | None = None

        def budget_exceeded() -> bool:
            return (time.monotonic() - request_started) >= timeouts.total

        def sleep_bounded(delay: float) -> bool:
            """Sleep unless it would blow the total budget (T1 §2.3)."""
            if delay <= 0:
                return True
            if (time.monotonic() - request_started) + delay >= timeouts.total:
                return False
            self._sleeper(delay)
            return True

        for target_index, target in enumerate(plan.candidates):
            if budget_exceeded():
                # PRD-149/T2 (T1 §2.4): the budget stopped the request before
                # the switched-to candidate was attempted.
                audit.settle(outcome="budget_exhausted")
                break
            slots = self.resilience.order_credentials(
                target.provider_name, _credentials(target)
            )
            if not slots:
                rejected.append({"model": target.canonical_id, "reason": "credential_missing"})
                continue
            # Backoff before switching candidates (T1 §2.2). The first attempt
            # of the request goes out immediately.
            if attempts > 0:
                backoff_switches += 1
                delay = compute_backoff(
                    backoff_switches,
                    policy.backoff_base_seconds,
                    policy.backoff_factor,
                    policy.backoff_cap_seconds,
                    policy.backoff_jitter,
                )
                if not sleep_bounded(delay):
                    # PRD-149/T2 (T1 §2.4): the backoff would blow the total
                    # budget, so the switch never gets its attempt.
                    audit.settle(outcome="budget_exhausted")
                    break
            attempted_target = False
            last_decision = None
            failed_slot = None
            slot_index = 0
            same_429_retried = False
            bypass_gate = False
            while slot_index < len(slots):
                credential_slot, credential = slots[slot_index]
                if attempts >= policy.max_attempts:
                    break
                if budget_exceeded():
                    break
                if not bypass_gate:
                    allowed, reason = self.resilience.can_attempt(
                        target.provider_name, target.upstream_model_id, credential_slot
                    )
                    if not allowed:
                        rejected.append({"model": target.canonical_id, "reason": reason})
                        slot_index += 1
                        continue
                bypass_gate = False

                attempted_target = True
                attempts += 1
                attempt_started = time.monotonic()
                timeout_phase: str | None = None
                try:
                    conn, response = self._open(
                        target, credential, endpoint_suffix, payload,
                        timeouts, request_id=request_id,
                    )
                except TimeoutPhase as timeout_exc:
                    timeout_phase = timeout_exc.phase
                    elapsed_ms = (time.monotonic() - attempt_started) * 1000
                    decision = classify_failure(0, "")
                    # PRD-060/T4: internal error normalization. Client-facing
                    # fields below are unchanged. Uses the real timeout phase
                    # from PRD-059/T4 (supersedes the _timeout_phase heuristic).
                    normalized = translate.normalize_error(
                        translate.get_table(target.provider_name),
                        http_status=0,
                        body=None,
                        retry_after=None,
                        provider_name=target.provider_name,
                        model_id=target.upstream_model_id,
                        timeout_phase=timeout_phase,
                    )
                    self.resilience.record_failure(
                        target.provider_name,
                        target.upstream_model_id,
                        decision,
                        credential_slot,
                    )
                    last_decision = decision
                    failed_slot = credential_slot
                    last_failure = RouteFailure(
                        request_id,
                        502,
                        _error_body(f"upstream {timeout_phase} timeout"),
                        "application/json",
                        None,
                        attempts,
                        tuple(rejected),
                        target,
                        total_latency_ms=elapsed_ms,
                        timeout_phase=timeout_phase,
                        decision_kind=decision.kind,
                        normalized=normalized,
                    )
                    attempt_results.append({
                        "provider": target.provider_name, "model": target.canonical_id,
                        "status": "failed", "status_code": 502,
                        "total_latency_ms": elapsed_ms,
                        "timeout_phase": timeout_phase,
                        "error_code": normalized.code,
                        "error_kind": normalized.decision_kind,
                    })
                    break

                except Exception as exc:
                    elapsed_ms = (time.monotonic() - attempt_started) * 1000
                    decision = classify_failure(0, str(exc))
                    # PRD-060/T4: internal error normalization. A plain
                    # transport failure (refused/DNS) is not a timeout, so
                    # timeout_phase stays None -> TRANSPORT_ERROR.
                    normalized = translate.normalize_error(
                        translate.get_table(target.provider_name),
                        http_status=0,
                        body=None,
                        retry_after=None,
                        provider_name=target.provider_name,
                        model_id=target.upstream_model_id,
                        timeout_phase=None,
                    )
                    self.resilience.record_failure(
                        target.provider_name,
                        target.upstream_model_id,
                        decision,
                        credential_slot,
                    )
                    last_decision = decision
                    failed_slot = credential_slot
                    last_failure = RouteFailure(
                        request_id,
                        502,
                        _error_body(str(exc)),
                        "application/json",
                        None,
                        attempts,
                        tuple(rejected),
                        target,
                        total_latency_ms=elapsed_ms,
                        decision_kind=decision.kind,
                        normalized=normalized,
                    )
                    attempt_results.append({
                        "provider": target.provider_name, "model": target.canonical_id,
                        "status": "failed", "status_code": 502,
                        "total_latency_ms": elapsed_ms,
                        "error_code": normalized.code,
                        "error_kind": normalized.decision_kind,
                    })
                    break

                # Never follow upstream redirects: provider URLs and auth
                # destinations are explicit. A 3xx is treated as an upstream
                # transport failure instead of forwarding credentials elsewhere.
                ttfb_ms = (time.monotonic() - attempt_started) * 1000
                # PRD-059/T3: first byte received — feed the latency store
                # whether the status is 2xx or an error response.
                self._record_latency(target, ttfb_ms)
                if 200 <= response.status < 300:
                    self.resilience.record_success(
                        target.provider_name, target.upstream_model_id, credential_slot
                    )
                    self.resilience.record_quota(
                        target.provider_name,
                        credential_slot,
                        parse_quota_headers(response.headers, response.status),
                    )
                    # PRD-149/T2 (T1 §2.4): the switched-to candidate received
                    # its first byte -- settle the pending pair as success and
                    # chain the latency backfill onto the completion callback.
                    audit_backfill = audit.settle(
                        outcome="success",
                        candidate_id=target.canonical_id,
                        status_code=response.status,
                        ttfb_ms=ttfb_ms,
                        attempt=attempts,
                    )
                    result = OpenedRoute(
                        request_id,
                        target,
                        credential_slot,
                        attempts,
                        conn,
                        response,
                        ttfb_ms,
                        attempt_started,
                        self._route_completion_callback(audit_backfill),
                    )
                    attempt_results.append({
                        "provider": target.provider_name, "model": target.canonical_id,
                        "status": "success", "status_code": response.status,
                        "ttfb_ms": ttfb_ms,
                    })
                    self._record(
                        result, requested_model, plan.strategy, rejected,
                        plan.scores, attempt_results,
                    )
                    return result

                # The body read is bounded by the remaining per-attempt budget.
                # A stall here is past the first-byte boundary: no failover
                # (T1 principle 1) -- fail this request cleanly instead.
                try:
                    if conn.sock is not None:
                        remaining = timeouts.request - (time.monotonic() - attempt_started)
                        conn.sock.settimeout(max(1.0, remaining))
                    body = response.read(MAX_ERROR_BODY)
                except socket.timeout:
                    conn.close()
                    total_latency_ms = (time.monotonic() - attempt_started) * 1000
                    failed_slot = credential_slot
                    last_failure = RouteFailure(
                        request_id,
                        504,
                        _error_body("upstream stalled after first byte"),
                        "application/json",
                        None,
                        attempts,
                        tuple(rejected),
                        target,
                        ttfb_ms=ttfb_ms,
                        total_latency_ms=total_latency_ms,
                        decision_kind="transport_error",
                    )
                    last_decision = None
                    attempt_results.append({
                        "provider": target.provider_name, "model": target.canonical_id,
                        "status": "failed", "status_code": 504,
                        "ttfb_ms": ttfb_ms, "total_latency_ms": total_latency_ms,
                    })
                    stop_request = True
                    break
                total_latency_ms = (time.monotonic() - attempt_started) * 1000
                content_type = response.getheader("Content-Type", "application/json")
                retry_after = response.getheader("Retry-After")
                conn.close()
                message = body.decode("utf-8", errors="replace")
                decision = classify_failure(response.status, message)
                if decision.kind == "quota_exhausted":
                    # T1 §7: the quota cooldown scale is policy-driven, not a
                    # classifier constant.
                    decision = replace(
                        decision,
                        cooldown_scale=self.resilience.quota_cooldown_scale,
                    )
                # PRD-060/T4: internal error normalization. Client-facing
                # fields below are unchanged.
                normalized = translate.normalize_error(
                    translate.get_table(target.provider_name),
                    http_status=response.status,
                    body=body,
                    retry_after=retry_after,
                    provider_name=target.provider_name,
                    model_id=target.upstream_model_id,
                )
                observation = parse_quota_headers(
                    response.headers, response.status, decision.kind
                )
                self.resilience.record_quota(
                    target.provider_name,
                    credential_slot,
                    observation,
                )
                retry_delay = parse_retry_after(retry_after)
                # A reset declared in the response headers is structured evidence,
                # unlike wording in the error body, so it sets the cooldown for any
                # rate-limited response rather than only the ones whose text said
                # "quota". Groq reports an exhausted daily token budget as a plain
                # "Rate limit reached ... tokens per day", which otherwise fell back
                # to the default cooldown and kept retrying against it all day.
                if decision.credential_cooldown and observation.next_reset_at:
                    declared = observation.next_reset_at - time.time()
                    retry_delay = max(retry_delay, min(declared, DECLARED_RESET_CAP))
                self.resilience.record_failure(
                    target.provider_name,
                    target.upstream_model_id,
                    decision,
                    credential_slot,
                    retry_delay,
                )
                last_decision = decision
                failed_slot = credential_slot
                last_failure = RouteFailure(
                    request_id,
                    response.status,
                    body or _error_body(message),
                    content_type,
                    retry_after,
                    attempts,
                    tuple(rejected),
                    target,
                    ttfb_ms=ttfb_ms,
                    total_latency_ms=total_latency_ms,
                    decision_kind=decision.kind,
                    normalized=normalized,
                )
                attempt_results.append({
                    "provider": target.provider_name, "model": target.canonical_id,
                    "status": "failed", "status_code": response.status,
                    "ttfb_ms": ttfb_ms, "total_latency_ms": total_latency_ms,
                    "error_code": normalized.code,
                    "error_kind": normalized.decision_kind,
                })

                # T1 §2.1: an ordinary 429 with a parseable Retry-After inside
                # the policy ceiling is retried once on the SAME candidate --
                # the upstream told us exactly how long to wait, so no
                # exponential backoff. The retry bypasses the cooldown gate:
                # we sleep the declared delay first.
                raw_retry_delay = parse_retry_after(retry_after, invalid_default=None)
                if (
                    policy.retry_same_candidate_on_429
                    and decision.kind == "rate_limited"
                    and not same_429_retried
                    and retry_after is not None
                    and raw_retry_delay is not None
                    and 0 <= raw_retry_delay <= policy.retry_after_max_seconds
                    and attempts < policy.max_attempts
                    and not budget_exceeded()
                ):
                    same_429_retried = True
                    # PRD-149/T2: the 429 settles whatever pair switched to
                    # this candidate; the retry is itself a new decision.
                    audit.settle(
                        outcome="failed",
                        candidate_id=target.canonical_id,
                        status_code=response.status,
                        error_kind=decision.kind,
                        ttfb_ms=ttfb_ms,
                        total_latency_ms=total_latency_ms,
                        attempt=attempts,
                    )
                    self._log_failover(
                        audit,
                        request_id=request_id,
                        from_candidate=None,
                        to_candidate=target,
                        reason="rate_limited",
                        attempt=attempts,
                        elapsed_ms=total_latency_ms,
                        ttfb_ms=ttfb_ms,
                        circuit=None,
                        requested_model=requested_model,
                        strategy=plan.strategy,
                        quota_provider=target.provider_name,
                        quota_slot=credential_slot,
                    )
                    if not sleep_bounded(raw_retry_delay):
                        audit.settle(outcome="budget_exhausted")
                        break
                    bypass_gate = True
                    continue

                has_more_credentials = slot_index + 1 < len(slots)
                if has_more_credentials and (
                    decision.credential_terminal or decision.credential_cooldown
                ):
                    slot_index += 1
                    continue
                break

            # PRD-149/T2 (T1 §2.4): this candidate was attempted and failed,
            # so it settles the pending pair as ``failed``. A candidate that
            # was gated before any attempt settles nothing -- that pair is
            # left for the incomplete judgment (不回填、不编造).
            if (
                last_failure is not None
                and last_failure.last_target is not None
                and last_failure.last_target.canonical_id == target.canonical_id
            ):
                audit.settle(
                    outcome="failed",
                    candidate_id=target.canonical_id,
                    status_code=last_failure.status,
                    error_kind=last_failure.decision_kind,
                    ttfb_ms=last_failure.ttfb_ms,
                    total_latency_ms=last_failure.total_latency_ms,
                    attempt=attempts,
                )
            if stop_request:
                break
            if attempts >= policy.max_attempts or budget_exceeded():
                if budget_exceeded():
                    audit.settle(outcome="budget_exhausted")
                break
            # --- candidate-advance policy (T1 §1) ---
            advance = True
            if last_decision is not None and last_decision.kind == "credential_invalid":
                # T1 §1.2 #9: a bad key opens no doors -- try the provider's
                # other slots (done above), but never cross candidates.
                advance = False
            elif (
                last_decision is not None
                and last_decision.kind == "credits_exhausted"
                and plan.strategy == "explicit"
            ):
                # T1 §1.2 #10: an explicit request names its provider; other
                # providers may still serve virtual-model requests.
                advance = False
            elif last_decision is not None and last_decision.kind == "context_overflow":
                # T1 §1.1 #7: only transfer to a larger-context model.
                current_window = target.model.context_window
                larger_ahead = any(
                    candidate.model.context_window > current_window
                    for candidate in plan.candidates[target_index + 1:]
                )
                if not larger_ahead:
                    advance = False
            has_more_models = target_index + 1 < len(plan.candidates)
            if not has_more_models or not allow_model_fallback:
                advance = False
            # Invalid request payloads are expected to fail identically on the
            # next model. Every narrower provider/key/model availability state
            # is safe to bypass before the client has received any bytes.
            if attempted_target and last_decision and last_decision.kind == "invalid_request":
                advance = False

            if last_decision is not None and last_decision.kind != "invalid_request":
                self._log_failover(
                    audit,
                    request_id=request_id,
                    from_candidate=target,
                    to_candidate=plan.candidates[target_index + 1] if advance else None,
                    reason=last_decision.kind,
                    attempt=attempts,
                    elapsed_ms=(last_failure.total_latency_ms if last_failure else None),
                    ttfb_ms=(last_failure.ttfb_ms if last_failure else None),
                    circuit=_circuit_for(last_decision),
                    requested_model=requested_model,
                    strategy=plan.strategy,
                    quota_provider=target.provider_name,
                    quota_slot=failed_slot,
                )
            if not advance:
                break

        if last_failure:
            result = RouteFailure(
                request_id,
                last_failure.status,
                last_failure.body,
                last_failure.content_type,
                last_failure.retry_after,
                attempts,
                tuple(rejected),
                last_failure.last_target,
                last_failure.ttfb_ms,
                last_failure.total_latency_ms,
                last_failure.timeout_phase,
                last_failure.decision_kind,
                last_failure.normalized,
            )
            self._record(
                result, requested_model, plan.strategy, rejected,
                plan.scores, attempt_results,
            )
            return result
        result = RouteFailure(
            request_id,
            503,
            _unavailable_body(requested_model, rejected),
            "application/json",
            None,
            attempts,
            tuple(rejected),
        )
        self._record(
            result, requested_model, plan.strategy, rejected,
            plan.scores, attempt_results,
        )
        return result
