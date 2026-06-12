"""Gateway-restart unit — the semantic restart sequence (M2 skeleton).

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``):
the production gateway restart is a semantic operation, not just a
subprocess — webhook delete so OpenClaw can long-poll, ``systemctl
reset-failed`` (mandatory: rapid restarts without it trip systemd's
start-rate limit) + ``restart``, then a bounded readiness wait that
follows the gateway's own log output to a terminal verdict.

:func:`run_gateway_restart_transaction` is the typed operation
transaction the next milestone exposes as ``tinyhat gateway restart``
under the global command lock. It ships dark in this slice: composed
and unit-tested, registered nowhere, and the daemon keeps driving the
same underlying functions through ``ensure_openclaw_gateway_ready``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

GATEWAY_RESTART_PHASES = (
    "webhook_delete",
    "child_running",
    "readiness_wait",
    "terminal",
)
GATEWAY_RESTART_OUTCOMES = ("succeeded", "failed", "timed_out")


# ── moved ────────────────────────────────────────────────────────────


def delete_telegram_webhook(binding: dict) -> None:
    """Clear Tinyhat's fallback webhook before OpenClaw long-polls."""
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")
    payload = json.dumps({"drop_pending_updates": False}).encode("utf-8")
    last_error = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/deleteWebhook",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8") or "{}"
            data = json.loads(body)
            if data.get("ok") is not True:
                raise RuntimeError("Telegram deleteWebhook returned ok=false")
            log.info(
                "cleared Telegram webhook for OpenClaw long polling (bot=@%s)",
                binding.get("telegram_bot_username"),
            )
            return
        except Exception as exc:
            last_error = exc
            log.warning(
                "Telegram deleteWebhook failed before OpenClaw handoff "
                "(attempt %d): %s",
                attempt,
                exc,
            )
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError(
        f"Telegram deleteWebhook failed before OpenClaw handoff: {last_error}"
    )


def start_openclaw_gateway(binding: dict) -> float:
    """Start real OpenClaw.

    In production the OpenClaw gateway runs as a separate systemd
    unit so it has first-class lifecycle, logs, and crash-restart
    semantics. In dev mode the supervisor runs it as a subprocess
    instead (no systemd in a typical dev container).
    """
    sup = _sup()
    if sup._dev_mode():
        return sup._start_openclaw_gateway_dev(binding)
    started_at = time.time()
    sup._mark_lifecycle("gateway_start_at_unix")
    log.info(
        "starting OpenClaw gateway unit: bot=@%s owner=%s port=%s",
        binding.get("telegram_bot_username"),
        binding.get("telegram_owner_user_id"),
        sup.OPENCLAW_GATEWAY_PORT,
    )
    sup._run_systemctl("reset-failed", sup.GATEWAY_SYSTEMD_UNIT, check=False)
    sup._run_systemctl("restart", sup.GATEWAY_SYSTEMD_UNIT)
    return started_at


def wait_for_openclaw_start(started_at: float) -> None:
    """Wait until OpenClaw reports the gateway is healthy."""
    sup = _sup()
    deadline = time.time() + sup.OPENCLAW_GATEWAY_START_TIMEOUT_SECONDS
    last_checkpoint = time.time()
    last_probe = ""

    def _checkpoint_if_due() -> None:
        nonlocal last_checkpoint
        now = time.time()
        if now - last_checkpoint >= sup.OPENCLAW_GATEWAY_WAIT_CHECKPOINT_SECONDS:
            sup.checkpoint_supervisor_progress(
                "phase-c-openclaw-wait",
                inspect_gateway=True,
            )
            last_checkpoint = now

    while time.time() < deadline:
        if not sup.is_openclaw_gateway_active():
            ok, detail = sup.probe_openclaw_gateway_health(started_at)
            if ok:
                log.info("OpenClaw gateway readiness probe succeeded")
                sup._mark_lifecycle("gateway_ready_at_unix")
                return
            if sup._is_openclaw_gateway_startup_failure(detail):
                raise RuntimeError("openclaw gateway failed to start: " + detail)
            inactive_detail = (
                "openclaw subprocess exited"
                if sup._dev_mode()
                else "systemd unit is not active"
            )
            last_probe = detail if detail else inactive_detail
            _checkpoint_if_due()
            time.sleep(1)
            continue
        ok, detail = sup.probe_openclaw_gateway_health(started_at)
        if ok:
            log.info("OpenClaw gateway readiness probe succeeded")
            sup._mark_lifecycle("gateway_ready_at_unix")
            return
        if sup._is_openclaw_gateway_startup_failure(detail):
            raise RuntimeError("openclaw gateway failed to start: " + detail)
        last_probe = detail
        _checkpoint_if_due()
        time.sleep(1)
    raise RuntimeError(
        "openclaw gateway did not become healthy within "
        f"{sup.OPENCLAW_GATEWAY_START_TIMEOUT_SECONDS}s"
        + (f": {last_probe}" if last_probe else "")
    )


# ── new: the typed operation transaction (ships dark in M1) ──────────


@dataclass
class GatewayRestartResult:
    """Terminal record of one gateway-restart operation transaction."""

    outcome: str  # succeeded | failed | timed_out
    detail: str  # pre-redacted
    phase_reached: str  # the furthest GATEWAY_RESTART_PHASES entry
    started_at_unix: int
    finished_at_unix: int
    # Command-class-owned marker: the gateway start timestamp the
    # readiness probe keys on. The lock's status record stores it as
    # ``operation_marker_unix`` so a runner-lost readiness wait can be
    # reconciled by a later holder.
    operation_marker_unix: int | None = None
    runner_lost: bool = field(default=False)

    def as_record(self) -> dict:
        return {
            "name": "gateway restart",
            "class": "operate",
            "outcome": self.outcome,
            "detail": self.detail,
            "phase_reached": self.phase_reached,
            "started_at_unix": self.started_at_unix,
            "finished_at_unix": self.finished_at_unix,
            "operation_marker_unix": self.operation_marker_unix,
            "runner_lost": self.runner_lost,
        }


def run_gateway_restart_transaction(binding: dict) -> GatewayRestartResult:
    """Run the restart sequence to a terminal outcome (no lock yet).

    The next milestone wraps this in the global command lock, records
    ``operation_phase`` transitions into the lock's status record, and
    appends the result to the command spool. The sequence and terminal
    classification ship (and are tested) now so the daemon's recovery
    and the future CLI command stay one unit.
    """
    sup = _sup()
    started_at_unix = int(time.time())
    phase = "webhook_delete"

    def _terminal(outcome: str, detail: str, marker: int | None) -> GatewayRestartResult:
        return GatewayRestartResult(
            outcome=outcome,
            detail=sup._sanitize_runtime_state_text(detail, limit=512),
            phase_reached=phase,
            started_at_unix=started_at_unix,
            finished_at_unix=int(time.time()),
            operation_marker_unix=marker,
        )

    try:
        sup.delete_telegram_webhook(binding)
    except Exception as exc:
        return _terminal("failed", f"webhook delete failed: {exc}", None)

    phase = "child_running"
    try:
        started_at = sup.start_openclaw_gateway(binding)
    except Exception as exc:
        return _terminal("failed", f"gateway restart failed: {exc}", None)

    marker = int(started_at)
    phase = "readiness_wait"
    try:
        sup.wait_for_openclaw_start(started_at)
    except RuntimeError as exc:
        detail = str(exc)
        phase = "terminal"
        if "did not become healthy within" in detail:
            return _terminal("timed_out", detail, marker)
        return _terminal("failed", detail, marker)
    except Exception as exc:
        phase = "terminal"
        return _terminal("failed", f"readiness wait failed: {exc}", marker)

    phase = "terminal"
    return _terminal("succeeded", "openclaw gateway restarted and ready", marker)
