#!/usr/bin/env python3
"""Driver for the seven global-command-lock proof cases.

Each subcommand exercises the REAL ``tinyhat_cli.units.command_lock``
(+ the real ``gateway_restart`` transaction for case 7) against the
real control-plane directories — nothing here stubs the lock itself.
``lock_proof.sh`` orchestrates these subcommands into the seven
assertions; this file only does one lock action per invocation and
prints machine-greppable lines:

    ACQUIRED generation=<n>
    STALE-TAKEOVER previous_generation=<n> previous_phase=<p>
    BUSY <typed reason>
    RESULT outcome=<o> key=<k> runner_lost=<bool>
    REPLAYED key=<k> outcome=<o>
    CHILD pid=<p> pgid=<p>

Run as root on a disposable proof VM (PYTHONPATH=/opt/tinyhat-runtime).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("TINYHAT_RUNTIME_DIR", "/opt/tinyhat-runtime"))

from tinyhat_cli.units import command_lock, command_spool  # noqa: E402
from tinyhat_cli.units import gateway_restart  # noqa: E402


def _print(line: str) -> None:
    print(line, flush=True)


def _announce(txn: command_lock.CommandLockTransaction) -> None:
    _print(f"ACQUIRED generation={txn.generation}")
    if txn.stale_previous is not None:
        _print(
            "STALE-TAKEOVER "
            f"previous_generation={txn.stale_previous.get('generation')} "
            f"previous_phase={txn.stale_previous.get('operation_phase')} "
            f"previous_key={txn.stale_previous.get('idempotency_key')}"
        )


def cmd_probe(_args) -> int:
    """LOCK_NB acquire attempt: FREE, or the §-typed busy answer."""
    try:
        txn = command_lock.acquire(
            "lock-proof probe", holder="cli", idempotency_key="probe"
        )
    except command_lock.CommandLockBusy as busy:
        _print(f"BUSY {busy.reason}")
        return 75
    # A probe must not look like a real mutation in the status record.
    txn.finish("succeeded", "probe", {"name": "lock-proof probe"})
    txn.release()
    _print("FREE")
    return 0


def cmd_hold(args) -> int:
    """Acquire and hold; optionally leave a detached mutation child.

    ``--child-sleep N`` spawns the child exactly the way a mid-command
    runner holds it (mutex fd inherited, own process group) but does
    NOT wait — so SIGKILLing this runner leaves the child alive and
    the flock held by the inherited fd (case 6).
    """
    txn = command_lock.acquire(
        args.command,
        holder=args.holder,
        idempotency_key=args.key or os.urandom(8).hex(),
        timeout_seconds=args.timeout,
    )
    _announce(txn)
    child = None
    if args.child_sleep > 0:
        child = subprocess.Popen(
            ["sleep", str(args.child_sleep)],
            pass_fds=(txn.fd,),
            start_new_session=True,
        )
        txn.child_pgid = child.pid
        txn._write_status()
        _print(f"CHILD pid={child.pid} pgid={child.pid}")
    txn.set_phase("child_running" if child else "readiness_wait")
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        time.sleep(0.2)
    if child is not None:
        child.wait()
    txn.finish(
        "succeeded",
        "hold completed",
        {
            "name": args.command,
            "class": "operate",
            "outcome": "succeeded",
            "idempotency_key": txn.idempotency_key,
            "finished_at_unix": int(time.time()),
        },
    )
    txn.release()
    _print("RESULT outcome=succeeded key=" + txn.idempotency_key)
    return 0


def cmd_acquire_blocking(args) -> int:
    """Defer-don't-race: poll until the holder releases/dies, then run."""
    waited_from = time.time()
    txn = command_lock.acquire(
        args.command,
        holder=args.holder,
        idempotency_key=os.urandom(8).hex(),
        timeout_seconds=args.timeout,
        wait_seconds=args.wait,
        on_wait=lambda: _print("WAITING"),
    )
    _print(f"WAITED seconds={time.time() - waited_from:.1f}")
    _announce(txn)
    txn.finish(
        "succeeded",
        "acquired after deferral",
        {
            "name": args.command,
            "class": "operate",
            "outcome": "succeeded",
            "idempotency_key": txn.idempotency_key,
            "finished_at_unix": int(time.time()),
        },
    )
    txn.release()
    _print("RESULT outcome=succeeded key=" + txn.idempotency_key)
    return 0


def cmd_run_op(args) -> int:
    """A full stub operation: replay honor, child, deadline, terminal."""
    key = args.key or os.urandom(8).hex()
    stored = command_lock.load_result(key)
    if stored is not None:
        _print(f"REPLAYED key={key} outcome={stored.get('outcome')}")
        return 0
    txn = command_lock.acquire(
        args.command, holder=args.holder, idempotency_key=key,
        timeout_seconds=args.timeout,
    )
    _announce(txn)
    if args.exec_marker:
        with open(args.exec_marker, "a", encoding="utf-8") as fh:
            fh.write(f"executed key={key} at={int(time.time())}\n")
    outcome, detail = "succeeded", "stub operation completed"
    txn.set_phase("child_running")
    if args.child_sleep > 0:
        try:
            txn.run_subprocess(["sleep", str(args.child_sleep)])
            _print("CHILD-DONE")
        except subprocess.TimeoutExpired:
            outcome, detail = "timed_out", (
                "stub child exceeded the declared deadline; its process "
                "group was killed"
            )
    record = {
        "name": args.command,
        "class": "operate",
        "outcome": outcome,
        "summary": detail,
        "idempotency_key": key,
        "started_at_unix": txn.acquired_at_unix,
        "finished_at_unix": int(time.time()),
    }
    command_spool.append_result(record)
    txn.finish(outcome, detail, record)
    txn.release()
    _print(f"RESULT outcome={outcome} key={key}")
    return 0 if outcome == "succeeded" else 3


def cmd_gateway_restart(args) -> int:
    """The REAL locked gateway-restart transaction (case 7)."""
    key = args.key or gateway_restart.mint_idempotency_key()
    result = gateway_restart.run_locked_gateway_restart(
        {},
        holder=args.holder,
        idempotency_key=key,
        delete_webhook=False,
        wait_for_lock_seconds=args.wait,
    )
    _print(
        f"RESULT outcome={result.outcome} key={key} "
        f"runner_lost={result.runner_lost} marker={result.operation_marker_unix}"
    )
    return 0 if result.outcome == "succeeded" else 3


def cmd_dump(_args) -> int:
    """Lock status + spool + results, for the shell's assertions."""
    _print("LOCK " + json.dumps(command_lock.read_lock_status() or {}))
    for path, record in command_spool.read_results():
        _print(f"SPOOL {os.path.basename(path)} " + json.dumps(record))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p) -> None:
        p.add_argument("--holder", default="cli", choices=("cli", "daemon"))
        p.add_argument("--command", default="lock-proof op")
        p.add_argument("--timeout", type=int, default=30)
        p.add_argument("--key", default=None)

    p = sub.add_parser("probe")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("hold")
    _common(p)
    p.add_argument("--seconds", type=float, default=5)
    p.add_argument("--child-sleep", type=float, default=0)
    p.set_defaults(func=cmd_hold)
    p = sub.add_parser("acquire-blocking")
    _common(p)
    p.add_argument("--wait", type=float, default=60)
    p.set_defaults(func=cmd_acquire_blocking)
    p = sub.add_parser("run-op")
    _common(p)
    p.add_argument("--child-sleep", type=float, default=0)
    p.add_argument("--exec-marker", default=None)
    p.set_defaults(func=cmd_run_op)
    p = sub.add_parser("gateway-restart")
    _common(p)
    p.add_argument("--wait", type=float, default=0)
    p.set_defaults(func=cmd_gateway_restart)
    p = sub.add_parser("dump")
    p.set_defaults(func=cmd_dump)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
