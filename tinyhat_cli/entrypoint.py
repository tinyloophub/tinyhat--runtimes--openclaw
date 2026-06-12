"""The root-only ``tinyhat`` entrypoint (diagnose surface).

Installed as ``/usr/local/bin/tinyhat`` by ``bootstrap.sh``. Privilege
model: **euid 0 only** — the control-plane state this reads is
root-owned, and v0.12.0 deliberately ships no non-root bridge. A
non-root caller gets a typed JSON error on stderr and exit code 77
(EX_NOPERM). Diagnose commands never mutate runtime state, never take
locks, and never post to the platform.

Every output — human and ``--json`` — carries the freshness fields
(``state_as_of``, ``state_age_seconds``, ``supervisor_alive``) and is
passed through the runtime-state sanitizer as a final egress guard.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from tinyhat_cli.registry import CommandContext, build_registry
from tinyhat_cli.units import snapshot as snapshot_unit
from tinyhat_cli.units.redaction import sanitize_json_tree

ENVELOPE_SCHEMA = "tinyhat_cli_v1"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
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

    return parser


def _registry_key(namespace: argparse.Namespace) -> str:
    if namespace.command == "manifest":
        return f"manifest {namespace.subcommand}"
    return str(namespace.command)


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
    except Exception as exc:  # noqa: BLE001 - a diagnose CLI must fail typed
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
