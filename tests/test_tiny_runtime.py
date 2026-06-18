"""Proof-gate tests for the greenfield tiny_runtime tree.

Usage:
    python -m unittest tests.test_tiny_runtime -v
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tiny_runtime"))

from tinyhat_runtime import RUNTIME_GENERATION  # noqa: E402
from tinyhat_runtime import attestation, bundle, launcher  # noqa: E402
from tinyhat_runtime.platform_client import PlatformClient  # noqa: E402


def _write_minimal_bundle(root: Path) -> dict:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tinyhat-runtime").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "tinyhat_runtime").mkdir()
    (root / "tinyhat_runtime" / "__init__.py").write_text("", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
