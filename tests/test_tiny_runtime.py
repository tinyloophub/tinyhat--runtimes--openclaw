"""Proof-gate tests for the greenfield tiny_runtime tree.

Usage:
    python -m unittest tests.test_tiny_runtime -v
"""

from __future__ import annotations

import ast
import contextlib
import io
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Callable
from unittest.mock import Mock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tiny_runtime"))

# Collapse the gateway-health cold-start settle window so health-failure /
# rollback tests don't sleep out the production 15x6s poll.
os.environ.setdefault("TINYHAT_GATEWAY_HEALTH_SETTLE_DELAY", "0")

from tinyhat_runtime import RUNTIME_GENERATION  # noqa: E402
from tinyhat_runtime import (  # noqa: E402
    attestation,
    bundle,
    hot_image,
    launcher,
    main,
    openclaw_adapter,
    platform_client,
    platform_loop,
    private_access,
    subscription_link,
)
from tinyhat_runtime.command_ledger import CommandLedger  # noqa: E402
from tinyhat_runtime.platform_client import (  # noqa: E402
    DEV_RUNTIME_BEARER,
    PlatformClient,
    dev_runtime_identity_token,
)
from tinyhat_runtime.runtime_commands import RuntimeCommandRunner  # noqa: E402


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakePlatformClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.get_payload: dict = {}

    def post_json(self, path: str, body: dict, **_kwargs: object) -> dict:
        self.posts.append((path, body))
        return {}

    def get_json(self, path: str, **_kwargs: object) -> dict:
        self.gets.append(path)
        return dict(self.get_payload)


def _write_minimal_bundle(root: Path, *, marker: str = "") -> dict:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tinyhat-runtime").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    (root / "tinyhat_runtime").mkdir()
    (root / "tinyhat_runtime" / "__init__.py").write_text("", encoding="utf-8")
    if marker:
        (root / "bundle-marker.txt").write_text(marker, encoding="utf-8")
    return bundle.write_manifest(
        root,
        components={
            "runtime": {"repo": "public", "ref": "abc123"},
            "openclaw": {"package": "openclaw", "ref": "openclaw@2026.6.8"},
            "tinyhat_openclaw_plugin": {
                "repo": "public",
                "ref": "676e6d878b58a2da8453573fa4b389fca32bc0a9",
            },
        },
    )


class BundleManifestTests(unittest.TestCase):
    def test_bundle_id_is_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_minimal_bundle(root)
            self.assertEqual(manifest["runtime_generation"], RUNTIME_GENERATION)
            self.assertTrue(manifest["bundle_id"].startswith("sha256:"))
            self.assertTrue(bundle.verify_manifest(root, manifest))

            (root / "bin" / "tinyhat-runtime").write_text(
                "# changed\n", encoding="utf-8"
            )
            with self.assertRaises(bundle.BundleVerificationError):
                bundle.verify_manifest(root, manifest)

    def test_bundle_lock_uses_pinned_public_refs(self) -> None:
        payload = json.loads(
            (_REPO_ROOT / "tiny_runtime" / "bake" / "bundle.lock").read_text(
                encoding="utf-8"
            )
        )
        deps = payload["dependencies"]
        self.assertEqual(deps["openclaw"]["resolved"], "openclaw@2026.6.8")
        plugin_ref = deps["tinyhat_openclaw_plugin"]["ref"]
        self.assertRegex(plugin_ref, r"^[0-9a-f]{40}$")
        self.assertNotIn(plugin_ref, {"main", "latest"})

    def test_dev_dockerfile_reads_component_refs_from_bundle_lock(self) -> None:
        dockerfile = (_REPO_ROOT / "tiny_runtime" / "dev" / "Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertIn("bundle.lock", dockerfile)
        self.assertNotIn("--openclaw-ref openclaw@2026.6.8", dockerfile)
        self.assertNotIn(
            "--plugin-ref 676e6d878b58a2da8453573fa4b389fca32bc0a9",
            dockerfile,
        )

    def test_legacy_dev_dockerfile_uses_locked_openclaw_and_runtime_packages(
        self,
    ) -> None:
        dockerfile = (_REPO_ROOT / "dev" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("tiny_runtime/bake/bundle.lock", dockerfile)
        self.assertIn("OPENCLAW_INSTALL_SPEC", dockerfile)
        self.assertNotIn("npm install -g openclaw@latest", dockerfile)
        self.assertIn("COPY tiny_runtime ./tiny_runtime", dockerfile)
        self.assertIn("COPY tinyhat_cli ./tinyhat_cli", dockerfile)
        self.assertIn("ARG TINYHAT_RUNTIME_IMAGE_MODE=legacy_supervisor", dockerfile)
        self.assertIn("TINYHAT_RUNTIME_IMAGE_MODE=${TINYHAT_RUNTIME_IMAGE_MODE}", dockerfile)
        self.assertIn('rm -f supervisor.py', dockerfile)


class HotImageTests(unittest.TestCase):
    def test_existing_plugin_branch_checkout_resets_to_origin_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "tinyhat"
            (checkout / ".git").mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run(args: list[str], *, cwd: Path | None = None) -> str:
                calls.append(list(args))
                self.assertEqual(cwd, checkout)
                if args[:4] == ["git", "rev-parse", "--verify", "origin/main^{commit}"]:
                    return "b" * 40
                if args == ["git", "rev-parse", "HEAD"]:
                    return "b" * 40
                return ""

            with (
                patch.object(hot_image, "TINYHAT_PLUGIN_REPO_REF", "main"),
                patch.object(hot_image, "_plugin_checkout_dir", return_value=checkout),
                patch.object(hot_image, "_run", side_effect=fake_run),
            ):
                resolved_checkout, resolved_sha = hot_image._checkout_tinyhat_plugin()

            self.assertEqual(resolved_checkout, checkout)
            self.assertEqual(resolved_sha, "b" * 40)
            self.assertIn(["git", "fetch", "--tags", "--prune", "origin"], calls)
            self.assertIn(["git", "checkout", "main"], calls)
            self.assertIn(["git", "reset", "--hard", "origin/main"], calls)

    def test_preinstall_hot_image_plugins_applies_warm_config(self) -> None:
        checkout = Path("/tmp/tinyhat-plugin-checkout")

        with (
            patch.object(
                openclaw_adapter,
                "install_plugin",
                return_value={"state": "ready"},
            ) as install_plugin,
            patch.object(
                openclaw_adapter,
                "inspect_plugin",
                return_value={"state": "ready"},
            ),
            patch.object(
                openclaw_adapter,
                "apply_warm_image_config",
                return_value={"state": "ready"},
            ) as apply_warm,
            patch.object(
                hot_image,
                "_checkout_tinyhat_plugin",
                return_value=(checkout, "a" * 40),
            ),
            patch.object(hot_image, "_write_tinyhat_marker") as write_marker,
        ):
            result = hot_image.preinstall_hot_image_plugins()

        self.assertEqual(install_plugin.call_count, 2)
        apply_warm.assert_called_once_with()
        write_marker.assert_called_once_with(checkout=checkout, resolved_sha="a" * 40)
        self.assertEqual(result["warm_config"], {"state": "ready"})

    def test_preinstall_reowns_plugins_to_root_before_inspect_gate(self) -> None:
        # OpenClaw 2026.6.9+ blocks non-root-owned plugins; the preinstall must
        # re-own the plugin trees to root BEFORE each inspect gate, or the codex
        # gate fails and bricks the box (the #112 incident).
        order: list[str] = []
        with (
            patch.object(
                openclaw_adapter,
                "install_plugin",
                side_effect=lambda *_a, **_k: (
                    order.append("install"),
                    {"state": "ready"},
                )[1],
            ),
            patch.object(
                openclaw_adapter,
                "inspect_plugin",
                side_effect=lambda *_a, **_k: (
                    order.append("inspect"),
                    {"state": "ready"},
                )[1],
            ),
            patch.object(
                openclaw_adapter, "apply_warm_image_config", return_value={"state": "ready"}
            ),
            patch.object(
                hot_image, "_checkout_tinyhat_plugin", return_value=(Path("/tmp/x"), "a" * 40)
            ),
            patch.object(hot_image, "_write_tinyhat_marker"),
            patch.object(
                hot_image,
                "reown_plugin_trees_to_root",
                side_effect=lambda *_a, **_k: order.append("reown") or {},
            ) as reown,
        ):
            hot_image.preinstall_hot_image_plugins()

        # Re-own runs at least once per plugin, and each codex/tinyhat inspect is
        # preceded by a re-own (root ownership established before the gate).
        self.assertGreaterEqual(reown.call_count, 2)
        for i, step in enumerate(order):
            if step == "inspect":
                self.assertIn("reown", order[:i], f"inspect at {i} not preceded by reown")


class PrivateAccessTests(unittest.TestCase):
    def test_enroll_from_env_runs_tailscale_up_and_writes_ready_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "bootstrap-status.json"
            calls: list[list[str]] = []
            auth_paths: list[Path] = []

            def fake_runner(args, **_kwargs):
                argv = list(args)
                calls.append(argv)
                if argv[:2] == ["tailscale", "up"]:
                    auth_arg = next(
                        arg for arg in argv if arg.startswith("--auth-key=file:")
                    )
                    auth_path = Path(auth_arg.removeprefix("--auth-key=file:"))
                    auth_paths.append(auth_path)
                    self.assertEqual(auth_path.parent, status_path.parent / "secrets")
                    self.assertEqual(
                        auth_path.read_text(encoding="utf-8"), "tskey-secret"
                    )
                    self.assertEqual(auth_path.stat().st_mode & 0o777, 0o600)
                return _Completed(returncode=0, stdout="ok\n")

            def fake_which(name: str) -> str | None:
                if name == "tailscale":
                    return "/usr/bin/tailscale"
                if name == "systemctl":
                    return None
                return None

            with (
                patch.dict(
                    os.environ,
                    {
                        "TINYHAT_PRIVATE_ACCESS_PROVIDER": "tailscale",
                        "TINYHAT_TAILSCALE_AUTH_KEY": "tskey-secret",
                        "TINYHAT_TAILSCALE_NODE_NAME": "computer-abc123ef",
                        "TINYHAT_TAILSCALE_TAGS": "tag:tinyhat-computer",
                        "TINYHAT_PRIVATE_ACCESS_STATUS_PATH": str(status_path),
                    },
                    clear=False,
                ),
                patch.object(private_access.shutil, "which", side_effect=fake_which),
            ):
                result = private_access.enroll_from_env(runner=fake_runner)

            self.assertEqual(result["state"], "ready")
            self.assertEqual(json.loads(status_path.read_text())["state"], "ready")
            tailscale_logout = next(
                call for call in calls if call[:2] == ["tailscale", "logout"]
            )
            tailscale_up = next(
                call for call in calls if call[:2] == ["tailscale", "up"]
            )
            self.assertLess(calls.index(tailscale_logout), calls.index(tailscale_up))
            self.assertIn("--hostname=computer-abc123ef", tailscale_up)
            self.assertIn("--ssh", tailscale_up)
            self.assertIn("--advertise-tags=tag:tinyhat-computer", tailscale_up)
            self.assertTrue(
                any(arg.startswith("--auth-key=file:") for arg in tailscale_up)
            )
            self.assertFalse(any("tskey-secret" in arg for arg in tailscale_up))
            self.assertEqual(result["ssh_enabled"], True)
            self.assertEqual(len(auth_paths), 1)
            self.assertFalse(auth_paths[0].exists())
            self.assertEqual(
                (status_path.parent / "secrets").stat().st_mode & 0o777, 0o700
            )

    def test_enroll_from_payload_never_persists_one_time_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "bootstrap-status.json"
            calls: list[list[str]] = []

            def fake_runner(args, **_kwargs):
                argv = list(args)
                calls.append(argv)
                if argv[:2] == ["tailscale", "up"]:
                    auth_arg = next(
                        arg for arg in argv if arg.startswith("--auth-key=file:")
                    )
                    auth_path = Path(auth_arg.removeprefix("--auth-key=file:"))
                    self.assertEqual(
                        auth_path.read_text(encoding="utf-8"), "tskey-secret"
                    )
                return _Completed(returncode=0, stdout="ok\n")

            def fake_which(name: str) -> str | None:
                if name == "tailscale":
                    return "/usr/bin/tailscale"
                if name == "systemctl":
                    return None
                return None

            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_PRIVATE_ACCESS_STATUS_PATH": str(status_path)},
                    clear=False,
                ),
                patch.object(private_access.shutil, "which", side_effect=fake_which),
            ):
                result = private_access.enroll_from_payload(
                    {
                        "provider": "tailscale",
                        "tailscale_auth_key": "tskey-secret",
                        "tailscale_node_name": "computer-abc123ef",
                        "tailscale_tags": ["tag:tinyhat-computer"],
                        "tailscale_ssh": True,
                    },
                    runner=fake_runner,
                )

            status_text = status_path.read_text(encoding="utf-8")
            self.assertEqual(result["state"], "ready")
            self.assertIn("--advertise-tags=tag:tinyhat-computer", calls[-1])
            self.assertNotIn("tskey-secret", repr(result))
            self.assertNotIn("tskey-secret", status_text)

    def test_private_access_report_parses_tailscale_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "bootstrap-status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "provider": "tailscale",
                        "state": "ready",
                        "node_name": "computer-abc123ef",
                    }
                ),
                encoding="utf-8",
            )

            def fake_runner(args, **_kwargs):
                self.assertEqual(args, ["tailscale", "status", "--json"])
                return _Completed(
                    stdout=json.dumps(
                        {
                            "BackendState": "Running",
                            "Self": {
                                "HostName": "computer-abc123ef",
                                "Online": True,
                                "TailscaleIPs": ["100.101.102.103"],
                            },
                        }
                    )
                )

            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_PRIVATE_ACCESS_STATUS_PATH": str(status_path)},
                    clear=False,
                ),
                patch.object(
                    private_access.shutil, "which", return_value="/usr/bin/tailscale"
                ),
            ):
                report = private_access.private_access_report(runner=fake_runner)

            self.assertEqual(report["provider"], "tailscale")
            self.assertEqual(report["state"], "ready")
            self.assertEqual(report["tailnet_ip"], "100.101.102.103")
            self.assertEqual(report["node_name"], "computer-abc123ef")


class SubscriptionLinkTests(unittest.TestCase):
    def test_extract_public_device_code_ignores_terminal_controls(self) -> None:
        buffer = (
            "\x1b[32mOpen this URL:\x1b[0m "
            "https://auth.openai.com/codex/device\n"
            "Code: ABCD-12345\n"
        )

        url, code = subscription_link.extract_public_device_code(buffer)

        self.assertEqual(url, "https://auth.openai.com/codex/device")
        self.assertEqual(code, "ABCD-12345")

    def test_start_chatgpt_link_dedupes_active_session(self) -> None:
        client = _FakePlatformClient()
        launched: list[Callable[[], None]] = []

        first = subscription_link.start_chatgpt_link(
            {"session_id": "sess-dedupe"},
            client=client,
            launcher=launched.append,
        )
        second = subscription_link.start_chatgpt_link(
            {"session_id": "sess-dedupe"},
            client=client,
            launcher=launched.append,
        )

        try:
            self.assertEqual(first["state"], "started")
            self.assertEqual(second["state"], "already_running")
            self.assertEqual(len(launched), 1)
        finally:
            with subscription_link._active_lock:
                subscription_link._active_sessions.discard("sess-dedupe")


class LauncherTests(unittest.TestCase):
    def test_activation_flips_current_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            candidate = base / "candidate"
            candidate.mkdir()
            manifest = _write_minimal_bundle(candidate)
            current = base / "current"

            result = launcher.activate_bundle(
                candidate,
                current_link=current,
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
            )

            self.assertTrue(result.activated)
            self.assertFalse(result.rolled_back)
            self.assertEqual(os.readlink(current), str(candidate.resolve()))
            self.assertEqual(result.bundle_id, manifest["bundle_id"])

    def test_activation_rolls_back_when_health_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            previous = base / "previous"
            candidate = base / "candidate"
            previous.mkdir()
            candidate.mkdir()
            _write_minimal_bundle(candidate)
            current = base / "current"
            os.symlink(previous, current)

            result = launcher.activate_bundle(
                candidate,
                current_link=current,
                health_command=[sys.executable, "-c", "raise SystemExit(7)"],
                # Single probe (no settle window) so the rollback path is
                # exercised without sleeping out the cold-start poll.
                health_attempts=1,
                health_delay=0,
            )

            self.assertFalse(result.activated)
            self.assertTrue(result.rolled_back)
            self.assertEqual(os.readlink(current), str(previous))

    def test_activation_stops_flips_starts_then_checks_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            candidate = base / "candidate"
            candidate.mkdir()
            _write_minimal_bundle(candidate)
            current = base / "current"
            events = base / "events.log"

            result = launcher.activate_bundle(
                candidate,
                current_link=current,
                stop_command=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(events)!r}).write_text('stop\\n')",
                ],
                start_command=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(events)!r}).write_text(Path({str(events)!r}).read_text() + 'start\\n')",
                ],
                health_command=[
                    sys.executable,
                    "-c",
                    (
                        "import os, pathlib; "
                        f"assert os.readlink({str(current)!r}) == {str(candidate.resolve())!r}; "
                        f"pathlib.Path({str(events)!r}).write_text(pathlib.Path({str(events)!r}).read_text() + 'health\\n')"
                    ),
                ],
            )

            self.assertTrue(result.activated)
            self.assertEqual(
                events.read_text(encoding="utf-8"), "stop\nstart\nhealth\n"
            )

    def test_activation_health_failure_restarts_previous_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            previous = base / "previous"
            candidate = base / "candidate"
            previous.mkdir()
            candidate.mkdir()
            _write_minimal_bundle(candidate)
            current = base / "current"
            os.symlink(previous, current)
            events = base / "events.log"
            append_event = (
                "from pathlib import Path; "
                f"p=Path({str(events)!r}); "
                "p.write_text((p.read_text() if p.exists() else '') + EVENT + '\\n')"
            )

            result = launcher.activate_bundle(
                candidate,
                current_link=current,
                stop_command=[
                    sys.executable,
                    "-c",
                    f"EVENT='stop'; {append_event}",
                ],
                start_command=[
                    sys.executable,
                    "-c",
                    f"EVENT='start'; {append_event}",
                ],
                health_command=[
                    sys.executable,
                    "-c",
                    f"EVENT='health'; {append_event}; raise SystemExit(9)",
                ],
                # Single probe (no settle window) so the persistent-failure
                # rollback path is exercised without sleeping.
                health_attempts=1,
                health_delay=0,
            )

            self.assertFalse(result.activated)
            self.assertTrue(result.rolled_back)
            self.assertEqual(os.readlink(current), str(previous))
            self.assertEqual(
                events.read_text(encoding="utf-8"),
                "stop\nstart\nhealth\nstop\nstart\n",
            )

    def test_activation_health_poll_recovers_from_cold_start(self) -> None:
        # A freshly started gateway is unhealthy for the first probes, then
        # becomes healthy. The settle poll must wait it out and activate rather
        # than false-fail + roll back (the #112-class destructive loop).
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            previous = base / "previous"
            candidate = base / "candidate"
            previous.mkdir()
            candidate.mkdir()
            _write_minimal_bundle(candidate)
            current = base / "current"
            os.symlink(previous, current)
            counter = base / "health_calls"
            # Health command: fail (exit 9) for the first 2 calls, then succeed.
            health_script = (
                "from pathlib import Path; "
                f"c=Path({str(counter)!r}); "
                "n=int(c.read_text()) if c.exists() else 0; "
                "n+=1; c.write_text(str(n)); "
                "raise SystemExit(0 if n >= 3 else 9)"
            )

            result = launcher.activate_bundle(
                candidate,
                current_link=current,
                start_command=[sys.executable, "-c", "pass"],
                health_command=[sys.executable, "-c", health_script],
                health_attempts=5,
                health_delay=0,
            )

            self.assertTrue(result.activated)
            self.assertFalse(result.rolled_back)
            self.assertEqual(os.readlink(current), str(candidate.resolve()))
            self.assertEqual(int(counter.read_text()), 3)


class CommandLedgerTests(unittest.TestCase):
    def test_mirror_writes_user_json_and_sqlite_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = CommandLedger(root=root)
            command = {
                "command_id": "cmd-123",
                "idempotency_key": "idem-123",
                "kind": "export_diagnostics",
                "spec": {"token": "secret-token-value", "reason": "admin-request"},
            }

            ledger.mirror(command)
            ledger.update(
                "cmd-123",
                status="applied",
                phase="exported",
                result={"output_path": "/var/log/tinyhat/diagnostics/cmd-123.zip"},
            )

            command_json = root / "cmd-123" / "command.json"
            self.assertTrue(command_json.exists())
            encoded = command_json.read_text(encoding="utf-8")
            self.assertNotIn("secret-token-value", encoded)
            self.assertIn('"status": "applied"', encoded)

            connection = sqlite3.connect(root / "commands.sqlite")
            try:
                row = connection.execute(
                    "SELECT command_id, kind, status, phase, on_box_path FROM commands"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                row,
                (
                    "cmd-123",
                    "export_diagnostics",
                    "applied",
                    "exported",
                    str(command_json),
                ),
            )


def _install_bundle_under_id(
    bundles_dir: Path,
    source_dir: Path,
    *,
    marker: str = "",
) -> tuple[Path, dict]:
    manifest = _write_minimal_bundle(source_dir, marker=marker)
    target = bundles_dir / manifest["bundle_id"].removeprefix("sha256:")
    shutil.move(str(source_dir), target)
    return target, manifest


class RuntimeCommandRunnerTests(unittest.TestCase):
    def test_activate_bundle_command_applies_and_mirrors_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            target, manifest = _install_bundle_under_id(bundles, base / "candidate")
            current = base / "current"
            ledger = CommandLedger(root=base / "commands")
            runner = RuntimeCommandRunner(
                ledger=ledger,
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-activate",
                    "idempotency_key": "idem-activate",
                    "kind": "activate_bundle",
                    "spec": {"bundle_id": manifest["bundle_id"]},
                }
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(os.readlink(current), str(target.resolve()))
            mirror = json.loads(
                (base / "commands" / "cmd-activate" / "command.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(mirror["status"], "applied")

    def test_cancel_requested_command_stops_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            _target, manifest = _install_bundle_under_id(
                bundles,
                base / "candidate",
                marker="cancel-candidate",
            )
            current = base / "current"
            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-cancel",
                    "idempotency_key": "idem-cancel",
                    "kind": "activate_bundle",
                    "cancel_requested_at": "2026-06-18T18:00:00Z",
                    "spec": {"bundle_id": manifest["bundle_id"]},
                }
            )

            self.assertEqual(result["status"], "canceled")
            self.assertFalse(current.exists())
            mirror = json.loads(
                (base / "commands" / "cmd-cancel" / "command.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(mirror["status"], "canceled")

    def test_invalid_command_id_returns_failed_without_raising(self) -> None:
        # A legacy/malformed platform command (e.g. the type-based
        # ``update_component`` shape) carries no ledger ``command_id``. execute()
        # must return a graceful failed result, NOT raise — raising here used to
        # escape the unguarded active-loop dispatch and re-bind the Computer in a
        # ~75s destructive loop.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "bundles").mkdir()
            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            # This is what _dispatch_runtime_command hands execute() after it
            # strips ``type``/``revision`` from a legacy update_component envelope.
            result = runner.execute({"targets": {"runtime": {"ref": "v0.16.6"}}})

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_code"], "invalid_command")
            self.assertEqual(result["phase"], "validate")

    def test_duplicate_terminal_command_is_local_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = CommandLedger(root=base / "commands")
            ledger.mirror(
                {
                    "command_id": "cmd-duplicate",
                    "idempotency_key": "idem-duplicate",
                    "kind": "export_diagnostics",
                    "spec": {},
                }
            )
            ledger.update(
                "cmd-duplicate",
                status="applied",
                phase="exported",
                result={
                    "output_path": "/var/log/tinyhat/diagnostics/cmd-duplicate.zip"
                },
            )
            runner = RuntimeCommandRunner(
                ledger=ledger,
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-duplicate",
                    "idempotency_key": "idem-duplicate",
                    "kind": "export_diagnostics",
                    "spec": {},
                }
            )

            self.assertEqual(result["status"], "applied")
            self.assertTrue(result["result"]["duplicate"])
            self.assertEqual(
                result["result"]["output_path"],
                "/var/log/tinyhat/diagnostics/cmd-duplicate.zip",
            )

    def test_idempotency_mismatch_does_not_overwrite_original_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = CommandLedger(root=base / "commands")
            ledger.mirror(
                {
                    "command_id": "cmd-mismatch",
                    "idempotency_key": "original-key",
                    "kind": "export_diagnostics",
                    "spec": {"reason": "original"},
                }
            )
            runner = RuntimeCommandRunner(
                ledger=ledger,
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-mismatch",
                    "idempotency_key": "other-key",
                    "kind": "export_diagnostics",
                    "spec": {"reason": "conflicting"},
                }
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_code"], "idempotency_mismatch")
            mirror = json.loads(
                (base / "commands" / "cmd-mismatch" / "command.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(mirror["idempotency_key"], "original-key")
            self.assertEqual(mirror["spec"]["reason"], "original")

    def test_apply_config_command_hot_reloads_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            get_paths: list[str] = []
            applied: list[dict] = []

            def fake_get_json(path: str) -> dict:
                get_paths.append(path)
                return {
                    "revision": 5,
                    "secrets": {"OPENAI_API_KEY": "sk-test-secret"},
                }

            def fake_apply_runtime_config(**kwargs) -> dict:
                applied.append(kwargs)
                return {
                    "revision": kwargs["revision"],
                    "secret_count": len(kwargs["secrets"]),
                    "reload": {"reloaded": True},
                    "env_block_changed": False,
                }

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                platform_get_json=fake_get_json,
                apply_runtime_config=fake_apply_runtime_config,
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-apply",
                    "idempotency_key": "idem-apply",
                    "kind": "apply_config",
                    "spec": {
                        "desired_config_revision": 5,
                        "reason": "credential_save",
                        "hot_required": True,
                    },
                }
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["phase"], "hot_reloaded")
            self.assertEqual(get_paths, ["/hapi/v1/computers/me/runtime-secrets"])
            self.assertEqual(len(applied), 2)
            self.assertTrue(applied[0]["dry_run"])
            self.assertFalse(applied[1]["dry_run"])
            self.assertEqual(applied[1]["revision"], 5)
            self.assertEqual(
                applied[1]["secrets"],
                {"OPENAI_API_KEY": "sk-test-secret"},
            )
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])

    def test_enroll_private_access_command_pulls_secret_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            get_paths: list[str] = []

            def fake_get_json(path: str) -> dict:
                get_paths.append(path)
                return {
                    "provider": "tailscale",
                    "tailscale_auth_key": "tskey-secret",
                    "tailscale_node_name": "computer-abc123ef",
                    "tailscale_tags": ["tag:tinyhat-computer"],
                    "tailscale_ssh": True,
                }

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                platform_get_json=fake_get_json,
                service_restart=False,
            )

            with (
                patch.object(
                    private_access,
                    "enroll_from_payload",
                    return_value={
                        "provider": "tailscale",
                        "state": "ready",
                        "node_name": "computer-abc123ef",
                    },
                ) as enroll,
                patch.object(
                    private_access,
                    "private_access_report",
                    return_value={
                        "provider": "tailscale",
                        "state": "ready",
                        "node_name": "computer-abc123ef",
                        "tailnet_ip": "100.101.102.103",
                    },
                ),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-private-access",
                        "idempotency_key": "idem-private-access",
                        "kind": "enroll_private_access",
                        "spec": {"reason": "startup_private_access_not_ready"},
                    }
                )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["phase"], "tailscale_ready")
            self.assertEqual(
                get_paths, ["/hapi/v1/computers/me/private-access/enrollment"]
            )
            enroll.assert_called_once()
            self.assertEqual(
                result["result"]["private_access"]["tailnet_ip"],
                "100.101.102.103",
            )
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])
            self.assertNotIn("tskey-secret", repr(result))
            mirror_text = (
                base / "commands" / "cmd-private-access" / "command.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("tskey-secret", mirror_text)

    def test_enroll_private_access_command_reports_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                platform_get_json=lambda _path: {
                    "provider": "tailscale",
                    "tailscale_auth_key": "tskey-secret",
                    "tailscale_node_name": "computer-abc123ef",
                },
                service_restart=False,
            )

            with patch.object(
                private_access,
                "enroll_from_payload",
                side_effect=RuntimeError("tailscale up failed"),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-private-access-failed",
                        "idempotency_key": "idem-private-access-failed",
                        "kind": "enroll_private_access",
                        "spec": {"reason": "startup_private_access_not_ready"},
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "tailscale_up")
            self.assertEqual(result["failure_code"], "private_access_enroll_failed")
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])
            self.assertNotIn("tskey-secret", repr(result))

    def test_private_access_cli_pulls_enrollment_with_computer_identity(
        self,
    ) -> None:
        client = _FakePlatformClient()
        client.get_payload = {
            "provider": "tailscale",
            "tailscale_auth_key": "tskey-secret",
            "tailscale_node_name": "computer-abc123ef",
            "tailscale_tags": ["tag:tinyhat-computer-prod"],
        }
        output = io.StringIO()

        with (
            patch.object(main, "default_platform_client", return_value=client),
            patch.object(
                private_access,
                "enroll_from_payload",
                return_value={
                    "provider": "tailscale",
                    "state": "ready",
                    "node_name": "computer-abc123ef",
                },
            ) as enroll,
            patch.object(
                private_access,
                "private_access_report",
                return_value={
                    "provider": "tailscale",
                    "state": "ready",
                    "node_name": "computer-abc123ef",
                    "tailnet_ip": "100.101.102.103",
                },
            ),
            contextlib.redirect_stdout(output),
        ):
            rc = main.main(["private-access", "enroll-platform"])

        self.assertEqual(rc, 0)
        self.assertEqual(
            client.gets, ["/hapi/v1/computers/me/private-access/enrollment"]
        )
        enroll.assert_called_once_with(client.get_payload)
        rendered = output.getvalue()
        self.assertIn('"state": "ready"', rendered)
        self.assertIn('"tailnet_ip": "100.101.102.103"', rendered)
        self.assertNotIn("tskey-secret", rendered)

    def test_apply_config_command_records_env_block_without_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            applied: list[dict] = []

            def fake_apply_runtime_config(**kwargs) -> dict:
                applied.append(kwargs)
                return {
                    "revision": kwargs["revision"],
                    "secret_count": 1,
                    "reload": {"skipped": bool(kwargs.get("dry_run"))},
                    "env_block_changed": True,
                }

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                platform_get_json=lambda _path: {
                    "revision": 9,
                    "secrets": {"EXA_API_KEY": "exa-test-secret"},
                },
                apply_runtime_config=fake_apply_runtime_config,
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-apply-unsupported",
                    "idempotency_key": "idem-apply-unsupported",
                    "kind": "apply_config",
                    "spec": {
                        "desired_config_revision": 9,
                        "reason": "credential_save",
                        "hot_required": True,
                    },
                }
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["failure_code"], None)
            self.assertEqual(result["phase"], "hot_reloaded")
            self.assertEqual([item["dry_run"] for item in applied], [True, False])
            self.assertTrue(result["result"]["env_block_changed"])
            self.assertFalse(result["result"]["gateway_rebind_requested"])
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])

    def test_apply_config_command_reports_typed_failure_on_reload_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_apply_runtime_config(**kwargs) -> dict:
                if kwargs.get("dry_run"):
                    return {
                        "revision": kwargs["revision"],
                        "secret_count": len(kwargs["secrets"]),
                        "reload": {"skipped": True},
                        "env_block_changed": False,
                    }
                raise RuntimeError("reload failed for token=secret-value")

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                platform_get_json=lambda _path: {
                    "revision": 6,
                    "secrets": {"OPENAI_API_KEY": "sk-test-secret"},
                },
                apply_runtime_config=fake_apply_runtime_config,
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-apply-failed",
                    "idempotency_key": "idem-apply-failed",
                    "kind": "apply_config",
                    "spec": {
                        "desired_config_revision": 6,
                        "reason": "credential_save",
                        "hot_required": True,
                    },
                }
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "hot_reload")
            self.assertEqual(result["failure_code"], "config_apply_failed")
            self.assertIn("[REDACTED]", result["result"]["detail"])
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])

    def test_apply_runtime_config_treats_failed_secret_reload_as_failed(self) -> None:
        loop = platform_loop.TinyRuntimePlatformLoop(client=_FakePlatformClient())

        with (
            patch.object(openclaw_adapter, "write_openclaw_secrets") as write_secrets,
            patch.object(
                openclaw_adapter,
                "secrets_reload",
                return_value={
                    "state": "failed",
                    "detail": {
                        "stderr": "gateway rejected token=secret-value",
                    },
                },
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "OpenClaw secrets reload failed"):
                loop._apply_runtime_config(
                    revision=2,
                    secrets={"OPENAI_API_KEY": "sk-test-secret"},
                    dry_run=False,
                )

        write_secrets.assert_called_once()

    def test_apply_config_command_rejects_invalid_spec_values(self) -> None:
        cases = [
            ({}, "desired_config_revision is required"),
            (
                {"desired_config_revision": -1},
                "desired_config_revision must be non-negative",
            ),
            (
                {"desired_config_revision": True},
                "desired_config_revision must be an integer",
            ),
            (
                {"desired_config_revision": 1, "hot_required": False},
                "hot_required must be true",
            ),
        ]
        for index, (spec, expected_detail) in enumerate(cases):
            with self.subTest(spec=spec), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                runner = RuntimeCommandRunner(
                    ledger=CommandLedger(root=base / "commands"),
                    bundles_dir=base / "bundles",
                    current_link=base / "current",
                    diagnostics_dir=base / "diagnostics",
                    platform_get_json=lambda _path: {},
                    apply_runtime_config=lambda **_kwargs: {},
                    service_restart=False,
                )

                result = runner.execute(
                    {
                        "command_id": f"cmd-apply-invalid-{index}",
                        "idempotency_key": f"idem-apply-invalid-{index}",
                        "kind": "apply_config",
                        "spec": spec,
                    }
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_code"], "invalid_command")
                self.assertIn(expected_detail, result["result"]["detail"])

    def test_link_chatgpt_command_starts_device_code_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            starts: list[dict] = []

            def fake_start(spec: dict) -> dict:
                starts.append(spec)
                return {"state": "started", "session_id": spec["session_id"]}

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                start_chatgpt_link=fake_start,
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-link",
                    "idempotency_key": "idem-link",
                    "kind": "link_chatgpt",
                    "spec": {
                        "session_id": "sess-123",
                        "provider": "openai",
                        "model_ref": "openai/gpt-5.5",
                        "auth_flow": "device_code",
                        "required_final_auth_path": "chatgpt_subscription",
                    },
                }
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["phase"], "device_code_started")
            self.assertEqual(starts[0]["type"], "start_chatgpt_link")
            self.assertEqual(starts[0]["session_id"], "sess-123")
            self.assertEqual(starts[0]["reason"], "runtime_command_link_chatgpt")
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])

    def test_link_chatgpt_command_reports_typed_failure_on_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                start_chatgpt_link=lambda _spec: (_ for _ in ()).throw(
                    RuntimeError("worker failed with token=secret-value")
                ),
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-link-failed",
                    "idempotency_key": "idem-link-failed",
                    "kind": "link_chatgpt",
                    "spec": {
                        "session_id": "sess-123",
                        "provider": "openai",
                        "model_ref": "openai/gpt-5.5",
                        "auth_flow": "device_code",
                        "required_final_auth_path": "chatgpt_subscription",
                    },
                }
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "start_device_code")
            self.assertEqual(result["failure_code"], "link_chatgpt_failed")
            self.assertIn("[REDACTED]", result["result"]["detail"])
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])

    def test_link_chatgpt_command_rejects_invalid_spec_values(self) -> None:
        cases = [
            ({"provider": "anthropic"}, "provider must be openai"),
            ({"auth_flow": "browser"}, "auth_flow must be device_code"),
            (
                {"required_final_auth_path": "platform_credits"},
                "required_final_auth_path must be chatgpt_subscription",
            ),
        ]
        for index, (override, expected_detail) in enumerate(cases):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                spec = {
                    "session_id": "sess-123",
                    "provider": "openai",
                    "model_ref": "openai/gpt-5.5",
                    "auth_flow": "device_code",
                    "required_final_auth_path": "chatgpt_subscription",
                }
                spec.update(override)
                runner = RuntimeCommandRunner(
                    ledger=CommandLedger(root=base / "commands"),
                    bundles_dir=base / "bundles",
                    current_link=base / "current",
                    diagnostics_dir=base / "diagnostics",
                    start_chatgpt_link=lambda _spec: {"state": "started"},
                    service_restart=False,
                )

                result = runner.execute(
                    {
                        "command_id": f"cmd-link-invalid-{index}",
                        "idempotency_key": f"idem-link-invalid-{index}",
                        "kind": "link_chatgpt",
                        "spec": spec,
                    }
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_code"], "invalid_command")
                self.assertIn(expected_detail, result["result"]["detail"])

    def test_rebuild_app_layer_snapshots_reactivates_and_attests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            target, manifest = _install_bundle_under_id(
                bundles,
                base / "current-bundle",
                marker="rebuild-target",
            )
            current = base / "current"
            os.symlink(target, current)
            backup_paths: list[Path] = []
            doctor_calls: list[bool] = []
            plugin_bindings: list[dict] = []

            def fake_backup_create(*, output_path: Path) -> dict:
                backup_paths.append(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"local backup bytes")
                return {
                    "state": "ready",
                    "archive_path": str(output_path),
                    "archive_bytes": output_path.stat().st_size,
                    "backup": {"verified": True},
                }

            def fake_doctor_repair() -> dict:
                doctor_calls.append(True)
                return {"state": "ready", "detail": {"command": ["openclaw", "doctor"]}}

            def fake_materialize_tinyhat_plugin(binding: dict) -> dict:
                plugin_bindings.append(binding)
                return {"state": "ready", "action": "installed"}

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                rebuild_backup_dir=base / "rebuild-backups",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            with (
                patch.object(openclaw_adapter, "backup_create", fake_backup_create),
                patch.object(
                    openclaw_adapter,
                    "materialize_tinyhat_plugin",
                    fake_materialize_tinyhat_plugin,
                ),
                patch.object(openclaw_adapter, "doctor_repair", fake_doctor_repair),
                patch.object(
                    openclaw_adapter,
                    "status_json",
                    lambda: {"state": "ready", "status": {"ok": True}},
                ),
                patch.object(
                    openclaw_adapter,
                    "adapter_attestation",
                    lambda: {"state": "ready"},
                ),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-rebuild",
                        "idempotency_key": "idem-rebuild",
                        "kind": "rebuild_app_layer",
                        "spec": {"reason": "manual_canary_repair"},
                    }
                )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["phase"], "attested")
            self.assertEqual(result["result"]["bundle_id"], manifest["bundle_id"])
            self.assertEqual(result["result"]["backup"]["state"], "ready")
            self.assertEqual(result["result"]["tinyhat_plugin"]["state"], "ready")
            self.assertEqual(
                plugin_bindings[0]["tinyhat_platform"]["plugin"]["resolved_commit_sha"],
                "676e6d878b58a2da8453573fa4b389fca32bc0a9",
            )
            self.assertEqual(len(backup_paths), 1)
            self.assertEqual(backup_paths[0].parent, base / "rebuild-backups")
            self.assertEqual(doctor_calls, [True])
            self.assertEqual(os.readlink(current), str(target.resolve()))
            self.assertFalse(result["result"]["restart_requested"])
            self.assertFalse(result["result"]["systemd_restart_requested"])
            self.assertFalse(result["result"]["automatic_restart_loop"])
            mirror = json.loads(
                (base / "commands" / "cmd-rebuild" / "command.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(mirror["status"], "applied")
            self.assertEqual(mirror["phase"], "attested")

    def test_force_update_fails_closed_on_doctor_failure(self) -> None:
        # A failed post-reinstall doctor must NOT settle force_update as applied
        # (same fail-closed gate rebuild_app_layer enforces).
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            target, _manifest = _install_bundle_under_id(
                bundles, base / "current-bundle", marker="fu-doctor"
            )
            current = base / "current"
            os.symlink(target, current)
            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                rebuild_backup_dir=base / "rebuild-backups",
                stop_command=[sys.executable, "-c", "raise SystemExit(0)"],
                start_command=[sys.executable, "-c", "raise SystemExit(0)"],
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )
            with (
                patch.object(
                    hot_image,
                    "preinstall_hot_image_plugins",
                    lambda: {"state": "ready", "plugins": []},
                ),
                patch.object(
                    openclaw_adapter,
                    "doctor_repair",
                    lambda: {"state": "failed", "detail": "boom"},
                ),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-fu-doctor",
                        "idempotency_key": "idem-fu-doctor",
                        "kind": "force_update",
                        "spec": {},
                    }
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "openclaw_doctor")
            self.assertEqual(result["failure_code"], "app_layer_rebuild_failed")

    def test_rebuild_app_layer_stops_before_restart_when_snapshot_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            target, _manifest = _install_bundle_under_id(
                bundles,
                base / "current-bundle",
                marker="rebuild-target",
            )
            current = base / "current"
            os.symlink(target, current)
            doctor_calls: list[bool] = []

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                rebuild_backup_dir=base / "rebuild-backups",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            with (
                patch.object(
                    openclaw_adapter,
                    "backup_create",
                    lambda *, output_path: {
                        "state": "failed",
                        "archive_path": str(output_path),
                        "detail": {"stderr": "backup failed"},
                    },
                ),
                patch.object(
                    openclaw_adapter,
                    "doctor_repair",
                    lambda: doctor_calls.append(True) or {"state": "ready"},
                ),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-rebuild-fail",
                        "idempotency_key": "idem-rebuild-fail",
                        "kind": "rebuild_app_layer",
                        "spec": {"reason": "manual_canary_repair"},
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "snapshot")
            self.assertEqual(result["failure_code"], "backup_failed")
            self.assertEqual(doctor_calls, [])
            self.assertEqual(Path(os.readlink(current)).resolve(), target.resolve())

    def test_rebuild_app_layer_fails_closed_when_plugin_materialization_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            target, _manifest = _install_bundle_under_id(
                bundles,
                base / "current-bundle",
                marker="rebuild-target",
            )
            current = base / "current"
            os.symlink(target, current)
            doctor_calls: list[bool] = []

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                rebuild_backup_dir=base / "rebuild-backups",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            with (
                patch.object(
                    openclaw_adapter,
                    "backup_create",
                    lambda *, output_path: {
                        "state": "ready",
                        "archive_path": str(output_path),
                        "archive_bytes": 12,
                        "backup": {"verified": True},
                    },
                ),
                patch.object(
                    openclaw_adapter,
                    "materialize_tinyhat_plugin",
                    lambda _binding: {"state": "failed", "stage": "install"},
                ),
                patch.object(
                    openclaw_adapter,
                    "doctor_repair",
                    lambda: doctor_calls.append(True) or {"state": "ready"},
                ),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-rebuild-plugin-fail",
                        "idempotency_key": "idem-rebuild-plugin-fail",
                        "kind": "rebuild_app_layer",
                        "spec": {"reason": "manual_canary_repair"},
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "tinyhat_plugin")
            self.assertEqual(result["failure_code"], "tinyhat_plugin_failed")
            self.assertEqual(result["result"]["tinyhat_plugin"]["state"], "failed")
            self.assertEqual(doctor_calls, [])
            self.assertNotIn("activation", result["result"])
            self.assertNotIn("attestation", result["result"])

    def test_rebuild_app_layer_reports_doctor_failure_without_false_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            target, _manifest = _install_bundle_under_id(
                bundles,
                base / "current-bundle",
                marker="rebuild-target",
            )
            current = base / "current"
            os.symlink(target, current)

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                rebuild_backup_dir=base / "rebuild-backups",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            with (
                patch.object(
                    openclaw_adapter,
                    "backup_create",
                    lambda *, output_path: {
                        "state": "ready",
                        "archive_path": str(output_path),
                        "archive_bytes": 12,
                        "backup": {"verified": True},
                    },
                ),
                patch.object(
                    openclaw_adapter,
                    "materialize_tinyhat_plugin",
                    lambda _binding: {"state": "ready", "action": "installed"},
                ),
                patch.object(
                    openclaw_adapter,
                    "doctor_repair",
                    lambda: {"state": "failed", "detail": {"stderr": "doctor failed"}},
                ),
                patch.object(
                    openclaw_adapter,
                    "status_json",
                    lambda: {"state": "ready", "status": {"ok": True}},
                ),
            ):
                result = runner.execute(
                    {
                        "command_id": "cmd-rebuild-doctor-fail",
                        "idempotency_key": "idem-rebuild-doctor-fail",
                        "kind": "rebuild_app_layer",
                        "spec": {"reason": "manual_canary_repair"},
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "openclaw_doctor")
            self.assertEqual(result["failure_code"], "app_layer_rebuild_failed")
            self.assertEqual(result["result"]["doctor"]["state"], "failed")
            self.assertNotIn("attestation", result["result"])

    def test_duplicate_link_chatgpt_command_does_not_respawn_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            starts: list[dict] = []

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                start_chatgpt_link=lambda spec: (
                    starts.append(spec) or {"state": "started"}
                ),
                service_restart=False,
            )
            command = {
                "command_id": "cmd-link-duplicate",
                "idempotency_key": "idem-link-duplicate",
                "kind": "link_chatgpt",
                "spec": {
                    "session_id": "sess-123",
                    "provider": "openai",
                    "model_ref": "openai/gpt-5.5",
                    "auth_flow": "device_code",
                    "required_final_auth_path": "chatgpt_subscription",
                },
            }

            first = runner.execute(command)
            second = runner.execute(command)

            self.assertEqual(first["status"], "applied")
            self.assertEqual(second["status"], "applied")
            self.assertTrue(second["result"]["duplicate"])
            self.assertEqual(len(starts), 1)

    def test_activate_bundle_command_rolls_back_on_health_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            previous, _previous_manifest = _install_bundle_under_id(
                bundles,
                base / "previous",
                marker="previous",
            )
            target, manifest = _install_bundle_under_id(
                bundles,
                base / "candidate",
                marker="candidate",
            )
            current = base / "current"
            os.symlink(previous, current)
            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                health_command=[sys.executable, "-c", "raise SystemExit(9)"],
                service_restart=False,
            )

            result = runner.execute(
                {
                    "command_id": "cmd-rollback",
                    "idempotency_key": "idem-rollback",
                    "kind": "activate_bundle",
                    "spec": {"bundle_id": manifest["bundle_id"]},
                }
            )

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(os.readlink(current), str(previous))
            self.assertNotEqual(os.readlink(current), str(target.resolve()))

    def test_activate_bundle_rolls_back_when_attestation_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundles = base / "bundles"
            bundles.mkdir()
            previous, _previous_manifest = _install_bundle_under_id(
                bundles,
                base / "previous",
                marker="previous-attestation",
            )
            _target, manifest = _install_bundle_under_id(
                bundles,
                base / "candidate",
                marker="candidate-attestation",
            )
            current = base / "current"
            os.symlink(previous, current)
            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=bundles,
                current_link=current,
                diagnostics_dir=base / "diagnostics",
                health_command=[sys.executable, "-c", "raise SystemExit(0)"],
                service_restart=False,
            )

            def raise_attestation():
                raise RuntimeError("attestation token=secret-token-value failed")

            runner._current_attestation = raise_attestation  # type: ignore[method-assign]

            result = runner.execute(
                {
                    "command_id": "cmd-attestation-rollback",
                    "idempotency_key": "idem-attestation-rollback",
                    "kind": "activate_bundle",
                    "spec": {"bundle_id": manifest["bundle_id"]},
                }
            )

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(result["failure_code"], "attestation_failed")
            self.assertEqual(Path(os.readlink(current)).resolve(), previous.resolve())
            encoded = json.dumps(result)
            self.assertNotIn("secret-token-value", encoded)


class PlatformLoopTests(unittest.TestCase):
    def test_ready_report_posts_hot_ready_snapshot_before_ready_edge(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = lambda path, payload: (
            posts.append((path, payload)) or {}
        )
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        loop._report_ready()
        loop._report_ready()

        self.assertEqual(
            [path for path, _payload in posts],
            [
                "/hapi/v1/computers/me/runtime-state",
                "/hapi/v1/computers/me/state",
                "/hapi/v1/computers/me/runtime-state",
            ],
        )
        runtime_payload = posts[0][1]
        self.assertEqual(runtime_payload["runtime_health"], "healthy")
        self.assertFalse(runtime_payload["assigned"])
        self.assertEqual(
            runtime_payload["gateway"],
            {"liveness_owner": "systemd", "restart_loop": False},
        )
        self.assertEqual(posts[1][1]["state"], "ready")

    def test_ready_report_treats_existing_ready_state_as_idempotent(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()

        def post_json(path: str, payload: dict) -> dict:
            posts.append((path, payload))
            if path == "/hapi/v1/computers/me/state":
                raise RuntimeError("HTTP Error 400: Bad Request")
            return {}

        client.post_json.side_effect = post_json
        client.get_json.return_value = {"state": "ready", "assigned": False}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        loop._report_ready()
        loop._report_ready()

        self.assertEqual(
            [path for path, _payload in posts],
            [
                "/hapi/v1/computers/me/runtime-state",
                "/hapi/v1/computers/me/state",
                "/hapi/v1/computers/me/runtime-state",
            ],
        )
        client.get_json.assert_called_once_with(
            "/hapi/v1/computers/me/platform-status",
            timeout=10,
        )

    def test_ready_report_skips_ready_edge_when_already_assigned(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()

        def post_json(path: str, payload: dict) -> dict:
            posts.append((path, payload))
            if path == "/hapi/v1/computers/me/state":
                raise RuntimeError("HTTP Error 400: Bad Request")
            return {}

        client.post_json.side_effect = post_json
        client.get_json.return_value = {"state": "assigned", "assigned": True}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        loop._report_ready()

        self.assertTrue(loop.ready_reported)
        self.assertEqual(
            [path for path, _payload in posts],
            [
                "/hapi/v1/computers/me/runtime-state",
                "/hapi/v1/computers/me/state",
            ],
        )
        client.get_json.assert_called_with(
            "/hapi/v1/computers/me/platform-status",
            timeout=10,
        )

    def test_ready_report_defers_gated_ready_edge_without_crashing(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()

        def post_json(path: str, payload: dict) -> dict:
            posts.append((path, payload))
            if path == "/hapi/v1/computers/me/state":
                raise RuntimeError("HTTP Error 409: Conflict")
            return {}

        client.post_json.side_effect = post_json
        client.get_json.return_value = {"state": "provisioning", "assigned": False}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        loop._report_ready()

        self.assertFalse(loop.ready_reported)
        self.assertEqual(
            [path for path, _payload in posts],
            [
                "/hapi/v1/computers/me/runtime-state",
                "/hapi/v1/computers/me/state",
            ],
        )
        self.assertEqual(posts[0][1]["runtime_health"], "healthy")
        self.assertFalse(posts[0][1]["assigned"])
        client.get_json.assert_called_once_with(
            "/hapi/v1/computers/me/platform-status",
            timeout=10,
        )

    def test_poll_failure_is_reported_without_exiting_loop(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = lambda path, payload: (
            posts.append((path, payload)) or {}
        )
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        def fail_once() -> dict:
            loop.stop_requested = True
            raise RuntimeError("temporary token=secret-value failure")

        with (
            patch.object(loop, "_report_ready"),
            patch.object(loop, "_poll_binding", side_effect=fail_once),
            patch.object(platform_loop.time, "sleep"),
        ):
            self.assertEqual(loop.run_forever(), 0)

        runtime_state_payloads = [
            payload
            for path, payload in posts
            if path == "/hapi/v1/computers/me/runtime-state"
        ]
        self.assertEqual(len(runtime_state_payloads), 1)
        self.assertEqual(
            runtime_state_payloads[0]["runtime_health"], "degraded_control_plane"
        )
        self.assertTrue(runtime_state_payloads[0]["binding_poll_failed"])
        self.assertEqual(
            runtime_state_payloads[0]["openclaw_control"],
            "no_restart_requested",
        )
        heartbeat_payloads = [
            payload
            for path, payload in posts
            if path == "/hapi/v1/computers/me/heartbeat"
        ]
        self.assertEqual(len(heartbeat_payloads), 1)
        self.assertFalse(heartbeat_payloads[0]["metrics"]["assigned"])

    def test_unassigned_state_report_failure_does_not_exit_loop(self) -> None:
        from tinyhat_runtime import platform_loop

        client = Mock()
        client.post_json.side_effect = RuntimeError("platform unavailable")
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        def unassigned_once() -> dict:
            loop.stop_requested = True
            return {"assigned": False}

        with (
            patch.object(loop, "_poll_binding", side_effect=unassigned_once),
            patch.object(platform_loop.time, "sleep"),
        ):
            self.assertEqual(loop.run_forever(), 0)

        self.assertGreaterEqual(client.post_json.call_count, 2)

    def test_unassigned_heartbeat_dispatches_runtime_command(self) -> None:
        from tinyhat_runtime import platform_loop

        command = {
            "type": "runtime_command",
            "command": {
                "command_id": "cmd-private-access",
                "idempotency_key": "enroll_private_access:abc123",
                "kind": "enroll_private_access",
                "spec": {"reason": "heartbeat_private_access_not_ready"},
            },
        }
        client = Mock()
        client.post_json.return_value = {"command": command}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        def unassigned_once() -> dict:
            loop.stop_requested = True
            return {"assigned": False}

        with (
            patch.object(loop, "_report_ready"),
            patch.object(loop, "_poll_binding", side_effect=unassigned_once),
            patch.object(loop, "_dispatch_runtime_command") as dispatch,
            patch.object(platform_loop.time, "sleep"),
        ):
            self.assertEqual(loop.run_forever(), 0)

        client.post_json.assert_called_once()
        self.assertEqual(
            client.post_json.call_args.args[0],
            "/hapi/v1/computers/me/heartbeat",
        )
        dispatch.assert_called_once_with(command)

    def test_unassigned_command_dispatch_failure_does_not_exit_loop(self) -> None:
        from tinyhat_runtime import platform_loop

        command = {
            "type": "runtime_command",
            "command": {
                "command_id": "cmd-private-access",
                "idempotency_key": "enroll_private_access:abc123",
                "kind": "enroll_private_access",
                "spec": {"reason": "heartbeat_private_access_not_ready"},
            },
        }
        client = Mock()
        client.post_json.return_value = {"command": command}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        def unassigned_once() -> dict:
            loop.stop_requested = True
            return {"assigned": False}

        with (
            patch.object(loop, "_report_ready"),
            patch.object(loop, "_poll_binding", side_effect=unassigned_once),
            patch.object(
                loop,
                "_dispatch_runtime_command",
                side_effect=RuntimeError("HTTP Error 409: Conflict"),
            ) as dispatch,
            patch.object(platform_loop.time, "sleep"),
        ):
            self.assertEqual(loop.run_forever(), 0)

        dispatch.assert_called_once_with(command)
        self.assertTrue(loop.stop_requested)

    def test_binding_activation_does_not_restart_openclaw_gateway(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = lambda path, payload: (
            posts.append((path, payload)) or {}
        )
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)
        binding = {
            "telegram_owner_user_id": "123456",
            "telegram_bot_user_id": "654321",
            "telegram_bot_token": "token=secret-value",
        }
        now = time.monotonic()

        with (
            patch.dict(
                os.environ,
                {"TINYHAT_PLATFORM_BASE_URL": "https://platform.example"},
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "apply_binding_config",
                return_value={"state": "ready"},
            ) as apply_config,
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_health",
                return_value={"state": "healthy"},
            ) as gateway_health,
        ):
            loop._activate_binding(
                binding,
                cycle_started_wall=0.0,
                cycle_started=now,
                binding_received=now,
                phase_spans=[],
            )

        apply_config.assert_called_once()
        self.assertIs(apply_config.call_args.kwargs["preserve_existing_secrets"], True)
        gateway_health.assert_called_once()
        self.assertFalse(
            hasattr(platform_loop.openclaw_adapter, "gateway_restart_once")
        )
        startup_payloads = [
            payload
            for path, payload in posts
            if path == "/hapi/v1/computers/me/runtime-state"
            and payload.get("startup_timings")
        ]
        self.assertEqual(len(startup_payloads), 1)
        sample = startup_payloads[0]["startup_timings"][0]
        self.assertEqual(
            sample["sample_metadata"]["gateway_restart"]["state"],
            "not_requested",
        )
        self.assertEqual(
            sample["sample_metadata"]["restart_policy"],
            "no_assignment_restart",
        )

    def test_heartbeat_includes_private_access_report(self) -> None:
        from tinyhat_runtime import platform_loop

        client = _FakePlatformClient()
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with (
            patch.object(
                platform_loop.private_access,
                "private_access_report",
                return_value={
                    "provider": "tailscale",
                    "state": "ready",
                    "tailnet_ip": "100.101.102.103",
                },
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_status",
                return_value={"state": "ready"},
            ),
        ):
            result = loop._post_heartbeat(assigned=False)

        self.assertEqual(result, {})
        self.assertEqual(client.posts[0][0], "/hapi/v1/computers/me/heartbeat")
        payload = client.posts[0][1]
        self.assertFalse(payload["metrics"]["assigned"])
        self.assertEqual(
            payload["metrics"]["private_access"]["tailnet_ip"],
            "100.101.102.103",
        )

    def test_heartbeat_reports_active_bundle_component_versions(self) -> None:
        from tinyhat_runtime import platform_loop

        runtime_sha = "a" * 40
        plugin_sha = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_minimal_bundle(root)
            bundle.write_manifest(
                root,
                components={
                    "runtime": {
                        "repo": "https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git",
                        "ref": runtime_sha,
                    },
                    "openclaw": {"package": "openclaw", "ref": "openclaw@2026.6.8"},
                    "tinyhat_openclaw_plugin": {
                        "repo": "https://github.com/tinyhat-ai/tinyhat.git",
                        "ref": plugin_sha,
                    },
                },
            )
            client = _FakePlatformClient()
            loop = platform_loop.TinyRuntimePlatformLoop(client=client)

            with (
                patch.object(platform_loop.paths, "CURRENT_LINK", root),
                patch.object(
                    platform_loop.private_access,
                    "private_access_report",
                    return_value=None,
                ),
                patch.object(
                    platform_loop.openclaw_adapter,
                    "gateway_status",
                    return_value={"state": "ready"},
                ),
            ):
                result = loop._post_heartbeat(assigned=True)

        self.assertEqual(result, {})
        payload = client.posts[0][1]
        self.assertEqual(
            payload["component_versions"],
            {
                "runtime": {"sha": runtime_sha},
                "plugin": {"sha": plugin_sha},
                "framework": {"version": "2026.6.8"},
            },
        )
        self.assertTrue(payload["metrics"]["assigned"])

    def test_heartbeat_omits_component_versions_when_manifest_missing(self) -> None:
        from tinyhat_runtime import platform_loop

        client = _FakePlatformClient()
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with (
            patch.object(
                platform_loop.bundle,
                "load_manifest",
                side_effect=FileNotFoundError("no active bundle"),
            ),
            patch.object(
                platform_loop.private_access,
                "private_access_report",
                return_value=None,
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_status",
                return_value={"state": "ready"},
            ),
        ):
            self.assertEqual(platform_loop._active_bundle_component_versions(), {})
            self.assertEqual(platform_loop._active_bundle_runtime_state_report(), {})
            loop._post_heartbeat(assigned=True)

        payload = client.posts[0][1]
        self.assertNotIn("component_versions", payload)
        self.assertEqual(payload["metrics"]["runtime_generation"], "tiny_runtime")

    def test_runtime_state_includes_private_access_report(self) -> None:
        from tinyhat_runtime import platform_loop

        client = _FakePlatformClient()
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with patch.object(
            platform_loop.private_access,
            "private_access_report",
            return_value={
                "provider": "tailscale",
                "state": "ready",
                "tailnet_ip": "100.101.102.103",
            },
        ):
            loop._post_runtime_state(
                "healthy",
                "control plane ready; awaiting binding",
                {"assigned": False},
            )

        self.assertEqual(client.posts[0][0], "/hapi/v1/computers/me/runtime-state")
        payload = client.posts[0][1]
        self.assertFalse(payload["assigned"])
        self.assertEqual(payload["private_access"]["provider"], "tailscale")
        self.assertEqual(payload["private_access"]["state"], "ready")
        self.assertEqual(payload["private_access"]["tailnet_ip"], "100.101.102.103")

    def test_runtime_state_reports_active_bundle_component_versions(self) -> None:
        from tinyhat_runtime import platform_loop

        runtime_sha = "c" * 40
        plugin_sha = "d" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_minimal_bundle(root)
            manifest = bundle.write_manifest(
                root,
                components={
                    "runtime": {
                        "repo": "https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git",
                        "ref": runtime_sha,
                    },
                    "openclaw": {"package": "openclaw", "ref": "openclaw@2026.6.8"},
                    "tinyhat_openclaw_plugin": {
                        "repo": "https://github.com/tinyhat-ai/tinyhat.git",
                        "ref": plugin_sha,
                    },
                },
            )
            client = _FakePlatformClient()
            loop = platform_loop.TinyRuntimePlatformLoop(client=client)

            with (
                patch.object(platform_loop.paths, "CURRENT_LINK", root),
                patch.object(
                    platform_loop.private_access,
                    "private_access_report",
                    return_value=None,
                ),
            ):
                loop._post_runtime_state(
                    "healthy",
                    "tiny_runtime OpenClaw ready",
                    {"assigned": True},
                )

        payload = client.posts[0][1]
        self.assertEqual(payload["runtime"], {"sha": runtime_sha})
        self.assertEqual(payload["plugin"]["sha"], plugin_sha)
        self.assertEqual(payload["plugin"]["framework_installed"], "2026.6.8")
        self.assertEqual(payload["openclaw"]["interface"], "official_cli")
        self.assertEqual(payload["openclaw"]["installed_version"], "2026.6.8")
        self.assertEqual(
            payload["component_versions"]["framework"]["version"],
            "2026.6.8",
        )
        self.assertEqual(payload["bundle"]["id"], manifest["bundle_id"])
        self.assertEqual(
            payload["components"]["tinyhat_openclaw_plugin"]["ref"],
            plugin_sha,
        )

    def test_runtime_state_bundle_evidence_wins_over_extra_payload(self) -> None:
        from tinyhat_runtime import platform_loop

        runtime_sha = "e" * 40
        plugin_sha = "f" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = bundle.write_manifest(
                root,
                components={
                    "runtime": {
                        "repo": "https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git",
                        "ref": runtime_sha,
                    },
                    "openclaw": {"package": "openclaw", "ref": "openclaw@2026.6.8"},
                    "tinyhat_openclaw_plugin": {
                        "repo": "https://github.com/tinyhat-ai/tinyhat.git",
                        "ref": plugin_sha,
                    },
                },
            )
            client = _FakePlatformClient()
            loop = platform_loop.TinyRuntimePlatformLoop(client=client)

            with (
                patch.object(platform_loop.paths, "CURRENT_LINK", root),
                patch.object(
                    platform_loop.private_access,
                    "private_access_report",
                    return_value=None,
                ),
            ):
                loop._post_runtime_state(
                    "healthy",
                    "tiny_runtime OpenClaw ready",
                    {
                        "runtime": {"sha": "stale-runtime"},
                        "plugin": {
                            "sha": "stale-plugin",
                            "framework_installed": "old-framework",
                        },
                        "openclaw": {"installed_version": "old-framework"},
                        "component_versions": {
                            "runtime": {"sha": "stale-runtime"},
                            "plugin": {"sha": "stale-plugin"},
                            "framework": {"version": "old-framework"},
                        },
                        "bundle": {"id": "stale-bundle"},
                        "components": {"runtime": {"ref": "stale-runtime"}},
                    },
                )

        payload = client.posts[0][1]
        self.assertEqual(payload["runtime"], {"sha": runtime_sha})
        self.assertEqual(payload["plugin"]["sha"], plugin_sha)
        self.assertEqual(payload["plugin"]["framework_installed"], "2026.6.8")
        self.assertEqual(payload["openclaw"]["interface"], "official_cli")
        self.assertEqual(payload["openclaw"]["installed_version"], "2026.6.8")
        self.assertEqual(payload["bundle"]["id"], manifest["bundle_id"])
        self.assertEqual(
            payload["component_versions"],
            {
                "runtime": {"sha": runtime_sha},
                "plugin": {"sha": plugin_sha},
                "framework": {"version": "2026.6.8"},
            },
        )

    def test_subscription_runtime_verification_uses_models_status(self) -> None:
        from tinyhat_runtime import platform_loop

        client = _FakePlatformClient()
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with patch.object(
            platform_loop.openclaw_adapter,
            "models_status",
            return_value={
                "state": "ready",
                "models": {
                    "defaultModel": "openai/gpt-5.5",
                    "resolvedDefault": "openai/gpt-5.5",
                },
            },
        ):
            loop._report_subscription_runtime_verification(
                {
                    "llm_auth_mode": "chatgpt_subscription",
                    "llm_model_ref": "openai/gpt-5.5",
                }
            )

        self.assertEqual(
            client.posts,
            [
                (
                    "/hapi/v1/computers/me/subscription-link/runtime-verification",
                    {
                        "expected_model_ref": "openai/gpt-5.5",
                        "observed_model_ref": "openai/gpt-5.5",
                        "command": "openclaw models status --json",
                        "verified": True,
                    },
                )
            ],
        )

    def test_subscription_runtime_verification_reports_unknown_status_shape(
        self,
    ) -> None:
        from tinyhat_runtime import platform_loop

        client = _FakePlatformClient()
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with (
            patch.object(
                platform_loop.openclaw_adapter,
                "models_status",
                return_value={"state": "ready", "models": {"models": []}},
            ),
            self.assertLogs(platform_loop.LOG, level="WARNING") as logs,
        ):
            loop._report_subscription_runtime_verification(
                {
                    "llm_auth_mode": "chatgpt_subscription",
                    "llm_model_ref": "openai/gpt-5.5",
                }
            )

        self.assertIn("did not expose a default model", "\n".join(logs.output))
        payload = client.posts[0][1]
        self.assertIsNone(payload["observed_model_ref"])
        self.assertFalse(payload["verified"])
        self.assertIn("shape=top=models,state;models=models", payload["detail"])

    def test_binding_activation_treats_existing_active_state_as_idempotent(
        self,
    ) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()

        def post_json(path: str, payload: dict) -> dict:
            posts.append((path, payload))
            if path == "/hapi/v1/computers/me/state":
                raise RuntimeError("HTTP Error 400: Bad Request")
            return {}

        client.post_json.side_effect = post_json
        client.get_json.return_value = {"state": "active", "assigned": True}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)
        binding = {
            "telegram_owner_user_id": "123456",
            "telegram_bot_user_id": "654321",
            "telegram_bot_token": "token=secret-value",
        }
        now = time.monotonic()

        with (
            patch.dict(
                os.environ, {"TINYHAT_PLATFORM_BASE_URL": "https://platform.example"}
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "apply_binding_config",
                return_value={"state": "ready"},
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_health",
                return_value={"state": "healthy"},
            ),
        ):
            loop._activate_binding(
                binding,
                cycle_started_wall=0.0,
                cycle_started=now,
                binding_received=now,
                phase_spans=[],
            )

        client.get_json.assert_called_once_with(
            "/hapi/v1/computers/me/platform-status",
            timeout=10,
        )
        runtime_state_payloads = [
            payload
            for path, payload in posts
            if path == "/hapi/v1/computers/me/runtime-state"
        ]
        self.assertEqual(len(runtime_state_payloads), 2)
        self.assertTrue(runtime_state_payloads[0].get("startup_timings"))
        self.assertEqual(runtime_state_payloads[1]["runtime_health"], "healthy")
        self.assertEqual(
            runtime_state_payloads[1]["gateway"]["liveness_owner"],
            "systemd",
        )

    def test_active_rebind_replaces_secrets_when_owner_changes(self) -> None:
        from tinyhat_runtime import platform_loop

        client = Mock()
        client.post_json.return_value = {}
        original_binding = {
            "telegram_owner_user_id": "owner-1",
            "telegram_bot_user_id": "bot-1",
            "telegram_bot_token": "token=secret-value",
        }
        next_binding = {
            **original_binding,
            "telegram_owner_user_id": "owner-2",
        }

        responses = [{"assigned": True, "binding": next_binding}]

        def get_json(path: str, **_kwargs):
            return responses.pop(0)

        client.get_json.side_effect = get_json
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with (
            patch.dict(
                os.environ,
                {"TINYHAT_PLATFORM_BASE_URL": "https://platform.example"},
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_status",
                return_value={"state": "healthy"},
            ),
            patch.object(
                platform_loop.openclaw_adapter,
                "apply_binding_config",
                return_value={"state": "ready"},
            ) as apply_config,
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_health",
                return_value={"state": "healthy"},
            ),
            patch.object(platform_loop.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop._active_loop(original_binding)

        apply_config.assert_called_once()
        self.assertIs(apply_config.call_args.kwargs["preserve_existing_secrets"], False)

    def test_gateway_ready_wait_covers_openclaw_self_restart(self) -> None:
        from tinyhat_runtime import platform_loop

        self.assertGreaterEqual(platform_loop.GATEWAY_READY_WAIT_SECONDS, 75)

    def test_runtime_command_dispatch_can_disable_service_restart_for_dev_container(
        self,
    ) -> None:
        from tinyhat_runtime import platform_loop

        captured_kwargs: dict = {}

        class FakeRunner:
            def __init__(self, **kwargs) -> None:
                captured_kwargs.update(kwargs)

            def execute(self, command: dict) -> dict:
                return {
                    "command_id": command["command_id"],
                    "kind": command["kind"],
                    "status": "applied",
                }

        client = Mock()
        client.post_json.return_value = {}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with (
            patch.dict(os.environ, {"TINYHAT_RUNTIME_NO_SERVICE_RESTART": "1"}),
            patch.object(platform_loop, "RuntimeCommandRunner", FakeRunner),
        ):
            loop._dispatch_runtime_command(
                {
                    "type": "runtime_command",
                    "command": {
                        "command_id": "cmd-rebuild",
                        "idempotency_key": "idem-rebuild",
                        "kind": "rebuild_app_layer",
                        "spec": {"reason": "admin_rebuild_app_layer"},
                    },
                }
            )

        self.assertFalse(captured_kwargs["service_restart"])
        client.post_json.assert_called_once_with(
            "/hapi/v1/computers/me/runtime-command/result",
            {
                "result": {
                    "command_id": "cmd-rebuild",
                    "kind": "rebuild_app_layer",
                    "status": "applied",
                }
            },
        )

    def test_legacy_command_dispatch_result_failure_does_not_raise(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []

        def post_json(path: str, payload: dict) -> dict:
            posts.append((path, payload))
            if path == "/hapi/v1/computers/me/runtime-command/result":
                raise RuntimeError("HTTP Error 409: Conflict")
            return {}

        client = Mock()
        client.post_json.side_effect = post_json
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        loop._dispatch_runtime_command(
            {
                "type": "update_component",
                "revision": 2,
                "targets": {"runtime": {"ref": "v0.16.6"}},
            }
        )

        self.assertEqual(
            [path for path, _payload in posts],
            ["/hapi/v1/computers/me/runtime-command/result"],
        )
        result = posts[0][1]["result"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_code"], "invalid_command")
        self.assertEqual(result["phase"], "validate")

    def test_gateway_ready_wait_polls_until_official_health_recovers(self) -> None:
        from tinyhat_runtime import platform_loop

        client = Mock()
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        with (
            patch.object(
                platform_loop.openclaw_adapter,
                "gateway_health",
                side_effect=[
                    {"state": "unhealthy"},
                    {"state": "healthy", "gateway": {"ok": True}},
                ],
            ) as gateway_health,
            patch.object(platform_loop.time, "sleep"),
        ):
            health = loop._wait_for_gateway_ready()

        self.assertEqual(health["state"], "healthy")
        self.assertEqual(gateway_health.call_count, 2)

    def test_binding_activation_failure_reports_supported_runtime_state(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = lambda path, payload: (
            posts.append((path, payload)) or {}
        )
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        def assigned_once() -> dict:
            loop.stop_requested = True
            return {
                "assigned": True,
                "binding": {
                    "telegram_owner_user_id": "123456",
                    "telegram_bot_user_id": "654321",
                    "telegram_bot_token": "token=secret-value",
                },
            }

        with (
            patch.dict(
                os.environ,
                {"TINYHAT_PLATFORM_BASE_URL": "https://platform.example"},
            ),
            patch.object(loop, "_report_ready"),
            patch.object(loop, "_poll_binding", side_effect=assigned_once),
            patch.object(
                platform_loop.openclaw_adapter,
                "apply_binding_config",
                return_value={"state": "failed"},
            ),
            patch.object(platform_loop.time, "sleep"),
        ):
            self.assertEqual(loop.run_forever(), 0)

        runtime_state_payloads = [
            payload
            for path, payload in posts
            if path == "/hapi/v1/computers/me/runtime-state"
        ]
        self.assertEqual(len(runtime_state_payloads), 1)
        self.assertEqual(
            runtime_state_payloads[0]["runtime_health"],
            "openclaw_not_ready",
        )
        self.assertTrue(runtime_state_payloads[0]["activation_failed"])
        self.assertEqual(
            runtime_state_payloads[0]["openclaw_control"],
            "no_restart_requested",
        )


class DiagnosticsExportTests(unittest.TestCase):
    def test_export_diagnostics_uses_official_command_and_redacts_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "diagnostics.zip"

            def runner(argv, **_kwargs):
                self.assertEqual(
                    argv[:4],
                    ["openclaw", "gateway", "diagnostics", "export"],
                )
                out = Path(argv[argv.index("--output") + 1])
                with zipfile.ZipFile(out, "w") as zf:
                    zf.writestr("summary.md", "token=secret-token-value\n")
                    zf.writestr("config.yaml", "api_key=sk-live-secret-value\n")
                    zf.writestr("env.conf", "authorization=Bearer abcdefghijklmno\n")
                    zf.writestr("rotated.log.1", "token=tskey-auth-secret-value\n")
                    zf.writestr("nostate", "secret=raw-secret-value-12345\n")
                    zf.writestr("binary.bin", b"\x00secret-token-value\x00")
                    zf.writestr(
                        "diagnostics.json",
                        json.dumps(
                            {
                                "state": "healthy",
                                "token": "secret-token-value",
                                "authorization": "Basic abcdefghijklmno",
                                "cookie": "session=abcdefghijklmno",
                            }
                        ),
                    )
                    zf.writestr("manifest.json", json.dumps({"schema": "fake"}))
                    zf.writestr(
                        "health/gateway.json", json.dumps({"status": "healthy"})
                    )
                    zf.writestr(
                        "stability/latest.json", json.dumps({"status": "stable"})
                    )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {"output": str(out), "token": "secret-token-value"}
                    ),
                    stderr="",
                )

            payload = openclaw_adapter.export_diagnostics(
                output_path=output,
                runner=runner,
            )

            self.assertEqual(payload["state"], "ready")
            self.assertIn("summary.md", payload["entries"])
            self.assertIn("config.yaml", payload["entries"])
            self.assertIn("env.conf", payload["entries"])
            self.assertIn("rotated.log.1", payload["entries"])
            self.assertIn("nostate", payload["entries"])
            self.assertIn("binary.bin", payload["entries"])
            self.assertIn("diagnostics.json", payload["entries"])
            self.assertIn("manifest.json", payload["entries"])
            self.assertIn("health/gateway.json", payload["entries"])
            self.assertIn("stability/latest.json", payload["entries"])
            with zipfile.ZipFile(output, "r") as zf:
                encoded = "\n".join(
                    zf.read(name).decode("utf-8", errors="replace")
                    for name in zf.namelist()
                )
            self.assertNotIn("secret-token-value", encoded)
            self.assertNotIn("sk-live-secret-value", encoded)
            self.assertNotIn("Bearer abcdefghijklmno", encoded)
            self.assertNotIn("tskey-auth-secret-value", encoded)
            self.assertNotIn("raw-secret-value-12345", encoded)
            self.assertNotIn("Basic abcdefghijklmno", encoded)
            self.assertNotIn("session=abcdefghijklmno", encoded)
            self.assertIn("[binary omitted by Tinyhat diagnostics redaction]", encoded)


class AttestationTests(unittest.TestCase):
    def test_attestation_is_non_secret_and_names_runtime_generation(self) -> None:
        manifest = {
            "bundle_id": "sha256:" + "a" * 64,
            "components": {"openclaw": {"ref": "openclaw@2026.6.8"}},
        }
        payload = attestation.build_attestation(
            bundle_manifest=manifest,
            identity_doc={
                "computer_id": "5066",
                "assignment_id": "a1",
                "token": "secret-token-value",
            },
            openclaw={"state": "healthy", "token": "secret-token-value"},
            observed_at="2026-06-18T00:00:00Z",
        )

        encoded = json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["runtime_generation"], RUNTIME_GENERATION)
        self.assertEqual(payload["bundle_id"], manifest["bundle_id"])
        self.assertNotIn("secret-token-value", encoded)


class PlatformClientTests(unittest.TestCase):
    def test_client_only_builds_me_urls(self) -> None:
        client = PlatformClient("https://platform.example")
        self.assertEqual(
            client.me_url("runtime-binding"),
            "https://platform.example/me/runtime-binding",
        )
        with self.assertRaises(ValueError):
            client.me_url("../admin")

    def test_client_only_builds_computer_me_api_urls(self) -> None:
        client = PlatformClient("https://platform.example")
        self.assertEqual(
            client.api_url("/hapi/v1/computers/me/private-access/enrollment"),
            (
                "https://platform.example/"
                "hapi/v1/computers/me/private-access/enrollment"
            ),
        )
        self.assertEqual(
            client.api_url("hapi/v1/computers/me/runtime-secrets"),
            "https://platform.example/hapi/v1/computers/me/runtime-secrets",
        )

        for path in (
            "/hapi/v1/admin/computers/1",
            "/hapi/v1/computers/42/binding",
            "/hapi/v1/computers/me/../admin",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    client.api_url(path)

    def test_dev_runtime_identity_token_is_dev_only_marker(self) -> None:
        with patch.dict(os.environ, {"TINYHAT_DEV_RUNTIME": "1"}, clear=False):
            self.assertEqual(dev_runtime_identity_token(), DEV_RUNTIME_BEARER)

        with patch.dict(os.environ, {"TINYHAT_DEV_RUNTIME": ""}, clear=False):
            self.assertIsNone(dev_runtime_identity_token())

    def test_default_client_uses_dev_marker_without_metadata(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "TINYHAT_DEV_RUNTIME": "1",
                    "TINYHAT_PLATFORM_BASE_URL": "https://dev.example.test",
                    "TINYHAT_BACKEND_AUDIENCE": "https://dev.example.test",
                },
                clear=False,
            ),
            patch.object(
                platform_client,
                "fetch_gce_identity_token",
                side_effect=AssertionError("metadata must not be called in dev"),
            ),
        ):
            client = platform_client.default_platform_client()
            self.assertEqual(client.base_url, "https://dev.example.test")
            self.assertEqual(client.token_provider(), DEV_RUNTIME_BEARER)


class SystemdUnitTests(unittest.TestCase):
    def test_install_normalizes_root_owned_bundle_copy(self) -> None:
        install_script = (_REPO_ROOT / "tiny_runtime" / "install.sh").read_text(
            encoding="utf-8"
        )
        chown_pos = install_script.index('chown -R 0:0 "${tmp_target}"')
        verify_pos = install_script.index(
            'python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${target}"'
        )
        self.assertLess(chown_pos, verify_pos)

    def test_computer_startup_script_owns_bundle_boot_path(self) -> None:
        script_path = _REPO_ROOT / "tiny_runtime" / "bin" / "tinyhat-computer-startup"
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(os.access(script_path, os.X_OK))
        self.assertIn("metadata_get()", script)
        self.assertIn("tinyhat-platform-base-url", script)
        self.assertIn("tinyhat-backend-audience", script)
        self.assertIn('private-access enroll-platform', script)
        self.assertIn("tinyhat-runtime-platform.service", script)
        self.assertNotIn("TINYHAT_TAILSCALE_AUTH_KEY", script)
        self.assertNotIn("tskey-", script)

    def test_dev_entrypoint_can_run_tiny_runtime_loop_without_supervisor(
        self,
    ) -> None:
        entrypoint = (_REPO_ROOT / "dev" / "entrypoint.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'runtime_mode="${TINYHAT_RUNTIME_MODE:-${TINYHAT_RUNTIME_IMAGE_MODE:-legacy_supervisor}}"',
            entrypoint,
        )
        self.assertIn('== "tiny_runtime"', entrypoint)
        self.assertIn("prepare_tiny_runtime_bundle", entrypoint)
        self.assertIn("TINYHAT_RUNTIME_SKIP_SYSTEMD=1", entrypoint)
        self.assertIn("/tmp/tinyhat-rebuild-backups", entrypoint)
        self.assertIn('TAILSCALE_STATE_DIR="${TINYHAT_TAILSCALE_STATE_DIR:-/var/lib/tinyhat-tailscale}"', entrypoint)
        self.assertIn('--state="${TAILSCALE_STATE_DIR}/tailscaled.state"', entrypoint)
        self.assertIn('rm -rf -- "${RUNTIME_HOME}/tailscale"', entrypoint)
        self.assertIn("python3 -m tinyhat_runtime.main gateway run", entrypoint)
        self.assertIn("python3 -m tinyhat_runtime.main platform loop", entrypoint)
        self.assertIn("TINYHAT_RUNTIME_NO_SERVICE_RESTART", entrypoint)
        self.assertIn("tailscale logout", entrypoint)
        self.assertIn('chown -R tinyhat:tinyhat "${PRIVATE_ACCESS_STATUS_DIR}"', entrypoint)
        self.assertIn('"node_name": os.environ.get("TINYHAT_TAILSCALE_NODE_NAME")', entrypoint)
        branch = entrypoint[
            entrypoint.index('== "tiny_runtime"') : entrypoint.index(
                'echo "[dev-entrypoint] starting supervisor as tinyhat..."'
            )
        ]
        self.assertNotIn("supervisor.py", branch)

    def test_source_bootstrap_pulls_private_access_with_computer_identity(self) -> None:
        script = (_REPO_ROOT / "bootstrap.sh").read_text(encoding="utf-8")

        self.assertIn(
            "/hapi/v1/computers/me/private-access/enrollment",
            script,
        )
        self.assertIn("private-access enroll-platform", script)
        self.assertIn("PYTHONPATH=\"${RUNTIME_DIR}/tiny_runtime\"", script)
        self.assertIn(
            'if [[ "${INSTALL_TINY_RUNTIME_FROM_SOURCE}" != "1" ]]; then\n'
            "  enroll_private_access_from_platform_source\n"
            "else\n"
            "  echo \"[tinyhat-runtime] source reinstall will enroll private "
            "access after installing tiny_runtime\"\n"
            "fi",
            script,
        )
        self.assertNotIn("TINYHAT_TAILSCALE_AUTH_KEY", script)
        self.assertNotIn("TAILSCALE_AUTH_KEY", script)
        self.assertNotIn("tailscale_auth_file", script)

    def test_source_reinstall_cleans_legacy_processes_before_tiny_runtime_start(
        self,
    ) -> None:
        script = (_REPO_ROOT / "bootstrap.sh").read_text(encoding="utf-8")

        cleanup_pos = script.index("cleanup_legacy_openclaw_processes")
        enable_pos = script.index("systemctl enable --now")

        self.assertLess(cleanup_pos, enable_pos)
        self.assertIn('&& "${args}" == *"${RUNTIME_DIR}/supervisor.py"*', script)
        self.assertIn('&& "${args}" == *"--auth none"*', script)
        self.assertIn('&& "${args}" == *"--tailscale off"*', script)
        self.assertIn('kill -TERM "${pids[@]}"', script)
        self.assertIn('kill -KILL "${pids[@]}"', script)

    def test_source_reinstall_quiesces_existing_runtime_before_install(self) -> None:
        script = (_REPO_ROOT / "bootstrap.sh").read_text(encoding="utf-8")

        quiesce_pos = script.index("quiesce_for_tiny_runtime_source_reinstall\n")
        apt_pos = script.index("apt-get update -y")

        self.assertLess(quiesce_pos, apt_pos)
        self.assertIn("stop_existing_tiny_runtime_units", script)
        self.assertIn("tinyhat-runtime-platform.service", script)
        self.assertIn("tinyhat-runtime-gateway.service", script)
        self.assertIn("tinyhat-runtime-attestation.service", script)
        self.assertIn(
            'write_runtime_bootstrap_status "updating" '
            '"stopping existing runtime before source reinstall"',
            script,
        )

    def test_source_reinstall_quiesce_guard_skips_normal_boots(self) -> None:
        script = (_REPO_ROOT / "bootstrap.sh").read_text(encoding="utf-8")

        function_start = script.index("quiesce_for_tiny_runtime_source_reinstall() {")
        function_end = script.index(
            "\n}\n\nfail_tiny_runtime_source_reinstall",
            function_start,
        )
        function_body = script[function_start:function_end]
        guard_pos = function_body.index(
            'if [[ "${INSTALL_TINY_RUNTIME_FROM_SOURCE}" != "1" ]]; then'
        )
        return_pos = function_body.index("return 0", guard_pos)
        first_stop_pos = min(
            function_body.index("stop_existing_tiny_runtime_units"),
            function_body.index("remove_legacy_openclaw_units"),
        )

        self.assertLess(guard_pos, return_pos)
        self.assertLess(return_pos, first_stop_pos)

    def test_assembled_bundle_carries_computer_startup_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "bundle"
            completed = subprocess.run(
                [
                    str(_REPO_ROOT / "tiny_runtime" / "bake" / "assemble-bundle.sh"),
                    str(out_dir),
                ],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            entrypoint = out_dir / "bin" / "tinyhat-computer-startup"
            self.assertTrue(entrypoint.exists())
            self.assertTrue(os.access(entrypoint, os.X_OK))
            self.assertIn(
                "private-access enroll-platform",
                entrypoint.read_text(encoding="utf-8"),
            )

    def test_gateway_unit_uses_stable_current_path_and_is_enabled_on_boot(self) -> None:
        unit = (
            _REPO_ROOT / "tiny_runtime" / "systemd" / "tinyhat-runtime-gateway.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/opt/tinyhat/current/bin/tinyhat-runtime gateway run", unit
        )
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("tinyhat-runtime-attestation.service", unit)

    def test_attestation_unit_uses_stable_current_path(self) -> None:
        unit = (
            _REPO_ROOT
            / "tiny_runtime"
            / "systemd"
            / "tinyhat-runtime-attestation.service"
        ).read_text(encoding="utf-8")
        self.assertIn("ExecStart=/opt/tinyhat/current/bin/tinyhat-attest", unit)

    def test_platform_unit_runs_tiny_runtime_loop_not_legacy_supervisor(self) -> None:
        unit = (
            _REPO_ROOT / "tiny_runtime" / "systemd" / "tinyhat-runtime-platform.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/opt/tinyhat/current/bin/tinyhat-runtime platform loop",
            unit,
        )
        self.assertIn("tinyhat-runtime-gateway.service", unit)
        self.assertNotIn("supervisor.py", unit)
        self.assertNotIn("tinyhat-openclaw.service", unit)

    def test_tiny_runtime_tree_does_not_reference_legacy_units(self) -> None:
        violations: list[str] = []
        for path in (_REPO_ROOT / "tiny_runtime").rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".sqlite"}:
                continue
            rel = os.path.relpath(path, _REPO_ROOT)
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "tinyhat-openclaw.service" in text or "/supervisor.py" in text:
                violations.append(rel)
        self.assertEqual(violations, [])

    def test_tiny_runtime_python_does_not_request_gateway_restart(self) -> None:
        violations: list[str] = []
        for path in _tiny_runtime_python_files():
            rel = os.path.relpath(path, _REPO_ROOT)
            text = path.read_text(encoding="utf-8", errors="ignore")
            if '"gateway", "restart"' in text or "'gateway', 'restart'" in text:
                violations.append(rel)
        self.assertEqual(violations, [])

    def test_tiny_runtime_package_has_no_supervisor_modules(self) -> None:
        modules = [
            os.path.relpath(path, _REPO_ROOT)
            for path in (_REPO_ROOT / "tiny_runtime" / "tinyhat_runtime").glob(
                "*supervisor*.py"
            )
        ]
        self.assertEqual(modules, [])


_ADAPTER_RELPATH = os.path.join(
    "tiny_runtime", "tinyhat_runtime", "openclaw_adapter.py"
)
_SUBPROCESS_CALL_NAMES = frozenset(
    {"run", "Popen", "check_output", "check_call", "call"}
)


def _scan_source_for_openclaw_literal(source: str) -> bool:
    # This catches literal command heads. It is a boundary tripwire for this
    # small M1 package, not a general shell-flow proof engine.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if func_name not in _SUBPROCESS_CALL_NAMES and func_name != "runner":
            continue
        if not node.args:
            continue
        first = node.args[0]
        head: str | None = None
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            element = first.elts[0]
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                head = element.value
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            head = first.value.split(" ", 1)[0] if first.value else None
        if head is not None and (head == "openclaw" or head.startswith("openclaw")):
            return True
    return False


def _tiny_runtime_python_files() -> list[Path]:
    package_root = _REPO_ROOT / "tiny_runtime" / "tinyhat_runtime"
    return sorted(path for path in package_root.rglob("*.py") if path.is_file())


class OpenClawAdapterBoundaryTests(unittest.TestCase):
    def test_no_openclaw_access_outside_adapter(self) -> None:
        violations: list[str] = []
        for path in _tiny_runtime_python_files():
            rel = os.path.relpath(path, _REPO_ROOT)
            if rel == _ADAPTER_RELPATH:
                continue
            if _scan_source_for_openclaw_literal(path.read_text(encoding="utf-8")):
                violations.append(rel)
        self.assertEqual(violations, [])

    def test_adapter_itself_reaches_openclaw(self) -> None:
        source = (_REPO_ROOT / _ADAPTER_RELPATH).read_text(encoding="utf-8")
        self.assertTrue(_scan_source_for_openclaw_literal(source))

    def test_scanner_flags_a_violation(self) -> None:
        violating_source = (
            'import subprocess\nsubprocess.run(["openclaw", "gateway", "health"])\n'
        )
        self.assertTrue(_scan_source_for_openclaw_literal(violating_source))

    def test_adapter_uses_injected_runner_for_official_commands(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["openclaw"], 0, stdout='{"status":"healthy"}', stderr=""
            )
        )
        payload = openclaw_adapter.gateway_health(runner=runner)
        self.assertEqual(payload["state"], "healthy")
        self.assertEqual(
            runner.call_args.args[0][:3], ["openclaw", "gateway", "health"]
        )

    def test_gateway_health_accepts_reachable_auth_required_probe(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["openclaw"],
                1,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "type": "gateway_credentials_required",
                            "message": "read-scope health RPC requires identity",
                        },
                        "gateway": {"reachable": True},
                    }
                ),
                stderr="",
            )
        )

        payload = openclaw_adapter.gateway_health(runner=runner)

        self.assertEqual(payload["state"], "healthy")
        self.assertEqual(payload["readiness"], "reachable_auth_required")
        self.assertTrue(payload["gateway"]["gateway"]["reachable"])

    def test_gateway_run_uses_local_token_auth_without_argv_secret(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        with (
            patch.object(
                openclaw_adapter,
                "ensure_gateway_token",
                return_value="local-gateway-token",
            ),
            patch.object(
                openclaw_adapter,
                "openclaw_env",
                return_value={"OPENCLAW_GATEWAY_TOKEN": "local-gateway-token"},
            ),
            patch.object(openclaw_adapter.subprocess, "call", return_value=0) as call,
        ):
            rc = openclaw_adapter.gateway_run()

        self.assertEqual(rc, 0)
        argv = call.call_args.args[0]
        self.assertIn("--auth", argv)
        self.assertIn("token", argv)
        self.assertNotIn("local-gateway-token", argv)
        self.assertEqual(
            call.call_args.kwargs["env"]["OPENCLAW_GATEWAY_TOKEN"],
            "local-gateway-token",
        )

    def test_secret_argv_values_are_redacted_from_command_summary(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        result = openclaw_adapter.AdapterResult(
            command=("openclaw", "secrets", "reload", "--token", "plain-secret"),
            returncode=1,
            stdout="",
            stderr="failed",
        )

        self.assertEqual(
            result.public_summary()["command"],
            ["openclaw", "secrets", "reload", "--token", "[REDACTED]"],
        )

    def test_secrets_reload_uses_backend_gateway_sdk_with_local_token(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "gateway-token"
            token_path.write_text("runtime-token\n", encoding="utf-8")
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "PATH": original_env.get("PATH", ""),
                        "TINYHAT_OPENCLAW_GATEWAY_TOKEN_FILE": str(token_path),
                    }
                )
                importlib.reload(runtime_paths)
                seen: dict[str, object] = {}

                def runner(args: list[str], **kwargs: object):
                    seen["args"] = args
                    seen["env"] = kwargs.get("env")
                    seen["input"] = kwargs.get("input")
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout='{"warningCount":0}',
                        stderr="",
                    )

                payload = openclaw_adapter.secrets_reload(runner=runner)

                self.assertEqual(payload["state"], "ready")
                self.assertEqual(seen["args"], ["node", "--input-type=module"])
                env = seen["env"]
                self.assertIsInstance(env, dict)
                self.assertEqual(env["OPENCLAW_GATEWAY_TOKEN"], "runtime-token")
                self.assertIn("/usr/local/lib/node_modules", env["NODE_PATH"])
                request = json.loads(env["TINYHAT_OPENCLAW_GATEWAY_CALL"])
                self.assertEqual(request["method"], "secrets.reload")
                self.assertEqual(request["scopes"], ["operator.admin"])
                self.assertNotIn("runtime-token", seen["args"])
                self.assertNotIn("runtime-token", str(seen["input"]))
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                importlib.reload(runtime_paths)

    def test_secrets_reload_fails_fast_without_gateway_token(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "PATH": original_env.get("PATH", ""),
                        "TINYHAT_OPENCLAW_GATEWAY_TOKEN_FILE": str(
                            Path(tmp) / "missing-token"
                        ),
                    }
                )
                importlib.reload(runtime_paths)

                runner = Mock()
                payload = openclaw_adapter.secrets_reload(runner=runner)

                self.assertEqual(payload["state"], "failed")
                self.assertIn(
                    "gateway token",
                    payload["detail"]["stderr"],
                )
                runner.assert_not_called()
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                importlib.reload(runtime_paths)

    def test_adapter_spawns_official_device_code_command(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        process = object()
        with (
            patch.object(
                openclaw_adapter,
                "openclaw_env",
                return_value={"HOME": "/tmp/state"},
            ),
            patch.object(
                openclaw_adapter.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
        ):
            result = openclaw_adapter.spawn_models_auth_login_device_code(
                stdin=1,
                stdout=2,
                stderr=3,
            )

        self.assertIs(result, process)
        self.assertEqual(
            popen.call_args.args[0],
            [
                "openclaw",
                "models",
                "auth",
                "login",
                "--provider",
                "openai",
                "--device-code",
            ],
        )
        self.assertEqual(popen.call_args.kwargs["env"], {"HOME": "/tmp/state"})

    def test_rebuild_adapter_commands_use_official_openclaw_surfaces(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        calls: list[list[str]] = []

        def fake_runner(args, **_kwargs):
            argv = list(args)
            calls.append(argv)
            if argv[1:3] == ["backup", "create"]:
                output = Path(argv[argv.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"backup")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"verified": True}),
                    stderr="",
                )
            if argv[1] == "doctor":
                return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
            if argv[1:3] == ["status", "--json"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout='{"state":"ready"}',
                    stderr="",
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as tmp:
            backup = openclaw_adapter.backup_create(
                output_path=Path(tmp) / "backup.tar.gz",
                runner=fake_runner,
            )
            doctor = openclaw_adapter.doctor_repair(runner=fake_runner)
            status = openclaw_adapter.status_json(runner=fake_runner)

        self.assertEqual(backup["state"], "ready")
        self.assertEqual(doctor["state"], "ready")
        self.assertEqual(status["state"], "ready")
        self.assertIn(
            [
                "openclaw",
                "backup",
                "create",
                "--output",
                calls[0][4],
                "--verify",
                "--json",
            ],
            calls,
        )
        self.assertIn(
            ["openclaw", "doctor", "--fix", "--non-interactive", "--yes"],
            calls,
        )
        self.assertIn(["openclaw", "status", "--json"], calls)

    def test_adapter_prefers_bundle_local_openclaw_path(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        env = openclaw_adapter.openclaw_env()
        self.assertTrue(
            env["PATH"].split(os.pathsep)[0].endswith("/vendor/openclaw/bin")
        )
        self.assertTrue(env["OPENCLAW_BUNDLE_DIR"].endswith("/vendor/openclaw"))

    def test_openclaw_paths_follow_dev_runtime_home(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "PATH": original_env.get("PATH", ""),
                        "TINYHAT_DEV_RUNTIME": "1",
                        "TINYHAT_RUNTIME_HOME": tmp,
                    }
                )
                importlib.reload(runtime_paths)

                env = openclaw_adapter.openclaw_env()
                self.assertEqual(env["HOME"], tmp)
                self.assertEqual(env["OPENCLAW_STATE_DIR"], tmp)
                self.assertEqual(
                    env["OPENCLAW_CONFIG_PATH"],
                    str(Path(tmp) / "openclaw" / "openclaw.json"),
                )
                self.assertEqual(
                    runtime_paths.OPENCLAW_GATEWAY_TOKEN_FILE,
                    Path(tmp) / "tinyhat-control" / "openclaw-gateway-token",
                )
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                importlib.reload(runtime_paths)

    def test_openclaw_paths_keep_production_defaults(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update({"PATH": original_env.get("PATH", "")})
            importlib.reload(runtime_paths)

            env = openclaw_adapter.openclaw_env()
            self.assertEqual(env["HOME"], "/var/lib/tinyhat-openclaw")
            self.assertEqual(env["OPENCLAW_STATE_DIR"], "/var/lib/tinyhat-openclaw")
            self.assertEqual(env["OPENCLAW_CONFIG_PATH"], "/etc/openclaw/openclaw.json")
            self.assertEqual(
                runtime_paths.OPENCLAW_SECRETS_PATH,
                Path("/etc/openclaw/tinyhat-secrets.json"),
            )
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            importlib.reload(runtime_paths)

    def test_openclaw_paths_follow_openclaw_env_without_tinyhat_overrides(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "openclaw-state"
            config = Path(tmp) / "openclaw.json"
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "PATH": original_env.get("PATH", ""),
                        "OPENCLAW_STATE_DIR": str(state),
                        "OPENCLAW_CONFIG_PATH": str(config),
                    }
                )
                importlib.reload(runtime_paths)

                env = openclaw_adapter.openclaw_env()
                self.assertEqual(env["OPENCLAW_STATE_DIR"], str(state))
                self.assertEqual(env["HOME"], str(state))
                self.assertEqual(env["OPENCLAW_CONFIG_PATH"], str(config))
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                importlib.reload(runtime_paths)

    def test_openclaw_path_overrides_keep_tinyhat_env_precedence(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            config = Path(tmp) / "config" / "openclaw.json"
            secrets = Path(tmp) / "secrets.json"
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "PATH": original_env.get("PATH", ""),
                        "OPENCLAW_STATE_DIR": "/ignored/openclaw-state",
                        "OPENCLAW_CONFIG_PATH": "/ignored/openclaw.json",
                        "OPENCLAW_SECRETS_PATH": "/ignored/secrets.json",
                        "TINYHAT_OPENCLAW_STATE_DIR": str(state),
                        "TINYHAT_OPENCLAW_CONFIG_PATH": str(config),
                        "TINYHAT_SECRETS_PATH": str(secrets),
                    }
                )
                importlib.reload(runtime_paths)

                env = openclaw_adapter.openclaw_env()
                self.assertEqual(env["OPENCLAW_STATE_DIR"], str(state))
                self.assertEqual(env["HOME"], str(state))
                self.assertEqual(env["OPENCLAW_CONFIG_PATH"], str(config))
                self.assertEqual(runtime_paths.OPENCLAW_SECRETS_PATH, secrets)
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                importlib.reload(runtime_paths)

    def test_gateway_token_file_is_local_only_and_loaded_into_openclaw_env(self) -> None:
        from tinyhat_runtime import openclaw_adapter
        from tinyhat_runtime import paths as runtime_paths

        original_env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "gateway-token"
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "PATH": original_env.get("PATH", ""),
                        "TINYHAT_OPENCLAW_GATEWAY_TOKEN_FILE": str(token_path),
                    }
                )
                importlib.reload(runtime_paths)

                token = openclaw_adapter.ensure_gateway_token()
                env = openclaw_adapter.openclaw_env()

                self.assertEqual(env["OPENCLAW_GATEWAY_TOKEN"], token)
                self.assertEqual(token_path.read_text(encoding="utf-8").strip(), token)
                self.assertEqual(oct(token_path.stat().st_mode & 0o777), "0o600")
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                importlib.reload(runtime_paths)

    def test_adapter_reports_missing_openclaw_without_raising(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        def missing_runner(*_args, **_kwargs):
            raise FileNotFoundError("openclaw")

        payload = openclaw_adapter.adapter_attestation(runner=missing_runner)
        self.assertEqual(payload["schema"], "openclaw_adapter_attestation_v1")
        self.assertEqual(payload["plugin"]["state"], "unavailable")
        self.assertEqual(payload["gateway"]["state"], "unhealthy")
        self.assertEqual(payload["models"]["state"], "unavailable")

    def test_config_patch_apply_mode_omits_json_flag(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        seen: dict[str, object] = {}

        def runner(argv, **kwargs):
            seen["argv"] = argv
            seen["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        result = openclaw_adapter.config_patch(
            {"channels": {"telegram": {"enabled": True}}},
            replace_paths=("channels.telegram",),
            runner=runner,
        )

        self.assertEqual(result["state"], "ready")
        self.assertEqual(
            seen["argv"],
            [
                "openclaw",
                "config",
                "patch",
                "--stdin",
                "--replace-path",
                "channels.telegram",
            ],
        )
        self.assertNotIn("--json", seen["argv"])
        self.assertIn('"channels"', str(seen["input"]))

    def test_config_patch_dry_run_keeps_json_flag(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        seen: dict[str, object] = {}

        def runner(argv, **_kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"state":"would_apply"}',
                stderr="",
            )

        result = openclaw_adapter.config_patch(
            {"channels": {"telegram": {"enabled": True}}},
            dry_run=True,
            runner=runner,
        )

        self.assertEqual(result["state"], "ready")
        self.assertIn("--dry-run", seen["argv"])
        self.assertIn("--json", seen["argv"])
        self.assertEqual(result["patch"], {"state": "would_apply"})

    def test_warm_image_config_patch_owns_stable_gateway_setup(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        patch = openclaw_adapter.warm_image_config_patch(
            platform_base_url="https://platform.example.test",
            backend_audience="https://audience.example.test",
        )

        self.assertEqual(patch["gateway"]["mode"], "local")
        self.assertEqual(patch["gateway"]["auth"], {"mode": "token"})
        self.assertFalse(patch["channels"]["telegram"]["enabled"])
        self.assertEqual(
            patch["secrets"]["providers"]["tinyhat"]["path"],
            str(openclaw_adapter.paths.OPENCLAW_SECRETS_PATH),
        )
        self.assertEqual(
            patch["plugins"]["entries"]["tinyhat"]["config"],
            {
                "platformBaseUrl": "https://platform.example.test",
                "backendAudience": "https://audience.example.test",
            },
        )
        self.assertIn("codex", patch["plugins"]["entries"])
        self.assertIn("tinyhat", patch["plugins"]["entries"])
        self.assertNotIn("commands", patch)
        self.assertNotIn("env", patch)

    def test_platform_warm_config_command_applies_startup_stable_config(self) -> None:
        with patch.object(
            openclaw_adapter,
            "apply_warm_image_config",
            return_value={"state": "ready"},
        ) as apply_warm:
            status = main.main(
                [
                    "platform",
                    "warm-config",
                    "--platform-base-url",
                    "https://platform.example.test",
                    "--backend-audience",
                    "https://audience.example.test",
                ]
            )

        self.assertEqual(status, 0)
        apply_warm.assert_called_once_with(
            platform_base_url="https://platform.example.test",
            backend_audience="https://audience.example.test",
        )

    def test_binding_config_patch_uses_secretrefs_on_hot_paths(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        patch = openclaw_adapter.binding_config_patch(
            {
                "telegram_owner_user_id": "12345",
                "telegram_bot_token": "123:token",
                "openrouter_api_key": "sk-or-v1-child",
                "openrouter_base_url": "https://openrouter.ai/api/v1",
                "openrouter_default_model": "openai/gpt-5.5",
            }
        )

        self.assertEqual(
            patch["agents"]["defaults"]["model"]["primary"],
            "openrouter/openai/gpt-5.5",
        )
        self.assertEqual(
            patch["channels"]["telegram"]["botToken"],
            {
                "source": "file",
                "provider": "tinyhat",
                "id": "/channels/telegram/botToken",
            },
        )
        self.assertEqual(
            patch["models"]["providers"]["openrouter"]["apiKey"],
            {
                "source": "file",
                "provider": "tinyhat",
                "id": "/providers/openrouter/apiKey",
            },
        )
        self.assertEqual(
            patch["channels"]["telegram"]["execApprovals"],
            {"approvers": ["12345"]},
        )
        self.assertEqual(
            patch["plugins"]["entries"]["tinyhat"]["config"],
            {},
        )
        self.assertNotIn("gateway", patch)
        self.assertNotIn("commands", patch)
        self.assertNotIn("env", patch)
        self.assertNotIn("openrouter.ai/api/v1", json.dumps(patch, sort_keys=True))
        self.assertNotIn("123:token", json.dumps(patch, sort_keys=True))
        self.assertNotIn("sk-or-v1-child", json.dumps(patch, sort_keys=True))

    def test_materialize_tinyhat_plugin_installs_missing_public_plugin(
        self,
    ) -> None:
        from tinyhat_runtime import openclaw_adapter

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            calls: list[list[str]] = []
            inspect_count = 0

            def runner(argv, **_kwargs):
                nonlocal inspect_count
                calls.append(list(argv))
                if argv[:4] == ["openclaw", "plugins", "inspect", "tinyhat"]:
                    inspect_count += 1
                    if inspect_count == 1:
                        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout='{"id":"tinyhat","version":"0.5.1"}',
                        stderr="",
                    )
                if argv[:2] == ["git", "clone"]:
                    checkout = Path(argv[-1])
                    (checkout / ".git").mkdir(parents=True)
                    (checkout / "package.json").write_text(
                        '{"version":"0.5.1"}',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if argv[:3] == ["git", "tag", "--sort=-v:refname"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout="v0.5.1\nv0.5.0\n",
                        stderr="",
                    )
                if argv[:4] == ["git", "rev-parse", "--verify", "origin/v0.5.1^{commit}"]:
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
                if argv[:3] == ["git", "rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout="a" * 40 + "\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch.object(openclaw_adapter.paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.materialize_tinyhat_plugin(
                    {
                        "tinyhat_platform": {
                            "plugin": {
                                "id": "tinyhat",
                                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                                "repo_ref": "latest",
                            }
                        }
                    },
                    runner=runner,
                )

            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["action"], "installed")
            self.assertIn(
                [
                    "openclaw",
                    "plugins",
                    "install",
                    str(state_dir / "platform-plugins" / "tinyhat"),
                    "--force",
                ],
                calls,
            )
            marker = json.loads(
                (state_dir / "tinyhat-plugin.version").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["requested_ref"], "latest")
            self.assertEqual(marker["resolved_commit_sha"], "a" * 40)

    def test_materialize_tinyhat_plugin_skips_matching_hot_image_marker(
        self,
    ) -> None:
        from tinyhat_runtime import openclaw_adapter

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "tinyhat-plugin.version").write_text(
                json.dumps(
                    {
                        "schema": "tinyhat_plugin_install_marker_v1",
                        "plugin_id": "tinyhat",
                        "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                        "requested_ref": "v0.5.1",
                        "resolved_commit_sha": "b" * 40,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(list(argv))
                if argv[:4] == ["openclaw", "plugins", "inspect", "tinyhat"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout='{"id":"tinyhat","version":"0.5.1"}',
                        stderr="",
                    )
                raise AssertionError(f"unexpected command: {argv}")

            with patch.object(openclaw_adapter.paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.materialize_tinyhat_plugin(
                    {
                        "tinyhat_platform": {
                            "plugin": {
                                "id": "tinyhat",
                                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                                "repo_ref": "v0.5.1",
                                "resolved_commit_sha": "b" * 40,
                            }
                        }
                    },
                    runner=runner,
                )

            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["action"], "skipped")
            self.assertEqual(
                calls,
                [["openclaw", "plugins", "inspect", "tinyhat", "--json"]],
            )

    def test_apply_binding_config_writes_secrets_and_patches_hot_paths_only(
        self,
    ) -> None:
        from tinyhat_runtime import openclaw_adapter

        with tempfile.TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "tinyhat-secrets.json"
            seen: dict[str, object] = {}

            def runner(argv, **kwargs):
                seen["argv"] = argv
                seen["input"] = kwargs.get("input")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch.object(
                openclaw_adapter.paths,
                "OPENCLAW_SECRETS_PATH",
                secrets_path,
            ), patch.object(
                openclaw_adapter,
                "materialize_tinyhat_plugin",
                return_value={"state": "ready", "action": "skipped"},
            ):
                result = openclaw_adapter.apply_binding_config(
                    {
                        "telegram_owner_user_id": "12345",
                        "telegram_bot_token": "123:token",
                        "openrouter_api_key": "sk-or-v1-child",
                        "openrouter_default_model": "openai/gpt-5.5",
                    },
                    runner=runner,
                )

            self.assertEqual(result["state"], "ready")
            self.assertEqual(
                seen["argv"],
                [
                    "openclaw",
                    "config",
                    "patch",
                    "--stdin",
                    "--replace-path",
                    "channels.telegram",
                ],
            )
            input_text = str(seen["input"])
            self.assertNotIn("commands", input_text)
            self.assertNotIn("gateway", input_text)
            self.assertNotIn("OPENROUTER_API_KEY", input_text)
            self.assertNotIn("123:token", input_text)
            self.assertNotIn("sk-or-v1-child", input_text)
            secrets_payload = json.loads(secrets_path.read_text(encoding="utf-8"))
            self.assertEqual(
                secrets_payload["channels"]["telegram"]["botToken"],
                "123:token",
            )
            self.assertEqual(
                secrets_payload["providers"]["openrouter"]["apiKey"],
                "sk-or-v1-child",
            )

    def test_apply_binding_config_can_replace_secrets_for_cross_owner_rebind(
        self,
    ) -> None:
        from tinyhat_runtime import openclaw_adapter

        with tempfile.TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "tinyhat-secrets.json"
            secrets_path.write_text(
                json.dumps({"EXA_API_KEY": "stale-owner-secret"}, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            def runner(argv, **_kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch.object(
                openclaw_adapter.paths,
                "OPENCLAW_SECRETS_PATH",
                secrets_path,
            ), patch.object(
                openclaw_adapter,
                "materialize_tinyhat_plugin",
                return_value={"state": "ready", "action": "skipped"},
            ):
                result = openclaw_adapter.apply_binding_config(
                    {
                        "telegram_owner_user_id": "67890",
                        "telegram_bot_token": "456:token",
                    },
                    preserve_existing_secrets=False,
                    runner=runner,
                )

            self.assertEqual(result["state"], "ready")
            secrets_payload = json.loads(secrets_path.read_text(encoding="utf-8"))
            self.assertNotIn("EXA_API_KEY", secrets_payload)
            self.assertEqual(
                secrets_payload["channels"]["telegram"]["botToken"],
                "456:token",
            )

    def test_write_openclaw_secrets_preserves_binding_refs_on_user_secret_update(
        self,
    ) -> None:
        from tinyhat_runtime import openclaw_adapter

        with tempfile.TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "tinyhat-secrets.json"
            secrets_path.write_text(
                json.dumps(
                    {
                        "channels": {"telegram": {"botToken": "123:token"}},
                        "providers": {"openrouter": {"apiKey": "sk-or-v1-child"}},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                openclaw_adapter.paths,
                "OPENCLAW_SECRETS_PATH",
                secrets_path,
            ):
                result = openclaw_adapter.write_openclaw_secrets(
                    {"EXA_API_KEY": "exa-test-secret"}
                )

            self.assertEqual(result["state"], "ready")
            merged = json.loads(secrets_path.read_text(encoding="utf-8"))
            self.assertEqual(
                merged["channels"]["telegram"]["botToken"],
                "123:token",
            )
            self.assertEqual(
                merged["providers"]["openrouter"]["apiKey"],
                "sk-or-v1-child",
            )
            self.assertEqual(merged["EXA_API_KEY"], "exa-test-secret")


class BakeScriptTests(unittest.TestCase):
    def test_assemble_bundle_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "bundle"
            completed = subprocess.run(
                [
                    str(_REPO_ROOT / "tiny_runtime" / "bake" / "assemble-bundle.sh"),
                    str(out_dir),
                ],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = bundle.load_manifest(out_dir)
            lock = json.loads(
                (_REPO_ROOT / "tiny_runtime" / "bake" / "bundle.lock").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(bundle.verify_manifest(out_dir, manifest))
            self.assertEqual(
                manifest["components"]["openclaw"]["ref"],
                lock["dependencies"]["openclaw"]["resolved"],
            )
            self.assertEqual(
                manifest["components"]["tinyhat_openclaw_plugin"]["ref"],
                lock["dependencies"]["tinyhat_openclaw_plugin"]["ref"],
            )
            helper = out_dir / "bake" / "preinstall-hot-image-plugins.sh"
            self.assertTrue(helper.exists())
            self.assertTrue(os.access(helper, os.X_OK))
            helper_text = helper.read_text(encoding="utf-8")
            self.assertIn("tinyhat_runtime.main bake preinstall-plugins", helper_text)
            self.assertIn("chown -R 0:0", helper_text)
            self.assertNotIn('chown -R "${runtime_user}:${runtime_group}"', helper_text)
            self.assertNotIn("import supervisor", helper_text)
            self.assertNotIn("ensure_codex_subscription_plugin_installed", helper_text)
            self.assertNotIn("ensure_tinyhat_plugin_installed", helper_text)
            self.assertNotIn("_is_codex_subscription_plugin_available", helper_text)
            self.assertNotIn("json.load(sys.stdin)", helper_text)

    def test_assemble_bundle_uses_explicit_authority_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "bundle"
            env = {
                **os.environ,
                "TINYHAT_OPENCLAW_REF": "openclaw@2026.6.8",
                "TINYHAT_PLUGIN_REF": "0d79c3ca35161c05b4987cff286f85e2c988a29d",
            }
            completed = subprocess.run(
                [
                    str(_REPO_ROOT / "tiny_runtime" / "bake" / "assemble-bundle.sh"),
                    str(out_dir),
                ],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = bundle.load_manifest(out_dir)
            self.assertTrue(bundle.verify_manifest(out_dir, manifest))
            self.assertEqual(
                manifest["components"]["openclaw"]["ref"],
                "openclaw@2026.6.8",
            )
            self.assertEqual(
                manifest["components"]["tinyhat_openclaw_plugin"]["ref"],
                "0d79c3ca35161c05b4987cff286f85e2c988a29d",
            )

    def test_assemble_bundle_copies_openclaw_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package_dir = tmp_path / "node_modules" / "openclaw"
            bin_dir = package_dir / "bin"
            dist_dir = package_dir / "dist"
            bin_dir.mkdir(parents=True)
            dist_dir.mkdir()
            (package_dir / "package.json").write_text(
                json.dumps({"name": "openclaw", "version": "2026.6.9"}),
                encoding="utf-8",
            )
            (bin_dir / "openclaw").write_text(
                "#!/usr/bin/env node\nimport '../dist/entry.mjs';\n",
                encoding="utf-8",
            )
            os.chmod(bin_dir / "openclaw", 0o755)
            (dist_dir / "entry.mjs").write_text(
                "export default {};\n",
                encoding="utf-8",
            )
            out_dir = tmp_path / "bundle"
            env = {
                **os.environ,
                "TINYHAT_OPENCLAW_BIN": str(bin_dir / "openclaw"),
                "TINYHAT_OPENCLAW_REF": "openclaw@2026.6.9",
            }

            completed = subprocess.run(
                [
                    str(_REPO_ROOT / "tiny_runtime" / "bake" / "assemble-bundle.sh"),
                    str(out_dir),
                ],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((out_dir / "vendor" / "openclaw" / "package.json").exists())
            self.assertTrue((out_dir / "vendor" / "openclaw" / "dist" / "entry.mjs").exists())
            self.assertTrue((out_dir / "vendor" / "openclaw" / "bin" / "openclaw").exists())


class GceStartupScriptTests(unittest.TestCase):
    def test_gce_startup_script_is_public_runtime_owner_boundary(self) -> None:
        script = (
            _REPO_ROOT / "tiny_runtime" / "bin" / "tinyhat-gce-startup"
        ).read_text(encoding="utf-8")

        self.assertIn("git clone", script)
        self.assertIn("cache_marker_has runtime_resolved_sha", script)
        self.assertIn("bash \"${RUNTIME_DIR}/bootstrap.sh\"", script)
        self.assertIn("tinyhat-computer-startup", script)
        runtime_entrypoints = "\n".join(
            [
                (_REPO_ROOT / "tiny_runtime" / "tinyhat_runtime" / "main.py").read_text(
                    encoding="utf-8"
                ),
                (
                    _REPO_ROOT
                    / "tiny_runtime"
                    / "tinyhat_runtime"
                    / "runtime_commands.py"
                ).read_text(encoding="utf-8"),
            ]
        )
        self.assertIn(
            "/hapi/v1/computers/me/private-access/enrollment",
            runtime_entrypoints,
        )
        self.assertNotIn("TINYHAT_TAILSCALE_AUTH_KEY", script)
        self.assertNotIn("OPENROUTER_API_KEY", script)
        self.assertNotIn("tinyloop/backend", script)

    def test_gce_startup_script_passes_shell_syntax_check(self) -> None:
        completed = subprocess.run(
            ["bash", "-n", str(_REPO_ROOT / "tiny_runtime" / "bin" / "tinyhat-gce-startup")],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


class BackupRestoreTests(unittest.TestCase):
    """openclaw_adapter.backup_restore — scoped, data-preserving restore."""

    def _make_archive(self, archive_path, state_rel, subtrees):
        import tarfile

        with tempfile.TemporaryDirectory() as staging:
            root = Path(staging) / "2026-01-01T00-00-00.000+00-00-openclaw-backup"
            payload = root / "payload" / "posix" / state_rel
            for name, fname in subtrees.items():
                d = payload / name
                d.mkdir(parents=True, exist_ok=True)
                (d / fname).write_text(f"data:{name}")
            with tarfile.open(archive_path, "w:gz") as tf:
                tf.add(root, arcname=root.name)

    def test_restore_copies_user_dirs_and_reports_complete(self):
        from tinyhat_runtime import paths as runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_dir = tmp / "state"
            state_rel = str(state_dir).lstrip("/")
            archive = tmp / "backup.tar.gz"
            self._make_archive(
                archive,
                state_rel,
                {
                    "identity": "id.json",
                    "state": "openclaw.sqlite",
                    "workspace": "scratch.txt",
                    "secrets": "s.json",
                },
            )
            with patch.object(runtime_paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.backup_restore(input_path=archive)
            self.assertEqual(result["state"], "ready", result)
            for name in ("identity", "state", "workspace", "secrets"):
                self.assertIn(name, result["restored_dirs"])
                self.assertTrue((state_dir / name).is_dir())
            # 'workspace' (singular) is restored — guards the workspaces/workspace typo.
            self.assertTrue((state_dir / "workspace" / "scratch.txt").exists())

    def test_restore_fails_when_required_subtree_missing(self):
        from tinyhat_runtime import paths as runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_dir = tmp / "state"
            state_rel = str(state_dir).lstrip("/")
            archive = tmp / "backup.tar.gz"
            # Archive WITHOUT 'identity' (a required subtree) — a partial restore
            # must report failed, not a bare "ready".
            self._make_archive(
                archive, state_rel, {"state": "openclaw.sqlite", "agents": "a.json"}
            )
            with patch.object(runtime_paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.backup_restore(input_path=archive)
            self.assertEqual(result["state"], "failed", result)
            self.assertEqual(result.get("failure_code"), "restore_incomplete")
            self.assertIn("identity", result["required_missing"])

    def test_restore_tolerates_absolute_symlink_in_archive(self):
        # A real `openclaw backup create` archive contains symlinks with absolute
        # targets (e.g. plugin-skills). Extraction must tolerate them and still
        # restore the user-data dirs — a strict filter="data" would reject the
        # whole archive (regression guard).
        from tinyhat_runtime import paths as runtime_paths
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_dir = tmp / "state"
            state_rel = str(state_dir).lstrip("/")
            archive = tmp / "backup.tar.gz"
            with tempfile.TemporaryDirectory() as staging:
                root = Path(staging) / "2026-01-01T00-00-00.000+00-00-openclaw-backup"
                payload = root / "payload" / "posix" / state_rel
                for name in ("identity", "state"):
                    d = payload / name
                    d.mkdir(parents=True)
                    (d / "f.json").write_text(name)
                # An install dir (not restored) with an absolute-target symlink.
                skills = payload / "plugin-skills"
                skills.mkdir(parents=True)
                os.symlink("/opt/tinyhat/current/skills/browser", skills / "browser")
                with tarfile.open(archive, "w:gz") as tf:
                    tf.add(root, arcname=root.name)
            with patch.object(runtime_paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.backup_restore(input_path=archive)
            self.assertEqual(result["state"], "ready", result)
            self.assertIn("identity", result["restored_dirs"])
            self.assertIn("state", result["restored_dirs"])

    def test_restore_rejects_symlink_escape_under_user_dir(self):
        # An absolute symlink under a RESTORED user-data dir is an escape vector
        # (a later member could be written through it). It must be rejected before
        # extraction so nothing is written outside the extraction dir.
        from tinyhat_runtime import paths as runtime_paths
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_dir = tmp / "state"
            state_rel = str(state_dir).lstrip("/")
            archive = tmp / "backup.tar.gz"
            outside = tmp / "outside"
            outside.mkdir()
            with tempfile.TemporaryDirectory() as staging:
                root = Path(staging) / "2026-01-01T00-00-00.000+00-00-openclaw-backup"
                payload = root / "payload" / "posix" / state_rel
                (payload / "identity").mkdir(parents=True)
                (payload / "identity" / "id.json").write_text("id")
                sd = payload / "state"
                sd.mkdir(parents=True)
                os.symlink(str(outside), sd / "escape")  # absolute-target symlink
                with tarfile.open(archive, "w:gz") as tf:
                    tf.add(root, arcname=root.name)
            with patch.object(runtime_paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.backup_restore(input_path=archive)
            self.assertEqual(result["state"], "failed", result)
            self.assertEqual(list(outside.iterdir()), [])  # nothing escaped

    def test_restore_rejects_non_openclaw_backup_root(self):
        from tinyhat_runtime import paths as runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_dir = tmp / "state"
            archive = tmp / "backup.tar.gz"
            import tarfile

            with tempfile.TemporaryDirectory() as staging:
                stray = Path(staging) / "not-a-backup"
                (stray / "payload").mkdir(parents=True)
                with tarfile.open(archive, "w:gz") as tf:
                    tf.add(stray, arcname=stray.name)
            with patch.object(runtime_paths, "OPENCLAW_STATE_DIR", state_dir):
                result = openclaw_adapter.backup_restore(input_path=archive)
            self.assertEqual(result["state"], "failed", result)


if __name__ == "__main__":
    unittest.main()
