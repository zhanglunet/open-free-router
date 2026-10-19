"""Model caller for the sandbox layer (PRD-079/T3).

Calls the gateway's local proxy over HTTP with the user-selected model ID
passed through in the ``model`` field. Implements the T1 §5 failure
classification and retry policy, plus the dual caps (max model retries +
total budget).

Red lines (T1 design): this module does NOT import any gateway internals
(routing / executor / translate / resilience / failover). It only speaks
HTTP to the local proxy and reads the proxy token file. The backoff
formula mirrors the PRD-059 policy doc §2.2 defaults, but is implemented
here independently -- ``failover.compute_backoff`` is never imported, so
the sandbox layer cannot drift with gateway internals.
"""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Callable, Optional

# T1 §7: the sandbox layer talks to the gateway only through this endpoint.
GATEWAY_BASE_URL = "http://127.0.0.1:8337"

# Failure dispositions (T1 §5).
DISPOSITION_SWITCH = "switch"  # 429/5xx/timeout/connection: next model + backoff
DISPOSITION_SKIP = "skip"      # 404: next model immediately, no backoff
DISPOSITION_ABORT = "abort"    # 401/403: stop the whole list
DISPOSITION_RETURN = "return"  # 400/other 4xx: return directly, no switch

# Backoff defaults aligned with the PRD-059 policy doc §2.2
# (delay_n = min(cap, base * factor^(n-1)); actual = delay_n * (1 +/- jitter)).
BACKOFF_BASE_S = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_S = 30.0
BACKOFF_JITTER = 0.25
# Upper bound for honoring an upstream Retry-After (policy doc §2.2 note).
RETRY_AFTER_MAX_S = 3600.0

# A 200 whose body is not usable chat-completions JSON is treated like a
# model-side failure (a different model will likely answer cleanly). The
# dual caps bound the blast radius if the gateway itself is at fault.
MALFORMED_200_DISPOSITION = DISPOSITION_SWITCH


class ModelCallError(Exception):
    """Terminal model-call failure (no more switching)."""

    def __init__(self, message: str, *, model: Optional[str] = None,
                 http_status: Optional[int] = None,
                 disposition: Optional[str] = None) -> None:
        super().__init__(message)
        self.model = model
        self.http_status = http_status
        self.disposition = disposition


class ModelListExhausted(ModelCallError):
    """Every candidate model was tried and none succeeded."""


class ModelBudgetExhausted(ModelCallError):
    """total_budget_s ran out before any model succeeded."""


class ModelAuthError(ModelCallError):
    """401/403: the credential/permission is bad; switching models won't help."""


class ModelBadRequest(ModelCallError):
    """400/other 4xx: the request itself is bad; returned directly, no switch."""


class TransportError(ModelCallError):
    """Connection failed / timed out before any HTTP status arrived."""


@dataclass
class ModelAttempt:
    """One model-call attempt (the 换模型记录 entries)."""

    model: str
    outcome: str  # "success" | "switched" | "skipped"
    http_status: Optional[int] = None
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class ModelCallResult:
    """Successful model call: which model answered, what it said, and the trail."""

    model: str
    response: dict
    attempts: list[ModelAttempt] = field(default_factory=list)

    @property
    def switches(self) -> list[dict]:
        """JSON-ready 换模型记录."""
        return [
            {
                "model": a.model,
                "outcome": a.outcome,
                "http_status": a.http_status,
                "latency_ms": round(a.latency_ms, 1),
                "error": a.error,
            }
            for a in self.attempts
        ]


def classify_http_status(status: int) -> str:
    """Map an HTTP status to a T1 §5 disposition.

    408 is not in the T1 table; it is timeout-like, so it switches (mild,
    documented extension). Other unlisted 4xx codes return directly -- the
    request is at fault, not the model, so switching would burn quota.
    """
    if status == 429:
        return DISPOSITION_SWITCH
    if 500 <= status <= 599:
        return DISPOSITION_SWITCH
    if status == 408:
        return DISPOSITION_SWITCH
    if status == 404:
        return DISPOSITION_SKIP
    if status in (401, 403):
        return DISPOSITION_ABORT
    # 400 and every other 4xx: the request itself is the problem.
    return DISPOSITION_RETURN


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header to seconds; None when absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None and seconds >= 0:
        return seconds
    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        delta = (dt - dt.now(dt.tzinfo)).total_seconds() if dt.tzinfo else None
        if delta is not None and delta >= 0:
            return delta
    except (TypeError, ValueError):
        pass
    return None


def compute_backoff(n: int, *, base: float = BACKOFF_BASE_S,
                    factor: float = BACKOFF_FACTOR, cap: float = BACKOFF_CAP_S,
                    jitter: float = BACKOFF_JITTER,
                    rand: Callable[[], float] = random.random) -> float:
    """Independent implementation of the PRD-059 policy doc §2.2 formula.

    ``n`` is 1-based (first retry -> n=1). Never imports failover.
    """
    delay = min(cap, base * (factor ** max(0, n - 1)))
    return delay * (1 + (rand() * 2 - 1) * jitter)


def extract_code(response: dict) -> str:
    """Pull executable Python code out of a chat-completions response.

    Prefers a ```python fenced block, then any fenced block, then the raw
    content. Raises ModelCallError when there is nothing usable.
    """
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not content or not content.strip():
        raise ModelCallError("model returned an empty response")
    text = content.strip()
    import re

    m = re.search(r"```python\s*\n(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return text


CODEGEN_SYSTEM_PROMPT = (
    "You are a code generator. Output ONLY executable Python 3 code for the "
    "user's task. Wrap the code in a ```python fenced block. Do not include "
    "explanations or any text outside the code block."
)


class ModelCaller:
    """Calls the gateway proxy with user-selected models and failover."""

    def __init__(
        self,
        *,
        base_url: str = GATEWAY_BASE_URL,
        token: Optional[str] = None,
        model_timeout_s: float = 120.0,
        max_model_retries: int = 3,
        total_budget_s: float = 300.0,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_factor: float = BACKOFF_FACTOR,
        backoff_cap_s: float = BACKOFF_CAP_S,
        backoff_jitter: float = BACKOFF_JITTER,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if max_model_retries < 1:
            raise ValueError("max_model_retries must be >= 1")
        if total_budget_s <= 0:
            raise ValueError("total_budget_s must be > 0")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model_timeout_s = model_timeout_s
        self.max_model_retries = max_model_retries
        self.total_budget_s = total_budget_s
        self._backoff = {
            "base": backoff_base_s, "factor": backoff_factor,
            "cap": backoff_cap_s, "jitter": backoff_jitter,
        }
        self._sleep = sleep
        self._rand = rand

    # -- low-level HTTP -------------------------------------------------

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def call_once(self, model: str, messages: list, **extra: object):
        """One POST to /v1/chat/completions.

        Returns (status, parsed_json_or_text, headers, latency_ms).
        Transport problems raise TransportError (no HTTP status).
        """
        url = f"{self.base_url}/v1/chat/completions"
        payload = {"model": model, "messages": messages}
        payload.update(extra)
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers=self._headers(),
                                     method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.model_timeout_s) as resp:
                raw = resp.read()
                status = resp.status
                headers = dict(resp.headers)
        except urllib.error.HTTPError as e:
            # HTTPError still carries the status + body: read them here so
            # the caller can classify instead of treating it as transport.
            try:
                raw = e.read()
            except Exception:
                raw = b""
            status = e.code
            try:
                headers = dict(e.headers)
            except Exception:
                headers = {}
            latency_ms = (time.monotonic() - started) * 1000
            return status, _maybe_json(raw), headers, latency_ms
        except (urllib.error.URLError, TimeoutError, socket.timeout,
                ConnectionError, HTTPException, OSError) as e:
            raise TransportError(f"transport failure calling model {model!r}: {e}",
                                 model=model) from e
        latency_ms = (time.monotonic() - started) * 1000
        return status, _maybe_json(raw), headers, latency_ms

    # -- retry orchestration --------------------------------------------

    def call_with_retry(self, models: list, messages: list,
                        **extra: object) -> ModelCallResult:
        """Try each model in order until one answers.

        Applies the T1 §5 failure table and the dual caps. ``models`` longer
        than ``max_model_retries`` is truncated -- at most max_model_retries
        models are ever tried, including the first.
        """
        models = list(models)
        if not models:
            raise ValueError("models must not be empty")
        deadline = time.monotonic() + self.total_budget_s
        attempts: list[ModelAttempt] = []
        tried = 0
        retry_n = 0  # counts backoff-eligible switches (for delay_n)

        for index, model in enumerate(models):
            if tried >= self.max_model_retries:
                break
            if time.monotonic() >= deadline:
                raise ModelBudgetExhausted(
                    f"total budget {self.total_budget_s:g}s exhausted after "
                    f"{tried} model(s); last error: "
                    f"{attempts[-1].error if attempts else 'none'}",
                    model=model)
            tried += 1
            try:
                status, data, headers, latency_ms = self.call_once(
                    model, messages, **extra)
            except TransportError as e:
                disposition = DISPOSITION_SWITCH
                status, data, headers, latency_ms = None, None, {}, 0.0
                err_text = str(e)
            else:
                err_text = None
                if 200 <= status < 300:
                    if not _looks_like_chat_response(data):
                        disposition = MALFORMED_200_DISPOSITION
                        err_text = "malformed chat-completions response"
                    else:
                        attempts.append(ModelAttempt(
                            model=model, outcome="success",
                            http_status=status, latency_ms=latency_ms))
                        return ModelCallResult(model=model, response=data,
                                               attempts=attempts)
                else:
                    disposition = classify_http_status(status)
                    err_text = _short_error(data, status)

            if disposition == DISPOSITION_ABORT:
                raise ModelAuthError(
                    f"model {model!r} returned {status}: not retrying other "
                    f"models (credential/permission failure)",
                    model=model, http_status=status, disposition=disposition)
            if disposition == DISPOSITION_RETURN:
                raise ModelBadRequest(
                    f"model {model!r} returned {status}: request not retried "
                    f"({err_text})",
                    model=model, http_status=status, disposition=disposition)

            # SWITCH or SKIP: record and move to the next model.
            attempts.append(ModelAttempt(
                model=model,
                outcome="switched" if disposition == DISPOSITION_SWITCH else "skipped",
                http_status=status, latency_ms=latency_ms, error=err_text))

            if tried >= self.max_model_retries or index + 1 >= len(models):
                break
            if time.monotonic() >= deadline:
                raise ModelBudgetExhausted(
                    f"total budget {self.total_budget_s:g}s exhausted after "
                    f"{tried} model(s); last error: {err_text}",
                    model=model)
            if disposition == DISPOSITION_SWITCH:
                retry_n += 1
                delay = self._switch_delay(retry_n, headers)
                remaining = deadline - time.monotonic()
                if delay >= remaining:
                    raise ModelBudgetExhausted(
                        f"total budget {self.total_budget_s:g}s would be "
                        f"exceeded by the {delay:.1f}s backoff; stopping after "
                        f"{tried} model(s)",
                        model=model)
                self._sleep(delay)
            # SKIP: straight to the next model, no backoff.

        last_err = attempts[-1].error if attempts else "no attempts"
        raise ModelListExhausted(
            f"all {tried} tried model(s) failed ({len(models)} listed); "
            f"last error: {last_err}",
            model=models[min(tried, len(models) - 1)])

    def _switch_delay(self, retry_n: int, headers: dict) -> float:
        # Policy doc §2.2 note: a parseable Retry-After wins over the
        # exponential formula (bounded by the same cap family).
        retry_after = parse_retry_after(
            headers.get("Retry-After") or headers.get("retry-after"))
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_MAX_S, self._backoff["cap"])
        return compute_backoff(retry_n, rand=self._rand, **self._backoff)

    # -- code generation --------------------------------------------------

    def generate_code(self, models: list, task: str,
                      **extra: object) -> tuple[str, ModelCallResult]:
        """Ask the model to produce executable Python 3 code for ``task``.

        Returns (code, result). The code is extracted from the model's
        response; the sandbox (T2) executes it.
        """
        messages = [
            {"role": "system", "content": CODEGEN_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        extra.setdefault("temperature", 0.0)
        result = self.call_with_retry(models, messages, **extra)
        return extract_code(result.response), result


def _maybe_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError):
        return raw.decode("utf-8", errors="replace")


def _looks_like_chat_response(data) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("choices"), list)
        and bool(data["choices"])
        and isinstance(data["choices"][0], dict)
        and isinstance(data["choices"][0].get("message"), dict)
    )


def _short_error(data, status: int) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            text = str(err["message"])
        elif isinstance(err, str):
            text = err
        else:
            text = json.dumps(data, ensure_ascii=False)[:300]
    else:
        text = str(data)[:300]
    return f"http {status}: {text}".strip()
