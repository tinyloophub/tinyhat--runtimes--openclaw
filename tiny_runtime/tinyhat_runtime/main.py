"""Command line entrypoints for tiny_runtime."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from . import attestation, bundle, identity, launcher, openclaw_adapter, paths


def _components(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "runtime": {
            "repo": "https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git",
            "ref": args.runtime_ref,
        },
        "openclaw": {"package": "openclaw", "ref": args.openclaw_ref},
        "tinyhat_openclaw_plugin": {
            "repo": "https://github.com/tinyhat-ai/tinyhat.git",
            "ref": args.plugin_ref,
        },
    }


def _cmd_bundle_write(args: argparse.Namespace) -> int:
    manifest = bundle.write_manifest(Path(args.bundle_dir), components=_components(args))
    print(json.dumps({"bundle_id": manifest["bundle_id"]}, sort_keys=True))
    return 0


def _cmd_bundle_verify(args: argparse.Namespace) -> int:
    bundle.verify_manifest(Path(args.bundle_dir))
    print(json.dumps({"ok": True, "bundle_id": bundle.load_manifest(Path(args.bundle_dir))["bundle_id"]}, sort_keys=True))
    return 0


def _cmd_bundle_id(args: argparse.Namespace) -> int:
    manifest = bundle.load_manifest(Path(args.bundle_dir))
    bundle.verify_manifest(Path(args.bundle_dir), manifest)
    print(manifest["bundle_id"])
    return 0


def _cmd_launcher_activate(args: argparse.Namespace) -> int:
    health_command = shlex.split(args.health_command) if args.health_command else None
    result = launcher.activate_bundle(
        Path(args.bundle_dir),
        current_link=Path(args.current_link),
        health_command=health_command,
        timeout=args.timeout,
    )
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0 if result.activated else 1


def _cmd_attest(args: argparse.Namespace) -> int:
    manifest = bundle.load_manifest(Path(args.bundle_dir))
    bundle.verify_manifest(Path(args.bundle_dir), manifest)
    identity_doc = identity.load_identity_document(Path(args.identity_file)) if args.identity_file else {}
    adapter_doc = (
        {"state": "skipped", "reason": "requested"}
        if args.skip_openclaw
        else openclaw_adapter.adapter_attestation()
    )
    payload = attestation.build_attestation(
        bundle_manifest=manifest,
        identity_doc=identity_doc,
        openclaw=adapter_doc,
    )
    if args.output:
        attestation.write_attestation(Path(args.output), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_gateway_health(_args: argparse.Namespace) -> int:
    payload = openclaw_adapter.gateway_health()
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("state") == "healthy" else 1


def _cmd_gateway_run(_args: argparse.Namespace) -> int:
    return openclaw_adapter.gateway_run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tinyhat-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bundle_parser = subparsers.add_parser("bundle")
    bundle_sub = bundle_parser.add_subparsers(dest="bundle_command", required=True)
    write = bundle_sub.add_parser("write")
    write.add_argument("--bundle-dir", required=True)
    write.add_argument("--runtime-ref", required=True)
    write.add_argument("--openclaw-ref", required=True)
    write.add_argument("--plugin-ref", required=True)
    write.set_defaults(func=_cmd_bundle_write)
    verify = bundle_sub.add_parser("verify")
    verify.add_argument("--bundle-dir", required=True)
    verify.set_defaults(func=_cmd_bundle_verify)
    bundle_id = bundle_sub.add_parser("id")
    bundle_id.add_argument("--bundle-dir", required=True)
    bundle_id.set_defaults(func=_cmd_bundle_id)

    launcher_parser = subparsers.add_parser("launcher")
    launcher_sub = launcher_parser.add_subparsers(dest="launcher_command", required=True)
    activate = launcher_sub.add_parser("activate")
    activate.add_argument("bundle_dir")
    activate.add_argument("--current-link", default=str(paths.CURRENT_LINK))
    activate.add_argument("--health-command")
    activate.add_argument("--timeout", type=int, default=30)
    activate.set_defaults(func=_cmd_launcher_activate)

    attest = subparsers.add_parser("attest")
    attest.add_argument("--bundle-dir", default=str(paths.CURRENT_LINK))
    attest.add_argument("--identity-file")
    attest.add_argument("--output")
    attest.add_argument("--skip-openclaw", action="store_true")
    attest.set_defaults(func=_cmd_attest)

    gateway = subparsers.add_parser("gateway")
    gateway_sub = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_health = gateway_sub.add_parser("health")
    gateway_health.set_defaults(func=_cmd_gateway_health)
    gateway_run = gateway_sub.add_parser("run")
    gateway_run.set_defaults(func=_cmd_gateway_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - command-line boundary
        print(f"tinyhat-runtime: {exc}", file=sys.stderr)
        return 2


def launcher_main() -> int:
    return main(["launcher", *sys.argv[1:]])


def attest_main() -> int:
    return main(["attest", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
