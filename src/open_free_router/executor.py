"""Shared pre-first-byte upstream execution with deterministic fallback."""
from __future__ import annotations

import http.client
import json
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from open_free_router.resilience import (
    FailureDecision,
    ResilienceManager,
    classify_failure,
    parse_retry_after,
)
from open_free_router.routing import RoutePlanner, RouteTarget
from open_free_router.telemetry import RouteDecisionStore
from open_free_router.quota import parse_quota_headers


MAX_ERROR_BODY = 64 * 1024


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

    @property
    def fallback_attempts(self) -> int:
        return max(0, self.attempts - 1)


def _credentials(target: RouteTarget) -> list[tuple[int, str]]:
    """Return configured credential slots without exposing identifiers."""
    provider = target.provider
    if provider.api_keys:
        slots = [(index, value) for index, value in enumerate(provider.api_keys) if value]
        if slots:
            return slots
    effective = provider.effective_key
    return [(0, effective)] if effective else []


def _error_body(message: str) -> bytes:
    safe = (message or "upstream request failed")[:2048]
    return json.dumps({"error": {"message": safe, "type": "upstream_error"}}).encode()


class UpstreamExecutor:
    """Open one upstream response, trying safe alternatives before returning."""

    def __init__(
        self,
        planner: RoutePlanner,
        resilience: ResilienceManager,
        timeout: int = 120,
        decisions: RouteDecisionStore | None = None,
    ):
        self.planner = planner
        self.resilience = resilience
        self.timeout = timeout
        self.decisions = decisions

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
        )

    @staticmethod
    def _open(
        target: RouteTarget,
        credential: str,
        endpoint_suffix: str,
        payload: dict,
        timeout: int,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        upstream = (target.provider.upstream_url or target.provider.base_url).rstrip("/")
        if not upstream:
            raise OSError("provider not configured")
        parts = urlsplit(f"{upstream}/{endpoint_suffix.lstrip('/')}")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise OSError("invalid provider upstream URL")
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        body = dict(payload)
        body["model"] = target.upstream_model_id
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credential}",
            "User-Agent": "open-free-router/0.1",
        }
        try:
            conn.connect()
            if conn.sock:
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.request("POST", path, body=data, headers=headers)
            return conn, conn.getresponse()
        except Exception:
            conn.close()
            raise

    def execute(self, requested_model: str, endpoint_suffix: str, payload: dict) -> OpenedRoute | RouteFailure:
        request_id = f"ofr_{uuid.uuid4().hex}"
        signals = self.resilience.scoring_signals()
        for provider_name in self.planner.registry.providers:
            signals.setdefault(provider_name, {}).setdefault("health", 1.0)
        if self.decisions:
            for model, values in self.decisions.scoring_signals().items():
                signals.setdefault(model, {}).update(values)
        plan = self.planner.plan(requested_model, signals)
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

        config = self.planner.config
        allow_model_fallback = (
            (plan.strategy != "explicit" and config.fallback_enabled)
            or (plan.strategy == "explicit" and config.explicit_model_fallback)
        )
        attempts = 0
        attempt_results = []
        last_failure: RouteFailure | None = None
        last_decision: FailureDecision | None = None

        for target_index, target in enumerate(plan.candidates):
            slots = self.resilience.order_credentials(
                target.provider_name, _credentials(target)
            )
            if not slots:
                rejected.append({"model": target.canonical_id, "reason": "credential_missing"})
                continue
            attempted_target = False
            last_decision = None
            for slot_index, (credential_slot, credential) in enumerate(slots):
                if attempts >= config.max_attempts:
                    break
                allowed, reason = self.resilience.can_attempt(
                    target.provider_name, target.upstream_model_id, credential_slot
                )
                if not allowed:
                    rejected.append({"model": target.canonical_id, "reason": reason})
                    continue

                attempted_target = True
                attempts += 1
                attempt_started = time.monotonic()
                try:
                    conn, response = self._open(
                        target, credential, endpoint_suffix, payload, self.timeout
                    )
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - attempt_started) * 1000
                    decision = classify_failure(0, str(exc))
                    self.resilience.record_failure(
                        target.provider_name,
                        target.upstream_model_id,
                        decision,
                        credential_slot,
                    )
                    last_decision = decision
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
                    )
                    attempt_results.append({
                        "provider": target.provider_name, "model": target.canonical_id,
                        "status": "failed", "status_code": 502,
                        "total_latency_ms": elapsed_ms,
                    })
                    break

                # Never follow upstream redirects: provider URLs and auth
                # destinations are explicit. A 3xx is treated as an upstream
                # transport failure instead of forwarding credentials elsewhere.
                ttfb_ms = (time.monotonic() - attempt_started) * 1000
                if 200 <= response.status < 300:
                    self.resilience.record_success(
                        target.provider_name, target.upstream_model_id, credential_slot
                    )
                    self.resilience.record_quota(
                        target.provider_name,
                        credential_slot,
                        parse_quota_headers(response.headers, response.status),
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
                        self.decisions.complete if self.decisions else None,
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

                body = response.read(MAX_ERROR_BODY)
                total_latency_ms = (time.monotonic() - attempt_started) * 1000
                content_type = response.getheader("Content-Type", "application/json")
                retry_after = response.getheader("Retry-After")
                conn.close()
                message = body.decode("utf-8", errors="replace")
                decision = classify_failure(response.status, message)
                observation = parse_quota_headers(
                    response.headers, response.status, decision.kind
                )
                self.resilience.record_quota(
                    target.provider_name,
                    credential_slot,
                    observation,
                )
                retry_delay = parse_retry_after(retry_after)
                # A declared reset turns recurring daily/monthly quota exhaustion
                # into a bounded cooldown. Unknown or credit exhaustion remains
                # terminal until the operator explicitly resets the slot.
                if decision.kind == "quota_exhausted" and observation.next_reset_at:
                    decision = FailureDecision(
                        "quota_exhausted", retryable=True, credential_cooldown=True
                    )
                    retry_delay = max(
                        retry_delay, observation.next_reset_at - time.time()
                    )
                self.resilience.record_failure(
                    target.provider_name,
                    target.upstream_model_id,
                    decision,
                    credential_slot,
                    retry_delay,
                )
                last_decision = decision
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
                )
                attempt_results.append({
                    "provider": target.provider_name, "model": target.canonical_id,
                    "status": "failed", "status_code": response.status,
                    "ttfb_ms": ttfb_ms, "total_latency_ms": total_latency_ms,
                })

                has_more_credentials = slot_index + 1 < len(slots)
                if has_more_credentials and (
                    decision.credential_terminal or decision.credential_cooldown
                ):
                    continue
                break

            if attempts >= config.max_attempts:
                break
            has_more_models = target_index + 1 < len(plan.candidates)
            if not has_more_models or not allow_model_fallback:
                break
            # Invalid request payloads are expected to fail identically on the
            # next model. Every narrower provider/key/model availability state
            # is safe to bypass before the client has received any bytes.
            if attempted_target and last_decision and last_decision.kind == "invalid_request":
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
            )
            self._record(
                result, requested_model, plan.strategy, rejected,
                plan.scores, attempt_results,
            )
            return result
        result = RouteFailure(
            request_id,
            503,
            _error_body(f"All routes for model '{requested_model}' are temporarily unavailable."),
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
