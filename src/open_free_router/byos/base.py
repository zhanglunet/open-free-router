"""BYOS adapter contract and shared machinery (PRD-173/T2, T1 §3.2).

``ByosDescriptor`` and the ``ByosAdapter`` protocol fix the only two actions
a BYOS adapter may take: ``resolve`` (env -> registrable ``ProviderConfig``)
and ``probe`` (one minimal 1-token Chat Completions request). Anything beyond
that -- reading client session stores, refreshing OAuth tokens, managing
subscriptions, bypassing paywalls -- is out of scope by T1 §2 rule 2.

Credentials live only in environment variables referenced by *name*
(``env.py`` is the single access point); endpoint addresses come from env or
the descriptor's placeholder default, never from a hard-coded provider host.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from open_free_router.byos.env import endpoint_is_clean, find_secret_leak, read_env
from open_free_router.registry import ByosMetadata, ProviderConfig
from open_free_router.resilience import classify_failure

log = logging.getLogger(__name__)

# T1 §2 rule 3: the fixed ToS disclaimer every descriptor must carry and the
# README BYOS section must repeat verbatim.
TOS_NOTE_ZH = "BYOS 接入是否被提供方条款允许，由用户自行确认；本项目不规避任何访问控制。"


@dataclass(frozen=True)
class ByosDescriptor:
    """Fixed adapter metadata (T1 §3.2). Extending it requires a T1 revision."""

    name: str  # registry provider name, e.g. "byos-openai-compat"
    display_name: str  # e.g. "BYOS: OpenAI 兼容订阅网关"
    auth_mode: str  # "bearer"; "none" only when the user's registry says so
    key_envs: tuple[str, ...]  # required env variable NAMES, never values
    base_url_env: str  # env variable name for the endpoint address
    default_base_url: str  # placeholder, e.g. "https://<byos-openai-compat-host>/v1"
    priority_class: str  # always "subscribed" for BYOS gateways (T1 §4)
    tos_note_zh: str  # T1 §2 rule 3 disclaimer, surfaced by doctor in T4


class ByosAdapter(Protocol):
    """T1 §3.2 protocol: resolve + probe, nothing else."""

    descriptor: ByosDescriptor

    def resolve(self, env: Mapping[str, str] = os.environ) -> ProviderConfig | None:
        """env 齐备 -> registrable ProviderConfig; 缺 env -> None.

        Never raises, never logs a value. The returned config satisfies:
        ``api_key_env`` holds the variable name, ``api_key`` is always the
        empty string, ``byos`` is a ``ByosMetadata`` block, and
        ``refresh_method="byos"`` marks it for the scheduled-refresh skip.
        """
        ...  # pragma: no cover - protocol

    def probe(self, provider: ProviderConfig, timeout: float = 10.0) -> dict:
        """One minimal 1-token Chat Completions probe (probe.py philosophy).

        Returns exactly ``{ok, status_code, latency_ms, classification}``;
        ``classification`` reuses the ``FailureDecision.kind`` vocabulary and
        stays ``None`` when no upstream response was observed at all. No
        request/response body context is ever retained.
        """
        ...  # pragma: no cover - protocol


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BaseByosAdapter:
    """Shared resolve/probe implementation; concrete adapters bind a descriptor."""

    descriptor: ByosDescriptor
    prefix: str = ""  # model ID prefix, e.g. "byos-oc" (T1 §3.3/§5)
    model_id_env: str = ""  # optional env naming the user's default model
    require_base_url_env: bool = False  # T1 §2 row 3: custom endpoints must come from env

    def resolve(self, env: Mapping[str, str] = os.environ) -> ProviderConfig | None:
        descriptor = self.descriptor
        secrets: dict[str, str] = {}
        for name in descriptor.key_envs:
            value = read_env(name, env)
            if not value:
                log.warning(
                    "byos adapter %s not registered: required env %s is not set",
                    descriptor.name, name,
                )
                return None
            secrets[name] = value

        base_url = read_env(descriptor.base_url_env, env).strip()
        if not base_url:
            if self.require_base_url_env or not descriptor.default_base_url:
                log.warning(
                    "byos adapter %s not registered: required env %s is not set",
                    descriptor.name, descriptor.base_url_env,
                )
                return None
            # Placeholder endpoint: explicitly not routable, so nothing talks
            # to a real host until the user sets the env (T1 §2 row 1).
            base_url = descriptor.default_base_url

        # T1 §3.5 redline: scan before anything is registered or written.
        # A endpoint that needs a secret in its URL is a secret in waiting.
        if not endpoint_is_clean(base_url) or find_secret_leak(base_url, secrets):
            log.warning(
                "byos adapter %s rejected: endpoint field carries credential material",
                descriptor.name,
            )
            return None

        models = []
        model_id = read_env(self.model_id_env, env).strip() if self.model_id_env else ""
        if model_id:
            if find_secret_leak(model_id, secrets) or not endpoint_is_clean(model_id):
                log.warning(
                    "byos adapter %s rejected: model field carries credential material",
                    descriptor.name,
                )
                return None
            # 用户显式登记（env 或 registry）；不做目录轮询（T1 §2）。
            models.append(_model(model_id))

        return ProviderConfig(
            name=descriptor.name,
            base_url=base_url,
            upstream_url=base_url,
            api_key="",  # T1 §6 redline 1: the value lives only in the env
            api_key_env=descriptor.key_envs[0],
            auth_mode=descriptor.auth_mode,
            models=models,
            auto_refresh=False,
            refresh_method="byos",  # T1 §3.3: scheduled refresh skips BYOS
            prefix=self.prefix,
            byos=ByosMetadata(
                kind="subscription_gateway",
                priority_class=descriptor.priority_class,
                enabled=True,
                adopted_at=_now_iso(),
            ),
        )

    def probe(self, provider: ProviderConfig, timeout: float = 10.0) -> dict:
        base = (provider.upstream_url or provider.base_url).rstrip("/")
        key = provider.effective_key
        if not base:
            # Local configuration gap, not an upstream verdict: no claim.
            return _probe_result(False, None, None, None)
        if not key and provider.auth_mode != "none":
            # resolve() guarantees a key, so the env must have vanished;
            # still no upstream observation, so still no classification.
            return _probe_result(False, None, None, None)
        model_id = self._probe_model_id(provider)
        if not model_id:
            return _probe_result(False, None, None, None)

        body = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "open-free-router/0.1"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(f"{base}/chat/completions", data=body, headers=headers, method="POST")

        start = time.monotonic()
        try:
            with urlopen(request, timeout=timeout) as response:
                response.read()
                latency = round((time.monotonic() - start) * 1000)
                return _probe_result(True, response.status, latency, None)
        except HTTPError as error:
            latency = round((time.monotonic() - start) * 1000)
            # The error body is never read: classification from the status
            # code alone, so 429 wording can never upgrade a rate limit into
            # quota exhaustion (宪法 §0「按结构分类，不按文案猜」).
            return _probe_result(False, error.code, latency, classify_failure(error.code).kind)
        except (URLError, OSError, ValueError):
            # No HTTP response arrived: status 0 maps to the generic
            # transport kind of the failover taxonomy.
            latency = round((time.monotonic() - start) * 1000)
            return _probe_result(False, None, latency, classify_failure(0, "").kind)

    def _probe_model_id(self, provider: ProviderConfig) -> str:
        """The model to spend the 1-token request on; "" when none is known."""
        if provider.models:
            return provider.models[0].effective_upstream_id
        return read_env(self.model_id_env) if self.model_id_env else ""


def _model(model_id: str):
    from open_free_router.registry import ModelInfo
    return ModelInfo(id=model_id, name=model_id)


def _probe_result(ok: bool, status_code: int | None, latency_ms: int | None,
                  classification: str | None) -> dict:
    """The fixed T1 §3.2 probe shape; never carries request context."""
    return {
        "ok": ok,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "classification": classification,
    }
