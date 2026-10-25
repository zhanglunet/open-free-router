"""Env-only credential access for BYOS adapters (PRD-173/T2, T1 §3.1/§6).

Every environment read in the BYOS layer goes through this module so the
security review has exactly one place to audit. The rules are fixed by the
T1 contract:

* only ``os.environ`` (or a mapping the caller passes in, for tests) is ever
  read -- no file, no keychain, no client session store;
* values never appear in log lines, exception messages or written files --
  providers reference the variable *name* only, and the guard helpers below
  return names, never the values they matched.
"""
from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

# Query parameter names that mark an endpoint URL as carrying credential
# material. Same philosophy as evidence.py's evidence_url checks: a URL that
# needs a secret in its query string is a URL that ends up in logs.
SENSITIVE_QUERY_NAMES = frozenset({
    "key", "api_key", "apikey", "token", "access_token",
    "secret", "signature", "password",
})


def read_env(name: str, env: Mapping[str, str] | None = None) -> str:
    """Read one environment variable; "" when unset, empty or non-string.

    The single environment access point of the BYOS layer (T1 §6 redline 1).
    """
    source = os.environ if env is None else env
    try:
        value = source.get(name, "")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def find_secret_leak(text: str, secrets: Mapping[str, str]) -> list[str]:
    """Return the names of ``secrets`` whose values appear inside ``text``.

    T1 §3.5 redline: before a BYOS resolve result is registered or written
    anywhere, the would-be-persisted fields are scanned for raw credential
    values. A hit means a value leaked into a field that must not carry it.
    Only the variable *names* are returned so a warning can never echo the
    values themselves.
    """
    if not text:
        return []
    return [
        name for name, value in secrets.items()
        if isinstance(value, str) and value and value in text
    ]


def endpoint_is_clean(url: str) -> bool:
    """True when an endpoint URL carries no credential-shaped query material."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return not any(
        name.lower() in SENSITIVE_QUERY_NAMES
        for name, _ in parse_qsl(parts.query, keep_blank_values=True)
    )
