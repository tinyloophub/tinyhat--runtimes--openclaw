"""The root-only ``tinyhat`` entrypoint (diagnose + operate surface).

Installed as ``/usr/local/bin/tinyhat`` by ``bootstrap.sh``. Privilege
model: **euid 0 only** — the control-plane state this reads is
root-owned, and v0.12.0 deliberately ships no non-root bridge. A
non-root caller gets a typed JSON error on stderr and exit code 77
(EX_NOPERM). Diagnose commands never mutate runtime state, never take
locks, and never post to the platform. Operate commands run under the
global command lock; a busy lock answers typed with exit code 75
(EX_TEMPFAIL) and mutates nothing.

Every output — human and ``--json`` — carries the freshness fields
(``state_as_of``, ``state_age_seconds``, ``supervisor_alive``) and is
passed through the runtime-state sanitizer as a final egress guard.
Diagnose failures (only failures — successes would flood the ring)
are appended to the command-result spool so the daemon can mirror
them; operate results are always appended by the operate runner.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from tinyhat_cli.registry import CommandContext, build_registry
from tinyhat_cli.units import command_lock, command_spool
from tinyhat_cli.units import snapshot as snapshot_unit
from tinyhat_cli.units.gateway_restart import GatewayRestartUnsupportedEnvironment
from tinyhat_cli.units.redaction import sanitize_json_tree

ENVELOPE_SCHEMA = "tinyhat_cli_v1"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 69  # EX_UNAVAILABLE
EXIT_BUSY = 75  # EX_TEMPFAIL
EXIT_NOT_ROOT = 77  # EX_NOPERM


def _build_parser(registry) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tinyhat",
        description=(
            "Root-only on-box diagnose CLI for a Tinyhat Computer. "
            "Read-only: the same redacted answers the platform admin "
            "surface shows, computed from this box's own state."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _add_json_flag(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--json",
            action="store_true",
            help="emit the schema-validated JSON envelope instead of text",
        )

    for name in ("status", "health", "whoami"):
        sub = subparsers.add_parser(name, help=registry[name].summary)
        _add_json_flag(sub)

    manifest = subparsers.add_parser(
        "manifest", help="running versions vs the box's own desired-state record"
    )
    manifest_sub = manifest.add_subparsers(dest="subcommand", required=True)
    for sub_name in ("show", "drift"):
        sub = manifest_sub.add_parser(
            sub_name, help=registry[f"manifest {sub_name}"].summary
        )
        _add_json_flag(sub)

    gateway = subparsers.add_parser(
        "gateway", help="operate the OpenClaw gateway unit (lock-held)"
    )
    gateway_sub = gateway.add_subparsers(dest="subcommand", required=True)
    restart = gateway_sub.add_parser(
        "restart", help=registry["gateway restart"].summary
    )
    _add_json_flag(restart)
    restart.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help=(
            "replay an earlier result instead of restarting again; "
            "every plain invocation mints a fresh key"
        ),
    )

    return parser


def _registry_key(namespace: argparse.Namespace) -> str:
    if namespace.command in ("manifest", "gateway"):
        return f"{namespace.command} {namespace.subcommand}"
    return str(namespace.command)


def _spool_diagnose_failure(command: str, error_type: str, detail: str) -> None:
    """Best-effort failure record (§ ring policy: diagnose failures only)."""
    now = int(time.time())
    try:
        command_spool.append_result(
            {
                "name": command,
                "class": "diagnose",
                "outcome": "failed",
                "started_at_unix": now,
                "finished_at_unix": now,
                "idempotency_key": f"diagnose-{now}",
                "summary": f"{error_type}: {detail}",
            }
        )
    except Exception:  # noqa: BLE001 - never let transport mask the answer
        pass


def _emit_typed_error(error_type: str, detail: str, exit_code: int) -> int:
    payload = {
        "schema": ENVELOPE_SCHEMA,
        "error": {"type": error_type, "detail": detail},
    }
    print(json.dumps(sanitize_json_tree(payload)), file=sys.stderr)
    return exit_code


def _generated_at(now: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def main(argv: list[str] | None = None) -> int:
    # The supervisor module logs INFO chatter (base-URL resolution and
    # friends) that belongs in the daemon's journal, not interleaved
    # with a diagnose answer. Warnings still surface.
    logging.getLogger("tinyhat-supervisor").setLevel(logging.WARNING)

    registry = build_registry()
    parser = _build_parser(registry)
    namespace = parser.parse_args(argv)

    if os.geteuid() != 0:
        return _emit_typed_error(
            "not_root",
            "tinyhat requires a root shell (euid 0): diagnose output is "
            "computed from root-owned control-plane state and v0.12.0 "
            "ships no non-root access path",
            EXIT_NOT_ROOT,
        )

    key = _registry_key(namespace)
    spec = registry[key]

    try:
        if spec.command_class == "operate":
            # The operate runner snapshots AFTER the mutation so the
            # freshness fields describe the state the command left.
            ctx = CommandContext(args=namespace, snapshot={}, state={})
            data = spec.handler(ctx)
            meta, state = snapshot_unit.control_plane_snapshot()
        else:
            meta, state = snapshot_unit.control_plane_snapshot()
            ctx = CommandContext(args=namespace, snapshot=meta, state=state)
            data = spec.handler(ctx)
        envelope = {
            "schema": ENVELOPE_SCHEMA,
            "command": key,
            "command_class": spec.command_class,
            "generated_at": _generated_at(int(time.time())),
            "state_as_of": meta.get("state_as_of"),
            "state_age_seconds": meta.get("state_age_seconds"),
            "supervisor_alive": meta.get("supervisor_alive"),
            "data": data,
        }
        envelope = sanitize_json_tree(envelope)
    except command_lock.CommandLockBusy as exc:
        return _emit_typed_error("busy", exc.reason, EXIT_BUSY)
    except GatewayRestartUnsupportedEnvironment as exc:
        return _emit_typed_error("unsupported_environment", str(exc), EXIT_UNSUPPORTED)
    except Exception as exc:  # noqa: BLE001 - the CLI must fail typed
        if spec.command_class == "diagnose":
            _spool_diagnose_failure(key, type(exc).__name__, str(exc))
        return _emit_typed_error("internal_error", f"{type(exc).__name__}: {exc}", EXIT_ERROR)

    if getattr(namespace, "json", False):
        print(json.dumps(envelope, indent=2, sort_keys=True))
        return EXIT_OK

    print(f"tinyhat {key} — generated at {envelope['generated_at']}")
    for line in spec.render(envelope["data"]):
        print(line)
    print()
    for line in snapshot_unit.freshness_lines(meta):
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
