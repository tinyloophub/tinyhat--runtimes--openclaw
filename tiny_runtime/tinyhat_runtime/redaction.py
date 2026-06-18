"""Small non-secret redaction helpers for diagnostics and attestations."""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]{12,}"),
    re.compile(r"(?i)(authorization[=:]\s*)[a-z0-9._:\-/+]{12,}"),
    re.compile(r"(?i)(token[=:]\s*)[a-z0-9._:\-/+]{12,}"),
    re.compile(r"(?i)(secret[=:]\s*)[a-z0-9._:\-/+]{12,}"),
    re.compile(r"\bsk-[a-z0-9][a-z0-9_\-]{12,}\b", re.IGNORECASE),
    re.compile(r"\btskey-[a-z0-9_\-]{12,}\b", re.IGNORECASE),
)


def redact_text(value: str, *, limit: int = 4000) -> str:
    text = value[:limit]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1) if match.groups() else ''}[REDACTED]", text)
    if len(value) > limit:
        text += "...[truncated]"
    return text


def redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(
                marker in lowered
                for marker in (
                    "token",
                    "secret",
                    "password",
                    "key",
                    "authorization",
                    "cookie",
                    "credential",
                    "private_key",
                    "identity_token",
                    "signed_url",
                )
            ):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact_json(item)
        return out
    return value
