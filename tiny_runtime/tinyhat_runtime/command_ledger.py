"""On-box mirror for platform command ledger rows."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .redaction import redact_text

COMMAND_MIRROR_SCHEMA = "tiny_runtime_command_mirror_v1"
TERMINAL_STATUSES = frozenset({"applied", "failed", "rolled_back", "canceled", "timed_out"})
NON_TERMINAL_STATUSES = frozenset(
    {"platform_created", "dispatched", "runtime_mirrored", "running", "cancel_requested"}
)
ALLOWED_STATUSES = TERMINAL_STATUSES | NON_TERMINAL_STATUSES
COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_COMMAND_JSON_BYTES = 64 * 1024
MAX_STRING_CHARS = 2048
MAX_LIST_ITEMS = 50
MAX_DICT_ITEMS = 200
MAX_DEPTH = 12

_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "identity_token",
    "signed_url",
)


class CommandLedgerError(ValueError):
    """Raised when a command mirror cannot be safely read or written."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_command_id(command_id: str) -> str:
    if not isinstance(command_id, str) or not COMMAND_ID_PATTERN.fullmatch(command_id):
        raise CommandLedgerError("command_id must be 1-128 safe path characters")
    return command_id


def _sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_command_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, limit=MAX_STRING_CHARS)
    if isinstance(value, (bytes, bytearray)):
        return "[binary]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (raw_key, raw_item) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS:
                out["..."] = "[truncated]"
                break
            key = str(raw_key)
            if _sensitive_key(key):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_command_value(raw_item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [
            redact_command_value(item, depth=depth + 1)
            for item in list(value)[:MAX_LIST_ITEMS]
        ]
    return redact_text(str(value), limit=MAX_STRING_CHARS)


def _canonical_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_COMMAND_JSON_BYTES:
        raise CommandLedgerError("command mirror payload is too large")
    return encoded + "\n"


class CommandLedger:
    """Durable, redacted on-box mirror under ``/var/log/tinyhat/commands``.

    ``command.json`` is the user-verifiable record. ``commands.sqlite`` is a
    local index for listing/filtering without making SQLite the only source a
    person can inspect on the box.
    """

    def __init__(
        self,
        root: Path = paths.COMMANDS_LOG_DIR,
        *,
        sqlite_path: Path | None = None,
    ) -> None:
        self.root = root
        self.sqlite_path = sqlite_path or (root / "commands.sqlite")

    def command_dir(self, command_id: str) -> Path:
        safe_id = validate_command_id(command_id)
        return self.root / safe_id

    def command_path(self, command_id: str) -> Path:
        return self.command_dir(command_id) / "command.json"

    def load(self, command_id: str) -> dict[str, Any] | None:
        path = self.command_path(command_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CommandLedgerError("command mirror must be a JSON object")
        return payload

    def iter_replayable(self) -> Iterable[dict[str, Any]]:
        if not self.root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/command.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            status = payload.get("status")
            if status in NON_TERMINAL_STATUSES:
                entries.append(payload)
        return entries

    def mirror(
        self,
        command: dict[str, Any],
        *,
        status: str = "runtime_mirrored",
        phase: str = "mirrored",
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        if status not in ALLOWED_STATUSES:
            raise CommandLedgerError(f"unsupported command status: {status}")
        command_id = validate_command_id(str(command.get("command_id") or ""))
        mirrored_at = observed_at or utc_now_iso()
        payload = {
            "schema": COMMAND_MIRROR_SCHEMA,
            "command_id": command_id,
            "idempotency_key": str(command.get("idempotency_key") or ""),
            "kind": str(command.get("kind") or ""),
            "spec": redact_command_value(command.get("spec") or {}),
            "status": status,
            "phase": phase,
            "cancel_requested_at": command.get("cancel_requested_at"),
            "created_at": mirrored_at,
            "updated_at": mirrored_at,
        }
        self._write(command_id, payload)
        return payload

    def update(
        self,
        command_id: str,
        *,
        status: str,
        phase: str,
        failure_code: str | None = None,
        result: dict[str, Any] | None = None,
        redacted_log_tail: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        if status not in ALLOWED_STATUSES:
            raise CommandLedgerError(f"unsupported command status: {status}")
        payload = self.load(command_id)
        if payload is None:
            payload = {
                "schema": COMMAND_MIRROR_SCHEMA,
                "command_id": validate_command_id(command_id),
                "created_at": observed_at or utc_now_iso(),
            }
        payload["status"] = status
        payload["phase"] = phase
        payload["updated_at"] = observed_at or utc_now_iso()
        if failure_code is not None:
            payload["failure_code"] = failure_code
        if result is not None:
            payload["result"] = redact_command_value(result)
        if redacted_log_tail is not None:
            payload["redacted_log_tail"] = redact_text(redacted_log_tail, limit=4000)
        self._write(command_id, payload)
        return payload

    def _write(self, command_id: str, payload: dict[str, Any]) -> None:
        command_dir = self.command_dir(command_id)
        command_dir.mkdir(parents=True, exist_ok=True)
        target = command_dir / "command.json"
        tmp = command_dir / ".command.json.tmp"
        tmp.write_text(_canonical_json(payload), encoding="utf-8")
        os.replace(tmp, target)
        self._upsert_index(payload, on_box_path=target)

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.sqlite_path), timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                failure_code TEXT,
                on_box_path TEXT NOT NULL,
                spec_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        return connection

    def _upsert_index(self, payload: dict[str, Any], *, on_box_path: Path) -> None:
        command_id = validate_command_id(str(payload.get("command_id") or ""))
        created_at = str(payload.get("created_at") or payload.get("updated_at") or utc_now_iso())
        updated_at = str(payload.get("updated_at") or created_at)
        spec_json = json.dumps(
            payload.get("spec") or {},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        result_json = json.dumps(
            payload.get("result") or {},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO commands (
                    command_id,
                    idempotency_key,
                    kind,
                    status,
                    phase,
                    failure_code,
                    on_box_path,
                    spec_json,
                    result_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                    idempotency_key=excluded.idempotency_key,
                    kind=excluded.kind,
                    status=excluded.status,
                    phase=excluded.phase,
                    failure_code=excluded.failure_code,
                    on_box_path=excluded.on_box_path,
                    spec_json=excluded.spec_json,
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (
                    command_id,
                    str(payload.get("idempotency_key") or ""),
                    str(payload.get("kind") or ""),
                    str(payload.get("status") or ""),
                    str(payload.get("phase") or ""),
                    payload.get("failure_code"),
                    str(on_box_path),
                    spec_json,
                    result_json,
                    created_at,
                    updated_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()
