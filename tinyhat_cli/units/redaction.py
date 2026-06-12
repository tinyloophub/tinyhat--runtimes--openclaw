"""Redaction unit — the always-on runtime-state sanitizer.

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``).
One sanitizer serves every egress surface: the daemon's platform
mirror, and every ``tinyhat`` CLI output (human and ``--json``). The
CLI additionally passes its whole output tree through
:func:`sanitize_json_tree` as defense in depth.

New in the extraction (v0.12.0 M1): a bare Tailscale key pattern
(``tskey-…``) — previously a standalone tailnet key outside a
``KEY=value`` assignment would have passed through unredacted.
"""

from __future__ import annotations

import re
from typing import Any

_RUNTIME_STATE_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([\"']?[A-Za-z0-9_-]*(?:api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|token|password|secret|cookie|"
    r"authorization)[A-Za-z0-9_-]*[\"']?)(\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
)
_RUNTIME_STATE_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
)
_RUNTIME_STATE_URL_USERINFO_RE = re.compile(
    r"(?i)(\bhttps?://)[^/\s:@]+:[^/\s@]+@"
)
_RUNTIME_STATE_SIGNED_QUERY_RE = re.compile(
    r"(?i)([?&][^=\s&]*(?:token|signature|credential|key|secret|password)"
    r"[^=\s&]*=)[^&\s]+"
)
_RUNTIME_STATE_SIGNED_URL_RE = re.compile(
    r"(?i)\bhttps?://[^\s'\"<>)]*\?[^\s'\"<>)]*"
    r"(?:token|signature|credential|key|secret|password)=[^\s'\"<>)]*"
)
_RUNTIME_STATE_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
_RUNTIME_STATE_LLM_PROVIDER_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])sk-(?:ant-api\d+-|ant-|or-v1-|proj-|live-)?"
    r"[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
_RUNTIME_STATE_GOOGLE_ACCESS_TOKEN_RE = re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b")
_RUNTIME_STATE_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")
_RUNTIME_STATE_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_RUNTIME_STATE_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_RUNTIME_STATE_SLACK_TOKEN_RE = re.compile(
    r"\b(?:xox[baprs]-[A-Za-z0-9-]{10,}|xapp-\d-[A-Za-z0-9-]{10,})\b"
)
_RUNTIME_STATE_TELEGRAM_TOKEN_RE = re.compile(
    r"\b(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}\b"
)
# Bare Tailscale keys (auth keys, client secrets) — added with the
# v0.12.0 M1 extraction. Outside a KEY=value assignment these carried
# no other marker the assignment/auth patterns would catch.
_RUNTIME_STATE_TAILSCALE_KEY_RE = re.compile(r"\btskey-[A-Za-z0-9_-]{6,}\b")
_RUNTIME_STATE_LOCAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])/(?:Users|home|root|etc|var|tmp|private|opt)/"
    r"[^\s,'\")]+"
)


def _sanitize_runtime_state_text(value: Any, *, limit: int = 1024) -> str:
    text = str(value or "")
    text = _RUNTIME_STATE_SIGNED_URL_RE.sub("[redacted-signed-url]", text)
    text = _RUNTIME_STATE_URL_USERINFO_RE.sub(r"\1[redacted-userinfo]@", text)
    text = _RUNTIME_STATE_AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group(1)} [redacted]",
        text,
    )
    text = _RUNTIME_STATE_JWT_RE.sub("[redacted-identity-token]", text)
    text = _RUNTIME_STATE_LLM_PROVIDER_KEY_RE.sub("[redacted-api-key]", text)
    text = _RUNTIME_STATE_GOOGLE_ACCESS_TOKEN_RE.sub(
        "[redacted-google-token]",
        text,
    )
    text = _RUNTIME_STATE_GOOGLE_API_KEY_RE.sub("[redacted-api-key]", text)
    text = _RUNTIME_STATE_GITHUB_TOKEN_RE.sub("[redacted-github-token]", text)
    text = _RUNTIME_STATE_AWS_ACCESS_KEY_RE.sub("[redacted-aws-key]", text)
    text = _RUNTIME_STATE_SLACK_TOKEN_RE.sub("[redacted-slack-token]", text)
    text = _RUNTIME_STATE_TELEGRAM_TOKEN_RE.sub("[redacted-telegram-token]", text)
    text = _RUNTIME_STATE_TAILSCALE_KEY_RE.sub("[redacted-tailscale-key]", text)
    text = _RUNTIME_STATE_SIGNED_QUERY_RE.sub(r"\1[redacted]", text)
    text = _RUNTIME_STATE_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        text,
    )
    text = _RUNTIME_STATE_LOCAL_PATH_RE.sub("[local-path]", text)
    return text[:limit]


# CLI string values are short scalars; the cap only exists so a
# pathological value cannot balloon the output.
_SANITIZE_TREE_TEXT_LIMIT = 4096

# Keys whose values are runtime-owned control-plane file paths the
# operator must be able to read verbatim (`manifest show` would
# otherwise print `creation spec: [local-path]`). Values under these
# keys skip sanitization ONLY when they look like a plain absolute
# path — a secret-shaped value under one of these keys still gets the
# full treatment.
_PATH_VALUE_KEY_ALLOWLIST = frozenset(
    {"path", "override_path", "state_path"}
)
_PLAIN_ABSOLUTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,256}$")


def sanitize_json_tree(value: Any, *, key: str | None = None) -> Any:
    """Sanitize every string leaf of a JSON-shaped tree (CLI egress).

    Keys are runtime-owned literals and stay untouched; only values are
    rewritten. Non-string scalars pass through unchanged.
    """
    if isinstance(value, str):
        if (
            key in _PATH_VALUE_KEY_ALLOWLIST
            and _PLAIN_ABSOLUTE_PATH_RE.match(value)
        ):
            return value
        return _sanitize_runtime_state_text(value, limit=_SANITIZE_TREE_TEXT_LIMIT)
    if isinstance(value, dict):
        return {
            item_key: sanitize_json_tree(item, key=item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_json_tree(item) for item in value]
    return value
