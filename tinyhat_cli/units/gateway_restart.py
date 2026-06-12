"""Gateway-restart unit — the semantic restart sequence (M2 skeleton).

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``):
the production gateway restart is a semantic operation, not just a
subprocess — webhook delete so OpenClaw can long-poll, ``systemctl
reset-failed`` (mandatory: rapid restarts without it trip systemd's
start-rate limit) + ``restart``, then a bounded readiness wait that
follows the gateway's own log output to a terminal verdict.

:func:`run_gateway_restart_transaction` is the typed operation
transaction; :func:`run_locked_gateway_restart` wraps it in the global
command lock (``units/command_lock``) with the §-contract phases
recorded in ``lock.json``, the result spooled for the daemon's
``commands`` ring, and runner-lost predecessors reconciled to a
terminal outcome before any second mutation. The CLI (``tinyhat
gateway restart``) and the daemon's own recovery/component-update
restarts all converge on this one unit.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from tinyhat_cli._facade import supervisor_module as _sup
from tinyhat_cli.units import command_lock, command_spool

UNIT_CATEGORY = "supervision"

log = logging.getLogger("tinyhat-supervisor")

GATEWAY_RESTART_COMMAND = "gateway restart"
GATEWAY_RESTART_TIMEOUT_SECONDS = 120
# SIGTERM-grace headroom on top of the holder's own deadline before a
# waiting daemon gives up deferring and reports busy upward.
LOCK_WAIT_GRACE_SECONDS = 30

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
    # Spool transport warning (never part of the verdict; CLI-visible).
    spool_warning: str | None = field(default=None, compare=False)

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


def run_gateway_restart_transaction(
    binding: dict,
    *,
    delete_webhook: bool = True,
    on_phase: Callable[..., None] | None = None,
) -> GatewayRestartResult:
    """Run the restart sequence to a terminal outcome (lock-agnostic).

    ``on_phase(phase, marker=...)`` lets a lock-held runner mirror the
    phase transitions into ``lock.json``; ``delete_webhook=False`` is
    the component-update path (a fresh package pickup does not own the
    Telegram webhook hand-off). ``ManualRecoveryRequired`` propagates —
    it is a recovery-policy verdict, never a transaction outcome.
    """
    sup = _sup()
    started_at_unix = int(time.time())
    phase = "webhook_delete" if delete_webhook else "child_running"

    def _notify(next_phase: str, marker: int | None = None) -> None:
        if on_phase is not None:
            try:
                on_phase(next_phase, marker=marker)
            except Exception:  # noqa: BLE001 - status mirroring is best-effort
                log.warning("gateway restart phase callback failed", exc_info=True)

    def _terminal(outcome: str, detail: str, marker: int | None) -> GatewayRestartResult:
        _notify("terminal", marker)
        return GatewayRestartResult(
            outcome=outcome,
            detail=sup._sanitize_runtime_state_text(detail, limit=512),
            phase_reached=phase,
            started_at_unix=started_at_unix,
            finished_at_unix=int(time.time()),
            operation_marker_unix=marker,
        )

    if delete_webhook:
        try:
            sup.delete_telegram_webhook(binding)
        except sup.ManualRecoveryRequired:
            raise
        except Exception as exc:
            return _terminal("failed", f"webhook delete failed: {exc}", None)

    phase = "child_running"
    _notify("child_running")
    try:
        started_at = sup.start_openclaw_gateway(binding)
    except sup.ManualRecoveryRequired:
        raise
    except Exception as exc:
        return _terminal("failed", f"gateway restart failed: {exc}", None)

    marker = int(started_at)
    phase = "readiness_wait"
    _notify("readiness_wait", marker)
    try:
        sup.wait_for_openclaw_start(started_at)
    except sup.ManualRecoveryRequired:
        raise
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


# ── the lock-held operation transaction ──────────────────────────────


class GatewayRestartUnsupportedEnvironment(RuntimeError):
    """No systemd here — the dev container's gateway is supervisor-owned."""


def mint_idempotency_key() -> str:
    return uuid.uuid4().hex


def _systemd_gateway_observables() -> dict[str, str]:
    """The reconcile handles: ActiveState/SubState/Result + in-flight Job."""
    sup = _sup()
    result = sup._run_systemctl(
        "show",
        sup.GATEWAY_SYSTEMD_UNIT,
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "Result",
        "-p",
        "Job",
        check=False,
    )
    observables: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key.strip():
            observables[key.strip()] = value.strip()
    return observables


def reconcile_runner_lost(previous: dict[str, Any]) -> GatewayRestartResult:
    """Normalize a runner-lost restart to a terminal outcome.

    Called by the next lock holder (CLI or daemon) when the mutex fd
    was free but the previous ``lock.json`` is non-terminal — the
    kernel says the old mutating tree is gone mid-operation. Both the
    ``readiness_wait`` and ``child_running`` shapes reconcile the same
    way: keep probing gateway readiness against the previous
    ``operation_marker_unix`` until the previous deadline, classify a
    failed unit as ``failed``, and call deadline expiry ``timed_out``.
    Never starts a second restart.
    """
    sup = _sup()
    now = int(time.time())
    try:
        marker = int(previous.get("operation_marker_unix"))
    except (TypeError, ValueError):
        marker = None
    try:
        deadline = int(previous.get("operation_deadline_unix"))
    except (TypeError, ValueError):
        deadline = now
    try:
        previous_started = int(previous.get("operation_started_at_unix"))
    except (TypeError, ValueError):
        previous_started = now
    probe_anchor = float(marker if marker is not None else previous_started)

    outcome: str | None = None
    detail = ""
    while True:
        active = False
        try:
            active = sup.is_openclaw_gateway_active()
        except Exception as exc:  # noqa: BLE001 - reconcile must terminate
            detail = f"gateway activity probe failed: {exc}"
        if active:
            try:
                ok, probe_detail = sup.probe_openclaw_gateway_health(probe_anchor)
            except Exception as exc:  # noqa: BLE001
                ok, probe_detail = False, f"readiness probe failed: {exc}"
            if ok:
                outcome = "succeeded"
                detail = "gateway became ready after its runner was lost"
                break
            detail = probe_detail
        else:
            observables = _systemd_gateway_observables()
            if observables.get("ActiveState") == "failed":
                outcome = "failed"
                detail = (
                    "gateway unit failed after its runner was lost "
                    f"(Result={observables.get('Result') or 'unknown'})"
                )
                break
            if not observables.get("Job"):
                detail = "gateway unit inactive with no pending systemd job"
        if time.time() >= deadline:
            outcome = "timed_out"
            detail = (
                "runner-lost restart did not reach readiness by its "
                f"deadline ({detail})"
                if detail
                else "runner-lost restart did not reach readiness by its deadline"
            )
            break
        time.sleep(1)

    return GatewayRestartResult(
        outcome=outcome,
        detail=sup._sanitize_runtime_state_text(detail, limit=512),
        phase_reached=str(previous.get("operation_phase") or "unknown"),
        started_at_unix=previous_started,
        finished_at_unix=int(time.time()),
        operation_marker_unix=marker,
        runner_lost=True,
    )


def _result_ring_record(
    result: GatewayRestartResult,
    *,
    idempotency_key: str,
    holder: str,
    generation: int | None = None,
    stale_takeover: bool = False,
) -> dict[str, Any]:
    record = result.as_record()
    record["summary"] = record.pop("detail", "") or ""
    record["idempotency_key"] = idempotency_key
    record["holder"] = holder
    if generation is not None:
        record["generation"] = generation
    if stale_takeover:
        record["stale_takeover"] = True
    if not record.get("runner_lost"):
        record.pop("runner_lost", None)
    return record


def _spool_best_effort(record: dict[str, Any]) -> str | None:
    """Spool transport failure is a warning, never a verdict change."""
    try:
        command_spool.append_result(record)
    except command_spool.SpoolRedactionError as exc:
        log.warning("%s", exc)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - the spool is transport
        log.warning("command result spool append failed: %s", exc)
        return f"spool append failed: {exc}"
    return None


def run_locked_gateway_restart(
    binding: dict | None = None,
    *,
    holder: str,
    idempotency_key: str | None = None,
    delete_webhook: bool = True,
    wait_for_lock_seconds: float = 0.0,
    on_lock_wait: Callable[[], None] | None = None,
) -> GatewayRestartResult:
    """The §-contract operation transaction under the global lock.

    Defer-don't-race: ``wait_for_lock_seconds > 0`` (the daemon) polls
    the mutex until free; ``0`` (the CLI) raises the typed busy answer
    immediately. A non-root process without an explicit lock-dir
    override mutates nothing real (systemd is root-only), so it runs
    the bare transaction exactly as before the lock existed.
    """
    sup = _sup()
    binding = binding or {}
    key = idempotency_key or mint_idempotency_key()

    if not command_lock.lock_available_to_this_process():
        return run_gateway_restart_transaction(
            binding, delete_webhook=delete_webhook
        )

    txn = command_lock.acquire(
        GATEWAY_RESTART_COMMAND,
        holder=holder,
        idempotency_key=key,
        timeout_seconds=GATEWAY_RESTART_TIMEOUT_SECONDS,
        wait_seconds=wait_for_lock_seconds,
        on_wait=on_lock_wait,
    )
    with txn:
        if txn.stale_previous is not None:
            previous = txn.stale_previous
            previous_key = str(previous.get("idempotency_key") or "unknown")
            log.warning(
                "command lock stale takeover: reconciling runner-lost "
                "'%s' (generation %s, phase %s) before '%s'",
                previous.get("command"),
                previous.get("generation"),
                previous.get("operation_phase"),
                GATEWAY_RESTART_COMMAND,
            )
            if previous.get("command") == GATEWAY_RESTART_COMMAND:
                reconciled = sup.reconcile_runner_lost(previous)
            else:
                reconciled = GatewayRestartResult(
                    outcome="failed",
                    detail=(
                        "runner lost mid-operation; no reconcile path for "
                        f"command {previous.get('command')!r}"
                    ),
                    phase_reached=str(previous.get("operation_phase") or "unknown"),
                    started_at_unix=int(time.time()),
                    finished_at_unix=int(time.time()),
                    runner_lost=True,
                )
                reconciled.detail = sup._sanitize_runtime_state_text(
                    reconciled.detail, limit=512
                )
            previous_record = _result_ring_record(
                reconciled,
                idempotency_key=previous_key,
                holder=str(previous.get("holder") or "unknown"),
                stale_takeover=True,
            )
            previous_record["name"] = str(
                previous.get("command") or GATEWAY_RESTART_COMMAND
            )
            _spool_best_effort(previous_record)
            try:
                command_lock.store_result(previous_key, previous_record)
            except Exception as exc:  # noqa: BLE001 - store is best-effort
                log.warning("stale-takeover result store failed: %s", exc)

        def _on_phase(phase: str, marker: int | None = None) -> None:
            txn.set_phase(phase, marker_unix=marker)

        result = run_gateway_restart_transaction(
            binding,
            delete_webhook=delete_webhook,
            on_phase=_on_phase,
        )
        if result.outcome == "failed" and txn.timed_out_children:
            # The runner's own deadline killed the mutation child group;
            # that is the timeout verdict, not a unit failure.
            result.outcome = "timed_out"
        record = _result_ring_record(
            result,
            idempotency_key=key,
            holder=holder,
            generation=txn.generation,
        )
        spool_warning = _spool_best_effort(record)
        if spool_warning:
            result.spool_warning = spool_warning
        txn.finish(result.outcome, result.detail, record)
    return result


# ── the CLI command (operate class) ──────────────────────────────────


def run(ctx) -> dict[str, Any]:
    sup = _sup()
    if sup._dev_mode():
        raise GatewayRestartUnsupportedEnvironment(
            "tinyhat gateway restart needs the systemd runtime of a real "
            "Computer; this dev container's gateway is owned by the "
            "supervisor process"
        )
    requested_key = getattr(ctx.args, "idempotency_key", None)
    if requested_key:
        stored = command_lock.load_result(requested_key)
        if stored is not None:
            replayed = dict(stored)
            replayed["replayed"] = True
            return replayed

    key = requested_key or mint_idempotency_key()
    binding = None
    try:
        from tinyhat_cli.adapters import openclaw as openclaw_adapter

        binding = openclaw_adapter.configured_telegram_binding()
    except Exception as exc:  # noqa: BLE001 - unbound boxes restart fine
        log.warning("could not read configured telegram binding: %s", exc)
    binding = binding or {}
    delete_webhook = bool(str(binding.get("telegram_bot_token") or "").strip())

    result = sup.run_locked_gateway_restart(
        binding,
        holder="cli",
        idempotency_key=key,
        delete_webhook=delete_webhook,
    )
    data = result.as_record()
    data["idempotency_key"] = key
    data["replayed"] = False
    if not delete_webhook:
        data["webhook_delete_skipped"] = "no telegram bot token configured"
    if getattr(result, "spool_warning", None):
        data["spool_warning"] = result.spool_warning
    return data


def render(data: dict[str, Any]) -> list[str]:
    outcome = str(data.get("outcome") or "unknown")
    lines = [f"outcome:   {outcome.upper()}"]
    if data.get("replayed"):
        lines.append(
            "replayed:  yes — stored result for this idempotency key; "
            "the gateway was NOT restarted again"
        )
    detail = data.get("detail") or data.get("summary")
    if detail:
        lines.append(f"detail:    {detail}")
    lines.append(f"phase:     reached {data.get('phase_reached') or '?'}")
    started = data.get("started_at_unix")
    finished = data.get("finished_at_unix")
    if isinstance(started, int) and isinstance(finished, int):
        lines.append(f"duration:  {max(0, finished - started)}s")
    if data.get("runner_lost"):
        lines.append("runner:    LOST mid-operation (reconciled to terminal)")
    lines.append(f"key:       {data.get('idempotency_key') or '?'}")
    if data.get("webhook_delete_skipped"):
        lines.append(f"webhook:   skipped — {data['webhook_delete_skipped']}")
    if data.get("spool_warning"):
        lines.append(f"WARNING:   {data['spool_warning']}")
    return lines
