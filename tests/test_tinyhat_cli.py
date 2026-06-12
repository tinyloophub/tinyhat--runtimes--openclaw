"""Tests for the tinyhat_cli package (v0.12.0 M1).

Covers the milestone proof gates that run in CI:

- ``state-payload-budget`` — the synthetic worst-case fixture (all log
  sources at 15 × 1,024-char lines, a full commands ring, a full
  capabilities.missing list) trims deterministically to <= 12,288
  bytes, in the contract order;
- ``redaction-sentinel`` (diagnose leg) — the seeded corpus (incl.
  Telegram bot token + identity JWT shapes) leaks zero times through
  the sanitizer and through real CLI outputs, with one deliberately
  failing pattern proving the harness can fail;
- unit behaviour: manifest staleness states + the divergence fixture,
  snapshot freshness fields, the root-only entrypoint gate, schema
  validation of all five command outputs, and the gateway-restart
  skeleton's terminal classification (which also proves the facade
  keeps ``patch.object(supervisor, ...)`` working in extracted code).

Usage:
    python -m unittest tests.test_tinyhat_cli -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import supervisor
from tinyhat_cli import entrypoint, schemas
from tinyhat_cli.units import gateway_restart, manifest, redaction, runtime_state, snapshot

_AMBIENT_ENV: dict[str, str | None] = {}
_AMBIENT_ENV_KEYS = (
    "TINYHAT_PLATFORM_BASE_URL",
    supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV,
)


def setUpModule() -> None:
    for key in _AMBIENT_ENV_KEYS:
        _AMBIENT_ENV[key] = os.environ.get(key)
    os.environ.pop("TINYHAT_PLATFORM_BASE_URL", None)
    os.environ[supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV] = "0"


def tearDownModule() -> None:
    for key, value in _AMBIENT_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ── state-payload-budget ─────────────────────────────────────────────


def _worst_case_payload() -> dict:
    """All three log sources maxed, full commands ring, full missing list."""
    line = "x" * supervisor.RUNTIME_STATE_LOG_LINE_MAX_CHARS
    entries = [
        {"text": f"{index:04d} {line}"[: supervisor.RUNTIME_STATE_LOG_LINE_MAX_CHARS]}
        for index in range(supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES)
    ]
    commands = [
        {
            "name": "gateway restart",
            "class": "operate",
            "outcome": "succeeded",
            "started_at_unix": 1760000000 + index,
            "finished_at_unix": 1760000100 + index,
            "idempotency_key": f"key-{index}-{'k' * 32}",
            "summary": "s" * 256,
        }
        for index in range(5)
    ]
    return {
        "schema": "runtime_state_v1",
        "schema_version": 1,
        "computer_id": "5066",
        "instance_id": "1234567890123456789",
        "runtime_ref": "0.11.14@427769f73779",
        "observed_at": "2026-06-12T11:00:00Z",
        "runtime_health": "healthy",
        "runtime_state": "healthy",
        "state": "healthy",
        "detail": "d" * 512,
        "updated_at_unix": 1760000000,
        "supervisor": {"version": "0.11.14", "status": "healthy", "journal": [dict(e) for e in entries]},
        "gateway": {"unit": "g.service", "status": "healthy", "journal": [dict(e) for e in entries]},
        "bootstrap": {"log_excerpt_lines": [dict(e) for e in entries]},
        "commands": commands,
        "capabilities": {
            "declared_tools": 12,
            "registered_tools": 0,
            "declared_skills": 8,
            "mounted_skills": 0,
            "missing": [f"tinyhat_tool_with_a_long_name_{index:02d}" for index in range(10)],
            "missing_truncated": False,
            "checked_at_unix": 1760000000,
            "mechanism": "inspect",
            "status": "shortfall",
        },
        "runtime_events": [
            {"type": "gateway_restart", "at": "2026-06-12T10:00:00Z", "detail": "e" * 256}
            for _ in range(8)
        ],
    }


class StatePayloadBudgetTests(unittest.TestCase):
    def test_worst_case_fixture_trims_to_budget(self) -> None:
        payload = _worst_case_payload()
        original_size = runtime_state._runtime_state_payload_bytes(payload)
        self.assertGreater(
            original_size,
            supervisor.RUNTIME_STATE_PLATFORM_POST_MAX_BYTES,
            "fixture is not worst-case — it already fits the budget",
        )
        trimmed = supervisor.budget_runtime_state_payload(payload)
        self.assertLessEqual(
            runtime_state._runtime_state_payload_bytes(trimmed),
            supervisor.RUNTIME_STATE_PLATFORM_POST_MAX_BYTES,
        )
        self.assertTrue(trimmed.get("payload_trimmed"))
        # The input payload (the local state file's content) is untouched.
        self.assertEqual(
            runtime_state._runtime_state_payload_bytes(payload), original_size
        )

    def test_trim_is_deterministic(self) -> None:
        first = supervisor.budget_runtime_state_payload(_worst_case_payload())
        second = supervisor.budget_runtime_state_payload(_worst_case_payload())
        self.assertEqual(first, second)

    def test_log_lines_trim_before_commands_ring(self) -> None:
        trimmed = supervisor.budget_runtime_state_payload(_worst_case_payload())
        # The worst case is dominated by log excerpts (~45 KiB vs ~2 KiB
        # ring): the deterministic order must exhaust log lines before
        # touching the ring, so the full ring survives.
        self.assertEqual(len(trimmed.get("commands") or []), 5)
        self.assertEqual(
            (trimmed.get("capabilities") or {}).get("missing_truncated"), False
        )

    def test_oldest_lines_drop_first(self) -> None:
        payload = _worst_case_payload()
        trimmed = supervisor.budget_runtime_state_payload(payload)
        for block_name, key in (("supervisor", "journal"), ("gateway", "journal")):
            survivors = (trimmed.get(block_name) or {}).get(key) or []
            if survivors:
                # Lines are stamped 0000..0014; survivors must be the tail.
                first_surviving = int(survivors[0]["text"].split(" ", 1)[0])
                last_surviving = int(survivors[-1]["text"].split(" ", 1)[0])
                self.assertEqual(
                    last_surviving, supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES - 1
                )
                self.assertGreaterEqual(first_surviving, 0)

    def test_commands_ring_then_missing_list_levers(self) -> None:
        payload = _worst_case_payload()
        # Strip the log excerpts so the next levers must do the work, and
        # inflate the ring so it alone exceeds the budget.
        del payload["bootstrap"]
        del payload["supervisor"]["journal"]
        del payload["gateway"]["journal"]
        payload["commands"] = [
            dict(entry, summary="s" * 256, idempotency_key="k" * 64)
            for entry in payload["commands"]
        ] * 12
        trimmed = supervisor.budget_runtime_state_payload(payload)
        self.assertLessEqual(
            runtime_state._runtime_state_payload_bytes(trimmed),
            supervisor.RUNTIME_STATE_PLATFORM_POST_MAX_BYTES,
        )
        self.assertLess(len(trimmed.get("commands") or []), 60)

    def test_missing_list_trim_sets_flag(self) -> None:
        payload = _worst_case_payload()
        del payload["bootstrap"]
        del payload["supervisor"]["journal"]
        del payload["gateway"]["journal"]
        del payload["commands"]
        del payload["runtime_events"]
        payload["capabilities"]["missing"] = [
            f"tinyhat_tool_{index:04d}_{'n' * 120}" for index in range(120)
        ]
        trimmed = supervisor.budget_runtime_state_payload(payload)
        self.assertLessEqual(
            runtime_state._runtime_state_payload_bytes(trimmed),
            supervisor.RUNTIME_STATE_PLATFORM_POST_MAX_BYTES,
        )
        self.assertTrue(trimmed["capabilities"]["missing_truncated"])
        self.assertLess(len(trimmed["capabilities"]["missing"]), 120)

    def test_small_payload_returned_unchanged(self) -> None:
        payload = {"schema": "runtime_state_v1", "runtime_health": "healthy"}
        self.assertIs(supervisor.budget_runtime_state_payload(payload), payload)


# ── redaction-sentinel (diagnose leg) ────────────────────────────────

# label -> (plaintext context, the secret substring that must vanish)
_SENTINEL_CORPUS: list[tuple[str, str, str]] = [
    (
        "anthropic-key-bare",
        "model call failed for sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF retrying",
        "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF",
    ),
    (
        "openrouter-key-bare",
        "openrouter sk-or-v1-0123456789abcdef0123456789abcdef rejected",
        "sk-or-v1-0123456789abcdef0123456789abcdef",
    ),
    (
        "openai-project-key-bare",
        "auth with sk-proj-ZZZZYYYYXXXXWWWWVVVVUUUU0000 failed",
        "sk-proj-ZZZZYYYYXXXXWWWWVVVVUUUU0000",
    ),
    (
        "tailscale-key-bare",
        "tailscale up --auth-key tskey-auth-kFJrk29CNTRL-mBvqqJzAdeadbeef1234 done",
        "tskey-auth-kFJrk29CNTRL-mBvqqJzAdeadbeef1234",
    ),
    (
        "telegram-bot-token",
        "polling https://api.telegram.org/bot7654321098:AAEhBOweik6ad9r_QXMENQjcG4euXckBVlo/getMe",
        "7654321098:AAEhBOweik6ad9r_QXMENQjcG4euXckBVlo",
    ),
    (
        "identity-jwt",
        "Authorization header eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJodHRwczovL3QifQ.sig0123456789sig0123456789 rejected",
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJodHRwczovL3QifQ.sig0123456789sig0123456789",
    ),
    (
        "signed-url",
        "fetched https://storage.googleapis.com/b/o?X-Goog-Signature=abcdef0123456789&x=1 ok",
        "X-Goog-Signature=abcdef0123456789",
    ),
    (
        "cookie-assignment",
        "request cookie: session_cookie=s3cr3tcookievalue99 sent",
        "s3cr3tcookievalue99",
    ),
    (
        "env-assignment",
        "spawning with OPENROUTER_API_KEY=plainenvsecret42 in env",
        "plainenvsecret42",
    ),
    (
        "bearer-scheme",
        "retrying with Bearer abc.def-ghi_jkl012345 header",
        "abc.def-ghi_jkl012345",
    ),
    (
        "auth-profile-path",
        "wiped /var/lib/tinyhat-openclaw/auth-profiles/openai-default.json on release",
        "/var/lib/tinyhat-openclaw/auth-profiles/openai-default.json",
    ),
    (
        "local-user-path",
        "copied from /Users/farid/secret-notes.txt during debug",
        "/Users/farid/secret-notes.txt",
    ),
]


def _corpus_leaks(transform) -> list[str]:
    """Labels whose secret substring survives ``transform(plaintext)``."""
    leaked: list[str] = []
    for label, plaintext, secret in _SENTINEL_CORPUS:
        if secret in transform(plaintext):
            leaked.append(label)
    return leaked


class RedactionSentinelTests(unittest.TestCase):
    def test_sanitizer_redacts_full_corpus(self) -> None:
        leaks = _corpus_leaks(
            lambda text: supervisor._sanitize_runtime_state_text(text, limit=4096)
        )
        self.assertEqual(leaks, [], f"sanitizer leaked: {leaks}")

    def test_json_tree_sanitizer_redacts_nested_values(self) -> None:
        tree = {
            "detail": _SENTINEL_CORPUS[0][1],
            "events": [{"detail": _SENTINEL_CORPUS[4][1]}],
            "nested": {"list": [_SENTINEL_CORPUS[5][1]]},
        }
        rendered = json.dumps(redaction.sanitize_json_tree(tree))
        for label, _plaintext, secret in (
            _SENTINEL_CORPUS[0],
            _SENTINEL_CORPUS[4],
            _SENTINEL_CORPUS[5],
        ):
            self.assertNotIn(secret, rendered, f"tree sanitizer leaked {label}")

    def test_path_key_allowlist_keeps_plain_paths_readable(self) -> None:
        tree = {
            "path": "/etc/tinyhat/runtime.env",
            "override_path": "/var/lib/tinyhat/tinyhat-plugin-source.json",
            "other": "/etc/tinyhat/runtime.env",
        }
        sanitized = redaction.sanitize_json_tree(tree)
        # An operator must be able to read control-plane paths verbatim…
        self.assertEqual(sanitized["path"], "/etc/tinyhat/runtime.env")
        self.assertEqual(
            sanitized["override_path"],
            "/var/lib/tinyhat/tinyhat-plugin-source.json",
        )
        # …but the same value under a non-allowlisted key stays redacted,
        self.assertEqual(sanitized["other"], "[local-path]")
        # and a secret-shaped value under an allowlisted key is not a
        # bypass: it fails the plain-path shape and gets sanitized.
        evil = redaction.sanitize_json_tree(
            {"path": "/tmp/x token=sk-or-v1-0123456789abcdef0123456789abcdef"}
        )
        self.assertNotIn("sk-or-v1-0123456789abcdef0123456789abcdef", evil["path"])

    def test_sentinel_can_fail(self) -> None:
        """The deliberately-failing leg: an identity transform MUST leak.

        If this assertion ever stops holding, the corpus checker has
        gone vacuous and every green sentinel result above is
        meaningless.
        """
        leaks = _corpus_leaks(lambda text: text)
        self.assertEqual(
            len(leaks),
            len(_SENTINEL_CORPUS),
            "the sentinel failed to flag unredacted text — the harness "
            "can no longer fail and proves nothing",
        )


# ── manifest unit ────────────────────────────────────────────────────


class _ManifestEnvironment:
    """Temp on-box desired-state artifacts behind env overrides."""

    def __init__(self) -> None:
        self._stack = contextlib.ExitStack()

    def __enter__(self):
        self.tmpdir = self._stack.enter_context(tempfile.TemporaryDirectory())
        self.runtime_env_path = os.path.join(self.tmpdir, "runtime.env")
        self.update_state_path = os.path.join(self.tmpdir, "component-update-state.json")
        self._stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "TINYHAT_DEV_RUNTIME": "1",
                    "TINYHAT_RUNTIME_HOME": self.tmpdir,
                    "TINYHAT_RUNTIME_ENV_FILE": self.runtime_env_path,
                    "TINYHAT_COMPONENT_UPDATE_STATE_PATH": self.update_state_path,
                },
                clear=False,
            )
        )
        return self

    def write_runtime_env(self, *, plugin_ref: str = "f27b652e9c374d7e27af675716657159e0293a03") -> None:
        with open(self.runtime_env_path, "w", encoding="utf-8") as fh:
            fh.write(
                "TINYHAT_BACKEND_AUDIENCE=https://example.test\n"
                "TINYHAT_PLATFORM_BASE_URL=https://example.test\n"
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL=https://github.com/tinyhat-ai/tinyhat.git\n"
                f"TINYHAT_PLATFORM_PLUGIN_REPO_REF={plugin_ref}\n"
                "TINYHAT_OPENCLAW_RUNTIME_USER=tinyhat\n"
                "TINYHAT_OPENCLAW_RUNTIME_GROUP=tinyhat\n"
                "TINYHAT_MYSTERY_EXTRA=do-not-echo-this-value\n"
            )

    def write_plugin_marker(self, *, version: str, sha: str, ref: str) -> None:
        marker_path = supervisor._tinyhat_plugin_marker_path()
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                    "repo_ref": ref,
                    "resolved_commit_sha": sha,
                    "version": version,
                },
                fh,
            )

    def write_update_state(self, *, revision: int, applied_versions: dict) -> None:
        with open(self.update_state_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "last_revision": revision,
                    "status": "applied",
                    "diagnostic": None,
                    "applied_versions": applied_versions,
                    "reported": True,
                },
                fh,
            )

    def __exit__(self, *exc_info):
        return self._stack.__exit__(*exc_info)


_RUNNING_VERSIONS = {
    "runtime": {"version": "0.11.14", "sha": "427769f73779b483fb8334949723a62ab05af143"},
    "plugin": {"version": "0.4.5", "sha": "f27b652e9c374d7e27af675716657159e0293a03"},
    "framework": {"version": "2026.6.5", "sha": None},
}


class ManifestUnitTests(unittest.TestCase):
    def test_creation_specs_parse_known_keys_and_name_unknown_ones(self) -> None:
        with _ManifestEnvironment() as env:
            env.write_runtime_env()
            spec = manifest.read_creation_specs()
        self.assertTrue(spec["present"])
        self.assertEqual(
            spec["values"]["TINYHAT_PLATFORM_PLUGIN_REPO_REF"],
            "f27b652e9c374d7e27af675716657159e0293a03",
        )
        self.assertEqual(spec["unknown_keys"], ["TINYHAT_MYSTERY_EXTRA"])
        self.assertNotIn("do-not-echo-this-value", json.dumps(spec))
        self.assertIsInstance(spec["mtime_unix"], int)

    def test_no_acked_update_means_creation_spec_staleness(self) -> None:
        with _ManifestEnvironment() as env:
            env.write_runtime_env()
            with patch.object(
                supervisor, "collect_component_versions", return_value=dict(_RUNNING_VERSIONS)
            ):
                show = supervisor.manifest_show()
        self.assertEqual(show["desired_source"], "on_box_last_known")
        self.assertTrue(show["admin_drift_authoritative"])
        self.assertFalse(show["last_acked_update"]["present"])
        self.assertIn("creation-time specs", show["desired_staleness"]["summary"])
        self.assertIn("no platform component", show["desired_staleness"]["summary"])

    def test_drift_in_sync_plugin_and_unknown_runtime_without_update(self) -> None:
        with _ManifestEnvironment() as env:
            env.write_runtime_env()
            env.write_plugin_marker(
                version="0.4.5",
                sha="f27b652e9c374d7e27af675716657159e0293a03",
                ref="f27b652e9c374d7e27af675716657159e0293a03",
            )
            with patch.object(
                supervisor, "collect_component_versions", return_value=dict(_RUNNING_VERSIONS)
            ):
                drift = supervisor.manifest_drift()
        self.assertEqual(drift["components"]["plugin"]["verdict"], "in_sync")
        # No on-box desired record exists for runtime/framework on a
        # never-updated box — the honest verdict is unknown, not in_sync.
        self.assertEqual(drift["components"]["runtime"]["verdict"], "unknown")
        self.assertEqual(drift["components"]["framework"]["verdict"], "unknown")
        self.assertIsNone(drift["drift_detected"])
        self.assertTrue(drift["admin_drift_authoritative"])

    def test_divergence_fixture_renders_honestly(self) -> None:
        """Admin-desired != last-acked on-box: the CLI must show divergence
        AND keep claiming only as-known-on-box truth."""
        with _ManifestEnvironment() as env:
            env.write_runtime_env(plugin_ref="v0.5.0")  # platform moved on
            env.write_plugin_marker(
                version="0.4.5",
                sha="f27b652e9c374d7e27af675716657159e0293a03",
                ref="f27b652e9c374d7e27af675716657159e0293a03",
            )
            env.write_update_state(
                revision=41,
                applied_versions={
                    "runtime": {"version": "0.11.13", "sha": "00000000aaaa"},
                    "framework": {"version": "2026.6.5", "sha": None},
                },
            )
            with patch.object(
                supervisor, "collect_component_versions", return_value=dict(_RUNNING_VERSIONS)
            ):
                drift = supervisor.manifest_drift()
        self.assertEqual(drift["components"]["plugin"]["verdict"], "divergent")
        self.assertEqual(drift["components"]["runtime"]["verdict"], "divergent")
        self.assertEqual(drift["components"]["framework"]["verdict"], "in_sync")
        self.assertTrue(drift["drift_detected"])
        self.assertEqual(drift["desired_source"], "on_box_last_known")
        self.assertTrue(drift["admin_drift_authoritative"])
        self.assertEqual(drift["desired_staleness"]["last_acked_revision"], 41)
        human = "\n".join(manifest.render_drift(drift))
        self.assertIn("authoritative", human)
        self.assertIn("DRIFT DETECTED", human)
        self.assertIn("as known on-box", human)

    def test_v_prefix_normalization(self) -> None:
        self.assertTrue(manifest._refs_match("v0.2.2", "0.2.2"))
        self.assertTrue(manifest._refs_match("0.2.2", "v0.2.2"))
        self.assertTrue(
            manifest._refs_match(
                "427769f7", "427769f73779b483fb8334949723a62ab05af143"
            )
        )
        self.assertFalse(manifest._refs_match("v0.2.2", "0.2.3"))

    def test_full_ref_normalization(self) -> None:
        # The installer accepts full tag refs; drift must not treat a
        # tag-pinned box as divergent (PR #89 review P1).
        self.assertTrue(manifest._refs_match("refs/tags/v0.5.0", "0.5.0"))
        self.assertTrue(manifest._refs_match("refs/tags/v0.5.0", "v0.5.0"))
        self.assertTrue(manifest._refs_match("0.5.0", "refs/tags/v0.5.0"))
        self.assertTrue(manifest._refs_match("refs/heads/main", "main"))
        self.assertFalse(manifest._refs_match("refs/tags/v0.5.0", "0.5.1"))

    def test_tag_pinned_plugin_is_in_sync(self) -> None:
        """Codex review repro: creation spec ``refs/tags/v0.5.0`` +
        installed marker/version ``0.5.0`` must NOT report drift."""
        running = {
            "runtime": dict(_RUNNING_VERSIONS["runtime"]),
            "framework": dict(_RUNNING_VERSIONS["framework"]),
            "plugin": {"version": "0.5.0", "sha": "abc1234abc1234abc1234abc1234abc1234abc12"},
        }
        with _ManifestEnvironment() as env:
            env.write_runtime_env(plugin_ref="refs/tags/v0.5.0")
            env.write_plugin_marker(
                version="0.5.0",
                sha="abc1234abc1234abc1234abc1234abc1234abc12",
                ref="refs/tags/v0.5.0",
            )
            with patch.object(
                supervisor, "collect_component_versions", return_value=running
            ):
                drift = supervisor.manifest_drift()
        self.assertEqual(drift["components"]["plugin"]["verdict"], "in_sync")
        self.assertIsNot(drift["drift_detected"], True)

    def test_marker_repo_ref_matches_branch_shaped_desired_ref(self) -> None:
        """A desired ref that is neither version- nor sha-shaped still
        matches when the install marker records that exact ref."""
        running = {
            "plugin": {"version": "0.5.0", "sha": "abc1234abc1234abc1234abc1234abc1234abc12"},
        }
        with _ManifestEnvironment() as env:
            env.write_runtime_env(plugin_ref="pin-branch")
            env.write_plugin_marker(
                version="0.5.0",
                sha="abc1234abc1234abc1234abc1234abc1234abc12",
                ref="pin-branch",
            )
            with patch.object(
                supervisor, "collect_component_versions", return_value=running
            ):
                drift = supervisor.manifest_drift()
        self.assertEqual(drift["components"]["plugin"]["verdict"], "in_sync")


# ── snapshot unit ────────────────────────────────────────────────────


class SnapshotTests(unittest.TestCase):
    def test_freshness_fields_from_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "observed_at": "2026-06-12T11:00:00Z",
                        "updated_at_unix": 1760000000,
                        "runtime_health": "healthy",
                    },
                    fh,
                )
            with patch.dict(
                os.environ,
                {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                clear=False,
            ):
                import subprocess as subprocess_module

                def _fake_systemctl(*args, check=True):
                    return subprocess_module.CompletedProcess(
                        ["systemctl", *args], 0, stdout="active\n", stderr=""
                    )

                with patch.object(supervisor, "_run_systemctl", _fake_systemctl):
                    meta, state = snapshot.control_plane_snapshot()
        self.assertEqual(meta["state_as_of"], "2026-06-12T11:00:00Z")
        self.assertTrue(meta["state_present"])
        self.assertIsInstance(meta["state_age_seconds"], int)
        self.assertTrue(meta["supervisor_alive"])
        self.assertEqual(meta["supervisor_unit_state"], "active")
        self.assertEqual(state["runtime_health"], "healthy")

    def test_dead_supervisor_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"observed_at": "2026-06-12T11:00:00Z"}, fh)
            with patch.dict(
                os.environ,
                {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                clear=False,
            ):
                import subprocess as subprocess_module

                def _fake_systemctl(*args, check=True):
                    return subprocess_module.CompletedProcess(
                        ["systemctl", *args], 3, stdout="inactive\n", stderr=""
                    )

                with patch.object(supervisor, "_run_systemctl", _fake_systemctl):
                    meta, _state = snapshot.control_plane_snapshot()
        self.assertFalse(meta["supervisor_alive"])
        warning = "\n".join(snapshot.freshness_lines(meta))
        self.assertIn("LAST snapshot", warning)

    def test_missing_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "missing.json")
            with patch.dict(
                os.environ,
                {
                    supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path,
                    "TINYHAT_DEV_RUNTIME": "1",
                },
                clear=False,
            ):
                meta, state = snapshot.control_plane_snapshot()
        self.assertFalse(meta["state_present"])
        self.assertIsNone(meta["state_as_of"])
        self.assertIsNone(meta["supervisor_alive"])
        self.assertEqual(state, {})


# ── entrypoint + schemas ─────────────────────────────────────────────


class _CliEnvironment(_ManifestEnvironment):
    """Manifest artifacts + a seeded runtime-state file with secrets."""

    def __enter__(self):
        super().__enter__()
        self.state_path = os.path.join(self.tmpdir, "runtime-state.json")
        self._stack.enter_context(
            patch.dict(
                os.environ,
                {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: self.state_path},
                clear=False,
            )
        )
        return self

    def write_state(self) -> None:
        # Plant every corpus secret in fields the CLI renders. The
        # daemon would have sanitized these at write time; the CLI's
        # egress sanitizer must hold even if a state file was written
        # by older code or corrupted.
        planted = " | ".join(text for _label, text, _secret in _SENTINEL_CORPUS)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema": "runtime_state_v1",
                    "computer_id": "5066",
                    "instance_id": "987654321",
                    "runtime_ref": "0.11.14@427769f73779",
                    "observed_at": "2026-06-12T11:00:00Z",
                    "updated_at_unix": 1760000000,
                    "runtime_health": "healthy",
                    "detail": planted,
                    "supervisor": {"version": "0.11.14", "status": "healthy"},
                    "gateway": {
                        "unit": "tinyhat-openclaw-gateway.service",
                        "status": "healthy",
                        "active": True,
                        "restart_count_window": 0,
                    },
                    "openclaw": {"ready": True},
                    "last_error": None,
                    "runtime_events": [
                        {
                            "type": "gateway_restart",
                            "at": "2026-06-12T10:59:00Z",
                            "detail": planted,
                        }
                    ],
                },
                fh,
            )


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = entrypoint.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


_ALL_COMMANDS: list[tuple[str, list[str]]] = [
    ("status", ["status"]),
    ("health", ["health"]),
    ("manifest show", ["manifest", "show"]),
    ("manifest drift", ["manifest", "drift"]),
    ("whoami", ["whoami"]),
]


class EntrypointTests(unittest.TestCase):
    def test_non_root_gets_typed_error_and_exit_77(self) -> None:
        with patch("os.geteuid", return_value=1000):
            exit_code, stdout, stderr = _run_cli(["status"])
        self.assertEqual(exit_code, entrypoint.EXIT_NOT_ROOT)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["error"]["type"], "not_root")

    def test_all_commands_json_schema_valid_and_redacted(self) -> None:
        with _CliEnvironment() as env:
            env.write_runtime_env()
            env.write_state()
            with patch("os.geteuid", return_value=0), patch.object(
                supervisor,
                "collect_component_versions",
                return_value=dict(_RUNNING_VERSIONS),
            ), patch.object(supervisor, "is_openclaw_gateway_active", return_value=True):
                for key, argv in _ALL_COMMANDS:
                    with self.subTest(command=key):
                        exit_code, stdout, stderr = _run_cli(argv + ["--json"])
                        self.assertEqual(exit_code, 0, stderr)
                        envelope = json.loads(stdout)
                        self.assertEqual(
                            schemas.validate_envelope(key, envelope), []
                        )
                        self.assertEqual(envelope["state_as_of"], "2026-06-12T11:00:00Z")
                        self.assertIn("supervisor_alive", envelope)
                        for _label, _plaintext, secret in _SENTINEL_CORPUS:
                            self.assertNotIn(secret, stdout)

    def test_human_output_carries_freshness_and_redaction(self) -> None:
        with _CliEnvironment() as env:
            env.write_runtime_env()
            env.write_state()
            with patch("os.geteuid", return_value=0), patch.object(
                supervisor,
                "collect_component_versions",
                return_value=dict(_RUNNING_VERSIONS),
            ), patch.object(supervisor, "is_openclaw_gateway_active", return_value=True):
                for key, argv in _ALL_COMMANDS:
                    with self.subTest(command=key):
                        exit_code, stdout, _stderr = _run_cli(argv)
                        self.assertEqual(exit_code, 0)
                        self.assertIn("state as of:", stdout)
                        self.assertIn("supervisor alive:", stdout)
                        for _label, _plaintext, secret in _SENTINEL_CORPUS:
                            self.assertNotIn(secret, stdout)

    def test_manifest_drift_json_contract_fields(self) -> None:
        with _CliEnvironment() as env:
            env.write_runtime_env()
            env.write_state()
            with patch("os.geteuid", return_value=0), patch.object(
                supervisor,
                "collect_component_versions",
                return_value=dict(_RUNNING_VERSIONS),
            ):
                exit_code, stdout, _stderr = _run_cli(["manifest", "drift", "--json"])
        self.assertEqual(exit_code, 0)
        envelope = json.loads(stdout)
        data = envelope["data"]
        self.assertEqual(data["desired_source"], "on_box_last_known")
        self.assertIs(data["admin_drift_authoritative"], True)
        self.assertIn("summary", data["desired_staleness"])

    def test_internal_error_is_typed(self) -> None:
        with patch("os.geteuid", return_value=0), patch.object(
            entrypoint.snapshot_unit,
            "control_plane_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            exit_code, _stdout, stderr = _run_cli(["status"])
        self.assertEqual(exit_code, entrypoint.EXIT_ERROR)
        error = json.loads(stderr)
        self.assertEqual(error["error"]["type"], "internal_error")

    def test_schema_validator_can_fail(self) -> None:
        errors = schemas.validate_envelope("status", {"schema": "wrong"})
        self.assertTrue(errors, "schema validator accepted a broken envelope")

    def test_cli_quiets_supervisor_info_logging(self) -> None:
        import logging

        logger = logging.getLogger("tinyhat-supervisor")
        previous_level = logger.level
        try:
            logger.setLevel(logging.INFO)
            with patch("os.geteuid", return_value=1000):
                _run_cli(["status"])
            self.assertEqual(logger.level, logging.WARNING)
        finally:
            logger.setLevel(previous_level)


# ── gateway-restart skeleton (also proves the facade keeps patches) ──


class GatewayRestartSkeletonTests(unittest.TestCase):
    def _binding(self) -> dict:
        return {"telegram_bot_token": "123456:ABC", "telegram_bot_username": "t"}

    def test_success_path(self) -> None:
        with patch.object(supervisor, "delete_telegram_webhook") as webhook, patch.object(
            supervisor, "start_openclaw_gateway", return_value=1760000000.0
        ) as start, patch.object(supervisor, "wait_for_openclaw_start") as wait:
            result = gateway_restart.run_gateway_restart_transaction(self._binding())
        self.assertEqual(result.outcome, "succeeded")
        self.assertEqual(result.phase_reached, "terminal")
        self.assertEqual(result.operation_marker_unix, 1760000000)
        webhook.assert_called_once()
        start.assert_called_once()
        wait.assert_called_once_with(1760000000.0)

    def test_timeout_classifies_timed_out(self) -> None:
        with patch.object(supervisor, "delete_telegram_webhook"), patch.object(
            supervisor, "start_openclaw_gateway", return_value=1760000000.0
        ), patch.object(
            supervisor,
            "wait_for_openclaw_start",
            side_effect=RuntimeError(
                "openclaw gateway did not become healthy within 90s: waiting"
            ),
        ):
            result = gateway_restart.run_gateway_restart_transaction(self._binding())
        self.assertEqual(result.outcome, "timed_out")

    def test_start_failure_classifies_failed(self) -> None:
        with patch.object(supervisor, "delete_telegram_webhook"), patch.object(
            supervisor,
            "start_openclaw_gateway",
            side_effect=RuntimeError("systemctl restart failed"),
        ):
            result = gateway_restart.run_gateway_restart_transaction(self._binding())
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.phase_reached, "child_running")

    def test_webhook_failure_classifies_failed(self) -> None:
        with patch.object(
            supervisor,
            "delete_telegram_webhook",
            side_effect=RuntimeError("telegram unreachable"),
        ):
            result = gateway_restart.run_gateway_restart_transaction(self._binding())
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.phase_reached, "webhook_delete")

    def test_result_record_shape(self) -> None:
        with patch.object(supervisor, "delete_telegram_webhook"), patch.object(
            supervisor, "start_openclaw_gateway", return_value=1.0
        ), patch.object(supervisor, "wait_for_openclaw_start"):
            record = gateway_restart.run_gateway_restart_transaction(
                self._binding()
            ).as_record()
        self.assertEqual(record["name"], "gateway restart")
        self.assertEqual(record["class"], "operate")
        self.assertIn(record["outcome"], gateway_restart.GATEWAY_RESTART_OUTCOMES)


# ── lifecycle rider ──────────────────────────────────────────────────


class LifecycleBlockTests(unittest.TestCase):
    def test_requires_phase_a_mark(self) -> None:
        self.assertIsNone(supervisor.lifecycle_block(None))
        self.assertIsNone(
            supervisor.lifecycle_block({"supervisor_started_at_unix": 100})
        )
        self.assertIsNone(
            supervisor.lifecycle_block(
                {"supervisor_started_at_unix": 100, "gateway_start_at_unix": 130}
            )
        )

    def test_spans_computed(self) -> None:
        block = supervisor.lifecycle_block(
            {
                "supervisor_started_at_unix": 100,
                "ready_reported_at_unix": 104,
                "binding_acquired_at_unix": 110,
                "gateway_start_at_unix": 130,
                "gateway_ready_at_unix": 142,
            }
        )
        self.assertEqual(
            block["spans"],
            {
                "boot_to_ready_seconds": 4,
                "ready_to_bind_seconds": 6,
                "bind_to_gateway_start_seconds": 20,
                "gateway_start_to_gateway_ready_seconds": 12,
            },
        )


if __name__ == "__main__":
    unittest.main()
