"""Tiny runtime attestation document assembly."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import RUNTIME_GENERATION
from . import paths
from .identity import identity_summary
from .redaction import redact_json

ATTESTATION_SCHEMA = "tiny_runtime_attestation_v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_attestation(
    *,
    bundle_manifest: dict[str, Any],
    identity_doc: dict[str, Any],
    openclaw: dict[str, Any],
    observed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": ATTESTATION_SCHEMA,
        "runtime_generation": RUNTIME_GENERATION,
        "observed_at": observed_at or utc_now_iso(),
        "bundle_id": bundle_manifest.get("bundle_id"),
        "components": bundle_manifest.get("components") or {},
        "identity": identity_summary(identity_doc),
        "paths": {
            "current": str(paths.CURRENT_LINK),
            "state_root": str(paths.STATE_ROOT),
            "openclaw_state_dir": str(paths.OPENCLAW_STATE_DIR),
        },
        "openclaw": redact_json(openclaw),
    }


def write_attestation(path: Path, payload: dict[str, Any]) -> None:
    paths.ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
