"""Proof-gate tests for the greenfield tiny_runtime tree.

Usage:
    python -m unittest tests.test_tiny_runtime -v
"""

from __future__ import annotations

import ast
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
from unittest.mock import Mock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tiny_runtime"))

from tinyhat_runtime import RUNTIME_GENERATION  # noqa: E402
from tinyhat_runtime import attestation, bundle, hot_image, launcher, openclaw_adapter  # noqa: E402
from tinyhat_runtime.command_ledger import CommandLedger  # noqa: E402
from tinyhat_runtime.platform_client import PlatformClient  # noqa: E402
from tinyhat_runtime.runtime_commands import RuntimeCommandRunner  # noqa: E402


def _write_minimal_bundle(root: Path, *, marker: str = "") -> dict:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tinyhat-runtime").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "tinyhat_runtime").mkdir()
    (root / "tinyhat_runtime" / "__init__.py").write_text("", encoding="utf-8")
    if marker:
        (root / "bundle-marker.txt").write_text(marker, encoding="utf-8")
    return bundle.write_manifest(
        root,
        components={
            "runtime": {"repo": "public", "ref": "abc123"},
            "openclaw": {"package": "openclaw", "ref": "openclaw@2026.6.8"},
            "tinyhat_openclaw_plugin": {"repo": "public", "ref": "9e564878f6057a6c66fa2047b265caa3389314e2"},
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

            (root / "bin" / "tinyhat-runtime").write_text("# changed\n", encoding="utf-8")
            with self.assertRaises(bundle.BundleVerificationError):
                bundle.verify_manifest(root, manifest)

    def test_bundle_lock_uses_pinned_public_refs(self) -> None:
        payload = json.loads(
            (_REPO_ROOT / "tiny_runtime" / "bake" / "bundle.lock").read_text(encoding="utf-8")
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
            "--plugin-ref 9e564878f6057a6c66fa2047b265caa3389314e2",
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
            self.assertEqual(events.read_text(encoding="utf-8"), "stop\nstart\nhealth\n")

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
            )

            self.assertFalse(result.activated)
            self.assertTrue(result.rolled_back)
            self.assertEqual(os.readlink(current), str(previous))
            self.assertEqual(
                events.read_text(encoding="utf-8"),
                "stop\nstart\nhealth\nstop\nstart\n",
            )


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
                result={"output_path": "/var/log/tinyhat/diagnostics/cmd-duplicate.zip"},
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

    def test_apply_config_command_rejects_invalid_spec_values(self) -> None:
        cases = [
            ({}, "desired_config_revision is required"),
            ({"desired_config_revision": -1}, "desired_config_revision must be non-negative"),
            ({"desired_config_revision": True}, "desired_config_revision must be an integer"),
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

    def test_duplicate_link_chatgpt_command_does_not_respawn_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            starts: list[dict] = []

            runner = RuntimeCommandRunner(
                ledger=CommandLedger(root=base / "commands"),
                bundles_dir=base / "bundles",
                current_link=base / "current",
                diagnostics_dir=base / "diagnostics",
                start_chatgpt_link=lambda spec: starts.append(spec)
                or {"state": "started"},
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
    def test_poll_failure_is_reported_without_exiting_loop(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = (
            lambda path, payload: posts.append((path, payload)) or {}
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
            payload for path, payload in posts if path == "/hapi/v1/computers/me/runtime-state"
        ]
        self.assertEqual(len(runtime_state_payloads), 1)
        self.assertEqual(runtime_state_payloads[0]["runtime_health"], "degraded_control_plane")
        self.assertTrue(runtime_state_payloads[0]["binding_poll_failed"])
        self.assertEqual(
            runtime_state_payloads[0]["openclaw_control"],
            "no_restart_requested",
        )

    def test_unassigned_state_report_failure_does_not_exit_loop(self) -> None:
        from tinyhat_runtime import platform_loop

        client = Mock()
        client.post_json.side_effect = RuntimeError("platform unavailable")
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)

        def unassigned_once() -> dict:
            loop.stop_requested = True
            return {"assigned": False}

        with (
            patch.object(loop, "_report_ready"),
            patch.object(loop, "_poll_binding", side_effect=unassigned_once),
            patch.object(platform_loop.time, "sleep"),
        ):
            self.assertEqual(loop.run_forever(), 0)

        client.post_json.assert_called_once()

    def test_binding_activation_does_not_restart_openclaw_gateway(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = lambda path, payload: posts.append((path, payload)) or {}
        loop = platform_loop.TinyRuntimePlatformLoop(client=client)
        binding = {
            "telegram_owner_user_id": "123456",
            "telegram_bot_user_id": "654321",
            "telegram_bot_token": "token=secret-value",
        }
        now = time.monotonic()

        with (
            patch.dict(os.environ, {"TINYHAT_PLATFORM_BASE_URL": "https://platform.example"}),
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
        gateway_health.assert_called_once()
        self.assertFalse(hasattr(platform_loop.openclaw_adapter, "gateway_restart_once"))
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

    def test_binding_activation_failure_reports_supported_runtime_state(self) -> None:
        from tinyhat_runtime import platform_loop

        posts: list[tuple[str, dict]] = []
        client = Mock()
        client.post_json.side_effect = lambda path, payload: posts.append((path, payload)) or {}
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
                    zf.writestr("health/gateway.json", json.dumps({"status": "healthy"}))
                    zf.writestr("stability/latest.json", json.dumps({"status": "stable"}))
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"output": str(out), "token": "secret-token-value"}),
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

    def test_gateway_unit_uses_stable_current_path_and_is_enabled_on_boot(self) -> None:
        unit = (_REPO_ROOT / "tiny_runtime" / "systemd" / "tinyhat-runtime-gateway.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("ExecStart=/opt/tinyhat/current/bin/tinyhat-runtime gateway run", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_attestation_unit_uses_stable_current_path(self) -> None:
        unit = (
            _REPO_ROOT / "tiny_runtime" / "systemd" / "tinyhat-runtime-attestation.service"
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


_ADAPTER_RELPATH = os.path.join("tiny_runtime", "tinyhat_runtime", "openclaw_adapter.py")
_SUBPROCESS_CALL_NAMES = frozenset({"run", "Popen", "check_output", "check_call", "call"})


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
            "import subprocess\n"
            'subprocess.run(["openclaw", "gateway", "health"])\n'
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
        self.assertEqual(runner.call_args.args[0][:3], ["openclaw", "gateway", "health"])

    def test_adapter_prefers_bundle_local_openclaw_path(self) -> None:
        from tinyhat_runtime import openclaw_adapter

        env = openclaw_adapter.openclaw_env()
        self.assertTrue(env["PATH"].split(os.pathsep)[0].endswith("/vendor/openclaw/bin"))
        self.assertTrue(env["OPENCLAW_BUNDLE_DIR"].endswith("/vendor/openclaw"))

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


class BakeScriptTests(unittest.TestCase):
    def test_assemble_bundle_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "bundle"
            completed = subprocess.run(
                [str(_REPO_ROOT / "tiny_runtime" / "bake" / "assemble-bundle.sh"), str(out_dir)],
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


if __name__ == "__main__":
    unittest.main()
