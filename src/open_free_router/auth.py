"""Local auth token for the UI's state-changing endpoints.

The dashboard has no user accounts. A single random token is generated on
first run and stored on disk with restrictive permissions. Browser clients
send it back via the ``Authorization: Bearer <token>`` header; the JS
front-end prompts for it once per browser session and keeps it in memory.

This is deliberately simple — it is not meant to make the UI safe to expose
on a public network, only to stop an unauthenticated client on the same
host/LAN from silently rewriting provider upstream URLs or the config file
(see README's security note).
"""
from __future__ import annotations

import hmac
import secrets
from pathlib import Path

TOKEN_FILENAME = "ui.token"
PROXY_TOKEN_FILENAME = "proxy.token"


def token_path_for(config_dir: Path) -> Path:
    return config_dir / TOKEN_FILENAME


def _get_or_create_named_token(config_dir: Path, filename: str) -> str:
    path = config_dir / filename
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    config_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort; not all platforms support POSIX perms
    return token


def get_or_create_token(config_dir: Path) -> str:
    """Return the local UI auth token, generating one on first run."""
    return _get_or_create_named_token(config_dir, TOKEN_FILENAME)


def get_or_create_proxy_token(config_dir: Path) -> str:
    """Return the bearer token required by the inference proxy."""
    return _get_or_create_named_token(config_dir, PROXY_TOKEN_FILENAME)


def check_auth(headers, token: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer <token>`` header.

    ``headers`` is anything with a ``.get(name, default)`` method, e.g. an
    ``http.client.HTTPMessage`` from ``BaseHTTPRequestHandler.headers``.
    """
    if not token:
        return False
    supplied = headers.get("Authorization", "")
    if not supplied.startswith("Bearer "):
        return False
    supplied = supplied[len("Bearer "):].strip()
    return hmac.compare_digest(supplied, token)
