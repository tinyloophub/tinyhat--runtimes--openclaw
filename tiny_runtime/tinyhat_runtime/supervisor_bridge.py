"""Bridge heartbeat-delivered supervisor commands into tiny_runtime."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any, Callable

from .command_ledger import utc_now_iso
from .redaction import redact_text
from .runtime_commands import RuntimeCommandRunner

RUNTIME_COMMAND_RESULT_ENDPOINT = "/hapi/v1/computers/me/runtime-command/result"
RUNTIME_COMMAND_ARTIFACT_MAX_BYTES_ENV = (
    "TINYHAT_RUNTIME_COMMAND_ARTIFACT_MAX_BYTES"
)
RUNTIME_COMMAND_ARTIFACT_MAX_BYTES_DEFAULT = 2 * 1024 * 1024


def handle_runtime_command(
    command: dict[str, Any],
    *,
    post_json: Callable[[str, dict[str, Any]], dict[str, Any]],
    logger: logging.Logger,
    runner: RuntimeCommandRunner | None = None,
) -> None:
    """Execute a typed runtime command and best-effort POST the result."""
    runtime_command = command.get("command")
    if not isinstance(runtime_command, dict):
        runtime_command = {
            key: value
            for key, value in command.items()
            if key not in {"type", "revision"}
        }
    try:
        result = (runner or RuntimeCommandRunner()).execute(runtime_command)
    except Exception as exc:  # noqa: BLE001 - command boundary
        logger.warning("runtime command execution failed before result: %s", exc)
        result = _runtime_command_failure_result(runtime_command, exc)

    body: dict[str, Any] = {"result": result}
    artifact = _diagnostics_artifact_for_result(result, logger=logger)
    if artifact is not None:
        body["artifact"] = artifact
    try:
        post_json(RUNTIME_COMMAND_RESULT_ENDPOINT, body)
    except Exception as exc:  # noqa: BLE001 - redelivery will repost
        logger.warning(
            "failed to post runtime command result command_id=%r: %s",
            result.get("command_id"),
            exc,
        )


def _runtime_command_failure_result(
    command: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    return {
        "schema": "tiny_runtime_command_result_v1",
        "command_id": str(command.get("command_id") or ""),
        "idempotency_key": str(command.get("idempotency_key") or ""),
        "kind": str(command.get("kind") or ""),
        "status": "failed",
        "phase": "supervisor_dispatch",
        "failure_code": "invalid_command",
        "observed_at": utc_now_iso(),
        "result": {"detail": redact_text(str(exc), limit=1000)},
    }


def _diagnostics_artifact_for_result(
    result: dict[str, Any],
    *,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    if result.get("kind") != "export_diagnostics":
        return None
    payload = result.get("result")
    if not isinstance(payload, dict):
        return None
    output_path = payload.get("output_path")
    if not isinstance(output_path, str) or not output_path:
        return None
    try:
        stat_result = os.stat(output_path)
    except OSError as exc:
        logger.warning("runtime command diagnostics artifact missing: %s", exc)
        return None

    max_bytes = _runtime_command_artifact_max_bytes()
    if max_bytes <= 0 or stat_result.st_size > max_bytes:
        logger.warning(
            "runtime command diagnostics artifact not uploaded: size=%d max=%d path=%s",
            stat_result.st_size,
            max_bytes,
            output_path,
        )
        return {
            "schema": "tiny_runtime_command_artifact_v1",
            "kind": "diagnostics_zip",
            "state": "too_large",
            "filename": os.path.basename(output_path),
            "content_type": "application/zip",
            "size_bytes": stat_result.st_size,
            "max_bytes": max_bytes,
            "sha256": _file_sha256(output_path),
        }

    with open(output_path, "rb") as fh:
        data = fh.read()
    return {
        "schema": "tiny_runtime_command_artifact_v1",
        "kind": "diagnostics_zip",
        "state": "uploaded",
        "filename": os.path.basename(output_path),
        "content_type": "application/zip",
        "encoding": "base64",
        "data_base64": base64.b64encode(data).decode("ascii"),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _runtime_command_artifact_max_bytes() -> int:
    raw = (os.environ.get(RUNTIME_COMMAND_ARTIFACT_MAX_BYTES_ENV) or "").strip()
    if not raw:
        return RUNTIME_COMMAND_ARTIFACT_MAX_BYTES_DEFAULT
    try:
        parsed = int(raw)
    except ValueError:
        return RUNTIME_COMMAND_ARTIFACT_MAX_BYTES_DEFAULT
    return max(0, parsed)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
