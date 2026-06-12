"""v0.12.0 M3 — declared-vs-registered capability verification.

The plugin's manifest declares its tools, skills, and supported
framework range; after gateway start the runtime verifies the
declaration against the framework registry (``mechanism: "inspect"``,
the M0-ratified primary) or, when the registry cannot be asked, the
load beacon (``mechanism: "self_check"`` — never inventing missing
names). Any shortfall maps to degraded health and an honest
``last_error_category`` — never silent ``healthy``.

Usage:
    python -m unittest tests.test_capability_check -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import supervisor

DECLARED_TOOLS = [f"tinyhat_tool_{index}" for index in range(12)]
DECLARED_SKILLS = ["alpha", "beta", "gamma"]


def _write_manifest(
    plugin_dir: str,
    *,
    tools=None,
    skills=None,
    framework=None,
    skill_dirs=None,
) -> None:
    os.makedirs(plugin_dir, exist_ok=True)
    contracts: dict = {}
    if tools is not None:
        contracts["tools"] = tools
    if skills is not None:
        contracts["skills"] = skills
    if framework is not None:
        contracts["framework"] = framework
    manifest = {"id": "tinyhat", "skills": ["skills"], "contracts": contracts}
    with open(
        os.path.join(plugin_dir, "openclaw.plugin.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(manifest, fh)
    for name in skill_dirs if skill_dirs is not None else (skills or []):
        skill_dir = os.path.join(plugin_dir, "skills", name)
        os.makedirs(skill_dir, exist_ok=True)
        skill_md = os.path.join(skill_dir, "SKILL.md")
        with open(skill_md, "w", encoding="utf-8") as fh:
            fh.write("# skill\n")
        # Deterministic modes regardless of the runner's umask: the
        # workload-readability tests flip these on purpose.
        os.chmod(skill_dir, 0o755)
        os.chmod(skill_md, 0o644)


def _registry_entry(tool_names) -> dict:
    return {"id": "tinyhat", "status": "loaded", "toolNames": list(tool_names)}


class CapabilityVerificationTests(unittest.TestCase):
    """The §7 capabilities block: counts, missing names, per-mechanism status."""

    def setUp(self) -> None:
        supervisor._reset_capability_verification_cache()
        self.addCleanup(supervisor._reset_capability_verification_cache)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_dir = self.tmpdir.name
        self.extension_dir = os.path.join(self.state_dir, "extensions", "tinyhat")
        env = {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": self.state_dir,
        }
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        state_patch = patch.object(
            supervisor, "openclaw_state_dir", return_value=self.state_dir
        )
        state_patch.start()
        self.addCleanup(state_patch.stop)
        version_patch = patch.object(
            supervisor, "_read_openclaw_framework_version", return_value="2026.6.5"
        )
        version_patch.start()
        self.addCleanup(version_patch.stop)

    def _write_marker(self, version: str = "0.5.0") -> None:
        with open(
            os.path.join(self.state_dir, "tinyhat-plugin.version"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump({"version": version}, fh)

    def _write_beacon(self, version: str, *, declared_tools=None) -> None:
        payload: dict = {"plugin": "tinyhat", "version": version}
        if declared_tools is not None:
            payload["declared"] = {"tools": declared_tools}
        with open(
            os.path.join(self.state_dir, supervisor.TINYHAT_PLUGIN_BEACON_FILENAME),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(payload, fh)

    # ── no plugin / pre-manifest ──────────────────────────────────────

    def test_no_manifest_anywhere_means_no_block(self) -> None:
        capabilities, framework = supervisor.capability_verification(now=1000)
        self.assertIsNone(capabilities)
        self.assertIsNone(framework)

    def test_manifest_with_no_declared_surface_is_unverifiable(self) -> None:
        _write_manifest(self.extension_dir)
        capabilities, _framework = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "unverifiable")
        self.assertEqual(capabilities["mechanism"], "self_check")
        self.assertEqual(capabilities["declared_tools"], 0)
        self.assertEqual(capabilities["missing"], [])

    # ── mechanism: inspect ────────────────────────────────────────────

    def test_inspect_full_registration_is_ok(self) -> None:
        _write_manifest(
            self.extension_dir, tools=DECLARED_TOOLS, skills=DECLARED_SKILLS
        )
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry(DECLARED_TOOLS), None),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "ok")
        self.assertEqual(capabilities["mechanism"], "inspect")
        self.assertEqual(capabilities["declared_tools"], 12)
        self.assertEqual(capabilities["registered_tools"], 12)
        self.assertEqual(capabilities["declared_skills"], 3)
        self.assertEqual(capabilities["mounted_skills"], 3)
        self.assertEqual(capabilities["missing"], [])
        self.assertFalse(capabilities["missing_truncated"])
        self.assertEqual(capabilities["checked_at_unix"], 1000)

    def test_inspect_partial_registration_names_the_missing_tools(self) -> None:
        _write_manifest(
            self.extension_dir, tools=DECLARED_TOOLS, skills=DECLARED_SKILLS
        )
        registered = DECLARED_TOOLS[:9]
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry(registered), None),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "shortfall")
        self.assertEqual(capabilities["registered_tools"], 9)
        self.assertEqual(capabilities["missing"], DECLARED_TOOLS[9:])

    def test_inspect_not_registered_is_a_shortfall_with_all_names(self) -> None:
        _write_manifest(self.extension_dir, tools=DECLARED_TOOLS[:3], skills=[])
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(None, "not_registered"),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "shortfall")
        self.assertEqual(capabilities["mechanism"], "inspect")
        self.assertEqual(capabilities["registered_tools"], 0)
        self.assertEqual(capabilities["missing"], DECLARED_TOOLS[:3])

    def test_missing_list_caps_at_ten_names_and_flags_truncation(self) -> None:
        _write_manifest(
            self.extension_dir, tools=DECLARED_TOOLS, skills=DECLARED_SKILLS
        )
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry([]), None),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        # 12 missing tools + 0 missing skills → capped at 10 + flag.
        self.assertEqual(len(capabilities["missing"]), 10)
        self.assertTrue(capabilities["missing_truncated"])
        self.assertEqual(capabilities["missing"], DECLARED_TOOLS[:10])

    def test_unreadable_skill_counts_as_unmounted_and_is_named(self) -> None:
        _write_manifest(
            self.extension_dir, tools=DECLARED_TOOLS, skills=DECLARED_SKILLS
        )
        # The workload user can read nothing root-owned: simulate by
        # making the permission math answer "no" for one skill dir.
        broken = os.path.join(self.extension_dir, "skills", "beta")
        os.chmod(broken, 0o700)
        with (
            patch.object(
                supervisor, "_runtime_ownership_ids", return_value=(4242, 4243)
            ),
            patch.object(
                supervisor,
                "openclaw_plugin_registry_entry",
                return_value=(_registry_entry(DECLARED_TOOLS), None),
            ),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        # uid 4242 owns nothing here, the dirs are uid-501-owned: with
        # 0o700 on beta the workload user cannot traverse it.
        self.assertEqual(capabilities["status"], "shortfall")
        self.assertIn("skill:beta", capabilities["missing"])
        self.assertLess(capabilities["mounted_skills"], 3)

    # ── mechanism: self_check (registry unavailable) ──────────────────

    def test_cli_unavailable_with_fresh_covering_beacon_is_ok(self) -> None:
        _write_manifest(
            self.extension_dir, tools=DECLARED_TOOLS, skills=DECLARED_SKILLS
        )
        self._write_marker("0.5.0")
        self._write_beacon("0.5.0", declared_tools=DECLARED_TOOLS)
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(None, "cli_unavailable"),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["mechanism"], "self_check")
        self.assertEqual(capabilities["status"], "ok")
        self.assertEqual(capabilities["registered_tools"], 12)

    def test_cli_unavailable_with_no_beacon_reports_counts_not_names(self) -> None:
        """The fallback never invents missing names."""
        _write_manifest(self.extension_dir, tools=DECLARED_TOOLS, skills=[])
        self._write_marker("0.5.0")
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(None, "cli_unavailable"),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["mechanism"], "self_check")
        self.assertEqual(capabilities["status"], "shortfall")
        self.assertEqual(capabilities["registered_tools"], 0)
        self.assertEqual(capabilities["missing"], [])

    def test_cli_unavailable_with_stale_beacon_is_a_shortfall(self) -> None:
        _write_manifest(self.extension_dir, tools=DECLARED_TOOLS, skills=[])
        self._write_marker("0.6.0")
        self._write_beacon("0.5.0")  # pre-update load
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(None, "cli_unavailable"),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "shortfall")
        self.assertEqual(capabilities["missing"], [])

    def test_old_beacon_without_declared_listing_covers_by_version(self) -> None:
        _write_manifest(self.extension_dir, tools=DECLARED_TOOLS, skills=[])
        self._write_marker("0.5.0")
        self._write_beacon("0.5.0")  # no declared block (older beacon)
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(None, "cli_unavailable"),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "ok")
        self.assertEqual(capabilities["registered_tools"], 12)

    # ── framework range ───────────────────────────────────────────────

    def test_framework_in_range(self) -> None:
        _write_manifest(
            self.extension_dir,
            tools=DECLARED_TOOLS,
            skills=[],
            framework={"name": "openclaw", "minimum": "2026.6.1"},
        )
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry(DECLARED_TOOLS), None),
        ):
            _, framework = supervisor.capability_verification(now=1000)
        self.assertEqual(framework["framework_installed"], "2026.6.5")
        self.assertIs(framework["framework_in_range"], True)

    def test_framework_below_minimum_is_out_of_range(self) -> None:
        _write_manifest(
            self.extension_dir,
            tools=DECLARED_TOOLS,
            skills=[],
            framework={"name": "openclaw", "minimum": "2026.6.6"},
        )
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry(DECLARED_TOOLS), None),
        ):
            _, framework = supervisor.capability_verification(now=1000)
        self.assertIs(framework["framework_in_range"], False)

    def test_unparseable_installed_version_is_unknown_not_violation(self) -> None:
        _write_manifest(
            self.extension_dir,
            tools=DECLARED_TOOLS,
            skills=[],
            framework={"name": "openclaw", "minimum": "2026.6.1"},
        )
        with (
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value=""
            ),
            patch.object(
                supervisor,
                "openclaw_plugin_registry_entry",
                return_value=(_registry_entry(DECLARED_TOOLS), None),
            ),
        ):
            _, framework = supervisor.capability_verification(now=1000)
        self.assertIsNone(framework["framework_in_range"])

    # ── manifest source fallback ──────────────────────────────────────

    def test_checkout_manifest_stands_in_for_missing_extension_copy(self) -> None:
        checkout = supervisor.tinyhat_plugin_checkout_dir()
        _write_manifest(checkout, tools=DECLARED_TOOLS[:2], skills=[])
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry(DECLARED_TOOLS[:2]), None),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["status"], "ok")
        self.assertEqual(capabilities["declared_tools"], 2)

    def test_pre_contracts_skills_manifest_enumerates_disk(self) -> None:
        """Older manifests declare no contracts.skills; the shipped
        skill directories stand in as the declaration."""
        _write_manifest(
            self.extension_dir,
            tools=DECLARED_TOOLS,
            skills=None,
            skill_dirs=["alpha", "beta"],
        )
        with patch.object(
            supervisor,
            "openclaw_plugin_registry_entry",
            return_value=(_registry_entry(DECLARED_TOOLS), None),
        ):
            capabilities, _ = supervisor.capability_verification(now=1000)
        self.assertEqual(capabilities["declared_skills"], 2)
        self.assertEqual(capabilities["mounted_skills"], 2)
        self.assertEqual(capabilities["status"], "ok")

    # ── cache behaviour ───────────────────────────────────────────────

    def test_cache_reuses_within_ttl_and_invalidates_on_gateway_start(self) -> None:
        _write_manifest(self.extension_dir, tools=DECLARED_TOOLS[:1], skills=[])
        self._write_marker("0.5.0")
        calls: list[int] = []

        def fake_verification(*, now: int):
            calls.append(now)
            return (
                {"status": "ok", "checked_at_unix": now},
                None,
            )

        with (
            patch.object(
                supervisor, "capability_verification", side_effect=fake_verification
            ),
            patch.object(supervisor, "_lifecycle_marks", {}, create=True),
        ):
            supervisor.capability_verification_cached(now=1000)
            supervisor.capability_verification_cached(now=1100)
            self.assertEqual(calls, [1000])
            # Gateway became ready after the last check → re-verify.
            supervisor._lifecycle_marks["gateway_ready_at_unix"] = 1150
            supervisor.capability_verification_cached(now=1200)
            self.assertEqual(calls, [1000, 1200])
            # TTL lapse → re-verify.
            supervisor.capability_verification_cached(
                now=1200 + supervisor.CAPABILITY_VERIFICATION_TTL_SECONDS + 1
            )
            self.assertEqual(len(calls), 3)


class CapabilityDemotionTests(unittest.TestCase):
    """The shared healthy-demotion rule — §7 health mapping."""

    def test_only_healthy_is_ever_demoted(self) -> None:
        self.assertIsNone(
            supervisor.capability_demotion(
                "degraded_workload",
                {"load_check": "not_loaded"},
                {"status": "shortfall", "declared_tools": 1, "registered_tools": 0},
                {"framework_in_range": False},
            )
        )

    def test_plugin_not_loaded_maps_to_degraded_workload(self) -> None:
        health, category, detail = supervisor.capability_demotion(
            "healthy", {"load_check": "not_loaded"}, None, None
        )
        self.assertEqual(health, "degraded_workload")
        self.assertEqual(category, "plugin_not_loaded")
        self.assertIn("not loaded", detail)

    def test_zero_registration_maps_to_plugin_not_loaded(self) -> None:
        health, category, _ = supervisor.capability_demotion(
            "healthy",
            {"load_check": "loaded"},
            {"status": "shortfall", "declared_tools": 12, "registered_tools": 0},
            None,
        )
        self.assertEqual(health, "degraded_workload")
        self.assertEqual(category, "plugin_not_loaded")

    def test_partial_shortfall_maps_to_capability_shortfall(self) -> None:
        health, category, detail = supervisor.capability_demotion(
            "healthy",
            {"load_check": "loaded"},
            {
                "status": "shortfall",
                "declared_tools": 12,
                "registered_tools": 9,
                "declared_skills": 8,
                "mounted_skills": 8,
            },
            None,
        )
        self.assertEqual(health, "degraded_workload")
        self.assertEqual(category, "capability_shortfall")
        self.assertIn("9/12", detail)

    def test_framework_violation_wins_and_uses_the_true_enum(self) -> None:
        health, category, detail = supervisor.capability_demotion(
            "healthy",
            {"load_check": "not_loaded"},
            {"status": "shortfall", "declared_tools": 1, "registered_tools": 0},
            {
                "framework_in_range": False,
                "framework_installed": "2026.5.0",
                "framework_minimum": "2026.6.1",
            },
        )
        self.assertEqual(health, "unsupported_openclaw_version")
        self.assertEqual(category, "unsupported_openclaw_version")
        self.assertIn("2026.5.0", detail)

    def test_ok_capabilities_never_demote(self) -> None:
        self.assertIsNone(
            supervisor.capability_demotion(
                "healthy",
                {"load_check": "loaded"},
                {"status": "ok"},
                {"framework_in_range": True},
            )
        )
        self.assertIsNone(
            supervisor.capability_demotion(
                "healthy",
                None,
                {"status": "unverifiable", "declared_tools": 0},
                None,
            )
        )


class WriteRuntimeStateCapabilitiesTests(unittest.TestCase):
    """The daemon write path folds the capabilities block + demotion."""

    def setUp(self) -> None:
        supervisor._reset_capability_verification_cache()
        self.addCleanup(supervisor._reset_capability_verification_cache)

    def _env(self, tmpdir: str) -> dict[str, str]:
        return {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": tmpdir,
            supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: os.path.join(
                tmpdir, "runtime-state.json"
            ),
            supervisor.TINYHAT_COMPUTER_ID_ENV: "cmp_test_m3",
            supervisor.TINYHAT_GCE_INSTANCE_ID_ENV: "instance-m3",
            supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "",
        }

    def test_write_path_folds_capabilities_and_demotes_shortfall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_manifest(
                os.path.join(tmpdir, "extensions", "tinyhat"),
                tools=DECLARED_TOOLS,
                skills=[],
            )
            with (
                patch.dict(os.environ, self._env(tmpdir), clear=False),
                patch.object(supervisor, "get_backend_base_url", return_value=""),
                patch.object(
                    supervisor, "openclaw_state_dir", return_value=tmpdir
                ),
                patch.object(
                    supervisor, "_read_runtime_repo_version", return_value="0.12.0"
                ),
                patch.object(
                    supervisor, "_read_runtime_git_sha", return_value="c" * 40
                ),
                patch.object(
                    supervisor,
                    "_read_openclaw_framework_version",
                    return_value="2026.6.5",
                ),
                patch.object(
                    supervisor,
                    "openclaw_plugin_registry_entry",
                    return_value=(_registry_entry(DECLARED_TOOLS[:9]), None),
                ),
            ):
                supervisor._write_runtime_state("healthy", "ok", gateway_active=True)
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "degraded_workload")
            self.assertEqual(payload["last_error"]["category"], "capability_shortfall")
            self.assertEqual(payload["capabilities"]["status"], "shortfall")
            self.assertEqual(payload["capabilities"]["mechanism"], "inspect")
            self.assertEqual(payload["capabilities"]["missing"], DECLARED_TOOLS[9:])
            self.assertIn("9/12", payload["detail"])

    def test_write_path_stays_healthy_when_capabilities_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_manifest(
                os.path.join(tmpdir, "extensions", "tinyhat"),
                tools=DECLARED_TOOLS,
                skills=[],
                framework={"name": "openclaw", "minimum": "2026.6.1"},
            )
            with (
                patch.dict(os.environ, self._env(tmpdir), clear=False),
                patch.object(supervisor, "get_backend_base_url", return_value=""),
                patch.object(
                    supervisor, "openclaw_state_dir", return_value=tmpdir
                ),
                patch.object(
                    supervisor, "_read_runtime_repo_version", return_value="0.12.0"
                ),
                patch.object(
                    supervisor, "_read_runtime_git_sha", return_value="c" * 40
                ),
                patch.object(
                    supervisor,
                    "_read_openclaw_framework_version",
                    return_value="2026.6.5",
                ),
                patch.object(
                    supervisor,
                    "openclaw_plugin_registry_entry",
                    return_value=(_registry_entry(DECLARED_TOOLS), None),
                ),
            ):
                supervisor._write_runtime_state("healthy", "ok", gateway_active=True)
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertEqual(payload["capabilities"]["status"], "ok")
            self.assertIsNone(payload["last_error"])


class UnitCategoryAllowlistTests(unittest.TestCase):
    """Every unit module declares one of the seven mechanism categories.

    The registry already refuses commands outside the closed set; this
    guard extends the no-product-verbs invariant to the unit layer so
    CI rejects an uncategorized (or product-categorized) unit module.
    """

    UNITS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tinyhat_cli",
        "units",
    )

    @staticmethod
    def _declared_category(source: str) -> str | None:
        import re

        match = re.search(
            r'^UNIT_CATEGORY = "([^"]+)"$', source, flags=re.MULTILINE
        )
        return match.group(1) if match else None

    def _unit_files(self) -> list[str]:
        return sorted(
            name
            for name in os.listdir(self.UNITS_DIR)
            if name.endswith(".py") and name != "__init__.py"
        )

    def test_every_unit_declares_an_allowed_category(self) -> None:
        from tinyhat_cli.registry import ALLOWED_UNIT_CATEGORIES

        problems: list[str] = []
        for name in self._unit_files():
            with open(os.path.join(self.UNITS_DIR, name), encoding="utf-8") as fh:
                category = self._declared_category(fh.read())
            if category is None:
                problems.append(f"{name}: no UNIT_CATEGORY declared")
            elif category not in ALLOWED_UNIT_CATEGORIES:
                problems.append(
                    f"{name}: category {category!r} outside the closed set"
                )
        self.assertEqual(
            problems,
            [],
            "unit-category allowlist violations:\n  " + "\n  ".join(problems),
        )

    def test_units_exist_to_guard(self) -> None:
        """Sanity: an empty walk would make the guard vacuous."""
        self.assertGreaterEqual(len(self._unit_files()), 10)

    def test_scanner_flags_a_violation(self) -> None:
        """Deliberate red fixtures: prove the guard CAN fail."""
        from tinyhat_cli.registry import ALLOWED_UNIT_CATEGORIES

        product_module = 'UNIT_CATEGORY = "product"\n'
        category = self._declared_category(product_module)
        self.assertEqual(category, "product")
        self.assertNotIn(category, ALLOWED_UNIT_CATEGORIES)

        undeclared_module = "x = 1\n"
        self.assertIsNone(self._declared_category(undeclared_module))


if __name__ == "__main__":
    unittest.main()
