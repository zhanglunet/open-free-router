"""PRD-079/T4: secret desensitization for sandbox logs and error echo.

The sensitive pattern is *aligned* with the gateway's ``failover_decisions``
table (``analytics.SENSITIVE_PATTERN``) -- same scope, self-implemented
here because the sandbox layer must not import gateway internals
(``routing`` / ``executor`` / ``translate`` / ``resilience`` /
``failover`` / ``analytics``).

Two modes, mirroring the gateway's philosophy:
- ``redact_secrets``: mask matches with ``[REDACTED]`` (for free text the
  user may see, e.g. echoed program output).
- ``assert_no_secrets``: reject outright (for audit records, which must
  never carry secrets at all -- like ``ExportSecurityError`` in
  analytics.py).
"""
from __future__ import annotations

import json
import re

#: Same scope as the gateway's analytics.SENSITIVE_PATTERN (kept in sync
#: manually; the sandbox cannot import analytics).
SENSITIVE_PATTERN = re.compile(
    r"authorization|api[_-]?key|bearer\s+|sk-[A-Za-z0-9_-]{8,}|"
    r"prompt|response[_-]?body|tool[_-]?arguments",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"


class SensitiveContentError(ValueError):
    """A record that must never carry secrets contained one."""


#: Single-pass masking. Alternatives are ordered longest-match-first so a
#: ``key=value`` span is masked as one unit instead of leaving the value
#: behind (a bare ``api[_-]?key`` match would otherwise leave
#: ``=supersecret`` visible). The trailing alternatives are the gateway's
#: scope (same as ``SENSITIVE_PATTERN``) as a backstop.
_MASK_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*\S+"
    r"|\bbearer\s+\S+"
    r"|authorization|api[_-]?key|bearer\s+|sk-[A-Za-z0-9_-]{8,}"
    r"|prompt|response[_-]?body|tool[_-]?arguments",
    re.IGNORECASE,
)


def redact_secrets(text: str | None) -> str | None:
    """Mask secret-looking spans in free text.

    ``None`` passes through; non-strings are coerced to ``str`` first.
    Masking is value-aware (``api_key=secret`` hides ``secret`` too);
    anything left in the gateway's sensitive scope is masked wholesale.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    return _MASK_PATTERN.sub(REDACTED, text)


def assert_no_secrets(record: dict) -> None:
    """Reject an audit-style record that contains anything secret-looking.

    Mirrors ``AnalyticsStore._assert_safe`` / ``ExportSecurityError``: the
    audit log must never carry secrets at all, so a match is a hard error,
    not a mask.
    """
    encoded = json.dumps(record, ensure_ascii=False, default=str)
    if SENSITIVE_PATTERN.search(encoded):
        raise SensitiveContentError(
            "sensitive content rejected from audit record")
