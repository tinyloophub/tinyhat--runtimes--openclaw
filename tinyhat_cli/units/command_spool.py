"""Command-result spool — how CLI results reach the daemon's mirror.

Single-writer rule: the **daemon is the only runtime-state poster**.
CLI commands never post; they append one pre-redacted JSON record per
result here (root-owned ``0700`` dir, ``0600`` files, temp file +
fsync + atomic rename). The daemon folds spool records into the
``commands`` ring on its next runtime-state write and prunes what it
folded; ``tinyhat status`` reads the same spool through the same
reader, so a support shell sees results immediately even when the
daemon is down.

Bounds (enforced at write time): max :data:`MAX_RECORD_BYTES` per
encoded record (deterministic ``detail``-then-``summary`` trim), max
:data:`MAX_SPOOL_RECORDS` records / :data:`MAX_SPOOL_BYTES` total
(oldest-first prune). Corrupt records move to a bounded
``quarantine/`` instead of blocking later folds.

Redaction is fail-closed: if the sanitizer cannot produce a clean
record, **nothing is written** and the caller gets a typed error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup
from tinyhat_cli.units.redaction import sanitize_json_tree

log = logging.getLogger("tinyhat-supervisor")

COMMAND_SPOOL_SCHEMA = "command_result_spool_v1"
TINYHAT_COMMAND_RESULTS_DIR_ENV = "TINYHAT_COMMAND_RESULTS_DIR"

MAX_RECORD_BYTES = 2048
MAX_SPOOL_BYTES = 64 * 1024
MAX_SPOOL_RECORDS = 50
MAX_QUARANTINE_RECORDS = 10
SUMMARY_MAX_BYTES = 256

# The §7 ring summary shape. Everything else a result carries stays in
# the idempotency store; the spool only transports what the ring shows.
RING_FIELDS = (
    "name",
    "class",
    "outcome",
    "started_at_unix",
    "finished_at_unix",
    "idempotency_key",
    "summary",
    "runner_lost",
    "stale_takeover",
)


class SpoolRedactionError(RuntimeError):
    """Redaction failed — by contract the record is NOT written."""


def command_results_dir() -> str:
    configured = (os.environ.get(TINYHAT_COMMAND_RESULTS_DIR_ENV) or "").strip()
    if configured:
        return configured
    sup = _sup()
    return os.path.join(
        os.path.dirname(sup.runtime_state_path()), "command-results"
    )


def spool_dir() -> str:
    return os.path.join(command_results_dir(), "spool")


def quarantine_dir() -> str:
    return os.path.join(command_results_dir(), "quarantine")


def _ring_projection(record: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in RING_FIELDS:
        if field in record and record[field] is not None:
            projected[field] = record[field]
    return projected


def _encode(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _truncate_text_field(record: dict[str, Any], field: str, limit: int) -> None:
    value = record.get(field)
    if isinstance(value, str) and len(value.encode("utf-8")) > limit:
        encoded = value.encode("utf-8")[:limit]
        record[field] = encoded.decode("utf-8", errors="ignore")


def append_result(record: dict[str, Any]) -> str:
    """Sanitize, bound, and atomically write one result record.

    Returns the written path. Raises :class:`SpoolRedactionError` when
    the sanitizer fails — fail-closed, nothing reaches the spool.
    """
    projected = _ring_projection(record)
    try:
        sanitized = sanitize_json_tree(projected)
        if not isinstance(sanitized, dict):
            raise TypeError("sanitizer returned a non-dict record")
    except Exception as exc:  # noqa: BLE001 - the contract is fail-closed
        raise SpoolRedactionError(
            f"redaction failed; command result NOT spooled: {exc}"
        ) from exc

    _truncate_text_field(sanitized, "summary", SUMMARY_MAX_BYTES)
    if len(_encode(sanitized)) > MAX_RECORD_BYTES:
        # Deterministic trim order: summary harder, then non-ring extras
        # are already gone (the projection), so the record always fits.
        _truncate_text_field(sanitized, "summary", 128)
    encoded = _encode(sanitized)
    if len(encoded) > MAX_RECORD_BYTES:
        raise SpoolRedactionError(
            f"command result record is {len(encoded)} bytes even after "
            f"trimming (max {MAX_RECORD_BYTES}); refusing to spool"
        )

    directory = spool_dir()
    sup = _sup()
    sup._prepare_control_plane_state_dir(command_results_dir())
    sup._prepare_control_plane_state_dir(directory)
    finished = sanitized.get("finished_at_unix")
    stamp = finished if isinstance(finished, int) else int(time.time())
    # The filename's key part is for ordering/uniqueness only (semantics
    # live inside the record): a short digest keeps any caller-supplied
    # key — long, slashed, unicode — within filesystem limits.
    key = str(sanitized.get("idempotency_key") or "no-key")
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    filename = f"{stamp:012d}-{key_digest}.json"
    path = os.path.join(directory, filename)

    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    _prune_spool()
    return path


def _spool_files() -> list[str]:
    try:
        names = sorted(
            name
            for name in os.listdir(spool_dir())
            if name.endswith(".json") and not name.startswith(".")
        )
    except OSError:
        return []
    return [os.path.join(spool_dir(), name) for name in names]


def _prune_spool() -> None:
    files = _spool_files()
    sizes: dict[str, int] = {}
    for path in files:
        try:
            sizes[path] = os.stat(path).st_size
        except OSError:
            sizes[path] = 0
    total = sum(sizes.values())
    while files and (len(files) > MAX_SPOOL_RECORDS or total > MAX_SPOOL_BYTES):
        oldest = files.pop(0)
        total -= sizes.get(oldest, 0)
        try:
            os.unlink(oldest)
        except OSError:
            pass


def _quarantine(path: str) -> None:
    directory = quarantine_dir()
    try:
        sup = _sup()
        sup._prepare_control_plane_state_dir(directory)
        shutil.move(path, os.path.join(directory, os.path.basename(path)))
    except OSError as exc:
        log.warning("failed to quarantine corrupt spool record %s: %s", path, exc)
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    try:
        names = sorted(
            name for name in os.listdir(directory) if name.endswith(".json")
        )
    except OSError:
        return
    while len(names) > MAX_QUARANTINE_RECORDS:
        oldest = names.pop(0)
        try:
            os.unlink(os.path.join(directory, oldest))
        except OSError:
            pass


def read_results() -> list[tuple[str, dict[str, Any]]]:
    """The shared reader (daemon fold + ``tinyhat status``), oldest first.

    Corrupt records are quarantined and skipped — they never block a
    later fold or a support answer.
    """
    results: list[tuple[str, dict[str, Any]]] = []
    for path in _spool_files():
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("spool record is not a JSON object")
        except (OSError, ValueError) as exc:
            log.warning("quarantining corrupt spool record %s: %s", path, exc)
            _quarantine(path)
            continue
        results.append((path, payload))
    return results


def prune_folded(paths: list[str]) -> None:
    """Drop records the daemon has folded into the ``commands`` ring."""
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
