"""Admin-triggered OpenClaw auth-store repair command.

The heartbeat handler stays importable from ``supervisor.py`` through a
delegating re-export, but the implementation lives here so new command
behavior does not grow the supervisor entrypoint past its extraction budget.
"""

from __future__ import annotations

import logging
import time

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

UNIT_CATEGORY = "release-update-lifecycle"


def _post_auth_store_repair_result(
    *,
    revision: int,
    status: str,
    diagnostic: str | None,
    migrated: bool,
    profile_present: bool,
) -> None:
    sup = _sup()
    sup.post_json(
        "/hapi/v1/computers/me/auth-store-repair/apply-result",
        {
            "revision": revision,
            "status": status,
            "diagnostic": diagnostic,
            "migrated": migrated,
            "profile_present": profile_present,
        },
    )


def _spool_auth_store_repair_result(
    *,
    revision: int,
    status: str,
    summary: str,
    started_at_unix: int,
    finished_at_unix: int,
) -> None:
    sup = _sup()
    outcome = "succeeded" if status == "applied" else "failed"
    record = {
        "name": "openclaw auth-store repair",
        "class": "operate",
        "outcome": outcome,
        "started_at_unix": started_at_unix,
        "finished_at_unix": finished_at_unix,
        "idempotency_key": f"auth-store-repair:{revision}",
        "summary": summary,
    }
    try:
        sup.command_spool.append_result(record)
    except Exception as exc:  # noqa: BLE001 - ring transport is best-effort
        log.warning("auth-store repair result spool failed: %s", exc)


def handle_repair_openclaw_auth_store_command(command: dict) -> None:
    """Run OpenClaw doctor auth migration and report the bounded result."""
    sup = _sup()
    revision = sup._command_revision(command)
    if revision is None:
        log.warning("ignoring malformed repair_openclaw_auth_store command: %r", command)
        return

    started = int(time.time())
    migrated = False
    profile_present = False
    status = "failed"
    diagnostic = "OpenClaw auth-store repair did not complete."
    try:
        had_legacy_json = sup._has_legacy_auth_store()
        migrated = sup.repair_openclaw_auth_store_for_upgrade(force=True)
        profile_present = sup.read_chatgpt_subscription_profile() is not None
        if profile_present:
            status = "applied"
            if had_legacy_json and migrated:
                diagnostic = "OpenClaw auth profile migrated to the current auth store."
            elif migrated:
                diagnostic = "OpenClaw auth-store doctor completed; profile is readable."
            else:
                diagnostic = "ChatGPT subscription profile is already readable."
        elif migrated:
            diagnostic = (
                "OpenClaw auth-store doctor completed, but no ChatGPT "
                "subscription profile is readable."
            )
        else:
            diagnostic = (
                "OpenClaw auth-store doctor did not produce a readable "
                "ChatGPT subscription profile."
            )
    except Exception as exc:  # noqa: BLE001 - report failures, do not raise
        diagnostic = f"OpenClaw auth-store repair failed: {exc}"
        log.warning("%s", diagnostic)

    safe_diagnostic = sup._sanitize_runtime_state_text(diagnostic, limit=512)
    finished = int(time.time())
    sup._spool_auth_store_repair_result(
        revision=revision,
        status=status,
        summary=safe_diagnostic,
        started_at_unix=started,
        finished_at_unix=finished,
    )
    try:
        sup._post_auth_store_repair_result(
            revision=revision,
            status=status,
            diagnostic=safe_diagnostic,
            migrated=migrated,
            profile_present=profile_present,
        )
    except Exception as exc:  # noqa: BLE001 - heartbeat redelivery retries
        log.warning(
            "auth-store repair revision=%d result post failed: %s",
            revision,
            exc,
        )
