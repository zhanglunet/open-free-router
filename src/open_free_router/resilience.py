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
from dataclasses import asdict, dataclass
from datetime import timezone


PROVIDER_FAILURE_CODES = frozenset({408, 500, 502, 503, 504})
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


def classify_failure(status: int, message: str = "") -> FailureDecision:
    """Classify an upstream failure without retaining the raw error body."""
    lowered = (message or "").lower()
    if status in PROVIDER_FAILURE_CODES:
        return FailureDecision("provider_unavailable", True, provider_failure=True)
    if status == 429:
        quota = any(marker in lowered for marker in ("quota", "credit", "balance"))
        return FailureDecision(
            "quota_exhausted" if quota else "rate_limited",
            retryable=not quota,
            credential_cooldown=not quota,
            credential_terminal=quota,
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
    ):
        self.provider_threshold = max(1, int(provider_threshold))
        self.provider_cooldown = max(0.0, float(provider_cooldown))
        self.credential_cooldown = max(0.0, float(credential_cooldown))
        self.model_cooldown = max(0.0, float(model_cooldown))
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
                if state == "terminal" or (state == "cooldown" and until > now):
                    credentials[(str(item["provider"]), int(item.get("slot", 0)))] = (
                        CredentialState(state, until, str(item.get("reason", "")))
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

    def can_attempt(
        self, provider: str, model: str, credential_slot: int = 0, now: float | None = None
    ) -> tuple[bool, str]:
        now = time.time() if now is None else now
        with self._lock:
            credential = self._credentials.get((provider, credential_slot))
            if credential:
                if credential.state == "terminal":
                    return False, f"credential_{credential.reason or 'terminal'}"
                if credential.state == "cooldown" and now < credential.cooldown_until:
                    return False, "credential_cooldown"
                if credential.state == "cooldown":
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

            if decision.credential_terminal:
                key = (provider, credential_slot)
                new_state = CredentialState(
                    state="terminal", reason=decision.kind
                )
                if self._credentials.get(key) != new_state:
                    self._credentials[key] = new_state
                    changed = True
            elif decision.credential_cooldown:
                delay = retry_after if retry_after > 0 else self.credential_cooldown
                key = (provider, credential_slot)
                current = self._credentials.get(key)
                if not (current and current.state == "terminal") and (
                    not current or current.state != "cooldown" or current.cooldown_until <= now
                ):
                    self._credentials[key] = CredentialState(
                        state="cooldown", cooldown_until=now + delay, reason=decision.kind
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

    def record_success(self, provider: str, model: str, credential_slot: int = 0) -> None:
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
            credential = self._credentials.get((provider, credential_slot))
            if credential and credential.state != "terminal":
                credential.state = "ready"
                credential.cooldown_until = 0.0
                credential.reason = ""
                changed = True
            if self._models.pop((provider, model), None) is not None:
                changed = True
            if changed:
                self._persist_locked()

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
