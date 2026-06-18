"""Tinyhat identity document loading.

The runtime consumes platform-provided `/me/*` identity material but does not
mint or parse tenant secrets itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import paths
from .redaction import redact_json


def load_identity_document(path: Path | None = None) -> dict[str, Any]:
    identity_path = path or paths.IDENTITY_FILE
    if not identity_path.exists():
        return {}
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("identity document must be a JSON object")
    return payload


def identity_summary(identity_doc: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "computer_id",
        "assignment_id",
        "runtime_ref",
        "platform_base_url",
        "tenant_id",
    }
    return redact_json({key: identity_doc.get(key) for key in sorted(allowed) if key in identity_doc})
