"""Provider, credential and model failure isolation primitives.

The state manager stores slot numbers, never credential values or hashes.  It
is intentionally independent from the proxy so classification and recovery can
be tested before retry execution is wired into every protocol adapter.
"""
from __future__ import annotations

import email.utils
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import timezone


PROVIDER_FAILURE_CODES = frozenset({408, 500, 502, 503, 504})
QUOTA_COOLDOWN_SCALE = 8.0
CONTEXT_ERROR_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)


@dataclass(frozen=True)
class FailureDecision:
    kind: str
    retryable: bool = False
    provider_failure: bool = False
    credential_terminal: bool = False
    credential_cooldown: bool = False
    model_lockout: bool = False
    cooldown_scale: float = 1.0


def classify_failure(status: int, message: str = "") -> FailureDecision:
    """Classify an upstream failure without retaining the raw error body."""
    lowered = (message or "").lower()
    if status in PROVIDER_FAILURE_CODES:
        return FailureDecision("provider_unavailable", True, provider_failure=True)
    if status == 429:
        # A 429 is retryable by definition. Quota-flavoured wording earns a much
        # longer cooldown, but never a terminal stop: the words "quota", "credit"
        # and "balance" are common in ordinary rate-limit copy, and treating them
        # as proof of permanent exhaustion took healthy providers offline until an
        # operator intervened by hand.
        quota = any(marker in lowered for marker in ("quota", "credit", "balance"))
        return FailureDecision(
            "quota_exhausted" if quota else "rate_limited",
            retryable=True,
            credential_cooldown=True,
            cooldown_scale=QUOTA_COOLDOWN_SCALE if quota else 1.0,
        )
    if status in (401, 403):
        return FailureDecision("credential_invalid", credential_terminal=True)
    if status == 402:
        return FailureDecision("credits_exhausted", credential_terminal=True)
    if status == 404:
        return FailureDecision("model_unavailable", True, model_lockout=True)
    if status == 400 and any(marker in lowered for marker in CONTEXT_ERROR_MARKERS):
        return FailureDecision("context_overflow", True, model_lockout=True)
    if 400 <= status < 500:
        return FailureDecision("invalid_request")
    return FailureDecision("transport_error", True, provider_failure=True)


def parse_retry_after(value: str | None, now: float | None = None, cap_seconds: int = 3600) -> float:
    """Return a bounded delay in seconds for numeric or HTTP-date values."""
    if not value:
        return 0.0
    now = time.time() if now is None else now
    try:
        delay = float(value.strip())
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = parsed.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return min(float(cap_seconds), max(0.0, delay))


@dataclass
class ProviderState:
    state: str = "closed"
    failures: int = 0
    open_until: float = 0.0
    probe_in_flight: bool = False


@dataclass
class CredentialState:
    state: str = "ready"
    cooldown_until: float = 0.0
    reason: str = ""
    quota: dict = field(default_factory=dict)
    last_success_at: float = 0.0
    terminal_since: float = 0.0
    terminal_failures: int = 0
    probe_in_flight: bool = False


def _safe_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed and abs(parsed) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_quota(value) -> dict:
    """Accept only the normalized quota shape written by quota.py."""
    if not isinstance(value, dict):
        return {}
    result = {
        "classification": str(value.get("classification", "unknown"))[:64],
        "observed_at": _safe_float(value.get("observed_at")),
        "retry_at": _safe_float(value.get("retry_at")),
        "has_headers": bool(value.get("has_headers", False)),
    }
    for name in ("requests", "tokens"):
        source = value.get(name, {})
        if not isinstance(source, dict):
            source = {}
        result[name] = {
            "limit": None if source.get("limit") is None else _safe_float(source.get("limit")),
            "remaining": None if source.get("remaining") is None else _safe_float(source.get("remaining")),
            "reset_at": _safe_float(source.get("reset_at")),
        }
    return result


@dataclass
class ModelState:
    failures: int = 0
    lockout_until: float = 0.0
    reason: str = ""


class ResilienceManager:
    """Thread-safe three-layer runtime state with bounded recovery rules."""

    def __init__(
        self,
        provider_threshold: int = 3,
        provider_cooldown: float = 30.0,
        credential_cooldown: float = 60.0,
        model_cooldown: float = 120.0,
        state_path: str | Path | None = None,
        credential_terminal_probe: float = 900.0,
    ):
        self.provider_threshold = max(1, int(provider_threshold))
        self.provider_cooldown = max(0.0, float(provider_cooldown))
        self.credential_cooldown = max(0.0, float(credential_cooldown))
        self.model_cooldown = max(0.0, float(model_cooldown))
        self.credential_terminal_probe = max(0.0, float(credential_terminal_probe))
        self._providers: dict[str, ProviderState] = {}
        self._credentials: dict[tuple[str, int], CredentialState] = {}
        self._models: dict[tuple[str, str], ModelState] = {}
        self._lock = threading.RLock()
        self.state_path = Path(state_path).expanduser() if state_path else None
        self.last_persist_error = ""
        if self.state_path:
            self._load_state()

    def _state_document(self) -> dict:
        """Build the owner-local persistence shape without credentials or payloads."""
        return {
            "schema_version": 1,
            "updated_at": time.time(),
            "providers": {
                name: asdict(state)
                for name, state in self._providers.items()
                if state.state != "closed" or state.failures
            },
            "credentials": [
                {"provider": provider, "slot": slot, **asdict(state)}
                for (provider, slot), state in self._credentials.items()
                if state.state != "ready"
                or state.quota
                or state.last_success_at
            ],
            "models": [
                {"provider": provider, "model": model, **asdict(state)}
                for (provider, model), state in self._models.items()
            ],
        }

    def _persist_locked(self) -> None:
        """Atomically persist request-level state; inference never fails on I/O errors."""
        if not self.state_path:
            return
        path = self.state_path
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._state_document(), handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                temporary = None
                os.chmod(path, 0o600)
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
            self.last_persist_error = ""
        except (OSError, TypeError, ValueError) as exc:
            self.last_persist_error = exc.__class__.__name__

    def _quarantine_corrupt_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        suffix = time.strftime("%Y%m%d-%H%M%S")
        target = self.state_path.with_name(f"{self.state_path.name}.corrupt-{suffix}")
        counter = 1
        while target.exists():
            target = self.state_path.with_name(
                f"{self.state_path.name}.corrupt-{suffix}-{counter}"
            )
            counter += 1
        try:
            os.replace(self.state_path, target)
            os.chmod(target, 0o600)
        except OSError as exc:
            self.last_persist_error = exc.__class__.__name__

    def _load_state(self) -> None:
        path = self.state_path
        if not path or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1:
                raise ValueError("unsupported runtime-state schema")
            now = time.time()
            providers: dict[str, ProviderState] = {}
            for name, item in raw.get("providers", {}).items():
                state = ProviderState(
                    state=str(item.get("state", "closed")),
                    failures=max(0, int(item.get("failures", 0))),
                    open_until=float(item.get("open_until", 0.0)),
                    probe_in_flight=False,
                )
                if state.state in ("open", "half_open") and state.open_until > now:
                    state.state = "open"
                    providers[str(name)] = state
            credentials: dict[tuple[str, int], CredentialState] = {}
            for item in raw.get("credentials", []):
                state = str(item.get("state", "ready"))
                until = float(item.get("cooldown_until", 0.0))
                quota = _safe_quota(item.get("quota", {})) if item.get("quota") else {}
                last_success_at = _safe_float(item.get("last_success_at", 0.0))
                active = state == "terminal" or (state == "cooldown" and until > now)
                if active or quota or last_success_at:
                    if not active:
                        state, until = "ready", 0.0
                    terminal = state == "terminal"
                    credentials[(str(item["provider"]), int(item.get("slot", 0)))] = (
                        CredentialState(
                            state, until, str(item.get("reason", "")),
                            quota, last_success_at,
                            # State written before this field existed anchors its
                            # window at load, so a legacy terminal slot waits one
                            # bounded interval instead of inheriting a permanent stop.
                            terminal_since=(
                                _safe_float(item.get("terminal_since"), now) if terminal else 0.0
                            ),
                            terminal_failures=(
                                max(0, min(4, int(item.get("terminal_failures", 0))))
                                if terminal else 0
                            ),
                            # A probe cannot survive the process that reserved it.
                            probe_in_flight=False,
                        )
                    )
            models: dict[tuple[str, str], ModelState] = {}
            for item in raw.get("models", []):
                until = float(item.get("lockout_until", 0.0))
                if until > now:
                    models[(str(item["provider"]), str(item["model"]))] = ModelState(
                        max(0, int(item.get("failures", 0))),
                        until,
                        str(item.get("reason", "")),
                    )
            self._providers = providers
            self._credentials = credentials
            self._models = models
            os.chmod(path, 0o600)
        except (AttributeError, OSError, OverflowError, KeyError, TypeError, ValueError):
            self._providers = {}
            self._credentials = {}
            self._models = {}
            self._quarantine_corrupt_state()

    def _terminal_probe_delay(self, credential: CredentialState) -> float:
        """Backoff before a terminal slot earns its next recovery probe."""
        multiplier = min(8, 2 ** max(0, credential.terminal_failures - 1))
        return self.credential_terminal_probe * multiplier

    def can_attempt(
        self, provider: str, model: str, credential_slot: int = 0, now: float | None = None
    ) -> tuple[bool, str]:
        now = time.time() if now is None else now
        with self._lock:
            credential = self._credentials.get((provider, credential_slot))
            terminal_probe = False
            if credential:
                if credential.state == "terminal":
                    # Terminal is a long stop, not a one-way door. Once the backoff
                    # window elapses one request is allowed through to find out
                    # whether the condition still holds -- which is also how a
                    # rotated credential recovers without an operator reset.
                    if now < credential.terminal_since + self._terminal_probe_delay(credential):
                        if credential.probe_in_flight:
                            return False, "credential_terminal_probe_busy"
                        return False, f"credential_{credential.reason or 'terminal'}"
                    terminal_probe = True
                elif credential.state == "cooldown" and now < credential.cooldown_until:
                    return False, "credential_cooldown"
                elif credential.state == "cooldown":
                    credential.state = "ready"
                    credential.reason = ""
                    credential.cooldown_until = 0.0
                    self._persist_locked()

            model_state = self._models.get((provider, model))
            if model_state and now < model_state.lockout_until:
                return False, "model_lockout"
            if model_state:
                self._models.pop((provider, model), None)
                self._persist_locked()

            # Reserve a half-open probe only after the credential and model
            # have passed their own gates. Otherwise an ineligible candidate
            # could occupy the provider's single recovery probe indefinitely.
            provider_state = self._providers.get(provider)
            if provider_state:
                if provider_state.state == "half_open" and provider_state.probe_in_flight:
                    return False, "provider_half_open_busy"
                if provider_state.state == "open":
                    if now < provider_state.open_until:
                        return False, "provider_circuit_open"
                    provider_state.state = "half_open"
                    provider_state.probe_in_flight = True
            if terminal_probe:
                # Reserve by restarting the window, so a concurrent caller inside
                # the same lock sees a closed window rather than racing this probe.
                credential.terminal_since = now
                credential.probe_in_flight = True
                self._persist_locked()
            return True, "ready"

    def record_failure(
        self,
        provider: str,
        model: str,
        decision: FailureDecision,
        credential_slot: int = 0,
        retry_after: float = 0.0,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self._lock:
            changed = False
            if decision.provider_failure:
                state = self._providers.setdefault(provider, ProviderState())
                before = asdict(state)
                was_open = state.state == "open" and now < state.open_until
                state.failures = min(self.provider_threshold, state.failures + 1)
                state.probe_in_flight = False
                if state.state == "half_open":
                    state.state = "open"
                    state.open_until = now + self.provider_cooldown
                elif not was_open and state.failures >= self.provider_threshold:
                    state.state = "open"
                    state.open_until = now + self.provider_cooldown
                changed = before != asdict(state)
            else:
                # A half-open probe that reached an upstream and received a
                # credential/model/client error proves the provider transport
                # is reachable. Release and close the provider circuit; the
                # narrower state below owns the failure.
                state = self._providers.get(provider)
                if state and state.state == "half_open":
                    state.state = "closed"
                    state.failures = 0
                    state.open_until = 0.0
                    state.probe_in_flight = False
                    changed = True

            key = (provider, credential_slot)
            current = self._credentials.get(key)
            was_probe = bool(current and current.probe_in_flight)
            if was_probe:
                # The recovery probe came back failing; release it whichever
                # branch below handles the failure, so the reservation can never
                # outlive the attempt that took it.
                current.probe_in_flight = False
                changed = True

            if decision.credential_terminal:
                already_terminal = bool(current and current.state == "terminal")
                # A duplicate in-flight failure against an already-terminal slot
                # must not extend the penalty; only a failed recovery probe is
                # fresh evidence that the condition still holds.
                if not already_terminal or was_probe:
                    new_state = CredentialState(
                        state="terminal",
                        reason=decision.kind,
                        quota=current.quota if current else {},
                        last_success_at=current.last_success_at if current else 0.0,
                        terminal_since=now,
                        terminal_failures=min(
                            4, (current.terminal_failures if already_terminal else 0) + 1
                        ),
                    )
                    if self._credentials.get(key) != new_state:
                        self._credentials[key] = new_state
                        changed = True
            elif decision.credential_cooldown:
                delay = retry_after if retry_after > 0 else (
                    self.credential_cooldown * max(0.0, decision.cooldown_scale)
                )
                if not (current and current.state == "terminal") and (
                    not current or current.state != "cooldown" or current.cooldown_until <= now
                ):
                    self._credentials[key] = CredentialState(
                        state="cooldown", cooldown_until=now + delay, reason=decision.kind,
                        quota=current.quota if current else {},
                        last_success_at=current.last_success_at if current else 0.0,
                    )
                    changed = True

            if decision.model_lockout:
                key = (provider, model)
                state = self._models.setdefault(key, ModelState())
                if state.lockout_until <= now:
                    state.failures = min(4, state.failures + 1)
                    state.reason = decision.kind
                    multiplier = min(8, 2 ** max(0, state.failures - 1))
                    state.lockout_until = now + self.model_cooldown * multiplier
                    changed = True
            if changed:
                self._persist_locked()

    def record_success(
        self, provider: str, model: str, credential_slot: int = 0,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self._lock:
            changed = False
            provider_state = self._providers.get(provider)
            if provider_state and (
                provider_state.state != "closed" or provider_state.failures
                or provider_state.open_until or provider_state.probe_in_flight
            ):
                provider_state.state = "closed"
                provider_state.failures = 0
                provider_state.open_until = 0.0
                provider_state.probe_in_flight = False
                changed = True
            credential = self._credentials.setdefault(
                (provider, credential_slot), CredentialState()
            )
            credential.last_success_at = now
            # A success is proof the credential works, including one that arrived
            # on a terminal slot's recovery probe. Clearing terminal here is what
            # lets a restored quota or a rotated key heal without an operator.
            if (
                credential.state != "ready" or credential.cooldown_until
                or credential.reason or credential.terminal_since
                or credential.terminal_failures or credential.probe_in_flight
            ):
                credential.state = "ready"
                credential.cooldown_until = 0.0
                credential.reason = ""
                credential.terminal_since = 0.0
                credential.terminal_failures = 0
                credential.probe_in_flight = False
                changed = True
            if self._models.pop((provider, model), None) is not None:
                changed = True
            if changed:
                self._persist_locked()

    def record_quota(self, provider: str, credential_slot: int, observation) -> None:
        """Store normalized numeric quota state, never raw upstream headers."""
        if not getattr(observation, "has_headers", False) and observation.classification == "unknown":
            return
        with self._lock:
            credential = self._credentials.setdefault(
                (provider, credential_slot), CredentialState()
            )
            payload = _safe_quota(observation.to_dict())
            if credential.quota != payload:
                credential.quota = payload
                self._persist_locked()

    def order_credentials(
        self, provider: str, slots: list[tuple[int, str]], now: float | None = None
    ) -> list[tuple[int, str]]:
        """Prefer attemptable slots, then earlier reset and more recent success."""
        now = time.time() if now is None else now
        with self._lock:
            def rank(item: tuple[int, str]):
                slot, _ = item
                state = self._credentials.get((provider, slot))
                if not state:
                    return (0, float("inf"), 0.0, slot)
                blocked = state.state == "terminal" or (
                    state.state == "cooldown" and state.cooldown_until > now
                )
                quota = state.quota or {}
                resets = [_safe_float(quota.get(dimension, {}).get("reset_at", 0.0))
                          for dimension in ("requests", "tokens")]
                positive = [value for value in resets if value > now]
                reset_at = min(positive) if positive else float("inf")
                return (1 if blocked else 0, reset_at, -_safe_float(state.last_success_at), slot)
            return sorted(slots, key=rank)

    def scoring_signals(self, now: float | None = None) -> dict[str, dict[str, float]]:
        """Return provider-level health/quota factors without credential identity."""
        now = time.time() if now is None else now
        with self._lock:
            providers = set(self._providers)
            providers.update(provider for provider, _ in self._credentials)
            result = {}
            for provider in providers:
                provider_state = self._providers.get(provider)
                if provider_state and provider_state.state == "open" and provider_state.open_until > now:
                    health = 0.0
                elif provider_state and provider_state.state == "half_open":
                    health = 0.4
                else:
                    health = 1.0
                credential_states = [state for (name, _), state in self._credentials.items()
                                     if name == provider]
                if credential_states:
                    available = [state for state in credential_states if state.state == "ready" or (
                        state.state == "cooldown" and state.cooldown_until <= now
                    )]
                    if not available:
                        health = min(health, 0.2)
                quota_scores = []
                for state in credential_states:
                    if state.state == "terminal" or (
                        state.state == "cooldown" and state.cooldown_until > now
                    ):
                        continue
                    for dimension in ("requests", "tokens"):
                        values = (state.quota or {}).get(dimension, {})
                        limit = _safe_float(values.get("limit"), -1.0)
                        remaining = _safe_float(values.get("remaining"), -1.0)
                        if limit > 0 and remaining >= 0:
                            quota_scores.append(min(1.0, remaining / limit))
                signals = {"health": health}
                if quota_scores:
                    signals["quota"] = max(quota_scores)
                result[provider] = signals
            return result

    def reset(self, provider: str, model: str | None = None) -> None:
        with self._lock:
            if model is None:
                self._providers.pop(provider, None)
                for key in [key for key in self._credentials if key[0] == provider]:
                    self._credentials.pop(key, None)
                for key in [key for key in self._models if key[0] == provider]:
                    self._models.pop(key, None)
            else:
                self._models.pop((provider, model), None)
            self._persist_locked()

    def snapshot(self) -> dict:
        """Return redacted state; credential identity is only a numeric slot."""
        with self._lock:
            return {
                "providers": {name: asdict(state) for name, state in self._providers.items()},
                "credentials": {
                    f"{provider}:slot-{slot}": asdict(state)
                    for (provider, slot), state in self._credentials.items()
                },
                "models": {
                    f"{provider}/{model}": asdict(state)
                    for (provider, model), state in self._models.items()
                },
                "persistence": {
                    "enabled": self.state_path is not None,
                    "healthy": not self.last_persist_error,
                    "error": self.last_persist_error,
                },
            }
