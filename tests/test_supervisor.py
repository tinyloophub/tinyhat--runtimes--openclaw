"""Regression tests for the Tinyhat Computer runtime supervisor.

Usage:
    python -m unittest tests.test_supervisor -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

import supervisor

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


def _runtime_repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bootstrap_script_text() -> str:
    with open(
        os.path.join(_runtime_repo_root(), "bootstrap.sh"),
        encoding="utf-8",
    ) as fh:
        return fh.read()


def _bootstrap_unit_block(unit_var: str) -> str:
    text = _bootstrap_script_text()
    marker = f'cat > "${{{unit_var}}}" <<UNIT'
    start = text.index(marker)
    start = text.index("\n", start) + 1
    end = text.index("\nUNIT", start)
    return text[start:end]


def _write_config_in_temp_runtime(
    binding: dict,
    *,
    secrets: dict[str, str] | None = None,
) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        secrets_path = os.path.join(
            tmpdir,
            "tinyhat-secrets.json",
        )
        if secrets is not None:
            with open(secrets_path, "w", encoding="utf-8") as fh:
                json.dump(secrets, fh)
        env = {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": tmpdir,
            "TINYHAT_SECRETS_PATH": secrets_path,
        }
        with patch.dict(os.environ, env, clear=False):
            supervisor.write_openclaw_config(binding)
            config_path = supervisor.openclaw_config_path()
            with open(config_path, encoding="utf-8") as fh:
                return json.load(fh)


def _openrouter_binding(model_package: dict) -> dict:
    return {
        "telegram_owner_user_id": "123456",
        "telegram_bot_token": "123456:ABC",
        "telegram_bot_username": "Tinychattestbot",
        "openrouter_api_key": "sk-or-v1-child",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_default_model": model_package["default_model"],
        "openrouter_model_package": model_package,
    }


def _openrouter_catalog_entry(alias: str) -> dict:
    return {
        "alias": alias,
        "params": {
            "max_completion_tokens": supervisor.OPENROUTER_COMPLETION_TOKEN_CAP,
        },
    }


def _assert_no_provider_runtime_pin(
    testcase: unittest.TestCase,
    config: dict,
    provider_id: str,
) -> None:
    providers = (config.get("models") or {}).get("providers") or {}
    testcase.assertNotIn("agentRuntime", providers.get(provider_id, {}))


class ReloadOpenClawSecretsTests(unittest.TestCase):
    def test_gateway_settle_retries_until_reload_succeeds(self) -> None:
        calls = 0

        def fake_run(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 5:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr=(
                        "Could not reload secrets because the Gateway did not "
                        "respond: gateway closed."
                    ),
                )
            return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        with (
            patch.object(supervisor, "_openclaw_cli_env", return_value={}),
            patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            patch.object(supervisor.time, "sleep") as sleep,
        ):
            result = supervisor.reload_openclaw_secrets({"TEST": "tok-value"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 5)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [5, 10, 20, 30],
        )

    def test_inactive_initial_snapshot_is_synced(self) -> None:
        secret_value = "sk-test-secret-value"
        failed_reload = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Error: secrets runtime snapshot is not active",
        )

        with (
            patch.object(supervisor, "_openclaw_cli_env", return_value={}),
            patch.object(supervisor.subprocess, "run", return_value=failed_reload),
            patch.object(supervisor.time, "sleep") as sleep,
        ):
            result = supervisor.reload_openclaw_secrets({"TEST": secret_value})

        self.assertEqual(
            result,
            {
                "skipped": True,
                "reason": "secrets_runtime_snapshot_inactive",
            },
        )
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [],
        )

    def test_non_retryable_reload_failure_raises(self) -> None:
        secret_value = "sk-test-secret-value"
        failed_reload = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"Error: invalid secret value {secret_value}",
        )

        with (
            patch.object(supervisor, "_openclaw_cli_env", return_value={}),
            patch.object(supervisor.subprocess, "run", return_value=failed_reload),
            patch.object(supervisor.time, "sleep") as sleep,
        ):
            with self.assertRaises(RuntimeError) as raised:
                supervisor.reload_openclaw_secrets({"TEST": secret_value})

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [])
        message = str(raised.exception)
        self.assertIn("openclaw secrets reload failed", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn(secret_value, message)


class OpenRouterModelPackageTests(unittest.TestCase):
    def test_paid_package_writes_enabled_catalog_and_fallback(self) -> None:
        package = {
            "default_model": "deepseek/deepseek-v4-pro",
            "default_role": "default",
            "enabled_roles": ["cheap", "default", "power"],
            "models": {
                "cheap": "deepseek/deepseek-v4-flash",
                "default": "deepseek/deepseek-v4-pro",
                "power": "moonshotai/kimi-k2.6",
                "premium": "anthropic/claude-sonnet-4.5",
                "frontier": "openai/gpt-5.5",
                "free_demo": "deepseek/deepseek-v4-flash:free",
            },
        }

        config = _write_config_in_temp_runtime(_openrouter_binding(package))

        self.assertEqual(
            config["agents"]["defaults"]["model"],
            {
                "primary": "openrouter/deepseek/deepseek-v4-pro",
                "fallbacks": ["openrouter/deepseek/deepseek-v4-flash"],
            },
        )
        self.assertEqual(
            config["agents"]["defaults"]["models"],
            {
                "openrouter/deepseek/deepseek-v4-flash": (
                    _openrouter_catalog_entry("cheap")
                ),
                "openrouter/deepseek/deepseek-v4-pro": (
                    _openrouter_catalog_entry("default")
                ),
                "openrouter/moonshotai/kimi-k2.6": (
                    _openrouter_catalog_entry("power")
                ),
            },
        )
        self.assertNotIn("agentRuntime", config["agents"]["defaults"])
        _assert_no_provider_runtime_pin(self, config, "openrouter")
        self.assertEqual(config["env"], {"OPENROUTER_API_KEY": "sk-or-v1-child"})

    def test_writes_compaction_reserve_floor(self) -> None:
        package = {
            "default_model": "deepseek/deepseek-v4-flash",
            "default_role": "default",
            "enabled_roles": ["default"],
            "models": {"default": "deepseek/deepseek-v4-flash"},
        }

        config = _write_config_in_temp_runtime(_openrouter_binding(package))

        self.assertEqual(
            config["agents"]["defaults"]["compaction"],
            {"reserveTokensFloor": 20000},
        )

    def test_no_credit_package_stays_on_free_demo_model(self) -> None:
        package = {
            "default_model": "deepseek/deepseek-v4-flash:free",
            "default_role": "free_demo",
            "enabled_roles": ["free_demo"],
            "models": {
                "cheap": "deepseek/deepseek-v4-flash",
                "default": "deepseek/deepseek-v4-pro",
                "power": "moonshotai/kimi-k2.6",
                "premium": "anthropic/claude-sonnet-4.5",
                "frontier": "openai/gpt-5.5",
                "free_demo": "deepseek/deepseek-v4-flash:free",
            },
        }

        config = _write_config_in_temp_runtime(_openrouter_binding(package))

        self.assertEqual(
            config["agents"]["defaults"]["model"],
            {"primary": "openrouter/deepseek/deepseek-v4-flash:free"},
        )
        self.assertEqual(
            config["agents"]["defaults"]["models"],
            {
                "openrouter/deepseek/deepseek-v4-flash:free": (
                    _openrouter_catalog_entry("free-demo")
                ),
            },
        )
        self.assertNotIn("agentRuntime", config["agents"]["defaults"])
        _assert_no_provider_runtime_pin(self, config, "openrouter")

    def test_binding_signature_changes_when_model_package_changes(self) -> None:
        package = {
            "default_model": "deepseek/deepseek-v4-pro",
            "default_role": "default",
            "enabled_roles": ["cheap", "default"],
            "models": {
                "cheap": "deepseek/deepseek-v4-flash",
                "default": "deepseek/deepseek-v4-pro",
            },
        }
        binding = _openrouter_binding(package)

        next_binding = {
            **binding,
            "openrouter_model_package": {
                **package,
                "enabled_roles": ["cheap", "default", "power"],
            },
        }

        self.assertNotEqual(
            supervisor._binding_signature(binding),
            supervisor._binding_signature(next_binding),
        )


class OpenClawGatewayHealthTests(unittest.TestCase):
    def test_startup_failure_detail_is_extracted_from_gateway_logs(self) -> None:
        detail = supervisor._openclaw_gateway_startup_failure_from_logs(
            "\n".join(
                [
                    "[gateway] loading configuration...",
                    "Gateway failed to start: Invalid config at /etc/openclaw/openclaw.json.",
                    "agents.defaults: Invalid input",
                    'Run "openclaw doctor --fix" to repair, then retry.',
                ]
            )
        )

        self.assertIsNotNone(detail)
        self.assertIn("Invalid config", detail or "")
        self.assertIn("agents.defaults: Invalid input", detail or "")

    def test_startup_failure_detail_ignores_benign_gateway_logs(self) -> None:
        detail = supervisor._openclaw_gateway_startup_failure_from_logs(
            "\n".join(
                [
                    "[gateway] loading configuration...",
                    "[gateway] ready",
                    "[telegram] connected to gateway",
                ]
            )
        )

        self.assertIsNone(detail)

    def test_wait_raises_startup_failure_without_waiting_for_timeout(self) -> None:
        with (
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(
                supervisor,
                "probe_openclaw_gateway_health",
                return_value=(
                    False,
                    "gateway startup failed: Invalid config at /etc/openclaw/openclaw.json. agents.defaults: Invalid input",
                ),
            ),
            patch.object(supervisor.time, "sleep") as sleep,
        ):
            with self.assertRaises(RuntimeError) as raised:
                supervisor.wait_for_openclaw_start(123.0)

        self.assertIn("openclaw gateway failed to start", str(raised.exception))
        self.assertEqual(sleep.call_args_list, [])

    def test_wait_raises_inactive_startup_failure_without_timeout(self) -> None:
        with (
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=False),
            patch.object(
                supervisor,
                "probe_openclaw_gateway_health",
                return_value=(
                    False,
                    "gateway startup failed: Invalid config at /etc/openclaw/openclaw.json.",
                ),
            ),
            patch.object(supervisor.time, "sleep") as sleep,
        ):
            with self.assertRaises(RuntimeError) as raised:
                supervisor.wait_for_openclaw_start(123.0)

        self.assertIn("openclaw gateway failed to start", str(raised.exception))
        self.assertEqual(sleep.call_args_list, [])

    def test_wait_keeps_90s_budget_and_checkpoints_while_openclaw_warms_up(
        self,
    ) -> None:
        self.assertEqual(supervisor.OPENCLAW_GATEWAY_START_TIMEOUT_SECONDS, 90)
        now = [0.0]
        probes = []

        def fake_time() -> float:
            return now[0]

        def fake_sleep(seconds: float) -> None:
            now[0] += 16 * seconds

        def fake_probe(_started_at: float) -> tuple[bool, str]:
            probes.append(now[0])
            if len(probes) >= 4:
                return True, "ok"
            return False, "waiting for OpenClaw gateway ready"

        with (
            patch.object(supervisor.time, "time", side_effect=fake_time),
            patch.object(supervisor.time, "sleep", side_effect=fake_sleep),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(
                supervisor,
                "probe_openclaw_gateway_health",
                side_effect=fake_probe,
            ),
            patch.object(supervisor, "checkpoint_supervisor_progress") as checkpoint,
        ):
            supervisor.wait_for_openclaw_start(123.0)

        self.assertEqual(
            checkpoint.call_args_list,
            [
                call(
                    "phase-c-openclaw-wait",
                    inspect_gateway=True,
                ),
                call(
                    "phase-c-openclaw-wait",
                    inspect_gateway=True,
                ),
            ],
        )

    def test_dev_health_probe_ignores_logs_before_current_gateway_start(
        self,
    ) -> None:
        previous_gateway = dict(supervisor._dev_gateway)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                env = {
                    "TINYHAT_DEV_RUNTIME": "1",
                    "TINYHAT_RUNTIME_HOME": tmpdir,
                }
                with patch.dict(os.environ, env, clear=False):
                    log_path = supervisor._dev_gateway_log_path()
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    with open(log_path, "w", encoding="utf-8") as fh:
                        fh.write("[gateway] ready\n")
                        fh.write("[telegram] connected to gateway\n")
                    supervisor._dev_gateway.update(
                        {
                            "log_path": log_path,
                            "log_offset": os.path.getsize(log_path),
                        }
                    )

                    ok, detail = supervisor._probe_openclaw_gateway_health_dev(123.0)
                    self.assertFalse(ok)
                    self.assertIn("waiting for OpenClaw", detail)

                    with open(log_path, "a", encoding="utf-8") as fh:
                        fh.write("[gateway] ready\n")
                        fh.write("[telegram] connected to gateway\n")
                    ok, detail = supervisor._probe_openclaw_gateway_health_dev(123.0)

                    self.assertTrue(ok)
                    self.assertEqual(detail, "ok")
        finally:
            supervisor._dev_gateway.clear()
            supervisor._dev_gateway.update(previous_gateway)


class RuntimeStateV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        supervisor._reset_runtime_state_identity_cache()
        supervisor._reset_runtime_state_platform_post_cache()
        supervisor._base_url_cache.update({"value": None, "ts": 0.0})

    def tearDown(self) -> None:
        supervisor._reset_runtime_state_identity_cache()
        supervisor._reset_runtime_state_platform_post_cache()
        supervisor._base_url_cache.update({"value": None, "ts": 0.0})

    def _env(self, state_path: str) -> dict[str, str]:
        return {
            supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path,
            supervisor.TINYHAT_COMPUTER_ID_ENV: "cmp_test_123",
            supervisor.TINYHAT_GCE_INSTANCE_ID_ENV: "9876543210",
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_PLATFORM_BASE_URL": "",
        }

    def test_runtime_state_v1_payload_shape_permissions_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(
                tmpdir,
                "tinyhat-control",
                "runtime-state.json",
            )
            gateway_recovery = {
                "failures": [
                    {"at_unix": 1_759_999_900, "reason": "restart_failed"},
                    {"at_unix": 1_750_000_000, "reason": "old_failure"},
                ],
            }

            with (
                patch.dict(os.environ, self._env(state_path), clear=False),
                patch.object(supervisor.time, "time", return_value=1_760_000_000),
                patch.object(
                    supervisor,
                    "_read_runtime_repo_version",
                    return_value="0.11.0",
                ),
                patch.object(
                    supervisor,
                    "_read_runtime_git_sha",
                    return_value="abcdef1234567890",
                ),
            ):
                supervisor._write_runtime_state(
                    "healthy",
                    "openclaw gateway started",
                    gateway_active=True,
                    gateway_action="started",
                    openclaw_ready=True,
                    gateway_recovery=gateway_recovery,
                )
                payload = supervisor.read_runtime_state()

            self.assertEqual(payload["schema"], supervisor.RUNTIME_STATE_SCHEMA)
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertEqual(payload["runtime_state"], "healthy")
            self.assertEqual(payload["state"], "healthy")
            self.assertEqual(payload["computer_id"], "cmp_test_123")
            self.assertEqual(payload["instance_id"], "9876543210")
            self.assertEqual(payload["runtime_ref"], "0.11.0@abcdef123456")
            self.assertEqual(
                payload["observed_at"],
                supervisor._runtime_state_observed_at(1_760_000_000),
            )
            self.assertEqual(
                payload["supervisor"],
                {"version": "0.11.0", "status": "healthy"},
            )
            self.assertEqual(payload["gateway"]["unit"], supervisor.GATEWAY_SYSTEMD_UNIT)
            self.assertEqual(payload["gateway"]["status"], "healthy")
            self.assertEqual(payload["gateway"]["restart_count_window"], 1)
            self.assertTrue(payload["gateway"]["active"])
            self.assertEqual(payload["gateway"]["action"], "started")
            self.assertTrue(payload["openclaw"]["ready"])
            self.assertFalse(payload["manual_recovery_required"])
            self.assertIsNone(payload["last_error"])
            self.assertEqual(os.stat(state_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(state_path)).st_mode & 0o777, 0o700)

    def test_runtime_state_caches_stable_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path,
                supervisor.TINYHAT_COMPUTER_ID_ENV: "cmp_test_123",
                supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "1",
                "TINYHAT_DEV_RUNTIME": "",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "get_backend_base_url", return_value=""),
                patch.object(
                    supervisor,
                    "_read_metadata_path",
                    return_value="gce-instance-123",
                ) as read_metadata_path,
                patch.object(
                    supervisor,
                    "_read_runtime_repo_version",
                    return_value="0.11.0",
                ),
                patch.object(
                    supervisor,
                    "_read_runtime_git_sha",
                    return_value="abcdef1234567890",
                ) as read_runtime_git_sha,
            ):
                supervisor._write_runtime_state("healthy", "first")
                supervisor._write_runtime_state("degraded_workload", "second")
                payload = supervisor.read_runtime_state()

            self.assertEqual(payload["computer_id"], "cmp_test_123")
            self.assertEqual(payload["instance_id"], "gce-instance-123")
            self.assertEqual(payload["runtime_ref"], "0.11.0@abcdef123456")
            read_metadata_path.assert_called_once_with("instance/id", timeout=2)
            read_runtime_git_sha.assert_called_once()

    def test_runtime_state_retries_identity_misses_until_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path,
                supervisor.TINYHAT_COMPUTER_ID_ENV: "",
                supervisor.TINYHAT_GCE_INSTANCE_ID_ENV: "",
                supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "1",
                "TINYHAT_DEV_RUNTIME": "",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "get_backend_base_url", return_value=""),
                patch.object(
                    supervisor,
                    "_read_metadata_value",
                    side_effect=["", "cmp_retry_123"],
                ) as read_metadata_value,
                patch.object(
                    supervisor,
                    "_read_metadata_path",
                    side_effect=["", "gce-instance-123"],
                ) as read_metadata_path,
                patch.object(
                    supervisor,
                    "_runtime_ref",
                    side_effect=[None, "0.11.0@abcdef123456"],
                ) as runtime_ref,
            ):
                supervisor._write_runtime_state("healthy", "first")
                first_payload = supervisor.read_runtime_state()
                supervisor._write_runtime_state("degraded_workload", "second")
                second_payload = supervisor.read_runtime_state()

            self.assertIsNone(first_payload["computer_id"])
            self.assertIsNone(first_payload["instance_id"])
            self.assertIsNone(first_payload["runtime_ref"])
            self.assertEqual(second_payload["computer_id"], "cmp_retry_123")
            self.assertEqual(second_payload["instance_id"], "gce-instance-123")
            self.assertEqual(second_payload["runtime_ref"], "0.11.0@abcdef123456")
            self.assertEqual(read_metadata_value.call_count, 2)
            self.assertEqual(read_metadata_path.call_count, 2)
            self.assertEqual(runtime_ref.call_count, 2)

    def test_runtime_state_posts_payload_to_platform_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                **self._env(state_path),
                "TINYHAT_PLATFORM_BASE_URL": "https://platform.test",
            }
            posted: list[tuple[str, dict]] = []

            def fake_post_json(path: str, body: dict) -> dict:
                posted.append((path, body))
                return {"ok": True}

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.time, "time", return_value=1_760_000_000),
                patch.object(
                    supervisor,
                    "_read_runtime_repo_version",
                    return_value="0.11.0",
                ),
                patch.object(
                    supervisor,
                    "_read_runtime_git_sha",
                    return_value="abcdef1234567890",
                ),
                patch.object(supervisor, "post_json", side_effect=fake_post_json),
            ):
                supervisor._write_runtime_state(
                    "healthy",
                    "openclaw gateway started",
                    gateway_active=True,
                    gateway_action="started",
                    openclaw_ready=True,
                )
                payload = supervisor.read_runtime_state()

            self.assertEqual(
                posted[0][0],
                "/hapi/v1/computers/me/runtime-state",
            )
            self.assertEqual(posted[0][1], payload)
            self.assertEqual(posted[0][1]["runtime_health"], "healthy")

    def test_ready_runtime_state_mirror_is_best_effort(self) -> None:
        with (
            patch.object(
                supervisor,
                "_write_runtime_state",
                side_effect=PermissionError("runtime state dir unavailable"),
            ) as write_state,
            self.assertLogs("tinyhat-supervisor", level="WARNING") as logs,
        ):
            supervisor.report_ready_runtime_state()

        write_state.assert_called_once_with(
            "healthy",
            "control plane ready; awaiting binding",
            gateway_active=False,
            gateway_action="awaiting_binding",
        )
        self.assertTrue(
            any("runtime_state ready mirror failed" in line for line in logs.output)
        )

    def test_runtime_state_includes_bounded_redacted_log_excerpts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            bootstrap_log_path = os.path.join(tmpdir, "tinyhat-bootstrap.log")
            bootstrap_lines = [
                f"bootstrap line {index} ok"
                for index in range(supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES + 5)
            ]
            bootstrap_lines[-1] = (
                "bootstrap Authorization: Bearer bootstrap-token "
                "/var/lib/tinyhat-openclaw/workspace/private.txt "
                + "x" * 2_000
            )
            with open(bootstrap_log_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bootstrap_lines))

            jwt = "eyJ" + ("a" * 12) + "." + ("b" * 12) + "." + ("c" * 12)
            journal_lines = {
                supervisor.SUPERVISOR_SYSTEMD_UNIT: [
                    f"2026-06-11T10:00:{index:02d} supervisor ok {index}"
                    for index in range(supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES + 2)
                ],
                supervisor.GATEWAY_SYSTEMD_UNIT: [
                    f"2026-06-11T10:01:{index:02d} gateway ok {index}"
                    for index in range(supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES + 2)
                ],
            }
            journal_lines[supervisor.SUPERVISOR_SYSTEMD_UNIT][-1] = (
                "2026-06-11T10:00:59 supervisor Cookie: session=secret-cookie "
                f"identity={jwt}"
            )
            journal_lines[supervisor.GATEWAY_SYSTEMD_UNIT][-1] = (
                "2026-06-11T10:01:59 gateway "
                "https://storage.googleapis.com/b?X-Goog-Signature=deadbeef "
                "OPENROUTER_API_KEY=sk-or-v1-secret"
            )

            def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
                unit = args[args.index("-u") + 1]
                limit = int(args[args.index("-n") + 1])
                return SimpleNamespace(
                    returncode=0,
                    stdout="\n".join(journal_lines[unit][-limit:]),
                    stderr="",
                )

            env = {
                **self._env(state_path),
                supervisor.TINYHAT_RUNTIME_BOOTSTRAP_LOG_PATH_ENV: bootstrap_log_path,
                supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "0",
                "TINYHAT_DEV_RUNTIME": "",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            ):
                supervisor._write_runtime_state(
                    "healthy",
                    "openclaw gateway started",
                    gateway_active=True,
                    gateway_action="started",
                    openclaw_ready=True,
                )
                payload = supervisor.read_runtime_state()

        self.assertEqual(
            len(payload["bootstrap"]["log_excerpt_lines"]),
            supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
        )
        self.assertEqual(
            len(payload["supervisor"]["journal"]),
            supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
        )
        self.assertEqual(
            len(payload["gateway"]["journal"]),
            supervisor.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
        )
        self.assertTrue(
            payload["bootstrap"]["log_excerpt_lines"][0]["text"].startswith(
                "bootstrap line 5"
            )
        )
        self.assertLessEqual(
            len(payload["bootstrap"]["log_excerpt_lines"][-1]["text"]),
            supervisor.RUNTIME_STATE_LOG_LINE_MAX_CHARS,
        )
        self.assertEqual(
            payload["gateway"]["journal"][-1]["unit"],
            supervisor.GATEWAY_SYSTEMD_UNIT,
        )
        raw = json.dumps(payload, sort_keys=True)
        self.assertNotIn("bootstrap-token", raw)
        self.assertNotIn("secret-cookie", raw)
        self.assertNotIn("deadbeef", raw)
        self.assertNotIn("sk-or-v1-secret", raw)
        self.assertNotIn(jwt, raw)
        self.assertNotIn("/var/lib/tinyhat-openclaw", raw)
        self.assertIn("[redacted]", raw)
        self.assertIn("[redacted-signed-url]", raw)
        self.assertIn("[redacted-identity-token]", raw)
        self.assertIn("[local-path]", raw)

    def test_runtime_state_posts_payload_with_metadata_only_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                **self._env(state_path),
                supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "1",
                "TINYHAT_DEV_RUNTIME": "",
                "TINYHAT_PLATFORM_BASE_URL": "",
            }
            posted: list[tuple[str, dict]] = []

            def fake_post_json(path: str, body: dict) -> dict:
                posted.append((path, body))
                return {"ok": True}

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    supervisor,
                    "_read_metadata_value",
                    return_value="https://metadata-platform.test",
                ),
                patch.object(supervisor, "post_json", side_effect=fake_post_json),
            ):
                supervisor._write_runtime_state("healthy", "ok")

            self.assertEqual(len(posted), 1)
            self.assertEqual(posted[0][0], "/hapi/v1/computers/me/runtime-state")

    def test_runtime_state_platform_post_failure_keeps_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                **self._env(state_path),
                "TINYHAT_PLATFORM_BASE_URL": "https://platform.test",
            }

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "post_json", side_effect=RuntimeError("boom")),
            ):
                supervisor._write_runtime_state(
                    "degraded_workload",
                    "platform unavailable",
                    gateway_active=False,
                    openclaw_ready=False,
                )
                payload = supervisor.read_runtime_state()

            self.assertEqual(payload["runtime_health"], "degraded_workload")
            self.assertEqual(os.stat(state_path).st_mode & 0o777, 0o600)

    def test_runtime_state_skips_repeated_unchanged_platform_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                **self._env(state_path),
                "TINYHAT_PLATFORM_BASE_URL": "https://platform.test",
            }
            posted: list[tuple[str, dict]] = []

            def fake_post_json(path: str, body: dict) -> dict:
                posted.append((path, body))
                return {"ok": True}

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    supervisor.time,
                    "time",
                    side_effect=[1000, 1000, 1005, 1005, 1070, 1070],
                ),
                patch.object(
                    supervisor,
                    "get_backend_base_url",
                    return_value="https://platform.test",
                ),
                patch.object(supervisor, "post_json", side_effect=fake_post_json),
            ):
                supervisor._write_runtime_state("degraded_workload", "holding")
                supervisor._write_runtime_state("degraded_workload", "holding")
                supervisor._write_runtime_state("degraded_workload", "holding")

            self.assertEqual(len(posted), 2)

    def test_runtime_state_posts_changed_payload_inside_platform_throttle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                **self._env(state_path),
                "TINYHAT_PLATFORM_BASE_URL": "https://platform.test",
            }
            posted: list[tuple[str, dict]] = []

            def fake_post_json(path: str, body: dict) -> dict:
                posted.append((path, body))
                return {"ok": True}

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    supervisor.time,
                    "time",
                    side_effect=[1000, 1000, 1005, 1005],
                ),
                patch.object(
                    supervisor,
                    "get_backend_base_url",
                    return_value="https://platform.test",
                ),
                patch.object(supervisor, "post_json", side_effect=fake_post_json),
            ):
                supervisor._write_runtime_state("degraded_workload", "holding")
                supervisor._write_runtime_state("openclaw_not_ready", "still booting")

            self.assertEqual(len(posted), 2)
            self.assertEqual(posted[0][1]["runtime_health"], "degraded_workload")
            self.assertEqual(posted[1][1]["runtime_health"], "openclaw_not_ready")

    def test_runtime_state_skips_platform_post_without_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")

            with (
                patch.dict(os.environ, self._env(state_path), clear=False),
                patch.object(supervisor, "post_json") as post_json,
            ):
                supervisor._write_runtime_state("healthy", "ok")

            post_json.assert_not_called()

    def test_runtime_state_write_uses_tempfile_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            original_replace = os.replace
            with (
                patch.dict(os.environ, self._env(state_path), clear=False),
                patch.object(
                    supervisor.os,
                    "replace",
                    side_effect=original_replace,
                ) as replace,
            ):
                supervisor._write_runtime_state("healthy", "ok")

            replace.assert_called_once()
            tmp_path, final_path = replace.call_args.args
            self.assertEqual(final_path, state_path)
            self.assertTrue(os.path.basename(tmp_path).startswith(".tmp-"))

    def test_runtime_state_read_ignores_parse_failure_and_preserves_future_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with patch.dict(
                os.environ,
                {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                clear=False,
            ):
                with open(state_path, "w", encoding="utf-8") as fh:
                    fh.write("{not-json")
                self.assertEqual(supervisor.read_runtime_state(), {})

                future_payload = {
                    "schema": "runtime_state_v2",
                    "runtime_health": "healthy",
                    "future_field": {"kept": True},
                }
                with open(state_path, "w", encoding="utf-8") as fh:
                    json.dump(future_payload, fh)

                self.assertEqual(supervisor.read_runtime_state(), future_payload)
                self.assertEqual(
                    supervisor._runtime_state_name(future_payload),
                    "healthy",
                )

    def test_runtime_state_manual_health_sets_flag_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with patch.dict(os.environ, self._env(state_path), clear=False):
                supervisor._write_runtime_state(
                    "unrecoverable_manual",
                    "gateway recovery exhausted",
                    gateway_active=False,
                    gateway_action="blocked",
                    openclaw_ready=False,
                    last_error_category="recovery_window_timeout",
                )
                payload = supervisor.read_runtime_state()

            self.assertEqual(payload["runtime_health"], "unrecoverable_manual")
            self.assertTrue(payload["manual_recovery_required"])
            self.assertEqual(payload["gateway"]["status"], "unrecoverable_manual")
            self.assertEqual(payload["gateway"]["action"], "blocked")
            self.assertEqual(
                payload["last_error"],
                {
                    "category": "recovery_window_timeout",
                    "detail": "gateway recovery exhausted",
                },
            )

    def test_runtime_state_caps_last_error_category_for_platform_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with patch.dict(os.environ, self._env(state_path), clear=False):
                supervisor._write_runtime_state(
                    "degraded_workload",
                    "gateway recovery failed",
                    last_error_category="x" * 200,
                )
                payload = supervisor.read_runtime_state()

            self.assertEqual(
                len(payload["last_error_category"]),
                supervisor.RUNTIME_STATE_ERROR_CATEGORY_MAX_LENGTH,
            )
            self.assertEqual(
                len(payload["last_error"]["category"]),
                supervisor.RUNTIME_STATE_ERROR_CATEGORY_MAX_LENGTH,
            )

    def test_runtime_state_reads_gce_instance_metadata_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path,
                supervisor.TINYHAT_COMPUTER_ID_ENV: "cmp_test_123",
                supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "1",
                "TINYHAT_DEV_RUNTIME": "",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "get_backend_base_url", return_value=""),
                patch.object(
                    supervisor,
                    "_read_metadata_path",
                    return_value="gce-instance-123",
                ) as read_metadata_path,
            ):
                supervisor._write_runtime_state("healthy", "ok")
                payload = supervisor.read_runtime_state()

            self.assertEqual(payload["instance_id"], "gce-instance-123")
            read_metadata_path.assert_called_once_with("instance/id", timeout=2)

    def test_runtime_state_redacts_sensitive_diagnostics(self) -> None:
        detail = (
            "restart failed Authorization: Bearer bearer-secret-123 "
            "Authorization: Basic dXNlcjpwYXNz "
            "api_key=sk-test-secret token=runtime-token "
            "OPENROUTER_API_KEY=sk-or-v1-abcdef0123456789 "
            "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ "
            "/etc/openclaw/openclaw.json "
            "https://storage.googleapis.com/b?X-Goog-Signature=deadbeef "
            "https://api.telegram.org/bot1234567890:AAEqhJkLmNoPqRsTuVwXyZ12345678/deleteWebhook"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with patch.dict(os.environ, self._env(state_path), clear=False):
                supervisor._write_runtime_state(
                    "openclaw_not_ready",
                    detail,
                    gateway_active=True,
                    gateway_action="restart",
                    openclaw_ready=False,
                    last_error_category="health_check_failed",
                )
                payload = supervisor.read_runtime_state()

        raw = json.dumps(payload, sort_keys=True)
        self.assertNotIn("bearer-secret-123", raw)
        self.assertNotIn("dXNlcjpwYXNz", raw)
        self.assertNotIn("sk-test-secret", raw)
        self.assertNotIn("sk-or-v1-abcdef0123456789", raw)
        self.assertNotIn("runtime-token", raw)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", raw)
        self.assertNotIn("AAEqhJkLmNoPqRsTuVwXyZ12345678", raw)
        self.assertNotIn("api.telegram.org/bot", raw)
        self.assertNotIn("/etc/openclaw/openclaw.json", raw)
        self.assertNotIn("deadbeef", raw)
        self.assertIn("[redacted]", raw)
        self.assertIn("[local-path]", raw)
        self.assertEqual(payload["last_error"]["category"], "health_check_failed")
        self.assertEqual(payload["runtime_health"], "openclaw_not_ready")

    def test_runtime_state_sanitizer_redacts_freeform_log_secrets(self) -> None:
        aws_access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        slack_token = "xoxb-" + "123456789012-abcdefghijklmnop"
        detail = (
            "provider rejected sk-proj-AbCdEf012345678901234567890abc "
            "anthropic 401 sk-ant-api03-AbCdEf0123456789012345678901234567 "
            "openrouter retry sk-or-v1-0123456789abcdef0123456789abcdef "
            "clone https://oauth2:GENERICtoken@github.com/example/repo.git "
            "{\"access_token\": \"json-access-token-secret\", "
            "\"password\": \"json-password-secret\"} "
            f"aws key {aws_access_key} "
            "aws_secret_access_key=\"aws-secret-access-key-value\" "
            f"slack {slack_token}"
        )

        redacted = supervisor._sanitize_runtime_state_text(detail, limit=4096)

        self.assertNotIn("sk-proj-AbCdEf012345678901234567890abc", redacted)
        self.assertNotIn(
            "sk-ant-api03-AbCdEf0123456789012345678901234567",
            redacted,
        )
        self.assertNotIn(
            "sk-or-v1-0123456789abcdef0123456789abcdef",
            redacted,
        )
        self.assertNotIn("GENERICtoken", redacted)
        self.assertNotIn("json-access-token-secret", redacted)
        self.assertNotIn("json-password-secret", redacted)
        self.assertNotIn(aws_access_key, redacted)
        self.assertNotIn("aws-secret-access-key-value", redacted)
        self.assertNotIn(slack_token, redacted)
        self.assertIn("[redacted-api-key]", redacted)
        self.assertIn("[redacted-userinfo]", redacted)
        self.assertIn("[redacted-aws-key]", redacted)
        self.assertIn("[redacted-slack-token]", redacted)

    def test_control_plane_state_dir_chowns_root_when_running_as_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control_dir = os.path.join(tmpdir, "tinyhat-control")
            with (
                patch.object(supervisor.os, "geteuid", return_value=0),
                patch.object(supervisor.os, "chown") as chown,
            ):
                supervisor._prepare_control_plane_state_dir(control_dir)

            chown.assert_called_once_with(control_dir, 0, 0)
            self.assertEqual(os.stat(control_dir).st_mode & 0o777, 0o700)


class OpenClawGatewayReattachTests(unittest.TestCase):
    def _fingerprint(self, value: str = "abc123") -> dict[str, str]:
        return {
            "algorithm": "sha256",
            "source": "openclaw_config",
            "path": "/etc/openclaw/openclaw.json",
            "value": value,
        }

    def test_reattach_keeps_healthy_matching_gateway_running(self) -> None:
        binding = {
            "telegram_bot_username": "Tinychattestbot",
            "telegram_owner_user_id": "123456",
        }
        fingerprint = self._fingerprint()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            marker_path = os.path.join(tmpdir, "clear-unrecoverable-manual")
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path,
                supervisor.TINYHAT_RUNTIME_STATE_CLEAR_MANUAL_PATH_ENV: marker_path,
            }
            with patch.dict(os.environ, env, clear=False):
                supervisor._write_runtime_state(
                    "healthy",
                    "previous healthy start",
                    config_fingerprint=fingerprint,
                    gateway_active=True,
                    gateway_action="started",
                    openclaw_ready=True,
                )
                with (
                    patch.object(
                        supervisor, "is_openclaw_gateway_active", return_value=True
                    ),
                    patch.object(
                        supervisor,
                        "probe_current_openclaw_gateway_health",
                        return_value=(True, "ok"),
                    ),
                    patch.object(supervisor, "delete_telegram_webhook") as delete,
                    patch.object(supervisor, "start_openclaw_gateway") as start,
                    patch.object(supervisor, "wait_for_openclaw_start") as wait,
                ):
                    result = supervisor.ensure_openclaw_gateway_ready(
                        binding,
                        fingerprint,
                    )

                self.assertEqual(result["action"], "reattached")
                delete.assert_not_called()
                start.assert_not_called()
                wait.assert_not_called()
                with open(state_path, encoding="utf-8") as fh:
                    state = json.load(fh)
                self.assertEqual(state["state"], "healthy")
                self.assertEqual(state["gateway"]["action"], "reattached")
                self.assertTrue(state["openclaw"]["ready"])
                self.assertEqual(state["config_fingerprint"], fingerprint)

    def test_gateway_oom_baseline_preserves_reattach_fingerprint(self) -> None:
        fingerprint = self._fingerprint()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            env = {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path}
            snapshot = {
                "available": True,
                "control_group": "/tinyhat.slice/workload",
                "memory_current_bytes": 500,
                "memory_max_bytes": 1000,
                "memory_events": {"oom": 1, "oom_kill": 3},
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.time, "time", return_value=100),
                patch.object(
                    supervisor,
                    "is_openclaw_gateway_active",
                    return_value=True,
                ),
            ):
                supervisor._write_runtime_state(
                    "healthy",
                    "openclaw gateway started",
                    config_fingerprint=fingerprint,
                    gateway_active=True,
                    gateway_action="started",
                    openclaw_ready=True,
                )
                result = supervisor._record_gateway_oom_delta(snapshot)

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        self.assertEqual(result, "baseline")
        self.assertEqual(state["state"], "healthy")
        self.assertEqual(state["config_fingerprint"], fingerprint)
        self.assertTrue(state["openclaw"]["ready"])
        self.assertTrue(
            supervisor._runtime_state_config_fingerprint_matches(
                state,
                fingerprint,
            )
        )

    def test_active_not_ready_records_degradation_before_restart(self) -> None:
        binding = {"telegram_bot_username": "Tinychattestbot"}
        fingerprint = self._fingerprint()
        writes: list[str] = []

        def fake_write_runtime_state(state: str, _detail: str, **_kwargs) -> None:
            writes.append(state)

        with (
            patch.object(
                supervisor,
                "read_runtime_state",
                return_value={
                    "state": "healthy",
                    "config_fingerprint": fingerprint,
                },
            ),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(
                supervisor,
                "probe_current_openclaw_gateway_health",
                return_value=(False, "waiting for OpenClaw telegram connected"),
            ),
            patch.object(
                supervisor,
                "_write_runtime_state",
                side_effect=fake_write_runtime_state,
            ),
            patch.object(supervisor, "delete_telegram_webhook") as delete,
            patch.object(
                supervisor, "start_openclaw_gateway", return_value=123.0
            ) as start,
            patch.object(supervisor, "wait_for_openclaw_start") as wait,
        ):
            result = supervisor.ensure_openclaw_gateway_ready(binding, fingerprint)

        self.assertEqual(result["action"], "started")
        self.assertEqual(
            writes,
            ["degraded_workload", "openclaw_not_ready", "healthy"],
        )
        delete.assert_called_once_with(binding)
        start.assert_called_once_with(binding)
        wait.assert_called_once_with(123.0)

    def test_healthy_gateway_with_config_mismatch_restarts(self) -> None:
        binding = {"telegram_bot_username": "Tinychattestbot"}
        fingerprint = self._fingerprint("new")
        writes: list[str] = []

        def fake_write_runtime_state(state: str, _detail: str, **_kwargs) -> None:
            writes.append(state)

        with (
            patch.object(
                supervisor,
                "read_runtime_state",
                return_value={
                    "state": "healthy",
                    "config_fingerprint": self._fingerprint("old"),
                },
            ),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(
                supervisor,
                "probe_current_openclaw_gateway_health",
                return_value=(True, "ok"),
            ),
            patch.object(
                supervisor,
                "_write_runtime_state",
                side_effect=fake_write_runtime_state,
            ),
            patch.object(supervisor, "delete_telegram_webhook") as delete,
            patch.object(
                supervisor, "start_openclaw_gateway", return_value=123.0
            ) as start,
            patch.object(supervisor, "wait_for_openclaw_start"),
        ):
            result = supervisor.ensure_openclaw_gateway_ready(binding, fingerprint)

        self.assertEqual(result["action"], "started")
        self.assertEqual(writes, ["healthy"])
        delete.assert_called_once_with(binding)
        start.assert_called_once_with(binding)

    def test_unrecoverable_manual_blocks_restart_without_clear_marker(
        self,
    ) -> None:
        binding = {"telegram_bot_username": "Tinychattestbot"}
        fingerprint = self._fingerprint()
        writes: list[str] = []

        def fake_write_runtime_state(state: str, _detail: str, **_kwargs) -> None:
            writes.append(state)

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_CLEAR_MANUAL_PATH_ENV: os.path.join(
                    tmpdir,
                    "clear-unrecoverable-manual",
                )
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    supervisor,
                    "read_runtime_state",
                    return_value={
                        "state": "unrecoverable_manual",
                        "manual_recovery_required": True,
                    },
                ),
                patch.object(
                    supervisor, "is_openclaw_gateway_active", return_value=True
                ),
                patch.object(
                    supervisor,
                    "_write_runtime_state",
                    side_effect=fake_write_runtime_state,
                ),
                patch.object(supervisor, "delete_telegram_webhook") as delete,
                patch.object(supervisor, "start_openclaw_gateway") as start,
            ):
                with self.assertRaises(supervisor.ManualRecoveryRequired):
                    supervisor.ensure_openclaw_gateway_ready(binding, fingerprint)

        self.assertEqual(writes, ["unrecoverable_manual"])
        delete.assert_not_called()
        start.assert_not_called()

    def test_manual_recovery_marker_writes_unrecoverable_manual_first(
        self,
    ) -> None:
        binding = {"telegram_bot_username": "Tinychattestbot"}
        fingerprint = self._fingerprint()
        writes: list[str] = []

        def fake_write_runtime_state(state: str, _detail: str, **_kwargs) -> None:
            writes.append(state)

        with tempfile.TemporaryDirectory() as tmpdir:
            manual_marker_path = os.path.join(tmpdir, "unrecoverable-manual")
            with open(manual_marker_path, "w", encoding="utf-8") as fh:
                fh.write("operator requested manual recovery\n")
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_MANUAL_MARKER_PATH_ENV: (
                    manual_marker_path
                ),
                supervisor.TINYHAT_RUNTIME_STATE_CLEAR_MANUAL_PATH_ENV: os.path.join(
                    tmpdir,
                    "clear-unrecoverable-manual",
                ),
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "read_runtime_state", return_value={}),
                patch.object(
                    supervisor, "is_openclaw_gateway_active", return_value=True
                ),
                patch.object(
                    supervisor,
                    "_write_runtime_state",
                    side_effect=fake_write_runtime_state,
                ),
                patch.object(supervisor, "delete_telegram_webhook") as delete,
                patch.object(supervisor, "start_openclaw_gateway") as start,
            ):
                with self.assertRaises(supervisor.ManualRecoveryRequired):
                    supervisor.ensure_openclaw_gateway_ready(binding, fingerprint)

        self.assertEqual(writes, ["unrecoverable_manual"])
        delete.assert_not_called()
        start.assert_not_called()

    def test_unrecoverable_manual_clear_marker_allows_recovery(self) -> None:
        binding = {"telegram_bot_username": "Tinychattestbot"}
        fingerprint = self._fingerprint()

        with tempfile.TemporaryDirectory() as tmpdir:
            manual_marker_path = os.path.join(tmpdir, "unrecoverable-manual")
            with open(manual_marker_path, "w", encoding="utf-8") as fh:
                fh.write("manual recovery requested\n")
            marker_path = os.path.join(tmpdir, "clear-unrecoverable-manual")
            with open(marker_path, "w", encoding="utf-8") as fh:
                fh.write("operator cleared\n")
            env = {
                supervisor.TINYHAT_RUNTIME_STATE_MANUAL_MARKER_PATH_ENV: (
                    manual_marker_path
                ),
                supervisor.TINYHAT_RUNTIME_STATE_CLEAR_MANUAL_PATH_ENV: marker_path
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    supervisor,
                    "read_runtime_state",
                    return_value={
                        "state": "unrecoverable_manual",
                        "manual_recovery_required": True,
                    },
                ),
                patch.object(
                    supervisor, "is_openclaw_gateway_active", return_value=False
                ),
                patch.object(supervisor, "_write_runtime_state"),
                patch.object(supervisor, "delete_telegram_webhook") as delete,
                patch.object(
                    supervisor, "start_openclaw_gateway", return_value=123.0
                ) as start,
                patch.object(supervisor, "wait_for_openclaw_start") as wait,
            ):
                result = supervisor.ensure_openclaw_gateway_ready(
                    binding,
                    fingerprint,
                )

            self.assertFalse(os.path.exists(marker_path))
            self.assertFalse(os.path.exists(manual_marker_path))

        self.assertEqual(result["action"], "started")
        delete.assert_called_once_with(binding)
        start.assert_called_once_with(binding)
        wait.assert_called_once_with(123.0)


class SupervisorWatchdogContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._last_watchdog_checkpoint_ts = supervisor._last_watchdog_checkpoint_ts
        supervisor._last_watchdog_checkpoint_ts = 0.0

    def tearDown(self) -> None:
        supervisor._last_watchdog_checkpoint_ts = self._last_watchdog_checkpoint_ts

    def test_ready_notification_does_not_feed_watchdog(self) -> None:
        with patch.object(
            supervisor,
            "_sd_notify",
            return_value=True,
        ) as sd_notify:
            self.assertTrue(supervisor.notify_supervisor_ready())

        sd_notify.assert_called_once()
        message = sd_notify.call_args.args[0]
        self.assertIn("READY=1", message)
        self.assertNotIn("WATCHDOG=1", message)

    def test_watchdog_checkpoint_sends_watchdog_after_sanitizing_status(
        self,
    ) -> None:
        with patch.object(
            supervisor,
            "_sd_notify",
            return_value=True,
        ) as sd_notify:
            self.assertTrue(
                supervisor.notify_watchdog_checkpoint("phase d: heartbeat ok")
            )

        sd_notify.assert_called_once()
        message = sd_notify.call_args.args[0]
        self.assertIn("WATCHDOG=1", message)
        self.assertIn("STATUS=checkpoint phase-d:-heartbeat-ok", message)
        self.assertNotIn("READY=1", message)

    def test_watchdog_checkpoint_warns_when_gap_exceeds_target(self) -> None:
        supervisor._last_watchdog_checkpoint_ts = 10.0
        with (
            patch.object(supervisor.time, "time", return_value=60.5),
            patch.object(supervisor, "_sd_notify", return_value=True),
            self.assertLogs("tinyhat-supervisor", level="WARNING") as logs,
        ):
            self.assertTrue(supervisor.notify_watchdog_checkpoint("late phase"))

        self.assertIn("watchdog checkpoint gap exceeded", "\n".join(logs.output))
        self.assertEqual(supervisor._last_watchdog_checkpoint_ts, 60.5)

    def test_sd_notify_supports_abstract_namespace_socket(self) -> None:
        events = []

        class _FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def connect(self, address):
                events.append(("connect", address))

            def sendall(self, payload):
                events.append(("sendall", payload))

        with (
            patch.dict(os.environ, {"NOTIFY_SOCKET": "@tinyhat-notify"}, clear=False),
            patch.object(
                supervisor.socket,
                "socket",
                return_value=_FakeSocket(),
            ) as socket_ctor,
        ):
            self.assertTrue(supervisor._sd_notify("WATCHDOG=1"))

        socket_ctor.assert_called_once_with(
            supervisor.socket.AF_UNIX,
            supervisor.socket.SOCK_DGRAM,
        )
        self.assertEqual(events[0], ("connect", "\0tinyhat-notify"))
        self.assertEqual(events[1], ("sendall", b"WATCHDOG=1"))

    def test_progress_checkpoint_reads_locals_and_cgroup_before_notify(
        self,
    ) -> None:
        calls = []

        def _local_snapshot():
            calls.append("local")
            return {}

        def _cgroup_snapshot():
            calls.append("cgroup")
            return {}

        def _notify(checkpoint):
            calls.append(f"notify:{checkpoint}")
            return True

        with (
            patch.object(
                supervisor,
                "local_watchdog_manifest_snapshot",
                side_effect=_local_snapshot,
            ),
            patch.object(
                supervisor,
                "gateway_cgroup_memory_snapshot",
                side_effect=_cgroup_snapshot,
            ),
            patch.object(
                supervisor,
                "notify_watchdog_checkpoint",
                side_effect=_notify,
            ),
        ):
            self.assertTrue(
                supervisor.checkpoint_supervisor_progress(
                    "phase-c-gateway-started",
                    inspect_gateway=True,
                )
            )

        self.assertEqual(
            calls,
            ["local", "cgroup", "notify:phase-c-gateway-started"],
        )

    def test_gateway_cgroup_memory_snapshot_reads_cgroup_v2_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cgroup = "system.slice/tinyhat-openclaw-gateway.service"
            cgroup_dir = os.path.join(tmpdir, cgroup)
            os.makedirs(cgroup_dir, exist_ok=True)
            with open(
                os.path.join(cgroup_dir, "memory.current"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("12345\n")
            with open(
                os.path.join(cgroup_dir, "memory.max"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("max\n")
            with open(
                os.path.join(cgroup_dir, "memory.events.local"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("low 0\nhigh 2\nmax 0\noom 1\noom_kill 1\n")

            with (
                patch.object(supervisor, "_dev_mode", return_value=False),
                patch.object(
                    supervisor,
                    "_run_systemctl",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout="/" + cgroup,
                    ),
                ) as run_systemctl,
                patch.dict(os.environ, {"TINYHAT_CGROUP_ROOT": tmpdir}, clear=False),
            ):
                snapshot = supervisor.gateway_cgroup_memory_snapshot()

        run_systemctl.assert_called_once_with(
            "show",
            supervisor.GATEWAY_SYSTEMD_UNIT,
            "--property=ControlGroup",
            "--value",
            check=False,
        )
        self.assertEqual(snapshot["available"], True)
        self.assertEqual(snapshot["control_group"], "/" + cgroup)
        self.assertEqual(snapshot["memory_current_bytes"], 12345)
        self.assertEqual(snapshot["memory_max_bytes"], "max")
        self.assertEqual(snapshot["memory_events"]["oom_kill"], 1)

    def test_gateway_cgroup_memory_snapshot_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(supervisor, "_dev_mode", return_value=False),
                patch.object(
                    supervisor,
                    "_run_systemctl",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout="../../outside",
                    ),
                ),
                patch.dict(os.environ, {"TINYHAT_CGROUP_ROOT": tmpdir}, clear=False),
            ):
                snapshot = supervisor.gateway_cgroup_memory_snapshot()

        self.assertEqual(snapshot["available"], False)
        self.assertEqual(snapshot["reason"], "invalid-control-group-path")

    def test_gateway_cgroup_memory_snapshot_falls_back_to_workload_slice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cgroup = "tinyhat.slice/tinyhat-openclaw-workload.slice"
            cgroup_dir = os.path.join(tmpdir, cgroup)
            os.makedirs(cgroup_dir, exist_ok=True)
            with open(
                os.path.join(cgroup_dir, "memory.current"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("700\n")
            with open(
                os.path.join(cgroup_dir, "memory.max"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("1000\n")
            with open(
                os.path.join(cgroup_dir, "memory.events.local"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("oom 2\noom_kill 1\n")

            def fake_systemctl(*args, **_kwargs):
                unit = args[1]
                if unit == supervisor.GATEWAY_SYSTEMD_UNIT:
                    return SimpleNamespace(returncode=0, stdout="\n")
                self.assertEqual(unit, supervisor.GATEWAY_WORKLOAD_SLICE_UNIT)
                return SimpleNamespace(returncode=0, stdout="/" + cgroup)

            with (
                patch.object(supervisor, "_dev_mode", return_value=False),
                patch.object(
                    supervisor,
                    "_run_systemctl",
                    side_effect=fake_systemctl,
                ),
                patch.dict(os.environ, {"TINYHAT_CGROUP_ROOT": tmpdir}, clear=False),
            ):
                snapshot = supervisor.gateway_cgroup_memory_snapshot()

        self.assertEqual(snapshot["available"], True)
        self.assertEqual(snapshot["unit"], supervisor.GATEWAY_WORKLOAD_SLICE_UNIT)
        self.assertEqual(snapshot["memory_current_bytes"], 700)
        self.assertEqual(snapshot["memory_max_bytes"], 1000)
        self.assertEqual(snapshot["memory_events"]["oom_kill"], 1)

    def test_gateway_oom_delta_enters_hold_down_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "state": "healthy",
                        "detail": "previously healthy",
                        "gateway_recovery": {
                            "last_oom_kill": 4,
                            "hold_down_cycles": 0,
                            "failures": [
                                {"at_unix": 100, "reason": "restart_failed"},
                                {"at_unix": 200, "reason": "restart_failed"},
                            ],
                        },
                    },
                    fh,
                )
            snapshot = {
                "available": True,
                "control_group": "/tinyhat.slice/workload",
                "memory_current_bytes": 600,
                "memory_max_bytes": 1000,
                "memory_events": {"oom": 2, "oom_kill": 5},
            }
            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                    clear=False,
                ),
                patch.object(supervisor.time, "time", return_value=250),
                patch.object(
                    supervisor,
                    "is_openclaw_gateway_active",
                    return_value=False,
                ),
            ):
                result = supervisor._record_gateway_oom_delta(snapshot)

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        self.assertEqual(result, "hold_down")
        self.assertEqual(state["state"], "degraded_workload")
        self.assertEqual(state["gateway"]["action"], "hold_down")
        self.assertEqual(state["last_error_category"], "oom_kill")
        policy = state["gateway_recovery"]
        self.assertEqual(policy["hold_down_cycles"], 1)
        self.assertEqual(policy["hold_down_until_unix"], 850)
        self.assertEqual(policy["last_oom_kill"], 5)
        self.assertEqual(len(policy["failures"]), 3)

    def test_gateway_recovery_wait_requires_memory_and_oom_stability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "state": "degraded_workload",
                        "detail": "hold-down",
                        "gateway_recovery": {
                            "last_oom_kill": 7,
                            "hold_down_cycles": 1,
                            "hold_down_until_unix": 100,
                            "failures": [
                                {"at_unix": 90, "reason": "oom_kill"},
                            ],
                        },
                    },
                    fh,
                )
            high = {
                "available": True,
                "control_group": "/tinyhat.slice/workload",
                "memory_current_bytes": 701,
                "memory_max_bytes": 1000,
                "memory_events": {"oom": 2, "oom_kill": 7},
            }
            stable = {
                "available": True,
                "control_group": "/tinyhat.slice/workload",
                "memory_current_bytes": 700,
                "memory_max_bytes": 1000,
                "memory_events": {"oom": 2, "oom_kill": 7},
            }
            snapshots = [high, stable, stable, stable]
            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                    clear=False,
                ),
                patch.object(supervisor.time, "time", return_value=100),
                patch.object(
                    supervisor,
                    "gateway_cgroup_memory_snapshot",
                    side_effect=snapshots,
                ),
                patch.object(
                    supervisor,
                    "checkpoint_supervisor_progress",
                    return_value=True,
                ),
                patch.object(
                    supervisor,
                    "is_openclaw_gateway_active",
                    return_value=False,
                ),
                patch.object(supervisor.time, "sleep") as sleep,
            ):
                supervisor._wait_for_gateway_recovery_window()

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        self.assertEqual(
            state["gateway"]["action"],
            "recovery_window_satisfied",
        )
        self.assertNotIn(
            "hold_down_until_unix",
            state["gateway_recovery"],
        )
        self.assertEqual(state["gateway_recovery"]["last_oom_kill"], 7)
        self.assertEqual(sleep.call_count, 3)

    def test_gateway_recovery_after_two_hold_down_cycles_goes_manual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "state": "degraded_workload",
                        "detail": "previous hold-downs failed",
                        "gateway_recovery": {
                            "last_oom_kill": 10,
                            "hold_down_cycles": 2,
                            "failures": [
                                {"at_unix": 100, "reason": "restart_failed"},
                                {"at_unix": 200, "reason": "restart_failed"},
                            ],
                        },
                    },
                    fh,
                )
            snapshot = {
                "available": True,
                "memory_current_bytes": 800,
                "memory_max_bytes": 1000,
                "memory_events": {"oom": 3, "oom_kill": 11},
            }
            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                    clear=False,
                ),
                patch.object(supervisor.time, "time", return_value=250),
                patch.object(
                    supervisor,
                    "is_openclaw_gateway_active",
                    return_value=False,
                ),
            ):
                result = supervisor._record_gateway_oom_delta(snapshot)

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        self.assertEqual(result, "manual")
        self.assertEqual(state["state"], "unrecoverable_manual")
        self.assertTrue(state["manual_recovery_required"])
        self.assertEqual(state["gateway"]["action"], "blocked")

    def test_gateway_recovery_wait_unavailable_samples_exhaust_to_manual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "state": "degraded_workload",
                        "detail": "hold-down expired",
                        "gateway_recovery": {
                            "last_oom_kill": 10,
                            "hold_down_cycles": 2,
                            "hold_down_until_unix": 100,
                            "failures": [
                                {"at_unix": 90, "reason": "restart_failed"},
                                {"at_unix": 95, "reason": "restart_failed"},
                            ],
                        },
                    },
                    fh,
                )
            unavailable = {
                "available": False,
                "reason": "gateway-cgroup-unavailable",
            }
            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                    clear=False,
                ),
                patch.object(supervisor.time, "time", return_value=100),
                patch.object(
                    supervisor,
                    "gateway_cgroup_memory_snapshot",
                    return_value=unavailable,
                ),
                patch.object(
                    supervisor,
                    "checkpoint_supervisor_progress",
                    return_value=True,
                ),
                patch.object(
                    supervisor,
                    "is_openclaw_gateway_active",
                    return_value=False,
                ),
                patch.object(
                    supervisor,
                    "GATEWAY_RECOVERY_MEMORY_WAIT_MAX_SAMPLES",
                    3,
                ),
                patch.object(supervisor.time, "sleep") as sleep,
            ):
                with self.assertRaises(supervisor.ManualRecoveryRequired):
                    supervisor._wait_for_gateway_recovery_window()

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        self.assertEqual(state["state"], "unrecoverable_manual")
        self.assertTrue(state["manual_recovery_required"])
        self.assertEqual(state["gateway"]["action"], "blocked")
        self.assertEqual(state["last_error_category"], "recovery_window_timeout")
        self.assertEqual(
            state["gateway_recovery"]["last_error_category"],
            "recovery_window_timeout",
        )
        self.assertEqual(sleep.call_count, 2)

    def test_gateway_recovery_wait_timeout_cycles_escalate_with_real_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "state": "degraded_workload",
                        "detail": "first hold-down expired",
                        "gateway_recovery": {
                            "last_oom_kill": 10,
                            "hold_down_cycles": 1,
                            "hold_down_until_unix": 100,
                            "failures": [],
                        },
                    },
                    fh,
                )
            unavailable = {
                "available": False,
                "reason": "gateway-cgroup-unavailable",
            }
            clock = {"now": 100}

            def _time() -> float:
                return float(clock["now"])

            def _sleep(seconds: float) -> None:
                clock["now"] += seconds

            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                    clear=False,
                ),
                patch.object(supervisor.time, "time", side_effect=_time),
                patch.object(
                    supervisor,
                    "gateway_cgroup_memory_snapshot",
                    return_value=unavailable,
                ),
                patch.object(
                    supervisor,
                    "checkpoint_supervisor_progress",
                    return_value=True,
                ),
                patch.object(
                    supervisor,
                    "is_openclaw_gateway_active",
                    return_value=False,
                ),
                patch.object(supervisor.time, "sleep", side_effect=_sleep) as sleep,
            ):
                with self.assertRaises(supervisor.ManualRecoveryRequired):
                    supervisor._wait_for_gateway_recovery_window()

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        expected_sampling_sleeps = (
            supervisor.GATEWAY_RECOVERY_MEMORY_WAIT_MAX_SAMPLES - 1
        ) * 2
        expected_hold_down_sleeps = (
            supervisor.GATEWAY_RECOVERY_HOLD_DOWN_SECONDS
            // supervisor.GATEWAY_RECOVERY_MEMORY_SAMPLE_INTERVAL_SECONDS
        )
        self.assertEqual(
            sleep.call_count,
            expected_sampling_sleeps + expected_hold_down_sleeps,
        )
        self.assertEqual(state["state"], "unrecoverable_manual")
        self.assertTrue(state["manual_recovery_required"])
        self.assertEqual(state["gateway"]["action"], "blocked")
        self.assertEqual(state["last_error_category"], "recovery_window_timeout")
        self.assertEqual(state["gateway_recovery"]["hold_down_cycles"], 2)
        self.assertEqual(
            state["gateway_recovery"]["last_error_category"],
            "recovery_window_timeout",
        )

    def test_stable_healthy_window_resets_gateway_recovery_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "runtime-state.json")
            fingerprint = {
                "algorithm": "sha256",
                "source": "openclaw_config",
                "path": "/etc/openclaw/openclaw.json",
                "value": "stable",
            }
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "state": "healthy",
                        "detail": "gateway healthy",
                        "updated_at_unix": 100,
                        "config_fingerprint": fingerprint,
                        "openclaw": {"ready": True},
                        "gateway_recovery": {
                            "last_oom_kill": 5,
                            "hold_down_cycles": 1,
                            "failures": [
                                {"at_unix": 50, "reason": "oom_kill"},
                            ],
                        },
                    },
                    fh,
                )
            snapshot = {
                "available": True,
                "memory_current_bytes": 500,
                "memory_max_bytes": 1000,
                "memory_events": {"oom": 1, "oom_kill": 5},
            }
            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: state_path},
                    clear=False,
                ),
                patch.object(supervisor.time, "time", return_value=2000),
            ):
                self.assertTrue(
                    supervisor._reset_gateway_recovery_after_stable_healthy(
                        snapshot
                    )
                )

            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)

        self.assertEqual(state["state"], "healthy")
        self.assertEqual(state["gateway"]["action"], "stable_reset")
        self.assertEqual(state["config_fingerprint"], fingerprint)
        self.assertTrue(state["openclaw"]["ready"])
        self.assertEqual(state["gateway_recovery"]["failures"], [])
        self.assertEqual(state["gateway_recovery"]["hold_down_cycles"], 0)
        self.assertEqual(state["gateway_recovery"]["last_oom_kill"], 5)

    def test_platform_http_timeouts_are_bounded(self) -> None:
        observed_timeouts = []

        class _Resp:
            def __init__(self, payload: bytes):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self._payload

        def _urlopen(_req, timeout):
            observed_timeouts.append(timeout)
            return _Resp(b"{}")

        with (
            patch.object(supervisor, "_dev_mode", return_value=False),
            patch.object(supervisor, "get_backend_base_url", return_value="https://p"),
            patch.object(supervisor, "fetch_identity_token", return_value="tok"),
            patch.object(supervisor.urllib.request, "urlopen", side_effect=_urlopen),
        ):
            supervisor.post_json("/x", {})
            supervisor.get_json("/x")

        self.assertEqual(
            observed_timeouts,
            [
                supervisor.PLATFORM_REQUEST_TIMEOUT_SECONDS,
                supervisor.PLATFORM_REQUEST_TIMEOUT_SECONDS,
            ],
        )

    def test_me_state_post_sanitizes_detail_before_platform_send(self) -> None:
        observed_bodies = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b"{}"

        def _urlopen(req, timeout):
            observed_bodies.append(json.loads(req.data.decode("utf-8")))
            return _Resp()

        detail = (
            "gateway failed Authorization: Basic dXNlcjpwYXNz "
            "OPENROUTER_API_KEY=sk-or-v1-abcdef0123456789"
        )
        with (
            patch.object(supervisor, "get_backend_base_url", return_value="https://p"),
            patch.object(supervisor, "fetch_identity_token", return_value="tok"),
            patch.object(supervisor.urllib.request, "urlopen", side_effect=_urlopen),
        ):
            supervisor.post_json(
                "/hapi/v1/computers/me/state",
                {"state": "broken", "detail": detail},
            )
            supervisor.post_json("/hapi/v1/other", {"detail": detail})

        state_body = observed_bodies[0]
        passthrough_body = observed_bodies[1]
        state_raw = json.dumps(state_body, sort_keys=True)
        self.assertEqual(state_body["state"], "broken")
        self.assertNotIn("dXNlcjpwYXNz", state_raw)
        self.assertNotIn("sk-or-v1-abcdef0123456789", state_raw)
        self.assertIn("dXNlcjpwYXNz", json.dumps(passthrough_body))

    def test_gce_identity_timeout_is_bounded(self) -> None:
        observed_timeouts = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b"identity-token"

        def _urlopen(_req, timeout):
            observed_timeouts.append(timeout)
            return _Resp()

        with (
            patch.object(supervisor, "_dev_mode", return_value=False),
            patch.object(supervisor, "get_backend_audience", return_value="aud"),
            patch.object(supervisor.urllib.request, "urlopen", side_effect=_urlopen),
        ):
            self.assertEqual(supervisor.fetch_identity_token(), "identity-token")

        self.assertEqual(
            observed_timeouts,
            [supervisor.GCE_IDENTITY_TOKEN_TIMEOUT_SECONDS],
        )

    def test_systemctl_timeout_is_reported_without_hanging_inspection(self) -> None:
        def _timeout(*_args, **_kwargs):
            raise supervisor.subprocess.TimeoutExpired(
                cmd=["systemctl", "show"],
                timeout=supervisor.SYSTEMCTL_TIMEOUT_SECONDS,
            )

        with patch.object(supervisor.subprocess, "run", side_effect=_timeout):
            result = supervisor._run_systemctl("show", check=False)
            self.assertEqual(result.returncode, 124)
            self.assertEqual(result.stderr, "timed out")
            with self.assertRaisesRegex(RuntimeError, "systemctl show timed out"):
                supervisor._run_systemctl("show")

        with patch.object(
            supervisor.subprocess,
            "run",
            side_effect=FileNotFoundError("systemctl"),
        ):
            result = supervisor._run_systemctl("show", check=False)
            self.assertEqual(result.returncode, 127)
            self.assertIn("systemctl", result.stderr)


class TinyhatPluginInstallTests(unittest.TestCase):
    def test_install_clones_public_repo_and_records_marker(self) -> None:
        repo_url = "https://example.com/tinyhat.git"
        repo_ref = "refs/tags/v0.5.0"
        plugin_sha = "abc123def4567890"

        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = os.path.join(tmpdir, "tinyhat-plugin")
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_PLUGIN_CHECKOUT_DIR": plugin_dir,
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL": repo_url,
                "TINYHAT_PLATFORM_PLUGIN_REPO_REF": repo_ref,
            }
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd == ["git", "clone", repo_url, plugin_dir]:
                    os.makedirs(os.path.join(plugin_dir, ".git"))
                    with open(
                        os.path.join(plugin_dir, "openclaw.plugin.json"),
                        "w",
                        encoding="utf-8",
                    ) as fh:
                        json.dump({"id": "tinyhat"}, fh)
                    with open(
                        os.path.join(plugin_dir, "package.json"),
                        "w",
                        encoding="utf-8",
                    ) as fh:
                        json.dump({"version": "0.5.0"}, fh)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "checkout", repo_ref]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "rev-parse", "HEAD"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{plugin_sha}\n",
                        stderr="",
                    )
                if cmd == ["openclaw", "plugins", "install", plugin_dir, "--force"]:
                    self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], tmpdir)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                self.fail(f"unexpected command: {cmd}")

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(supervisor.ensure_tinyhat_plugin_installed())

            self.assertIn(["git", "clone", repo_url, plugin_dir], calls)
            self.assertIn(["openclaw", "plugins", "install", plugin_dir, "--force"], calls)
            with open(
                os.path.join(tmpdir, "tinyhat-plugin.version"),
                encoding="utf-8",
            ) as fh:
                marker = json.load(fh)
            self.assertEqual(
                marker,
                {
                    "repo_ref": repo_ref,
                    "repo_url": repo_url,
                    "resolved_commit_sha": plugin_sha,
                    "version": "0.5.0",
                },
            )

    def test_installed_marker_skips_openclaw_install_after_fetch(self) -> None:
        repo_url = "https://example.com/tinyhat.git"
        repo_ref = "main"
        plugin_sha = "fedcba9876543210"

        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = os.path.join(tmpdir, "tinyhat-plugin")
            os.makedirs(os.path.join(plugin_dir, ".git"))
            with open(
                os.path.join(plugin_dir, "openclaw.plugin.json"),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump({"id": "tinyhat"}, fh)
            with open(
                os.path.join(plugin_dir, "package.json"),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump({"version": "0.5.0"}, fh)
            os.makedirs(os.path.join(tmpdir, "extensions", "tinyhat"))
            with open(
                os.path.join(
                    tmpdir,
                    "extensions",
                    "tinyhat",
                    "openclaw.plugin.json",
                ),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump({"id": "tinyhat"}, fh)
            with open(
                os.path.join(tmpdir, "tinyhat-plugin.version"),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump(
                    {
                        "repo_ref": repo_ref,
                        "repo_url": repo_url,
                        "resolved_commit_sha": plugin_sha,
                        "version": "0.5.0",
                    },
                    fh,
                )

            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_PLUGIN_CHECKOUT_DIR": plugin_dir,
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL": repo_url,
                "TINYHAT_PLATFORM_PLUGIN_REPO_REF": repo_ref,
            }
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd == ["git", "-C", plugin_dir, "remote", "set-url", "origin", repo_url]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == [
                    "git",
                    "-C",
                    plugin_dir,
                    "fetch",
                    "--tags",
                    "--prune",
                    "origin",
                ]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "checkout", repo_ref]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "rev-parse", "HEAD"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{plugin_sha}\n",
                        stderr="",
                    )
                if cmd == ["openclaw", "plugins", "inspect", "tinyhat", "--json"]:
                    self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], tmpdir)
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "plugin": {
                                    "id": "tinyhat",
                                    "status": "disabled",
                                    "dependencyStatus": {
                                        "requiredInstalled": True,
                                    },
                                }
                            }
                        ),
                        stderr="",
                    )
                self.fail(f"unexpected command: {cmd}")

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(supervisor.ensure_tinyhat_plugin_installed())

            self.assertEqual(
                calls,
                [
                    ["git", "-C", plugin_dir, "remote", "set-url", "origin", repo_url],
                    [
                        "git",
                        "-C",
                        plugin_dir,
                        "fetch",
                        "--tags",
                        "--prune",
                        "origin",
                    ],
                    ["git", "-C", plugin_dir, "checkout", repo_ref],
                    ["git", "-C", plugin_dir, "rev-parse", "HEAD"],
                    ["openclaw", "plugins", "inspect", "tinyhat", "--json"],
                ],
            )

    def test_stale_marker_reinstalls_when_openclaw_registry_is_missing_plugin(
        self,
    ) -> None:
        repo_url = "https://example.com/tinyhat.git"
        repo_ref = "main"
        plugin_sha = "fedcba9876543210"

        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = os.path.join(tmpdir, "tinyhat-plugin")
            os.makedirs(os.path.join(plugin_dir, ".git"))
            with open(
                os.path.join(plugin_dir, "openclaw.plugin.json"),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump({"id": "tinyhat"}, fh)
            with open(
                os.path.join(plugin_dir, "package.json"),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump({"version": "0.5.0"}, fh)
            os.makedirs(os.path.join(tmpdir, "extensions", "tinyhat"))
            with open(
                os.path.join(
                    tmpdir,
                    "extensions",
                    "tinyhat",
                    "openclaw.plugin.json",
                ),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump({"id": "tinyhat"}, fh)
            with open(
                os.path.join(tmpdir, "tinyhat-plugin.version"),
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump(
                    {
                        "repo_ref": repo_ref,
                        "repo_url": repo_url,
                        "resolved_commit_sha": plugin_sha,
                        "version": "0.5.0",
                    },
                    fh,
                )

            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_PLUGIN_CHECKOUT_DIR": plugin_dir,
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL": repo_url,
                "TINYHAT_PLATFORM_PLUGIN_REPO_REF": repo_ref,
            }
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd == ["git", "-C", plugin_dir, "remote", "set-url", "origin", repo_url]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == [
                    "git",
                    "-C",
                    plugin_dir,
                    "fetch",
                    "--tags",
                    "--prune",
                    "origin",
                ]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "checkout", repo_ref]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "rev-parse", "HEAD"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{plugin_sha}\n",
                        stderr="",
                    )
                if cmd == ["openclaw", "plugins", "inspect", "tinyhat", "--json"]:
                    self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], tmpdir)
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Plugin not found: tinyhat",
                    )
                if cmd == ["openclaw", "plugins", "install", plugin_dir, "--force"]:
                    self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], tmpdir)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                self.fail(f"unexpected command: {cmd}")

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(supervisor.ensure_tinyhat_plugin_installed())

            self.assertIn(
                ["openclaw", "plugins", "inspect", "tinyhat", "--json"],
                calls,
            )
            self.assertIn(
                ["openclaw", "plugins", "install", plugin_dir, "--force"],
                calls,
            )

    def test_plugin_source_prefers_component_update_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override_path = os.path.join(tmpdir, "plugin-source.json")
            env = {
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL": "https://example.com/old.git",
                "TINYHAT_PLATFORM_PLUGIN_REPO_REF": "old-ref",
                "TINYHAT_PLUGIN_SOURCE_OVERRIDE_PATH": override_path,
            }
            with patch.dict(os.environ, env, clear=False):
                supervisor._write_tinyhat_plugin_source_override(
                    repo_url="https://example.com/new.git",
                    repo_ref="v2.0.0",
                    resolved_commit_sha="abc123",
                    version="2.0.0",
                )

                self.assertEqual(
                    supervisor._tinyhat_plugin_source(),
                    ("https://example.com/new.git", "v2.0.0"),
                )


class ChatgptSubscriptionProviderTests(unittest.TestCase):
    def _codex_plugin_payload(self) -> str:
        return json.dumps(
            {
                "plugin": {
                    "id": "codex",
                    "enabled": True,
                    "status": "loaded",
                    "providerIds": ["codex"],
                    "dependencyStatus": {"requiredInstalled": True},
                }
            }
        )

    def test_provider_registry_accepts_current_openai_provider(self) -> None:
        def fake_run(cmd, **_kwargs):
            self.assertEqual(cmd, ["openclaw", "plugins", "inspect", "openai", "--json"])
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "plugin": {
                            "id": "openai",
                            "providerIds": ["openai"],
                            "dependencyStatus": {"requiredInstalled": True},
                        }
                    }
                ),
                stderr="",
            )

        with patch.object(supervisor.subprocess, "run", side_effect=fake_run):
            self.assertTrue(supervisor._is_chatgpt_subscription_provider_available())

    def test_provider_registry_accepts_legacy_openai_codex_provider(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "plugin": {
                            "id": "openai",
                            "providerIds": ["openai-codex"],
                            "dependencyStatus": {"requiredInstalled": True},
                        }
                    }
                ),
                stderr="",
            )

        with patch.object(supervisor.subprocess, "run", side_effect=fake_run):
            self.assertTrue(supervisor._is_chatgpt_subscription_provider_available())

        self.assertEqual(
            calls,
            [["openclaw", "plugins", "inspect", "openai", "--json"]],
        )

    def test_provider_check_raises_when_current_and_legacy_are_missing(self) -> None:
        def fake_run(_cmd, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "plugin": {
                            "id": "openai",
                            "providerIds": ["api-key-only"],
                            "dependencyStatus": {"requiredInstalled": True},
                        }
                    }
                ),
                stderr="",
            )

        with patch.object(supervisor.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                supervisor.ensure_chatgpt_subscription_provider_available()

    def test_codex_subscription_plugin_skips_install_when_available(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            self.assertEqual(cmd, ["openclaw", "plugins", "inspect", "codex", "--json"])
            return SimpleNamespace(
                returncode=0,
                stdout=self._codex_plugin_payload(),
                stderr="",
            )

        with patch.object(supervisor.subprocess, "run", side_effect=fake_run):
            self.assertTrue(supervisor.ensure_codex_subscription_plugin_installed())

        self.assertEqual(calls, [["openclaw", "plugins", "inspect", "codex", "--json"]])

    def test_codex_subscription_plugin_installs_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd == ["openclaw", "plugins", "inspect", "codex", "--json"]:
                    self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], tmpdir)
                    if calls.count(list(cmd)) == 1:
                        return SimpleNamespace(
                            returncode=1,
                            stdout="",
                            stderr="Plugin not found: codex",
                        )
                    return SimpleNamespace(
                        returncode=0,
                        stdout=self._codex_plugin_payload(),
                        stderr="",
                    )
                if cmd == [
                    "openclaw",
                    "plugins",
                    "install",
                    "@openclaw/codex",
                    "--force",
                ]:
                    self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], tmpdir)
                    return SimpleNamespace(
                        returncode=0,
                        stdout="Installed plugin: codex\nRestart the gateway to load plugins.",
                        stderr="",
                    )
                self.fail(f"unexpected command: {cmd}")

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(supervisor.ensure_codex_subscription_plugin_installed())

        self.assertEqual(
            calls,
            [
                ["openclaw", "plugins", "inspect", "codex", "--json"],
                ["openclaw", "plugins", "install", "@openclaw/codex", "--force"],
                ["openclaw", "plugins", "inspect", "codex", "--json"],
            ],
        )

    def test_codex_subscription_plugin_install_failure_raises(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd == ["openclaw", "plugins", "inspect", "codex", "--json"]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Plugin not found: codex",
                )
            if cmd == [
                "openclaw",
                "plugins",
                "install",
                "@openclaw/codex",
                "--force",
            ]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="npm registry timeout",
                )
            self.fail(f"unexpected command: {cmd}")

        with patch.object(supervisor.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as raised:
                supervisor.ensure_codex_subscription_plugin_installed()

        self.assertIn("Codex subscription plugin install failed", str(raised.exception))
        self.assertEqual(
            calls,
            [
                ["openclaw", "plugins", "inspect", "codex", "--json"],
                ["openclaw", "plugins", "install", "@openclaw/codex", "--force"],
            ],
        )

    def test_codex_subscription_plugin_verify_failure_raises(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd == ["openclaw", "plugins", "inspect", "codex", "--json"]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Plugin not found: codex",
                )
            if cmd == [
                "openclaw",
                "plugins",
                "install",
                "@openclaw/codex",
                "--force",
            ]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="Installed plugin: codex",
                    stderr="",
                )
            self.fail(f"unexpected command: {cmd}")

        with patch.object(supervisor.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as raised:
                supervisor.ensure_codex_subscription_plugin_installed()

        self.assertIn(
            "install completed but provider 'codex' is still unavailable",
            str(raised.exception),
        )
        self.assertEqual(
            calls,
            [
                ["openclaw", "plugins", "inspect", "codex", "--json"],
                ["openclaw", "plugins", "install", "@openclaw/codex", "--force"],
                ["openclaw", "plugins", "inspect", "codex", "--json"],
            ],
        )

    def test_device_code_login_command_uses_current_openai_provider(self) -> None:
        self.assertEqual(
            supervisor._chatgpt_subscription_login_command("openclaw"),
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

    def test_subscription_profiles_accept_current_and_legacy_shapes(self) -> None:
        self.assertTrue(
            supervisor._is_chatgpt_subscription_profile(
                "openai:default",
                {"type": "oauth", "provider": "openai"},
            )
        )
        self.assertTrue(
            supervisor._is_chatgpt_subscription_profile(
                "openai-codex:owner@example.com",
                {"type": "oauth", "provider": "openai-codex"},
            )
        )
        self.assertFalse(
            supervisor._is_chatgpt_subscription_profile(
                "codex:default",
                {"type": "oauth", "provider": "codex"},
            )
        )
        self.assertFalse(
            supervisor._is_chatgpt_subscription_profile(
                "xai:default",
                {"type": "oauth", "provider": "xai"},
            )
        )

    def test_subscription_model_refs_normalize_to_openai_provider(self) -> None:
        self.assertEqual(
            supervisor._chatgpt_subscription_model_ref("openai/gpt-5.5"),
            "openai/gpt-5.5",
        )
        self.assertEqual(
            supervisor._chatgpt_subscription_model_ref("openai-codex/gpt-5.5"),
            "openai/gpt-5.5",
        )
        self.assertEqual(
            supervisor._chatgpt_subscription_model_ref("codex/gpt-5.5"),
            "openai/gpt-5.5",
        )


class RuntimeSecretEnvBlockTests(unittest.TestCase):
    """Regression coverage for the runtime-secret env injection contract.

    The promise the secret-management UX makes ("saved values are available
    to OpenClaw processes on that Computer") only holds if user-added
    runtime secrets land in the agent shell's `process.env`. OpenClaw's
    `applyConfigEnvVars` is the only path that populates `process.env`
    from the on-disk config at gateway boot, and it walks plaintext
    `config["env"]` entries only. These tests pin that the supervisor's
    apply path actually fills `config["env"]` for arbitrary user-saved
    secrets while leaving the binding-managed entries and the OpenAI
    SecretRef wiring alone.
    """

    def test_write_config_mirrors_runtime_secrets_into_env(self) -> None:
        binding = {
            "telegram_owner_user_id": "123456",
            "telegram_bot_token": "123456:ABC",
            "telegram_bot_username": "Tinychattestbot",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            with open(secrets_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "EXA_API_KEY": "exa-test-key",
                        "TEST_SECRET": "shh",
                    },
                    fh,
                )
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                supervisor.write_openclaw_config(binding)
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        self.assertEqual(
            config.get("env"),
            {"EXA_API_KEY": "exa-test-key", "TEST_SECRET": "shh"},
        )

    def test_openai_api_key_stays_on_secret_ref_not_in_env(self) -> None:
        binding = {
            "telegram_owner_user_id": "123456",
            "telegram_bot_token": "123456:ABC",
            "telegram_bot_username": "Tinychattestbot",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            with open(secrets_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "OPENAI_API_KEY": "sk-openai-test",
                        "EXA_API_KEY": "exa-test-key",
                    },
                    fh,
                )
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                supervisor.write_openclaw_config(binding)
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        # OpenAI is the only key wired into the model SecretRef.
        self.assertEqual(
            config["models"]["providers"]["openai"]["apiKey"],
            {
                "source": "file",
                "provider": supervisor.TINYHAT_SECRETS_PROVIDER,
                "id": supervisor.TINYHAT_OPENAI_API_KEY_POINTER,
            },
        )
        # Plain-text env block carries everything else, but NOT OPENAI_API_KEY:
        # surfacing both would shadow the SecretRef.
        self.assertEqual(config.get("env"), {"EXA_API_KEY": "exa-test-key"})

    def test_openrouter_binding_keeps_openai_secret_ref_without_runtime_pin(
        self,
    ) -> None:
        binding = _openrouter_binding(
            {
                "default_model": "moonshotai/kimi-k2.6",
                "default_role": "power",
                "enabled_roles": ["power"],
                "models": {
                    "power": "moonshotai/kimi-k2.6",
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            with open(secrets_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "OPENAI_API_KEY": "sk-openai-test",
                        "EXA_API_KEY": "exa-test-key",
                    },
                    fh,
                )
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                supervisor.write_openclaw_config(binding)
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        self.assertNotIn("agentRuntime", config["agents"]["defaults"])
        _assert_no_provider_runtime_pin(self, config, "openrouter")
        providers = config["models"]["providers"]
        self.assertEqual(
            providers["openai"]["apiKey"],
            {
                "source": "file",
                "provider": supervisor.TINYHAT_SECRETS_PROVIDER,
                "id": supervisor.TINYHAT_OPENAI_API_KEY_POINTER,
            },
        )
        self.assertNotIn("agentRuntime", providers["openai"])
        self.assertEqual(
            config["env"],
            {
                "OPENROUTER_API_KEY": "sk-or-v1-child",
                "EXA_API_KEY": "exa-test-key",
            },
        )

    def test_binding_openrouter_wins_over_user_runtime_secret(self) -> None:
        package = {
            "default_model": "deepseek/deepseek-v4-pro",
            "default_role": "default",
            "enabled_roles": ["cheap", "default"],
            "models": {
                "cheap": "deepseek/deepseek-v4-flash",
                "default": "deepseek/deepseek-v4-pro",
            },
        }
        binding = _openrouter_binding(package)
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            with open(secrets_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        # User tried to override the platform-issued key
                        # through the Mini App. The binding's value must
                        # still win.
                        "OPENROUTER_API_KEY": "sk-or-v1-user-attempt",
                        "EXA_API_KEY": "exa-test-key",
                    },
                    fh,
                )
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                supervisor.write_openclaw_config(binding)
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        self.assertEqual(
            config["env"],
            {
                "OPENROUTER_API_KEY": "sk-or-v1-child",  # binding-issued
                "EXA_API_KEY": "exa-test-key",
            },
        )

    def test_sync_secret_ref_config_updates_env_and_flags_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                # Seed openclaw.json with a binding-managed OPENROUTER_API_KEY
                # so we can prove _apply_runtime_secret_env_block preserves it.
                config_path = supervisor.openclaw_config_path()
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as fh:
                    json.dump(
                        {"env": {"OPENROUTER_API_KEY": "sk-or-v1-child"}},
                        fh,
                    )
                changed = supervisor.sync_openclaw_secret_ref_config(
                    {"EXA_API_KEY": "exa-test-key", "OPENAI_API_KEY": "sk"}
                )
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        self.assertTrue(changed, "EXA_API_KEY add should flag env_block_changed")
        self.assertEqual(
            config["env"],
            {
                "OPENROUTER_API_KEY": "sk-or-v1-child",  # preserved
                "EXA_API_KEY": "exa-test-key",
            },
        )
        self.assertEqual(
            config["models"]["providers"]["openai"]["apiKey"],
            {
                "source": "file",
                "provider": supervisor.TINYHAT_SECRETS_PROVIDER,
                "id": supervisor.TINYHAT_OPENAI_API_KEY_POINTER,
            },
        )

    def test_sync_secret_ref_config_openai_only_does_not_flag_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                # Empty config; only OPENAI_API_KEY changes after sync.
                changed = supervisor.sync_openclaw_secret_ref_config(
                    {"OPENAI_API_KEY": "sk-openai"}
                )

        # An OpenAI-only edit lands on the SecretRef wiring and on disk via
        # `openclaw secrets reload`; the env block stays empty so we don't
        # need to restart the gateway.
        self.assertFalse(changed)

    def test_deleting_runtime_secret_drops_it_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with patch.dict(os.environ, env, clear=False):
                config_path = supervisor.openclaw_config_path()
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "env": {
                                "OPENROUTER_API_KEY": "sk-or-v1-child",
                                "EXA_API_KEY": "exa-test-key",
                                "STALE_SECRET": "old",
                            }
                        },
                        fh,
                    )
                # User deleted both EXA_API_KEY and STALE_SECRET; only the
                # binding-managed key remains.
                changed = supervisor.sync_openclaw_secret_ref_config({})
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        self.assertTrue(changed)
        self.assertEqual(
            config["env"],
            {"OPENROUTER_API_KEY": "sk-or-v1-child"},
        )

    def test_apply_runtime_secret_map_returns_env_block_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = os.path.join(tmpdir, "tinyhat-secrets.json")
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": secrets_path,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    supervisor,
                    "reload_openclaw_secrets",
                    return_value={"ok": True},
                ),
            ):
                result = supervisor.apply_runtime_secret_map(
                    revision=42,
                    secrets={"EXA_API_KEY": "exa-test-key"},
                )

        self.assertEqual(result["revision"], 42)
        self.assertEqual(result["secret_count"], 1)
        self.assertTrue(result["env_block_changed"])

    def test_handle_apply_config_signals_rebind_on_env_change(self) -> None:
        # Reset the module-level rebind flags so the test is order-independent.
        supervisor._stop_holder["rebind"] = False
        supervisor._stop_holder["stop"] = False
        supervisor._config_apply_state["failed_revision"] = None
        supervisor._config_apply_state["failed_diagnostic"] = None
        supervisor._config_apply_state["failed_reported"] = False

        with (
            patch.object(
                supervisor,
                "get_json",
                return_value={
                    "revision": 5,
                    "secrets": {"EXA_API_KEY": "exa-test-key"},
                },
            ),
            patch.object(
                supervisor,
                "apply_runtime_secret_map",
                return_value={
                    "revision": 5,
                    "secret_count": 1,
                    "reload": {"ok": True},
                    "env_block_changed": True,
                },
            ),
            patch.object(supervisor, "_post_config_apply_result") as posted,
        ):
            supervisor.handle_apply_config_command({"type": "apply_config", "revision": 5})

        self.assertTrue(supervisor._stop_holder["rebind"])
        self.assertTrue(supervisor._stop_holder["stop"])
        posted.assert_called_once()
        self.assertEqual(posted.call_args.kwargs["revision"], 5)
        self.assertEqual(posted.call_args.kwargs["status"], "applied")

        # Cleanup so unrelated tests are not affected.
        supervisor._stop_holder["rebind"] = False
        supervisor._stop_holder["stop"] = False

    def test_handle_apply_config_skips_rebind_when_env_unchanged(self) -> None:
        supervisor._stop_holder["rebind"] = False
        supervisor._stop_holder["stop"] = False
        supervisor._config_apply_state["failed_revision"] = None
        supervisor._config_apply_state["failed_diagnostic"] = None
        supervisor._config_apply_state["failed_reported"] = False

        with (
            patch.object(
                supervisor,
                "get_json",
                return_value={
                    "revision": 6,
                    "secrets": {"OPENAI_API_KEY": "sk-openai"},
                },
            ),
            patch.object(
                supervisor,
                "apply_runtime_secret_map",
                return_value={
                    "revision": 6,
                    "secret_count": 1,
                    "reload": {"ok": True},
                    "env_block_changed": False,
                },
            ),
            patch.object(supervisor, "_post_config_apply_result"),
        ):
            supervisor.handle_apply_config_command({"type": "apply_config", "revision": 6})

        # OpenAI-only edits resolve through the SecretRef snapshot already
        # refreshed by `openclaw secrets reload`; the gateway must NOT restart.
        self.assertFalse(supervisor._stop_holder["rebind"])
        self.assertFalse(supervisor._stop_holder["stop"])


class RuntimeOwnershipTests(unittest.TestCase):
    def test_runtime_owned_json_write_chowns_parent_and_temp_before_replace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "openclaw.json")
            chown_calls: list[tuple[str, int, int]] = []

            def fake_chown(target: str, uid: int, gid: int) -> None:
                chown_calls.append((target, uid, gid))

            real_replace = supervisor.os.replace
            replace_calls: list[tuple[str, str]] = []

            def fake_replace(source: str, target: str) -> None:
                replace_calls.append((source, target))
                self.assertIn((source, 123, 456), chown_calls)
                real_replace(source, target)

            with (
                patch.dict(
                    os.environ,
                    {
                        supervisor.TINYHAT_OPENCLAW_RUNTIME_USER_ENV: "tinyhat",
                        supervisor.TINYHAT_OPENCLAW_RUNTIME_GROUP_ENV: "tinyhat",
                    },
                    clear=False,
                ),
                patch.object(supervisor.os, "geteuid", return_value=0),
                patch.object(
                    supervisor.pwd,
                    "getpwnam",
                    return_value=SimpleNamespace(pw_uid=123),
                ),
                patch.object(
                    supervisor.grp,
                    "getgrnam",
                    return_value=SimpleNamespace(gr_gid=456),
                ),
                patch.object(supervisor.os, "chown", side_effect=fake_chown),
                patch.object(supervisor.os, "replace", side_effect=fake_replace),
            ):
                supervisor._atomic_write_json(
                    path,
                    {"ok": True},
                    runtime_owned=True,
                )

            self.assertEqual(chown_calls[0], (tmpdir, 123, 456))
            self.assertEqual(len(replace_calls), 1)
            self.assertEqual(replace_calls[0][1], path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_runtime_owned_json_write_does_not_chown_when_not_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "openclaw.json")
            with (
                patch.dict(
                    os.environ,
                    {supervisor.TINYHAT_OPENCLAW_RUNTIME_USER_ENV: "tinyhat"},
                    clear=False,
                ),
                patch.object(supervisor.os, "geteuid", return_value=501),
                patch.object(supervisor.os, "chown") as chown,
            ):
                supervisor._atomic_write_json(
                    path,
                    {"ok": True},
                    runtime_owned=True,
                )

            chown.assert_not_called()
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_runtime_owned_json_write_does_not_chown_without_runtime_user(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "openclaw.json")
            with (
                patch.dict(
                    os.environ,
                    {
                        supervisor.TINYHAT_OPENCLAW_RUNTIME_USER_ENV: "",
                        supervisor.TINYHAT_OPENCLAW_RUNTIME_GROUP_ENV: "",
                    },
                    clear=False,
                ),
                patch.object(supervisor.os, "geteuid", return_value=0),
                patch.object(supervisor.os, "chown") as chown,
            ):
                supervisor._atomic_write_json(
                    path,
                    {"ok": True},
                    runtime_owned=True,
                )

            chown.assert_not_called()
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


def _subscription_binding(*, with_openrouter: bool) -> dict:
    """Binding for a Computer opted into the ChatGPT BYO subscription."""
    base = {
        "telegram_owner_user_id": "123456",
        "telegram_bot_token": "123456:ABC",
        "telegram_bot_username": "Tinychattestbot",
        "llm_auth_mode": "chatgpt_subscription",
        # Platform records may still carry legacy provider refs; the
        # supervisor normalizes subscription mode to the current OpenAI provider.
        "llm_model_ref": "openai/gpt-5.5",
    }
    if with_openrouter:
        base.update(
            {
                "openrouter_api_key": "sk-or-v1-child",
                "openrouter_base_url": "https://openrouter.ai/api/v1",
                "openrouter_default_model": "openai/gpt-5.5",
            }
        )
    return base


def _seed_auth_profile(state_dir: str) -> None:
    """Drop a current ChatGPT/Codex subscription profile into the auth store."""
    path = os.path.join(state_dir, "agents", "main", "agent", "auth-profiles.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "version": 1,
                "profiles": {
                    "openai:default": {
                        "type": "oauth",
                        "provider": "openai",
                        "access": "redacted-access",
                        "refresh": "redacted-refresh",
                        "expires": 9999999999999,
                        "email": "owner@example.com",
                    }
                },
            },
            fh,
        )


def _seed_legacy_auth_profile(state_dir: str) -> None:
    """Drop the legacy openai-codex profile shape into the auth store."""
    path = os.path.join(state_dir, "agents", "main", "agent", "auth-profiles.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "version": 1,
                "profiles": {
                    "openai-codex:owner@example.com": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "redacted-access",
                        "refresh": "redacted-refresh",
                        "expires": 9999999999999,
                        "email": "owner@example.com",
                    }
                },
                "order": {"openai-codex": ["openai-codex:owner@example.com"]},
                "usageStats": {
                    "openai-codex": {
                        "lastProfileId": "openai-codex:owner@example.com"
                    },
                    "openai-codex:owner@example.com": {"requests": 3},
                },
                "lastGood": {
                    "openai-codex": "openai-codex:owner@example.com",
                    "provider": "openai-codex",
                    "profileId": "openai-codex:owner@example.com",
                },
            },
            fh,
        )


def _seed_other_provider_profile(state_dir: str) -> None:
    """Drop a non-subscription profile to test the wipe preserves it."""
    path = os.path.join(state_dir, "agents", "main", "agent", "auth-profiles.json")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "profiles": {}}, fh)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["profiles"]["xai:other@example.com"] = {
        "type": "oauth",
        "provider": "xai",
        "access": "x-redacted",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class BindingCycleSubscriptionProviderWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_stop_holder = dict(supervisor._stop_holder)
        supervisor._stop_holder.update(
            {
                "stop": False,
                "rebind": False,
                "signature": None,
                "owner_signature": None,
            }
        )

    def tearDown(self) -> None:
        supervisor._stop_holder.clear()
        supervisor._stop_holder.update(self._old_stop_holder)

    def test_ready_phase_posts_runtime_state_before_binding(self) -> None:
        supervisor._reset_runtime_state_identity_cache()
        supervisor._reset_runtime_state_platform_post_cache()
        state_posts: list[dict] = []
        runtime_state_posts: list[dict] = []

        def fake_post_json(path: str, body: dict) -> dict:
            if path == "/hapi/v1/computers/me/state":
                state_posts.append(dict(body))
                return {}
            if path == "/hapi/v1/computers/me/runtime-state":
                runtime_state_posts.append(dict(body))
                return {}
            self.fail(f"unexpected POST path: {path}")

        def stop_after_first_sleep(_seconds: float) -> None:
            supervisor._stop_holder["stop"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_PLATFORM_BASE_URL": "https://dev.example.test",
                "DEV_AUTO_COMPUTER_ID": "123",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "post_json", side_effect=fake_post_json),
                patch.object(
                    supervisor,
                    "get_json",
                    return_value={"assigned": False},
                ),
                patch.object(supervisor, "_wipe_on_owner_release"),
                patch.object(supervisor, "checkpoint_supervisor_progress"),
                patch.object(
                    supervisor.time,
                    "sleep",
                    side_effect=stop_after_first_sleep,
                ),
            ):
                self.assertEqual(supervisor._run_one_binding_cycle(), 0)
                payload = supervisor.read_runtime_state()

        self.assertEqual(
            state_posts,
            [{"state": "ready", "detail": "bootstrap complete"}],
        )
        self.assertEqual(len(runtime_state_posts), 1)
        self.assertEqual(runtime_state_posts[0]["schema"], "runtime_state_v1")
        self.assertEqual(runtime_state_posts[0]["runtime_health"], "healthy")
        self.assertEqual(
            runtime_state_posts[0]["detail"],
            "control plane ready; awaiting binding",
        )
        self.assertEqual(runtime_state_posts[0]["gateway"]["status"], "inactive")
        self.assertEqual(
            runtime_state_posts[0]["gateway"]["action"],
            "awaiting_binding",
        )
        self.assertEqual(runtime_state_posts[0]["computer_id"], "123")
        self.assertEqual(payload, runtime_state_posts[0])

    def test_refused_ready_transition_still_posts_runtime_state_when_unbound(
        self,
    ) -> None:
        import urllib.error

        supervisor._reset_runtime_state_identity_cache()
        supervisor._reset_runtime_state_platform_post_cache()
        runtime_state_posts: list[dict] = []

        def fake_post_json(path: str, body: dict) -> dict:
            if path == "/hapi/v1/computers/me/state":
                raise urllib.error.HTTPError(
                    path,
                    400,
                    "already ready",
                    {},
                    None,
                )
            if path == "/hapi/v1/computers/me/runtime-state":
                runtime_state_posts.append(dict(body))
                return {}
            self.fail(f"unexpected POST path: {path}")

        def stop_after_first_sleep(_seconds: float) -> None:
            supervisor._stop_holder["stop"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_PLATFORM_BASE_URL": "https://dev.example.test",
                "DEV_AUTO_COMPUTER_ID": "123",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor, "post_json", side_effect=fake_post_json),
                patch.object(
                    supervisor,
                    "get_json",
                    return_value={"assigned": False},
                ),
                patch.object(supervisor, "_wipe_on_owner_release"),
                patch.object(supervisor, "checkpoint_supervisor_progress"),
                patch.object(
                    supervisor.time,
                    "sleep",
                    side_effect=stop_after_first_sleep,
                ),
            ):
                self.assertEqual(supervisor._run_one_binding_cycle(), 0)
                payload = supervisor.read_runtime_state()

        self.assertEqual(len(runtime_state_posts), 1)
        self.assertEqual(runtime_state_posts[0]["runtime_health"], "healthy")
        self.assertEqual(runtime_state_posts[0]["gateway"]["status"], "inactive")
        self.assertEqual(
            runtime_state_posts[0]["gateway"]["action"],
            "awaiting_binding",
        )
        self.assertEqual(payload, runtime_state_posts[0])

    def test_codex_plugin_failure_does_not_disable_openai_subscription_provider(
        self,
    ) -> None:
        import threading as threading_mod

        binding = _subscription_binding(with_openrouter=True)
        captured_config_kwargs: list[dict] = []

        class _NoopThread:
            def __init__(self, target, daemon):
                self._target = target
                self.daemon = daemon

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        def fake_write_openclaw_config(config_binding: dict, **kwargs) -> None:
            self.assertEqual(config_binding, binding)
            captured_config_kwargs.append(dict(kwargs))

        def fake_gateway_active() -> bool:
            supervisor._stop_holder["stop"] = True
            return True

        config_fingerprint = {
            "algorithm": "sha256",
            "source": "openclaw_config",
            "path": "/etc/openclaw/openclaw.json",
            "value": "test",
        }

        with (
            patch.object(supervisor, "post_json", return_value={}),
            patch.object(
                supervisor,
                "get_json",
                return_value={"assigned": True, "binding": binding},
            ),
            patch.object(
                supervisor,
                "try_install_codex_subscription_plugin",
                return_value=False,
            ),
            patch.object(
                supervisor,
                "try_check_chatgpt_subscription_provider",
                return_value=True,
            ),
            patch.object(supervisor, "try_install_tinyhat_plugin", return_value=True),
            patch.object(
                supervisor,
                "write_openclaw_config",
                side_effect=fake_write_openclaw_config,
            ),
            patch.object(
                supervisor,
                "_openclaw_config_fingerprint",
                return_value=config_fingerprint,
            ),
            patch.object(
                supervisor,
                "probe_current_openclaw_gateway_health",
                return_value=(True, "ok"),
            ),
            patch.object(supervisor, "_write_runtime_state"),
            patch.object(supervisor, "delete_telegram_webhook"),
            patch.object(supervisor, "start_openclaw_gateway", return_value=123.0),
            patch.object(supervisor, "wait_for_openclaw_start"),
            patch.object(
                supervisor,
                "is_openclaw_gateway_active",
                side_effect=fake_gateway_active,
            ),
            patch.object(supervisor, "stop_openclaw_gateway"),
            patch.object(supervisor.time, "sleep"),
            patch.object(threading_mod, "Thread", _NoopThread),
        ):
            self.assertEqual(supervisor._run_one_binding_cycle(), 0)

        self.assertEqual(
            captured_config_kwargs,
            [
                {
                    "enable_tinyhat_plugin": True,
                    "enable_chatgpt_subscription_provider": True,
                    "enable_codex_subscription_plugins": False,
                }
            ],
        )


class BindingCycleManualRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_stop_holder = dict(supervisor._stop_holder)
        supervisor._stop_holder.update(
            {
                "stop": False,
                "rebind": False,
                "signature": None,
                "owner_signature": None,
            }
        )

    def tearDown(self) -> None:
        supervisor._stop_holder.clear()
        supervisor._stop_holder.update(self._old_stop_holder)

    def test_manual_recovery_returns_nonzero_to_avoid_outer_loop_spin(
        self,
    ) -> None:
        binding = _subscription_binding(with_openrouter=True)
        config_fingerprint = {
            "algorithm": "sha256",
            "source": "openclaw_config",
            "path": "/etc/openclaw/openclaw.json",
            "value": "test",
        }

        with (
            patch.object(supervisor, "post_json", return_value={}) as post_json,
            patch.object(
                supervisor,
                "get_json",
                return_value={"assigned": True, "binding": binding},
            ),
            patch.object(
                supervisor,
                "try_install_codex_subscription_plugin",
                return_value=True,
            ),
            patch.object(
                supervisor,
                "try_check_chatgpt_subscription_provider",
                return_value=True,
            ),
            patch.object(supervisor, "try_install_tinyhat_plugin", return_value=True),
            patch.object(supervisor, "write_openclaw_config"),
            patch.object(
                supervisor,
                "_openclaw_config_fingerprint",
                return_value=config_fingerprint,
            ),
            patch.object(
                supervisor,
                "ensure_openclaw_gateway_ready",
                side_effect=supervisor.ManualRecoveryRequired(
                    "manual recovery required; automatic gateway recovery blocked"
                ),
            ),
            patch.object(supervisor, "checkpoint_supervisor_progress"),
            patch.object(supervisor, "stop_openclaw_gateway") as stop_gateway,
        ):
            self.assertEqual(supervisor._run_one_binding_cycle(), 1)

        stop_gateway.assert_not_called()
        post_json.assert_any_call(
            "/hapi/v1/computers/me/state",
            {"state": "ready", "detail": "bootstrap complete"},
        )
        post_json.assert_any_call(
            "/hapi/v1/computers/me/state",
            {
                "state": "broken",
                "detail": (
                    "manual recovery required; automatic gateway recovery blocked"
                ),
            },
        )


class ChatgptSubscriptionBranchTests(unittest.TestCase):
    """Issue #23 — supervisor branches on auth-profile presence."""

    def test_opted_in_with_profile_writes_subscription_config(self) -> None:
        """auth-profile present + llm_auth_mode=chatgpt_subscription on
        binding → openai/gpt-5.5, no provider runtime pin for OpenAI,
        no openai SecretRef, cross-provider fallback to OpenRouter when
        its key is present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                _seed_auth_profile(supervisor.openclaw_state_dir())
                supervisor.write_openclaw_config(
                    _subscription_binding(with_openrouter=True),
                )
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)
        defaults = config["agents"]["defaults"]
        self.assertEqual(defaults["model"]["primary"], "openai/gpt-5.5")
        self.assertEqual(defaults["imageModel"], {"primary": "openai/gpt-5.5"})
        # No whole-agent runtime pin — let OpenClaw auto-select the harness.
        self.assertNotIn("agentRuntime", defaults)
        # No openai SecretRef — the OAuth profile owns auth.
        self.assertNotIn("models", defaults.get("models", {}))
        providers = (config.get("models") or {}).get("providers") or {}
        self.assertNotIn("apiKey", providers.get("openai", {}))
        self.assertNotIn("agentRuntime", providers.get("openai", {}))
        _assert_no_provider_runtime_pin(self, config, "openrouter")
        # Cross-provider fallback to OpenRouter for rate-window relief.
        self.assertEqual(
            defaults["model"].get("fallbacks"), ["openrouter/openai/gpt-5.5"]
        )
        # OpenRouter env stays set so the fallback has an auth path.
        self.assertEqual(
            config.get("env", {}).get("OPENROUTER_API_KEY"), "sk-or-v1-child"
        )
        self.assertEqual(
            config["tools"]["media"]["audio"],
            {
                "enabled": True,
                "models": [
                    {"provider": "openai", "model": "gpt-4o-transcribe"},
                    {
                        "provider": "openrouter",
                        "model": "openai/whisper-large-v3-turbo",
                    },
                ],
            },
        )
        # Automated coverage stops at configuring OpenAI first plus OpenRouter
        # fallback; the HTTP 500-to-fallback recovery is OpenClaw runtime
        # behavior and is covered by live Telegram E2E.
        self.assertEqual(config["plugins"]["entries"]["openai"], {"enabled": True})
        self.assertEqual(config["plugins"]["entries"]["codex"], {"enabled": True})
        self.assertEqual(
            config["plugins"]["entries"]["codex-supervisor"], {"enabled": True}
        )
        self.assertEqual(
            config["auth"],
            {
                "profiles": {
                    "openai:default": {
                        "provider": "openai",
                        "mode": "oauth",
                        "email": "owner@example.com",
                    },
                },
                "order": {"openai": ["openai:default"]},
            },
        )

    def test_legacy_profile_is_normalized_for_codex_native_auth(self) -> None:
        """OpenClaw 2026.6.x still writes some device-code profiles as
        openai-codex, but the native Codex route resolves auth under openai."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                _seed_legacy_auth_profile(supervisor.openclaw_state_dir())
                supervisor.write_openclaw_config(
                    _subscription_binding(with_openrouter=True),
                )
                config_path = supervisor.openclaw_config_path()
                auth_path = supervisor.openclaw_auth_profiles_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)
                with open(auth_path, encoding="utf-8") as fh:
                    auth_store = json.load(fh)

        self.assertEqual(config["agents"]["defaults"]["model"]["primary"], "openai/gpt-5.5")
        self.assertEqual(
            config["agents"]["defaults"]["imageModel"],
            {"primary": "openai/gpt-5.5"},
        )
        self.assertEqual(
            config["auth"]["profiles"]["openai:owner@example.com"],
            {
                "provider": "openai",
                "mode": "oauth",
                "email": "owner@example.com",
            },
        )
        self.assertEqual(config["auth"]["order"], {"openai": ["openai:owner@example.com"]})
        self.assertNotIn("openai-codex:owner@example.com", auth_store["profiles"])
        self.assertEqual(
            auth_store["profiles"]["openai:owner@example.com"]["provider"],
            "openai",
        )
        self.assertEqual(
            auth_store["profiles"]["openai:owner@example.com"]["access"],
            "redacted-access",
        )
        self.assertEqual(auth_store["order"], {"openai": ["openai:owner@example.com"]})
        self.assertNotIn("openai-codex", auth_store["usageStats"])
        self.assertNotIn("openai-codex:owner@example.com", auth_store["usageStats"])
        self.assertEqual(
            auth_store["usageStats"]["openai"]["lastProfileId"],
            "openai:owner@example.com",
        )
        self.assertEqual(
            auth_store["usageStats"]["openai:owner@example.com"],
            {"requests": 3},
        )
        self.assertEqual(
            auth_store["lastGood"],
            {
                "openai": "openai:owner@example.com",
                "provider": "openai",
                "profileId": "openai:owner@example.com",
            },
        )

    def test_opted_in_without_profile_stays_on_default_config(self) -> None:
        """llm_auth_mode=chatgpt_subscription but NO auth-profile yet →
        default-mode (OpenClaw runtime + OpenRouter) so the agent keeps replying
        while the device-code flow is in flight or before the user has approved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                # NO auth-profile file. Binding still says opted-in.
                supervisor.write_openclaw_config(
                    _subscription_binding(with_openrouter=True),
                )
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)
        defaults = config["agents"]["defaults"]
        # Stays on the OpenClaw runtime because the credential isn't on disk yet.
        self.assertNotIn("agentRuntime", defaults)
        _assert_no_provider_runtime_pin(self, config, "openrouter")
        self.assertTrue(defaults["model"]["primary"].startswith("openrouter/"))
        self.assertEqual(config["plugins"]["entries"]["codex"], {"enabled": True})
        self.assertEqual(
            config["plugins"]["entries"]["codex-supervisor"], {"enabled": True}
        )
        self.assertEqual(
            config["tools"]["media"]["audio"]["models"],
            [
                {
                    "provider": "openrouter",
                    "model": "openai/whisper-large-v3-turbo",
                }
            ],
        )

    def test_profile_without_provider_stays_on_default_config(self) -> None:
        """A saved profile is not enough if the provider plugin is unavailable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                _seed_auth_profile(supervisor.openclaw_state_dir())
                supervisor.write_openclaw_config(
                    _subscription_binding(with_openrouter=True),
                    enable_chatgpt_subscription_provider=False,
                )
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        defaults = config["agents"]["defaults"]
        self.assertTrue(defaults["model"]["primary"].startswith("openrouter/"))
        self.assertNotEqual(defaults["model"]["primary"], "openai/gpt-5.5")
        self.assertEqual(config["plugins"]["entries"]["codex"], {"enabled": True})
        self.assertEqual(
            config["plugins"]["entries"]["codex-supervisor"], {"enabled": True}
        )

    def test_profile_uses_subscription_route_when_codex_plugin_unavailable(self) -> None:
        """The optional Codex plugin should not gate OpenAI model routing.

        Device-code/model auth uses OpenClaw's bundled openai provider, so an
        existing OAuth profile must keep using the subscription route even if
        the auxiliary Codex plugin install failed and its config entries are
        disabled for this boot.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                _seed_auth_profile(supervisor.openclaw_state_dir())
                supervisor.write_openclaw_config(
                    _subscription_binding(with_openrouter=True),
                    enable_chatgpt_subscription_provider=True,
                    enable_codex_subscription_plugins=False,
                )
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

        defaults = config["agents"]["defaults"]
        self.assertEqual(defaults["model"]["primary"], "openai/gpt-5.5")
        self.assertEqual(defaults["imageModel"], {"primary": "openai/gpt-5.5"})
        self.assertEqual(
            defaults["model"].get("fallbacks"), ["openrouter/openai/gpt-5.5"]
        )
        self.assertEqual(config["plugins"]["entries"]["openai"], {"enabled": True})
        self.assertNotIn("codex", config["plugins"]["entries"])
        self.assertNotIn("codex-supervisor", config["plugins"]["entries"])

    def test_platform_credit_route_configures_openrouter_audio_stt(self) -> None:
        """Fresh Computers should use provider STT instead of local CLI audio.

        OpenClaw auto-detects local whisper-style CLIs before some provider
        fallbacks. Tinyhat-managed Computers already receive an OpenRouter key,
        so generate an explicit provider transcription list and keep voice-note
        UX off the slower terminal transcription path.
        """
        config = _write_config_in_temp_runtime(
            _openrouter_binding(
                {
                    "default_model": "moonshotai/kimi-k2.6",
                    "models": {"default": "moonshotai/kimi-k2.6"},
                    "enabled_roles": ["default"],
                }
            )
        )

        self.assertEqual(
            config["tools"]["media"]["audio"],
            {
                "enabled": True,
                "models": [
                    {
                        "provider": "openrouter",
                        "model": "openai/whisper-large-v3-turbo",
                    }
                ],
            },
        )
        self.assertNotIn("imageModel", config["agents"]["defaults"])

    def test_openai_secret_prefers_openai_audio_without_image_pin(self) -> None:
        """BYO OpenAI key uses OpenAI STT without changing the image route."""
        config = _write_config_in_temp_runtime(
            _openrouter_binding(
                {
                    "default_model": "moonshotai/kimi-k2.6",
                    "models": {"default": "moonshotai/kimi-k2.6"},
                    "enabled_roles": ["default"],
                }
            ),
            secrets={"OPENAI_API_KEY": "sk-openai-test"},
        )

        self.assertEqual(
            config["tools"]["media"]["audio"],
            {
                "enabled": True,
                "models": [
                    {"provider": "openai", "model": "gpt-4o-transcribe"},
                    {
                        "provider": "openrouter",
                        "model": "openai/whisper-large-v3-turbo",
                    },
                ],
            },
        )
        self.assertNotIn("imageModel", config["agents"]["defaults"])
        self.assertEqual(
            config["models"]["providers"]["openai"]["apiKey"],
            {
                "source": "file",
                "provider": supervisor.TINYHAT_SECRETS_PROVIDER,
                "id": supervisor.TINYHAT_OPENAI_API_KEY_POINTER,
            },
        )
        self.assertEqual(
            config.get("env"),
            {"OPENROUTER_API_KEY": "sk-or-v1-child"},
        )

    def test_default_binding_uses_provider_runtime_policy(self) -> None:
        """Non-subscription Computers pin the provider runtime instead
        of the schema-invalid whole-agent runtime policy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                # No auth-profile, no llm_auth_mode override.
                supervisor.write_openclaw_config(
                    _openrouter_binding(
                        {
                            "default_model": "openai/gpt-5.2",
                            "default_role": "default",
                            "enabled_roles": ["default"],
                            "models": {"default": "openai/gpt-5.2"},
                        }
                    )
                )
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)
        defaults = config["agents"]["defaults"]
        self.assertNotIn("agentRuntime", defaults)
        _assert_no_provider_runtime_pin(self, config, "openrouter")
        self.assertEqual(config["plugins"]["entries"]["codex"], {"enabled": True})
        self.assertEqual(
            config["plugins"]["entries"]["codex-supervisor"], {"enabled": True}
        )


class WipeChatgptSubscriptionProfileTests(unittest.TestCase):
    """Issue #23 — admin-driven wipe of the per-agent OAuth credential."""

    def test_wipe_removes_current_openai_profile_and_preserves_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with patch.dict(os.environ, env, clear=False):
                state_dir = supervisor.openclaw_state_dir()
                _seed_auth_profile(state_dir)
                _seed_other_provider_profile(state_dir)

                removed = supervisor.wipe_chatgpt_subscription_profile()
                self.assertEqual(removed, ["openai:default"])

                # File still exists with the non-subscription profile.
                path = supervisor.openclaw_auth_profiles_path()
                with open(path, encoding="utf-8") as fh:
                    after = json.load(fh)
                self.assertNotIn("openai:default", after["profiles"])
                self.assertIn("xai:other@example.com", after["profiles"])

                # Second call is a no-op (idempotent).
                self.assertEqual(supervisor.wipe_chatgpt_subscription_profile(), [])

    def test_wipe_removes_legacy_openai_codex_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with patch.dict(os.environ, env, clear=False):
                _seed_legacy_auth_profile(supervisor.openclaw_state_dir())

                removed = supervisor.wipe_chatgpt_subscription_profile()
                self.assertEqual(removed, ["openai-codex:owner@example.com"])

                path = supervisor.openclaw_auth_profiles_path()
                with open(path, encoding="utf-8") as fh:
                    after = json.load(fh)
                self.assertNotIn(
                    "openai-codex:owner@example.com",
                    after["profiles"],
                )

    def test_wipe_on_missing_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(supervisor.wipe_chatgpt_subscription_profile(), [])


class BindingSignatureSubscriptionFieldsTests(unittest.TestCase):
    """Issue #23 / PR #24 review #1 — signature must move on a mode flip."""

    def _base_binding(self) -> dict:
        return {
            "telegram_owner_user_id": "123456",
            "telegram_bot_user_id": "999",
            "telegram_bot_username": "Tinychattestbot",
            "telegram_bot_token": "123456:ABC",
            "account_handle": "test-account",
            "openrouter_api_key": "sk-or-v1-child",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
            "openrouter_default_model": "openai/gpt-5.5",
        }

    def test_signature_stays_stable_for_pending_subscription_flow(self) -> None:
        before = self._base_binding()  # implicit platform_credits
        after = dict(before, llm_auth_mode="chatgpt_subscription")
        self.assertEqual(
            supervisor._binding_signature(before),
            supervisor._binding_signature(after),
        )

    def test_signature_moves_when_subscription_model_ref_links(self) -> None:
        before = dict(
            self._base_binding(), llm_auth_mode="chatgpt_subscription"
        )
        after = dict(before, llm_model_ref="openai/gpt-5.5")
        self.assertNotEqual(
            supervisor._binding_signature(before),
            supervisor._binding_signature(after),
        )

    def test_owner_signature_stable_across_mode_flip(self) -> None:
        """Mode flip for the same owner must NOT change the owner-
        identity tuple — otherwise the wipe would fire on every flip
        and drop the OAuth credential the user just linked."""
        before = self._base_binding()
        after = dict(
            before,
            llm_auth_mode="chatgpt_subscription",
            llm_model_ref="openai/gpt-5.5",
        )
        self.assertEqual(
            supervisor._owner_identity_signature(before),
            supervisor._owner_identity_signature(after),
        )

    def test_owner_signature_moves_on_owner_change(self) -> None:
        before = self._base_binding()
        after = dict(before, telegram_owner_user_id="987654")
        self.assertNotEqual(
            supervisor._owner_identity_signature(before),
            supervisor._owner_identity_signature(after),
        )


class WipeOnOwnerReleaseTests(unittest.TestCase):
    """Issue #23 / PR #24 review #2 — production wipe wiring."""

    def test_wipe_helper_logs_removed_profiles(self) -> None:
        """``_wipe_on_owner_release`` must call into the actual wipe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with patch.dict(os.environ, env, clear=False):
                _seed_auth_profile(supervisor.openclaw_state_dir())
                # Helper is a thin wrapper — no return value, just
                # delegates + logs. Verify the file got wiped after.
                supervisor._wipe_on_owner_release(reason="reassign")
                path = supervisor.openclaw_auth_profiles_path()
                with open(path, encoding="utf-8") as fh:
                    after = json.load(fh)
                self.assertEqual(after["profiles"], {})

    def test_wipe_helper_is_safe_when_no_profile_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
            }
            with patch.dict(os.environ, env, clear=False):
                # No file on disk — must not raise.
                supervisor._wipe_on_owner_release(reason="unassign")

    def test_wipe_helper_swallows_unexpected_errors(self) -> None:
        """A flaky filesystem must not break the rebind path."""
        with patch.object(
            supervisor,
            "wipe_chatgpt_subscription_profile",
            side_effect=OSError("disk gremlin"),
        ):
            # Must not raise — the watchdog needs the rebind path to
            # keep moving even if the auth-store is temporarily
            # unreachable.
            supervisor._wipe_on_owner_release(reason="reassign")


class StaleProfileCarryoverGuardTests(unittest.TestCase):
    """Issue #23 / PR #24 review #2 — stale-profile carryover regression test.

    Simulates the failure mode Codex called out: an admin-driven
    unassign leaves the previous owner's subscription profile on
    disk; a later subscription-mode binding for a different owner
    would then make ``write_openclaw_config`` treat the stale
    profile as valid and run against the prior user's subscription.
    This test pins that the wipe-on-release helper clears the
    profile so the next binding starts from a clean slate.
    """

    def test_unassign_clears_profile_so_next_binding_starts_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(tmpdir, "tinyhat-secrets.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                # Phase 1: previous owner had a linked subscription.
                _seed_auth_profile(supervisor.openclaw_state_dir())
                self.assertIsNotNone(
                    supervisor.read_chatgpt_subscription_profile()
                )

                # Phase 2: platform unassigns the Computer. The
                # watchdog calls _wipe_on_owner_release.
                supervisor._wipe_on_owner_release(reason="unassign")

                # Phase 3: a NEW owner takes over and the platform
                # later flips their binding to chatgpt_subscription.
                # write_openclaw_config must NOT pick up the prior
                # profile — it should fall back to the default
                # OpenRouter path because no profile exists on disk
                # for this owner yet.
                new_owner_binding = {
                    "telegram_owner_user_id": "987654",  # different owner!
                    "telegram_bot_token": "987654:DEF",
                    "telegram_bot_username": "OtherTestbot",
                    "llm_auth_mode": "chatgpt_subscription",
                    "llm_model_ref": "openai/gpt-5.5",
                    "openrouter_api_key": "sk-or-v1-other",
                    "openrouter_base_url": "https://openrouter.ai/api/v1",
                    "openrouter_default_model": "openai/gpt-5.2",
                }
                supervisor.write_openclaw_config(new_owner_binding)
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)

                defaults = config["agents"]["defaults"]
                # New owner without a profile yet → STAYS on OpenClaw
                # runtime + OpenRouter (the opted-in-without-profile branch).
                # The critical assertion: the prior owner's profile
                # did NOT survive to make the supervisor flip to
                # subscription mode for the new owner.
                self.assertNotIn("agentRuntime", defaults)
                _assert_no_provider_runtime_pin(self, config, "openrouter")
                self.assertTrue(defaults["model"]["primary"].startswith("openrouter/"))


class HandleStartChatgptLinkCommandTests(unittest.TestCase):
    """Issue #23 — supervisor's start_chatgpt_link heartbeat handler.

    Tests the dispatch + idempotency layer. The actual subprocess
    spawn is integration-tested via the live E2E walk; here we
    intercept `_run_chatgpt_device_code_login_in_thread` so the
    handler logic can be exercised without touching the OpenClaw CLI.
    """

    def setUp(self) -> None:
        # Clear the in-memory idempotency set so test order doesn't matter.
        supervisor._subscription_link_sessions_started.clear()
        with supervisor._subscription_link_active_workers_lock:
            supervisor._subscription_link_active_workers.clear()

    def test_handler_rejects_malformed_command(self) -> None:
        captured = []
        with patch.object(
            supervisor,
            "_run_chatgpt_device_code_login_in_thread",
            side_effect=lambda **kw: captured.append(kw),
        ):
            supervisor.handle_start_chatgpt_link_command({"type": "start_chatgpt_link"})
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": ""}
            )
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "   "}
            )
        self.assertEqual(captured, [])

    def test_handler_is_idempotent_within_lifetime(self) -> None:
        import threading as threading_mod

        captured = []

        def _fake_runner(**kw):
            captured.append(kw["session_id"])

        class _InlineThread:
            """Run the target inline so the test can observe the call."""

            def __init__(self, target, kwargs, name, daemon):
                self._target = target
                self._kwargs = kwargs

            def start(self):
                self._target(**self._kwargs)

        with patch.object(
            supervisor,
            "_run_chatgpt_device_code_login_in_thread",
            side_effect=_fake_runner,
        ), patch.object(threading_mod, "Thread", _InlineThread):
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "sess-xyz"}
            )
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "sess-xyz"}
            )
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "other-sess"}
            )

        # Same session id only spawned once; different session id spawns.
        self.assertEqual(captured, ["sess-xyz", "other-sess"])

    def test_handler_applies_device_code_timing_env(self) -> None:
        import threading as threading_mod

        captured = []

        def _fake_runner(**kw):
            captured.append(kw)

        class _InlineThread:
            """Run the target inline so the test can observe the call."""

            def __init__(self, target, kwargs, name, daemon):
                self._target = target
                self._kwargs = kwargs

            def start(self):
                self._target(**self._kwargs)

        with (
            patch.object(
                supervisor,
                "_run_chatgpt_device_code_login_in_thread",
                side_effect=_fake_runner,
            ),
            patch.object(threading_mod, "Thread", _InlineThread),
            patch.dict(
                os.environ,
                {
                    supervisor.CHATGPT_DEVICE_CODE_URL_EMIT_TIMEOUT_ENV: "0.25",
                    supervisor.CHATGPT_DEVICE_CODE_URL_EMIT_ATTEMPTS_ENV: "2",
                    supervisor.CHATGPT_DEVICE_CODE_RETRY_DELAY_ENV: "0",
                    supervisor.CHATGPT_DEVICE_CODE_OVERALL_TIMEOUT_ENV: "5",
                },
                clear=False,
            ),
        ):
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "timing-env-test"}
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["session_id"], "timing-env-test")
        self.assertEqual(captured[0]["url_emit_timeout_s"], 0.25)
        self.assertEqual(captured[0]["url_emit_attempts"], 2)
        self.assertEqual(captured[0]["url_emit_retry_delay_s"], 0.0)
        self.assertEqual(captured[0]["overall_timeout_s"], 5.0)


class StripAnsiForCliCaptureTests(unittest.TestCase):
    """Issue #23 — ANSI cleanup so URL/Code regex matches OpenClaw's panel output."""

    def test_strips_color_and_cursor_sequences(self) -> None:
        raw = (
            "\x1b[?25l\x1b[2K\x1b[1G"
            "URL: https://auth.openai.com/codex/device\n"
            "\x1b[33mCode:\x1b[0m RJOE-NOHMF\n"
        )
        out = supervisor._strip_ansi_for_cli_capture(raw)
        self.assertIn("URL: https://auth.openai.com/codex/device", out)
        self.assertIn("Code: RJOE-NOHMF", out)
        # No raw ESC bytes survive.
        self.assertNotIn("\x1b", out)

    def test_normalizes_crlf_and_cr(self) -> None:
        out = supervisor._strip_ansi_for_cli_capture("line\r\nother\rlast\n")
        self.assertEqual(out, "line\nother\nlast\n")


class PostSubscriptionLinkResultShapeTests(unittest.TestCase):
    """Issue #23 — result POSTs must hit the documented endpoint shape."""

    def test_pending_with_url_and_code(self) -> None:
        calls = []

        def _fake_post(path, body):
            calls.append((path, body))
            return {}

        with patch.object(supervisor, "post_json", side_effect=_fake_post):
            supervisor._post_subscription_link_result(
                session_id="abc",
                status="pending",
                verification_url="https://auth.openai.com/codex/device",
                user_code="RJOE-NOHMF",
            )
        self.assertEqual(
            calls,
            [
                (
                    "/hapi/v1/computers/me/subscription-link-result",
                    {
                        "session_id": "abc",
                        "status": "pending",
                        "verification_url": "https://auth.openai.com/codex/device",
                        "user_code": "RJOE-NOHMF",
                    },
                )
            ],
        )

    def test_linked_omits_url_code_keys(self) -> None:
        calls = []
        with patch.object(
            supervisor, "post_json", side_effect=lambda p, b: calls.append((p, b)) or {}
        ):
            supervisor._post_subscription_link_result(
                session_id="abc", status="linked"
            )
        self.assertEqual(
            calls,
            [
                (
                    "/hapi/v1/computers/me/subscription-link-result",
                    {"session_id": "abc", "status": "linked"},
                )
            ],
        )

    def test_failed_carries_non_secret_reason(self) -> None:
        calls = []
        with patch.object(
            supervisor, "post_json", side_effect=lambda p, b: calls.append((p, b)) or {}
        ):
            supervisor._post_subscription_link_result(
                session_id="abc",
                status="failed",
                error="device-code login disabled on your ChatGPT account",
            )
        self.assertEqual(len(calls), 1)
        path, body = calls[0]
        self.assertEqual(body["status"], "failed")
        self.assertEqual(
            body["error"], "device-code login disabled on your ChatGPT account"
        )

    def test_post_failure_is_swallowed(self) -> None:
        """Network errors must not crash the worker thread."""
        with patch.object(
            supervisor, "post_json", side_effect=OSError("network down")
        ):
            # Must not raise.
            supervisor._post_subscription_link_result(
                session_id="abc", status="pending"
            )


class DeviceCodeWorkerQuickExitTests(unittest.TestCase):
    """PR #24 review (Codex 00:29Z) — child exits before URL/code emit.

    Reproduces Codex's exact case (`openclaw_bin='/bin/false'`) and
    pins that the worker thread POSTs a terminal status=failed instead
    of leaving the platform row stuck in pending forever.
    """

    def setUp(self) -> None:
        supervisor._subscription_link_sessions_started.clear()

    def test_quick_exit_posts_terminal_failed(self) -> None:
        calls: list[tuple[str, dict]] = []

        def _fake_post_json(path: str, body: dict) -> dict:
            calls.append((path, body))
            return {}

        with (
            patch.object(supervisor, "post_json", side_effect=_fake_post_json),
            patch.object(
                supervisor,
                "ensure_chatgpt_subscription_provider_available",
                return_value=True,
            ),
        ):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="quick-exit-test",
                openclaw_bin="/bin/false",
                url_emit_timeout_s=2.0,
                url_emit_retry_delay_s=0.0,
                overall_timeout_s=5.0,
            )

        # Exactly one POST, of type failed, with the standard endpoint.
        self.assertEqual(len(calls), 1, f"expected 1 POST, got: {calls}")
        path, body = calls[0]
        self.assertEqual(
            path, "/hapi/v1/computers/me/subscription-link-result"
        )
        self.assertEqual(body["session_id"], "quick-exit-test")
        self.assertEqual(body["status"], "failed")
        # The non-secret diagnostic should mention the security-settings
        # hint AND include the CLI tail / exit code.
        self.assertIn("device-code", body["error"].lower())
        self.assertIn("exit code", body["error"].lower())
        self.assertIn("Tried 3 startup attempts", body["error"])

    def test_quick_exit_posts_once_even_with_late_drain(self) -> None:
        """No double-post: only ONE terminal status per invocation."""
        calls: list[tuple[str, dict]] = []
        with (
            patch.object(
                supervisor,
                "post_json",
                side_effect=lambda p, b: calls.append((p, b)) or {},
            ),
            patch.object(
                supervisor,
                "ensure_chatgpt_subscription_provider_available",
                return_value=True,
            ),
        ):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="dup-guard-test",
                openclaw_bin="/bin/false",
                url_emit_timeout_s=2.0,
                url_emit_retry_delay_s=0.0,
                overall_timeout_s=5.0,
            )
        # Only one terminal post, not two.
        terminal_posts = [
            b for _, b in calls if b.get("status") in ("linked", "failed")
        ]
        self.assertEqual(len(terminal_posts), 1, f"got {terminal_posts}")

    def test_quick_exit_retries_before_reporting_failure(self) -> None:
        calls: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            attempts_path = os.path.join(tmpdir, "attempts.txt")
            script_path = os.path.join(tmpdir, "fake-openclaw")
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "count=0\n"
                    "if [ -f \"$ATTEMPTS_PATH\" ]; then\n"
                    "  count=$(cat \"$ATTEMPTS_PATH\")\n"
                    "fi\n"
                    "count=$((count + 1))\n"
                    "printf '%s' \"$count\" > \"$ATTEMPTS_PATH\"\n"
                    "if [ \"$count\" = \"1\" ]; then\n"
                    "  exit 1\n"
                    "fi\n"
                    "printf 'URL: https://auth.openai.com/codex/device\\n'\n"
                    "printf 'Code: RETR-YOKAY\\n'\n"
                    "printf 'OpenAI device code complete\\n'\n"
                )
            os.chmod(script_path, 0o755)

            with (
                patch.object(
                    supervisor,
                    "post_json",
                    side_effect=lambda p, b: calls.append((p, b)) or {},
                ),
                patch.object(
                    supervisor,
                    "ensure_chatgpt_subscription_provider_available",
                    return_value=True,
                ),
                patch.dict(os.environ, {"ATTEMPTS_PATH": attempts_path}, clear=False),
            ):
                supervisor._run_chatgpt_device_code_login_in_thread(
                    session_id="retry-start-test",
                    openclaw_bin=script_path,
                    url_emit_timeout_s=1.0,
                    url_emit_attempts=2,
                    url_emit_retry_delay_s=0.0,
                    overall_timeout_s=5.0,
                )

            with open(attempts_path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "2")

        bodies = [body for _, body in calls]
        self.assertEqual([body["status"] for body in bodies], ["pending", "linked"])
        self.assertEqual(bodies[0]["session_id"], "retry-start-test")
        self.assertEqual(
            bodies[0]["verification_url"],
            "https://auth.openai.com/codex/device",
        )
        self.assertEqual(bodies[0]["user_code"], "RETR-YOKAY")

    def test_url_emit_timeout_retries_before_reporting_failure(self) -> None:
        calls: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            attempts_path = os.path.join(tmpdir, "timeout-attempts.txt")
            script_path = os.path.join(tmpdir, "fake-openclaw-timeout")
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "count=0\n"
                    "if [ -f \"$ATTEMPTS_PATH\" ]; then\n"
                    "  count=$(cat \"$ATTEMPTS_PATH\")\n"
                    "fi\n"
                    "count=$((count + 1))\n"
                    "printf '%s' \"$count\" > \"$ATTEMPTS_PATH\"\n"
                    "if [ \"$count\" = \"1\" ]; then\n"
                    "  exec \"$PYTHON_BIN\" -c 'import time; time.sleep(30)'\n"
                    "fi\n"
                    "printf 'URL: https://auth.openai.com/codex/device\\n'\n"
                    "printf 'Code: TIME-OKAY\\n'\n"
                    "printf 'OpenAI device code complete\\n'\n"
                )
            os.chmod(script_path, 0o755)

            with (
                patch.object(
                    supervisor,
                    "post_json",
                    side_effect=lambda p, b: calls.append((p, b)) or {},
                ),
                patch.object(
                    supervisor,
                    "ensure_chatgpt_subscription_provider_available",
                    return_value=True,
                ),
                patch.dict(
                    os.environ,
                    {
                        "ATTEMPTS_PATH": attempts_path,
                        "PYTHON_BIN": sys.executable,
                    },
                    clear=False,
                ),
            ):
                supervisor._run_chatgpt_device_code_login_in_thread(
                    session_id="retry-timeout-test",
                    openclaw_bin=script_path,
                    url_emit_timeout_s=0.1,
                    url_emit_attempts=2,
                    url_emit_retry_delay_s=0.0,
                    overall_timeout_s=5.0,
                )

            with open(attempts_path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "2")

        bodies = [body for _, body in calls]
        self.assertEqual([body["status"] for body in bodies], ["pending", "linked"])
        self.assertEqual(bodies[0]["session_id"], "retry-timeout-test")
        self.assertEqual(bodies[0]["user_code"], "TIME-OKAY")

    def test_pty_fork_failure_posts_terminal_failed(self) -> None:
        """If we can't even allocate a PTY, the platform still hears
        about it (failed) instead of waiting indefinitely."""
        calls: list[tuple[str, dict]] = []
        import pty as pty_mod

        with (
            patch.object(
                supervisor,
                "post_json",
                side_effect=lambda p, b: calls.append((p, b)) or {},
            ),
            patch.object(
                supervisor,
                "ensure_chatgpt_subscription_provider_available",
                return_value=True,
            ),
            patch.object(pty_mod, "fork", side_effect=OSError("no ptys")),
        ):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="pty-fail-test",
                openclaw_bin="/bin/false",
            )
        self.assertEqual(len(calls), 1)
        path, body = calls[0]
        self.assertEqual(body["status"], "failed")
        self.assertIn("pseudo-terminal", body["error"])

    def test_provider_check_failure_posts_terminal_failed(self) -> None:
        calls: list[tuple[str, dict]] = []

        with (
            patch.object(
                supervisor,
                "post_json",
                side_effect=lambda p, b: calls.append((p, b)) or {},
            ),
            patch.object(
                supervisor,
                "ensure_chatgpt_subscription_provider_available",
                side_effect=RuntimeError("Provider not found: openai"),
            ),
        ):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="provider-missing-test",
                openclaw_bin="/bin/false",
            )

        self.assertEqual(len(calls), 1)
        path, body = calls[0]
        self.assertEqual(
            path, "/hapi/v1/computers/me/subscription-link-result"
        )
        self.assertEqual(body["session_id"], "provider-missing-test")
        self.assertEqual(body["status"], "failed")
        self.assertIn("OpenAI provider plugin", body["error"])


class CrossOwnerCredentialLeakGuardTests(unittest.TestCase):
    """PR #24 review at 01:19Z — late-arriving worker + cold-start.

    Codex's exact reproduction: owner A starts the device-code flow,
    the Computer is unassigned/reassigned before the user approves,
    owner A approves after the wipe; the previously still-alive
    worker's CLI must not write a subscription profile back to the
    shared auth store the new owner will inherit.
    """

    def setUp(self) -> None:
        supervisor._subscription_link_sessions_started.clear()
        with supervisor._subscription_link_active_workers_lock:
            supervisor._subscription_link_active_workers.clear()
        # Reset generation back to a deterministic value so tests
        # don't depend on prior-test state.
        with supervisor._binding_generation_lock:
            supervisor._binding_generation = 0

    def test_bump_generation_increments_monotonically(self) -> None:
        start = supervisor._current_binding_generation()
        supervisor._bump_binding_generation(reason="test-1")
        supervisor._bump_binding_generation(reason="test-2")
        self.assertEqual(supervisor._current_binding_generation(), start + 2)

    def test_wipe_on_release_bumps_generation_and_cancels_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"TINYHAT_DEV_RUNTIME": "1", "TINYHAT_RUNTIME_HOME": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                # Register a fake in-flight worker.
                with supervisor._subscription_link_active_workers_lock:
                    supervisor._subscription_link_active_workers["fake-session"] = {
                        "pid": 999_999,
                        "generation": 0,
                    }
                killed = []

                def _fake_kill(pid, sig):
                    killed.append((pid, sig))

                start_gen = supervisor._current_binding_generation()
                with patch.object(supervisor.os, "kill", side_effect=_fake_kill):
                    supervisor._wipe_on_owner_release(reason="reassign")
                # Generation bumped.
                self.assertEqual(
                    supervisor._current_binding_generation(), start_gen + 1
                )
                # Worker subprocess was SIGTERMed.
                self.assertEqual(killed, [(999_999, supervisor.signal.SIGTERM)])

    def test_late_worker_after_release_does_not_post_or_persist(self) -> None:
        """The full race: a worker that has already posted `pending` keeps
        running while owner release fires; the worker's check on the next
        loop iteration sees it's superseded, kills the CLI, re-wipes any
        late profile write, and exits WITHOUT posting linked/failed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_SECRETS_PATH": os.path.join(
                    tmpdir, "tinyhat-secrets.json"
                ),
            }
            with patch.dict(os.environ, env, clear=False):
                # Start with a clean auth store; simulate the worker
                # later writing a profile.
                state_dir = supervisor.openclaw_state_dir()
                posts: list[tuple[str, dict]] = []

                def _fake_post(path, body):
                    posts.append((path, body))
                    return {}

                # Build a fake "previous worker thread" that wrote a
                # profile right around the time of release.
                _seed_auth_profile(state_dir)
                self.assertIsNotNone(
                    supervisor.read_chatgpt_subscription_profile()
                )

                # The worker's stale generation; the supervisor has
                # since rebound, so the current generation has moved.
                supervisor._bump_binding_generation(reason="reassign")

                # Now drive a brand-new write_openclaw_config for the
                # NEW owner in chatgpt_subscription mode WITH the old
                # profile still on disk. Without the cold-start +
                # release-time re-wipe, this is exactly the cross-
                # owner leak Codex reproduced. We verify that the
                # release-path wipe (called by the watchdog) clears
                # the profile so the new owner stays on OpenClaw +
                # OpenRouter.
                killed: list[int] = []

                with patch.object(
                    supervisor.os,
                    "kill",
                    side_effect=lambda pid, sig: killed.append(pid),
                ), patch.object(
                    supervisor, "post_json", side_effect=_fake_post
                ):
                    supervisor._wipe_on_owner_release(reason="reassign")

                # Profile gone.
                self.assertIsNone(
                    supervisor.read_chatgpt_subscription_profile()
                )

                # Now apply config for the new owner.
                supervisor.write_openclaw_config(
                    _subscription_binding(with_openrouter=True),
                )
                config_path = supervisor.openclaw_config_path()
                with open(config_path, encoding="utf-8") as fh:
                    config = json.load(fh)
                defaults = config["agents"]["defaults"]
                # NEW owner sees the opted-in-but-no-profile-yet
                # branch — stays on OpenClaw runtime + OpenRouter (not
                # subscription mode with the prior owner's credential).
                self.assertNotIn("agentRuntime", defaults)
                _assert_no_provider_runtime_pin(self, config, "openrouter")
                self.assertTrue(
                    defaults["model"]["primary"].startswith("openrouter/")
                )

                # No subscription-link result POSTs were issued (the
                # release path doesn't talk about subscription
                # lifecycle; the supervisor stays silent on
                # superseded workers).
                self.assertEqual(
                    [
                        body
                        for path, body in posts
                        if "subscription-link-result" in path
                    ],
                    [],
                )


class ColdStartOrphanProfileWipeTests(unittest.TestCase):
    """PR #24 review at 01:19Z attack path #2 — supervisor restart while
    Computer is unassigned, prior owner's profile on disk.

    Phase B's binding-poll loop wipes orphaned profiles before the
    next owner is assigned, so they cannot be consumed by a future
    chatgpt_subscription binding.
    """

    def setUp(self) -> None:
        with supervisor._binding_generation_lock:
            supervisor._binding_generation = 0
        with supervisor._subscription_link_active_workers_lock:
            supervisor._subscription_link_active_workers.clear()

    def test_phase_b_path_wipes_orphan_profile_on_unassigned_observation(
        self,
    ) -> None:
        """Exercise the inline cold-start branch without driving Phase B
        end-to-end — the inline branch calls _wipe_on_owner_release."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"TINYHAT_DEV_RUNTIME": "1", "TINYHAT_RUNTIME_HOME": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                state_dir = supervisor.openclaw_state_dir()
                _seed_auth_profile(state_dir)
                self.assertIsNotNone(
                    supervisor.read_chatgpt_subscription_profile()
                )

                # Phase B's cold-start branch sequence: see profile,
                # call _wipe_on_owner_release(reason="cold-start-orphan").
                with patch.object(supervisor.os, "kill"):
                    supervisor._wipe_on_owner_release(
                        reason="cold-start-orphan"
                    )

                self.assertIsNone(
                    supervisor.read_chatgpt_subscription_profile()
                )

    def test_phase_b_cancels_active_worker_even_when_no_profile_exists_yet(
        self,
    ) -> None:
        """PR #24 review at 01:32Z — Codex's exact reproduction.

        A device-code worker is in flight (user hasn't approved yet
        → no profile on disk). Phase B observes `assigned=false`.
        Phase B's cold-start branch MUST cancel the worker via the
        owner-release path even though the wipe-file step is a no-op,
        so the worker can't write `openai:old@example.com` to
        the auth store after the user finally approves.

        Without this fix (gated `if profile is not None: _wipe...`):
        the in-flight worker survives Phase B's check, writes the
        profile when the user approves later, and the next owner's
        `write_openclaw_config` selects subscription mode using the
        prior owner's credential.

        With the fix (unconditional `_wipe_on_owner_release` call):
        Phase B's first `assigned=false` observation bumps the
        generation + SIGTERMs the active worker. The worker exits
        on its next loop iteration without writing.
        """
        # Register a fake worker representing the previous owner's
        # in-flight CLI subprocess.
        with supervisor._subscription_link_active_workers_lock:
            supervisor._subscription_link_active_workers[
                "owner-A-session"
            ] = {"pid": 424_242, "generation": 0}

        starting_gen = supervisor._current_binding_generation()
        killed_pids: list[int] = []

        # The inline Phase B branch boils down to this single call.
        # Execute it directly so we can pin the side-effects without
        # standing up the full polling loop.
        with patch.object(
            supervisor.os, "kill",
            side_effect=lambda pid, sig: killed_pids.append(pid),
        ):
            supervisor._wipe_on_owner_release(reason="cold-start-orphan")

        # 1. Generation bumped (worker's next loop-top check will
        # observe supersession and exit silently).
        self.assertEqual(
            supervisor._current_binding_generation(), starting_gen + 1
        )
        # 2. SIGTERM was sent to the active worker's CLI subprocess
        # (so even if the generation check hasn't fired yet, the
        # CLI dies before it can write a profile).
        self.assertIn(424_242, killed_pids)


class DispatcherCapturesGenerationSynchronouslyTests(unittest.TestCase):
    """PR #24 review at 01:41Z — generation capture must precede thread spawn.

    Codex's reproduction: stale `start_chatgpt_link` command lands;
    dispatcher spawns thread; owner-release fires AFTER Thread.start()
    but BEFORE the worker captures its `starting_generation`. With
    the bug, the worker stamps the post-release generation as its
    own and never observes supersession; with the fix, the
    dispatcher captures synchronously and passes it in, so the
    worker's first loop iteration immediately observes supersession.
    """

    def setUp(self) -> None:
        supervisor._subscription_link_sessions_started.clear()
        with supervisor._subscription_link_active_workers_lock:
            supervisor._subscription_link_active_workers.clear()
        with supervisor._binding_generation_lock:
            supervisor._binding_generation = 0

    def test_dispatcher_passes_starting_generation_explicitly(self) -> None:
        """The handler must pass starting_generation as a kwarg, not let
        the worker re-capture it. This pins the fix's contract: any
        future refactor that drops the kwarg pass breaks this test."""
        import threading as threading_mod

        captured_kwargs: list[dict] = []

        class _InlineThread:
            def __init__(self, target, kwargs, name, daemon):
                self._target = target
                self._kwargs = kwargs

            def start(self):
                captured_kwargs.append(dict(self._kwargs))
                # Don't actually run; we just want to inspect what
                # the dispatcher would have passed.

        with patch.object(
            threading_mod, "Thread", _InlineThread
        ):
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "race-test"}
            )

        self.assertEqual(len(captured_kwargs), 1)
        kw = captured_kwargs[0]
        self.assertEqual(kw["session_id"], "race-test")
        self.assertIn("starting_generation", kw)
        # And it matches the supervisor's current generation at
        # dispatch time (since no race interposed in this test).
        self.assertEqual(
            kw["starting_generation"],
            supervisor._current_binding_generation(),
        )

    def test_dispatcher_pre_registers_worker_before_thread_starts(self) -> None:
        """The release path needs to see the worker registered even
        before the thread runs, so a release in the dispatch-then-
        thread-start gap can at least bump the generation and have
        the worker observe it."""
        import threading as threading_mod

        observed_during_dispatch: list[dict[str, Any]] = []

        class _ObservingThread:
            def __init__(self, target, kwargs, name, daemon):
                # Snapshot the registry as the dispatcher saw it just
                # before Thread.start() — pre-registration MUST have
                # already happened.
                with supervisor._subscription_link_active_workers_lock:
                    observed_during_dispatch.append(
                        dict(supervisor._subscription_link_active_workers)
                    )

            def start(self):
                pass  # don't run

        with patch.object(
            threading_mod, "Thread", _ObservingThread
        ):
            supervisor.handle_start_chatgpt_link_command(
                {"type": "start_chatgpt_link", "session_id": "pre-reg-test"}
            )

        # By the time Thread() was constructed, the dispatcher had
        # already pre-registered the worker entry (with pid=None).
        self.assertIn("pre-reg-test", observed_during_dispatch[0])
        entry = observed_during_dispatch[0]["pre-reg-test"]
        self.assertIsNone(entry["pid"])  # forked PID not known yet
        self.assertEqual(
            entry["generation"], supervisor._current_binding_generation()
        )

    def test_worker_pre_fork_check_exits_silently_on_supersession(self) -> None:
        """If owner-release fires between dispatcher and the worker
        actually running, the worker's pre-fork check exits without
        forking and without posting a terminal status."""
        # Pre-register as the dispatcher would have.
        with supervisor._subscription_link_active_workers_lock:
            supervisor._subscription_link_active_workers["pre-fork-race"] = {
                "pid": None,
                "generation": 5,
            }
        # Simulate release: bump generation past 5.
        with supervisor._binding_generation_lock:
            supervisor._binding_generation = 10

        # Patch pty.fork so the test FAILS LOUDLY if we somehow reach
        # it (we shouldn't — the pre-fork check should bail first).
        posts: list[tuple[str, dict]] = []
        import pty as pty_mod

        def _should_not_fork():
            raise AssertionError(
                "Pre-fork supersession check failed to short-circuit"
            )

        with patch.object(pty_mod, "fork", side_effect=_should_not_fork), \
             patch.object(
                 supervisor, "post_json",
                 side_effect=lambda p, b: posts.append((p, b)) or {},
             ):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="pre-fork-race",
                starting_generation=5,  # stale!
            )

        # No fork was attempted; no terminal POST was issued.
        self.assertEqual(posts, [])
        # Registry was cleaned up.
        with supervisor._subscription_link_active_workers_lock:
            self.assertNotIn(
                "pre-fork-race",
                supervisor._subscription_link_active_workers,
            )


class CollectComponentVersionsTests(unittest.TestCase):
    """Issue #26 — payload-shape coverage for the heartbeat component_versions.

    ``collect_component_versions`` feeds ``POST /me/heartbeat`` so the
    platform can show what a Computer is actually running. These tests pin
    the contract the platform ingests: the three components
    (runtime / plugin / framework), each with a ``version`` and an
    optional ``sha`` (framework never carries one); components with no
    resolvable version or sha are omitted entirely (not emitted with a
    null body); and a failing reader must never break the heartbeat.
    """

    def test_includes_all_three_components(self) -> None:
        """All readers resolve → runtime + plugin carry their sha, the
        framework carries ``version`` with ``sha`` always ``None``."""
        with (
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value="0.10.1"
            ),
            patch.object(
                supervisor, "_read_runtime_git_sha", return_value="abc1234"
            ),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={
                    "version": "0.2.2",
                    "resolved_commit_sha": "def5678",
                },
            ),
            patch.object(
                supervisor,
                "_read_openclaw_framework_version",
                return_value="2026.5.28",
            ),
        ):
            components = supervisor.collect_component_versions()

        self.assertEqual(
            components,
            {
                "runtime": {"version": "0.10.1", "sha": "abc1234"},
                "plugin": {"version": "0.2.2", "sha": "def5678"},
                "framework": {"version": "2026.5.28", "sha": None},
            },
        )
        # The framework is an npm package — it must never report a sha.
        self.assertIsNone(components["framework"]["sha"])

    def test_omits_components_with_no_resolvable_version_or_sha(self) -> None:
        """A component whose version AND sha both come back empty is
        dropped from the result — not emitted with null fields — so the
        platform falls back to its provisioning manifest for it.

        Here the runtime resolves nothing (no VERSION file, not a git
        tree) and the framework CLI is absent; only the plugin survives.
        """
        with (
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value=""
            ),
            patch.object(supervisor, "_read_runtime_git_sha", return_value=""),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={
                    "version": "0.2.2",
                    "resolved_commit_sha": "def5678",
                },
            ),
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value=""
            ),
        ):
            components = supervisor.collect_component_versions()

        self.assertNotIn("runtime", components)
        self.assertNotIn("framework", components)
        self.assertEqual(
            components,
            {"plugin": {"version": "0.2.2", "sha": "def5678"}},
        )

    def test_optional_sha_is_nulled_when_only_version_resolves(self) -> None:
        """A component with a version but no sha is still reported, with
        its ``sha`` coerced to ``None`` (the optional-SHA contract)."""
        with (
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value="0.10.1"
            ),
            patch.object(supervisor, "_read_runtime_git_sha", return_value=""),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                # Marker present but only a sha, no version string.
                return_value={"resolved_commit_sha": "def5678"},
            ),
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value=""
            ),
        ):
            components = supervisor.collect_component_versions()

        # runtime: version present, sha missing -> sha is None, still reported.
        self.assertEqual(components["runtime"], {"version": "0.10.1", "sha": None})
        # plugin: sha present, version missing -> version None, still reported.
        self.assertEqual(components["plugin"], {"version": None, "sha": "def5678"})

    def test_plugin_unknown_version_is_treated_as_unreported(self) -> None:
        """An ``"unknown"`` plugin marker version (the installer's
        placeholder when no package.json/version.txt was found) is not
        surfaced as a version, but the resolved sha still is."""
        with (
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value=""
            ),
            patch.object(supervisor, "_read_runtime_git_sha", return_value=""),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={
                    "version": "unknown",
                    "resolved_commit_sha": "def5678",
                },
            ),
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value=""
            ),
        ):
            components = supervisor.collect_component_versions()

        self.assertEqual(
            components,
            {"plugin": {"version": None, "sha": "def5678"}},
        )

    def test_never_raises_when_a_reader_fails(self) -> None:
        """A reader raising must not propagate: the failing component is
        skipped while the others survive, so the heartbeat still ships."""
        with (
            patch.object(
                supervisor,
                "_read_runtime_repo_version",
                side_effect=RuntimeError("git exploded"),
            ),
            patch.object(
                supervisor, "_read_runtime_git_sha", return_value="abc1234"
            ),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={
                    "version": "0.2.2",
                    "resolved_commit_sha": "def5678",
                },
            ),
            patch.object(
                supervisor,
                "_read_openclaw_framework_version",
                return_value="2026.5.28",
            ),
        ):
            # Must not raise.
            components = supervisor.collect_component_versions()

        self.assertIsInstance(components, dict)
        # The runtime reader blew up, so runtime is dropped; the other two
        # components are unaffected.
        self.assertNotIn("runtime", components)
        self.assertEqual(components["plugin"], {"version": "0.2.2", "sha": "def5678"})
        self.assertEqual(
            components["framework"], {"version": "2026.5.28", "sha": None}
        )


class BootstrapSystemdIsolationTests(unittest.TestCase):
    def test_bootstrap_creates_unprivileged_runtime_user(self) -> None:
        script = _bootstrap_script_text()

        self.assertIn(
            'TINYHAT_RUNTIME_USER="${TINYHAT_OPENCLAW_RUNTIME_USER:-tinyhat}"',
            script,
        )
        self.assertIn(
            'TINYHAT_RUNTIME_GROUP="${TINYHAT_OPENCLAW_RUNTIME_GROUP:-tinyhat}"',
            script,
        )
        self.assertIn(
            'WORKLOAD_SLICE_UNIT_NAME="tinyhat-openclaw-workload.slice"',
            script,
        )
        self.assertIn('groupadd --system "${TINYHAT_RUNTIME_GROUP}"', script)
        self.assertIn('--gid "${TINYHAT_RUNTIME_GROUP}"', script)
        self.assertIn('--home-dir "${OPENCLAW_STATE_DIR}"', script)
        self.assertIn('chown -R \\', script)
        self.assertIn('"${OPENCLAW_CONFIG_DIR}"', script)
        self.assertIn('"${OPENCLAW_STATE_DIR}"', script)
        # Bootstrap chowns once before install and again after plugin install,
        # because npm/OpenClaw plugin commands can create root-owned state.
        self.assertGreaterEqual(script.count("chown_runtime_paths"), 3)
        self.assertIn('systemctl start "${WORKLOAD_SLICE_UNIT_NAME}"', script)

    def test_workload_slice_is_bounded_for_hold_down_sampling(self) -> None:
        unit = _bootstrap_unit_block("WORKLOAD_SLICE_UNIT")

        for directive in (
            "MemoryAccounting=true",
            "MemoryHigh=2400M",
            "MemoryMax=3072M",
            "CPUAccounting=true",
            "CPUQuota=175%",
            "TasksAccounting=true",
            "TasksMax=512",
        ):
            self.assertIn(directive, unit)

    def test_gateway_unit_is_bounded_unprivileged_workload(self) -> None:
        unit = _bootstrap_unit_block("GATEWAY_UNIT")

        # #685: deliberately NOT PartOf= the supervisor — that bounced
        # the gateway on every supervisor watchdog/crash restart and
        # broke reattach continuity. Teardown is owned by the
        # supervisor's finally: stop_openclaw_gateway().
        self.assertNotIn("PartOf=", unit)
        for directive in (
            "After=network-online.target ${SUPERVISOR_UNIT_NAME}",
            "StartLimitIntervalSec=10min",
            "StartLimitBurst=3",
            "User=${TINYHAT_RUNTIME_USER}",
            "Group=${TINYHAT_RUNTIME_GROUP}",
            "UMask=0077",
            "Slice=${WORKLOAD_SLICE_UNIT_NAME}",
            "MemoryAccounting=true",
            "MemoryHigh=2400M",
            "MemoryMax=3072M",
            "CPUAccounting=true",
            "CPUQuota=175%",
            "TasksAccounting=true",
            "TasksMax=512",
            "OOMPolicy=stop",
            "OOMScoreAdjust=500",
            "Nice=5",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectHome=true",
            "ReadWritePaths=${OPENCLAW_CONFIG_DIR} ${OPENCLAW_STATE_DIR}",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "Restart=on-failure",
        ):
            self.assertIn(directive, unit)

    def test_supervisor_unit_is_protected_control_plane(self) -> None:
        unit = _bootstrap_unit_block("SUPERVISOR_UNIT")

        for directive in (
            "Type=notify",
            "NotifyAccess=main",
            "WatchdogSec=180s",
            "StartLimitIntervalSec=10min",
            "StartLimitBurst=6",
            "Environment=TINYHAT_OPENCLAW_RUNTIME_USER=${TINYHAT_RUNTIME_USER}",
            "Environment=TINYHAT_OPENCLAW_RUNTIME_GROUP=${TINYHAT_RUNTIME_GROUP}",
            "Slice=tinyhat-openclaw-control.slice",
            "MemoryAccounting=true",
            "MemoryHigh=512M",
            "MemoryMax=1536M",
            "CPUAccounting=true",
            "CPUQuota=100%",
            "TasksAccounting=true",
            "TasksMax=512",
            "OOMPolicy=continue",
            "OOMScoreAdjust=-800",
            "Restart=on-failure",
            "TimeoutStopSec=30",
        ):
            self.assertIn(directive, unit)


class ComponentUpdateGatewayRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stop_holder = dict(supervisor._stop_holder)

    def tearDown(self) -> None:
        supervisor._stop_holder.clear()
        supervisor._stop_holder.update(self._stop_holder)

    def test_component_update_restart_waits_for_fresh_gateway_health(self) -> None:
        supervisor._stop_holder["stop"] = False
        supervisor._stop_holder["rebind"] = False
        binding = {"telegram_bot_username": "Tinychattestbot"}

        def fake_wait(_started_at):
            self.assertTrue(supervisor._stop_holder["component_update_restart"])

        with (
            patch.object(
                supervisor,
                "start_openclaw_gateway",
                return_value=1234.5,
            ) as start,
            patch.object(
                supervisor, "wait_for_openclaw_start", side_effect=fake_wait
            ) as wait,
        ):
            supervisor._restart_gateway_for_component_update(binding)

        start.assert_called_once_with(binding)
        wait.assert_called_once_with(1234.5)
        self.assertFalse(supervisor._stop_holder["stop"])
        self.assertFalse(supervisor._stop_holder["rebind"])
        self.assertFalse(supervisor._stop_holder["component_update_restart"])

    def test_component_update_restart_propagates_readiness_failure(self) -> None:
        with (
            patch.object(
                supervisor,
                "start_openclaw_gateway",
                return_value=1234.5,
            ),
            patch.object(
                supervisor,
                "wait_for_openclaw_start",
                side_effect=RuntimeError("telegram did not reconnect"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "telegram did not reconnect"):
                supervisor._restart_gateway_for_component_update()
        self.assertFalse(supervisor._stop_holder["component_update_restart"])


class UpdateComponentCommandTests(unittest.TestCase):
    """In-place ``update_component`` heartbeat command (tinyloophub/tinyloop#562).

    The supervisor updates any subset of {plugin, framework, runtime} to a
    target release, in place, then POSTs the result. These tests stub every
    side-effecting boundary (git/npm subprocess, gateway restart, supervisor
    restart, result POST) so no real process is touched, and assert the
    contract: correct installer/npm invocation, applied-vs-failed status,
    non-secret diagnostics, the runtime post-before-restart ordering, dev-mode
    relaxation, and per-revision dedupe.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._state_path = os.path.join(self._tmp, "component-update-state.json")
        self._plugin_override_path = os.path.join(
            self._tmp,
            "plugin-source.json",
        )
        # Point the dedupe-state file at a tempdir and force prod (non-dev)
        # mode unless an individual test overrides it.
        self._env = patch.dict(
            os.environ,
            {
                "TINYHAT_COMPONENT_UPDATE_STATE_PATH": self._state_path,
                "TINYHAT_PLUGIN_SOURCE_OVERRIDE_PATH": self._plugin_override_path,
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL": "https://example.com/tinyhat.git",
                "TINYHAT_PLATFORM_PLUGIN_REPO_REF": "boot-ref",
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("TINYHAT_DEV_RUNTIME", None)
        # collect_component_versions hits git + the openclaw CLI; stub it for
        # every test so the result POST has a deterministic payload. The same
        # payload is cached into the dedupe-state record, so tests assert
        # against self._versions_payload.
        self._versions_payload = {
            "runtime": {"version": "0.10.2", "sha": "rsha"},
            "plugin": {"version": "1.2.3", "sha": "psha"},
            "framework": {"version": "1.4.2", "sha": None},
        }
        self._versions = patch.object(
            supervisor,
            "collect_component_versions",
            return_value=self._versions_payload,
        )
        self._versions.start()
        # Never make a real network call from the result POST.
        self._post = patch.object(supervisor, "_post_component_update_result")
        self._posted = self._post.start()

    def tearDown(self) -> None:
        patch.stopall()

    def _read_state(self) -> dict:
        with open(self._state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_supervisor_restart_unit_matches_bootstrap_unit(self) -> None:
        self.assertEqual(
            supervisor.SUPERVISOR_SYSTEMD_UNIT,
            "tinyhat-openclaw.service",
        )

    def test_state_write_defaults_to_unreported_with_empty_cache(self) -> None:
        # New state shape: reported defaults to False and the cached-result
        # fields are present (empty / null when not supplied).
        supervisor._write_component_update_state(7, "applied")
        state = supervisor._read_component_update_state()
        self.assertEqual(state["last_revision"], 7)
        self.assertEqual(state["status"], "applied")
        self.assertIs(state["reported"], False)
        self.assertEqual(state["applied_versions"], {})
        self.assertIsNone(state["diagnostic"])

    def test_state_write_persists_reported_and_cached_result(self) -> None:
        supervisor._write_component_update_state(
            8,
            "failed",
            diagnostic="boom",
            applied_versions={"plugin": {"version": "1.2.3", "sha": "p"}},
            reported=True,
        )
        state = supervisor._read_component_update_state()
        self.assertEqual(state["last_revision"], 8)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["diagnostic"], "boom")
        self.assertIs(state["reported"], True)
        self.assertEqual(
            state["applied_versions"], {"plugin": {"version": "1.2.3", "sha": "p"}}
        )

    def test_dispatch_routes_update_component_to_handler(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 11,
            "targets": {"plugin": {"ref": "v1.2.3"}},
        }
        with patch.object(supervisor, "handle_update_component_command") as handler:
            supervisor.handle_heartbeat_command(cmd)
        handler.assert_called_once_with(cmd, binding=None)

    def test_dispatch_routes_apply_packages_to_handler(self) -> None:
        cmd = {
            "type": "apply_packages",
            "revision": 12,
            "platform_plugin": {
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "requested_ref": "v0.4.5",
            },
            "default_skills": [],
        }
        with patch.object(supervisor, "handle_apply_packages_command") as handler:
            supervisor.handle_heartbeat_command(cmd)
        handler.assert_called_once_with(cmd, binding=None)

    def test_plugin_only_target_installs_ref_restarts_and_reports_applied(
        self,
    ) -> None:
        cmd = {
            "type": "update_component",
            "revision": 3,
            "targets": {"plugin": {"ref": "v2.0.0"}},
        }
        captured_ref = {}
        order: list[str] = []
        self._posted.side_effect = lambda *a, **k: order.append("post")

        def fake_install(**kwargs):
            captured_ref.update(kwargs)

        with (
            patch.object(
                supervisor, "ensure_tinyhat_plugin_installed", side_effect=fake_install
            ) as installer,
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={"resolved_commit_sha": "abc123", "version": "2.0.0"},
            ),
            patch.object(
                supervisor,
                "_restart_gateway_for_component_update",
                side_effect=lambda *_args, **_kwargs: order.append("restart"),
            ) as restart_gateway,
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)

        installer.assert_called_once_with(
            repo_url="https://example.com/tinyhat.git",
            repo_ref="v2.0.0",
        )
        self.assertEqual(captured_ref["repo_url"], "https://example.com/tinyhat.git")
        self.assertEqual(captured_ref["repo_ref"], "v2.0.0")
        restart_gateway.assert_called_once()
        self.assertEqual(order, ["restart", "post"])
        # No runtime target -> the supervisor process is never restarted.
        restart_supervisor.assert_not_called()
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 3)
        self.assertEqual(kwargs["status"], "applied")
        state = self._read_state()
        self.assertEqual(state["last_revision"], 3)
        self.assertEqual(state["status"], "applied")
        # The result POST was acknowledged (the mocked post did not raise),
        # so the revision is marked reported and a redelivery will dedupe.
        self.assertIs(state["reported"], True)
        with open(self._plugin_override_path, encoding="utf-8") as fh:
            override = json.load(fh)
        self.assertEqual(override["repo_url"], "https://example.com/tinyhat.git")
        self.assertEqual(override["repo_ref"], "v2.0.0")
        self.assertEqual(override["resolved_commit_sha"], "abc123")
        self.assertEqual(override["version"], "2.0.0")

    def test_component_restart_failure_reports_failed_without_raising(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 32,
            "targets": {"plugin": {"ref": "v2.0.0"}},
        }
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed"),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={"resolved_commit_sha": "abc123", "version": "2.0.0"},
            ),
            patch.object(
                supervisor,
                "_restart_gateway_for_component_update",
                side_effect=RuntimeError("telegram did not reconnect"),
            ) as restart_gateway,
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)

        restart_gateway.assert_called_once()
        restart_supervisor.assert_not_called()
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 32)
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("gateway restart after component update failed", kwargs["diagnostic"])
        self.assertIn("telegram did not reconnect", kwargs["diagnostic"])
        state = self._read_state()
        self.assertEqual(state["last_revision"], 32)
        self.assertEqual(state["status"], "failed")
        self.assertIs(state["reported"], True)

    def test_plugin_update_persists_ref_for_later_rebind_without_mutating_env(
        self,
    ) -> None:
        sentinel = "ref-before-update"
        os.environ[supervisor.TINYHAT_PLUGIN_REPO_REF_ENV] = sentinel
        try:
            cmd = {
                "type": "update_component",
                "revision": 31,
                "targets": {"plugin": {"ref": "v3.0.0"}},
            }
            with (
                patch.object(supervisor, "ensure_tinyhat_plugin_installed"),
                patch.object(
                    supervisor, "_read_installed_plugin_marker", return_value={}
                ),
                patch.object(supervisor, "_restart_gateway_for_component_update"),
            ):
                supervisor.handle_update_component_command(cmd)
            # The boot-time ref env var stays untouched; persistence comes from
            # the source override that later rebinds/restarts will read.
            self.assertEqual(
                os.environ.get(supervisor.TINYHAT_PLUGIN_REPO_REF_ENV), sentinel
            )
            self.assertEqual(
                supervisor._tinyhat_plugin_source(),
                ("https://example.com/tinyhat.git", "v3.0.0"),
            )
        finally:
            os.environ.pop(supervisor.TINYHAT_PLUGIN_REPO_REF_ENV, None)

    def test_framework_target_invokes_npm_and_verifies_version(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 4,
            "targets": {"framework": {"version": "1.5.0"}},
        }
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(supervisor.subprocess, "run", return_value=ok) as runner,
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value="1.5.0"
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
        ):
            supervisor.handle_update_component_command(cmd)

        invoked = [call.args[0] for call in runner.call_args_list]
        self.assertIn(
            ["npm", "install", "-g", "--no-fund", "--no-audit", "openclaw@1.5.0"],
            invoked,
        )
        restart_gateway.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 4)
        self.assertEqual(kwargs["status"], "applied")

    def test_plugin_and_framework_targets_restart_gateway_once(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 33,
            "targets": {
                "plugin": {"ref": "v2.0.0"},
                "framework": {"version": "1.5.0"},
            },
        }
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={"resolved_commit_sha": "abc123", "version": "2.0.0"},
            ),
            patch.object(supervisor.subprocess, "run", return_value=ok),
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value="1.5.0"
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
        ):
            supervisor.handle_update_component_command(cmd)

        installer.assert_called_once()
        restart_gateway.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 33)
        self.assertEqual(kwargs["status"], "applied")

    def test_framework_version_mismatch_marks_failed(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 5,
            "targets": {"framework": {"version": "1.5.0"}},
        }
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(supervisor.subprocess, "run", return_value=ok),
            patch.object(
                supervisor, "_read_openclaw_framework_version", return_value="1.4.2"
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
        ):
            supervisor.handle_update_component_command(cmd)

        # A version that did not land must NOT restart the gateway.
        restart_gateway.assert_not_called()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 5)
        self.assertEqual(kwargs["status"], "failed")
        diagnostic = kwargs["diagnostic"]
        self.assertIsInstance(diagnostic, str)
        self.assertIn("mismatch", diagnostic)
        # Failed revision is still recorded so a re-delivery dedupes, and
        # because the result POST was acknowledged it is marked reported.
        state = self._read_state()
        self.assertEqual(state["last_revision"], 5)
        self.assertEqual(state["status"], "failed")
        self.assertIs(state["reported"], True)

    def test_component_failure_reports_failed_without_crashing(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 6,
            "targets": {"plugin": {"ref": "v9.9.9"}},
        }
        # Installer raises — the handler must still POST failed, not propagate.
        with (
            patch.object(
                supervisor,
                "ensure_tinyhat_plugin_installed",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)  # must not raise

        restart_gateway.assert_not_called()
        restart_supervisor.assert_not_called()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 6)
        self.assertEqual(kwargs["status"], "failed")
        self.assertIsInstance(kwargs["diagnostic"], str)
        # Failed revision is recorded and, because the result POST was
        # acknowledged, marked reported.
        state = self._read_state()
        self.assertEqual(state["last_revision"], 6)
        self.assertEqual(state["status"], "failed")
        self.assertIs(state["reported"], True)

    def test_dev_mode_runtime_self_update_is_relaxed(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 7,
            "targets": {"runtime": {"ref": "v0.10.2"}},
        }
        with (
            patch.dict(os.environ, {"TINYHAT_DEV_RUNTIME": "1"}, clear=False),
            patch.object(supervisor.subprocess, "run") as runner,
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)

        # Dev mode: no git checkout, no supervisor restart.
        runner.assert_not_called()
        restart_supervisor.assert_not_called()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 7)
        # Relaxed -> not a hard failure.
        self.assertEqual(kwargs["status"], "applied")
        self.assertIn("dev runtime", kwargs["diagnostic"])

    def test_runtime_success_posts_before_restart(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 8,
            "targets": {"runtime": {"ref": "v0.10.2"}},
        }
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        order: list[str] = []
        self._posted.side_effect = lambda *a, **k: order.append("post")
        with (
            patch.object(supervisor.subprocess, "run", return_value=ok),
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value="0.10.2"
            ),
            patch.object(
                supervisor, "_read_runtime_git_sha", return_value="newsha"
            ),
            patch.object(
                supervisor,
                "_restart_supervisor",
                side_effect=lambda *a, **k: order.append("restart"),
            ),
        ):
            supervisor.handle_update_component_command(cmd)

        # The applied-result POST must land BEFORE the supervisor restart, so
        # the platform records success even if the restart kills this process.
        self.assertEqual(order, ["post", "restart"])

    def test_runtime_success_with_plugin_skips_redundant_gateway_restart(
        self,
    ) -> None:
        cmd = {
            "type": "update_component",
            "revision": 34,
            "targets": {
                "plugin": {"ref": "v2.0.0"},
                "runtime": {"ref": "v0.10.2"},
            },
        }
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed"),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={"resolved_commit_sha": "abc123", "version": "2.0.0"},
            ),
            patch.object(supervisor.subprocess, "run", return_value=ok),
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value="0.10.2"
            ),
            patch.object(
                supervisor, "_read_runtime_git_sha", return_value="newsha"
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)

        restart_gateway.assert_not_called()
        restart_supervisor.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 34)
        self.assertEqual(kwargs["status"], "applied")

    def test_runtime_checkout_failure_does_not_restart(self) -> None:
        cmd = {
            "type": "update_component",
            "revision": 9,
            "targets": {"runtime": {"ref": "v0.10.2"}},
        }
        # fetch ok, checkout fails -> stay on current ref, report failed, do
        # not restart (never leave the box without a working supervisor).
        results = iter(
            [
                SimpleNamespace(returncode=0, stdout="", stderr=""),  # fetch
                SimpleNamespace(returncode=1, stdout="", stderr="bad ref"),  # checkout
            ]
        )
        with (
            patch.object(
                supervisor.subprocess, "run", side_effect=lambda *a, **k: next(results)
            ),
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)

        restart_supervisor.assert_not_called()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")

    def test_legacy_state_without_reported_reposts_not_reattempts(self) -> None:
        # Backward-compat / upgrade migration: a state file written by the
        # previous runtime version has only {last_revision, status} and no
        # "reported" key. After upgrade, a redelivery of that revision must
        # NOT re-attempt the update (no install, no restart) — that is the
        # guarantee the old dedupe carried — but, because the result was
        # never recorded as acknowledged, it re-POSTs the cached result once
        # (so a result that was lost pre-upgrade is recovered).
        with open(self._state_path, "w", encoding="utf-8") as fh:
            json.dump({"last_revision": 12, "status": "failed"}, fh)
        cmd = {
            "type": "update_component",
            "revision": 12,
            "targets": {"plugin": {"ref": "v1.0.0"}},
        }
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
            patch.object(supervisor, "_restart_supervisor") as restart_supervisor,
        ):
            supervisor.handle_update_component_command(cmd)

        # The update itself is NOT re-run.
        installer.assert_not_called()
        restart_gateway.assert_not_called()
        restart_supervisor.assert_not_called()
        # The cached (legacy) result is re-POSTed exactly once.
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 12)
        self.assertEqual(kwargs["status"], "failed")
        # And the record is upgraded in place to reported=True.
        self.assertIs(self._read_state()["reported"], True)

    def test_transient_post_failure_then_redelivery_reposts(self) -> None:
        """P1 regression (tinyloophub/tinyloop#562): a swallowed result-POST
        failure must NOT mark the revision deduped. The redelivery re-POSTs
        the cached result without re-running the install or restarting, and
        flips reported=True.
        """
        cmd = {
            "type": "update_component",
            "revision": 100,
            "targets": {"plugin": {"ref": "v2.0.0"}},
        }
        # --- First delivery: install succeeds, result POST fails. ---
        self._posted.side_effect = RuntimeError("transient 503")
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer_1,
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={"resolved_commit_sha": "abc123", "version": "2.0.0"},
            ),
            patch.object(supervisor, "_restart_gateway_for_component_update"),
        ):
            # Must not raise even though the result POST failed.
            supervisor.handle_update_component_command(cmd)
        installer_1.assert_called_once()
        # State persisted the cached result, unreported, so a redelivery
        # re-POSTs it instead of dropping it.
        state = self._read_state()
        self.assertEqual(state["last_revision"], 100)
        self.assertEqual(state["status"], "applied")
        self.assertIs(state["reported"], False)
        self.assertEqual(state["applied_versions"], self._versions_payload)

        # --- Redelivery of the SAME revision, POST now succeeding. ---
        self._posted.reset_mock()
        self._posted.side_effect = None
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer_2,
            patch.object(supervisor, "_restart_gateway_for_component_update") as gw_2,
            patch.object(supervisor, "_restart_supervisor") as restart_2,
        ):
            supervisor.handle_update_component_command(cmd)
        # No re-install, no gateway restart, no supervisor restart.
        installer_2.assert_not_called()
        gw_2.assert_not_called()
        restart_2.assert_not_called()
        # The cached result was re-POSTed (revision + cached status).
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 100)
        self.assertEqual(kwargs["status"], "applied")
        # State now records the result as reported.
        self.assertIs(self._read_state()["reported"], True)

    def test_fully_reported_revision_is_deduped(self) -> None:
        # A revision whose state is already reported is fully deduped:
        # zero install, zero restart, zero post.
        supervisor._write_component_update_state(
            100, "applied", applied_versions={"plugin": "1.2.3"}, reported=True
        )
        cmd = {
            "type": "update_component",
            "revision": 100,
            "targets": {"plugin": {"ref": "v2.0.0"}},
        }
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(supervisor, "_restart_gateway_for_component_update") as gw,
            patch.object(supervisor, "_restart_supervisor") as restart,
        ):
            supervisor.handle_update_component_command(cmd)
        installer.assert_not_called()
        gw.assert_not_called()
        restart.assert_not_called()
        self._posted.assert_not_called()

    def test_repost_failure_leaves_reported_false_for_retry(self) -> None:
        # If the repost itself fails again, reported stays False so a later
        # redelivery will retry — still no re-install, no restart.
        supervisor._write_component_update_state(
            100, "applied", applied_versions={"plugin": "1.2.3"}, reported=False
        )
        cmd = {
            "type": "update_component",
            "revision": 100,
            "targets": {"plugin": {"ref": "v2.0.0"}},
        }
        self._posted.side_effect = RuntimeError("still down")
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(supervisor, "_restart_gateway_for_component_update") as gw,
            patch.object(supervisor, "_restart_supervisor") as restart,
        ):
            supervisor.handle_update_component_command(cmd)
        installer.assert_not_called()
        gw.assert_not_called()
        restart.assert_not_called()
        # The repost was attempted (and failed); reported stays False.
        self._posted.assert_called_once()
        state = self._read_state()
        self.assertEqual(state["last_revision"], 100)
        self.assertIs(state["reported"], False)

    def test_successful_update_persists_reported_true(self) -> None:
        # Happy path: a successful update whose POST is acknowledged ends with
        # reported=True so the next redelivery fully dedupes.
        cmd = {
            "type": "update_component",
            "revision": 101,
            "targets": {"plugin": {"ref": "v2.0.0"}},
        }
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed"),
            patch.object(
                supervisor,
                "_read_installed_plugin_marker",
                return_value={"resolved_commit_sha": "abc123", "version": "2.0.0"},
            ),
            patch.object(supervisor, "_restart_gateway_for_component_update"),
        ):
            supervisor.handle_update_component_command(cmd)
        state = self._read_state()
        self.assertEqual(state["last_revision"], 101)
        self.assertEqual(state["status"], "applied")
        self.assertIs(state["reported"], True)

    def test_runtime_self_update_reposts_after_restart_when_post_failed(self) -> None:
        """Runtime restart-safety: if the runtime self-update's POST fails, the
        persisted state is reported=False BEFORE the restart, so the
        post-restart process re-POSTs the cached result (no re-install, no
        second restart).
        """
        cmd = {
            "type": "update_component",
            "revision": 102,
            "targets": {"runtime": {"ref": "v0.10.3"}},
        }
        # --- First delivery: runtime applies, POST fails, then restart. ---
        self._posted.side_effect = RuntimeError("post failed")
        with (
            patch.object(
                supervisor, "_update_runtime_component", return_value=(True, None)
            ) as update_1,
            patch.object(supervisor, "_restart_supervisor") as restart_1,
        ):
            supervisor.handle_update_component_command(cmd)
        update_1.assert_called_once()
        # The restart STILL happens (the new runtime must take effect), and
        # the pre-restart state records the unreported outcome.
        restart_1.assert_called_once()
        state = self._read_state()
        self.assertEqual(state["last_revision"], 102)
        self.assertEqual(state["status"], "applied")
        self.assertIs(state["reported"], False)
        self.assertEqual(state["applied_versions"], self._versions_payload)

        # --- Post-restart redelivery: re-POSTs, no re-apply, no restart. ---
        self._posted.reset_mock()
        self._posted.side_effect = None
        with (
            patch.object(supervisor, "_update_runtime_component") as update_2,
            patch.object(supervisor, "_restart_supervisor") as restart_2,
        ):
            supervisor.handle_update_component_command(cmd)
        update_2.assert_not_called()
        restart_2.assert_not_called()
        self._posted.assert_called_once()
        self.assertEqual(self._posted.call_args.kwargs["status"], "applied")
        self.assertIs(self._read_state()["reported"], True)

    def test_malformed_command_is_ignored(self) -> None:
        # Defensive: missing revision or targets must not raise or POST.
        supervisor.handle_update_component_command({"type": "update_component"})
        supervisor.handle_update_component_command(
            {"type": "update_component", "revision": 1}
        )
        supervisor.handle_update_component_command(
            {"type": "update_component", "targets": {}}
        )
        self._posted.assert_not_called()


class ApplyPackagesCommandTests(unittest.TestCase):
    """``apply_packages`` installs Tinyhat plugin/default skills and rebinds."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._state_path = os.path.join(self._tmp, "package-apply-state.json")
        self._plugin_override_path = os.path.join(
            self._tmp,
            "plugin-source.json",
        )
        self._env = patch.dict(
            os.environ,
            {
                supervisor.TINYHAT_PACKAGE_APPLY_STATE_PATH_ENV: self._state_path,
                "TINYHAT_PLUGIN_SOURCE_OVERRIDE_PATH": self._plugin_override_path,
            },
            clear=False,
        )
        self._env.start()
        self._posted = patch.object(supervisor, "_post_package_apply_result").start()

    def tearDown(self) -> None:
        patch.stopall()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _command(self, revision: int = 5) -> dict:
        return {
            "type": "apply_packages",
            "revision": revision,
            "reason": "default_package_refs_changed",
            "preserve_user_installed": True,
            "platform_plugin": {
                "id": "tinyhat",
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "repo_name": "tinyhat",
                "requested_ref": "v0.4.5",
                "source": "git",
            },
            "default_skills": [
                {"name": "tinyhat-platform", "role": "router"},
                {"name": "tinyhat-software-updates", "role": "software_updates"},
            ],
        }

    def _read_state(self) -> dict:
        with open(self._state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_missing_default_skill_names_reads_extension_skill_layout(self) -> None:
        runtime_home = os.path.join(self._tmp, "runtime-home")
        skill_root = os.path.join(
            runtime_home,
            "extensions",
            supervisor.TINYHAT_PLUGIN_ID,
            "skills",
        )
        first_skill = os.path.join(skill_root, "tinyhat-platform", "SKILL.md")
        second_skill = os.path.join(
            skill_root,
            "tinyhat-software-updates",
            "SKILL.md",
        )
        os.makedirs(os.path.dirname(first_skill), exist_ok=True)
        with open(first_skill, "w", encoding="utf-8") as fh:
            fh.write("# Tinyhat Platform\n")

        env = {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": runtime_home,
        }
        default_skills = self._command()["default_skills"]
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                supervisor._missing_default_skill_names(default_skills),
                ["tinyhat-software-updates"],
            )

            os.makedirs(os.path.dirname(second_skill), exist_ok=True)
            with open(second_skill, "w", encoding="utf-8") as fh:
                fh.write("# Tinyhat Software Updates\n")
            self.assertEqual(
                supervisor._missing_default_skill_names(default_skills),
                [],
            )

    def test_dev_mode_defaults_state_paths_to_runtime_home(self) -> None:
        runtime_home = os.path.join(self._tmp, "runtime-home")
        env = {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": runtime_home,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                supervisor._package_apply_state_path(),
                os.path.join(runtime_home, "package-apply-state.json"),
            )
            self.assertEqual(
                supervisor._component_update_state_path(),
                os.path.join(runtime_home, "component-update-state.json"),
            )
            self.assertEqual(
                supervisor._tinyhat_plugin_source_override_path(),
                os.path.join(runtime_home, "tinyhat-plugin-source.json"),
            )

    def test_success_installs_packages_reports_and_restarts_gateway(self) -> None:
        cmd = self._command()
        marker = {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "repo_ref": "v0.4.5",
            "resolved_commit_sha": "abc123",
            "version": "0.4.5",
        }
        order: list[str] = []
        self._posted.side_effect = lambda *a, **k: order.append("post")
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(supervisor, "_read_installed_plugin_marker", return_value=marker),
            patch.object(supervisor, "_missing_default_skill_names", return_value=[]),
            patch.object(
                supervisor,
                "_restart_gateway_for_component_update",
                side_effect=lambda *_args, **_kwargs: order.append("restart"),
            ) as restart_gateway,
        ):
            supervisor.handle_apply_packages_command(cmd)

        installer.assert_called_once_with(
            repo_url="https://github.com/tinyhat-ai/tinyhat.git",
            repo_ref="v0.4.5",
        )
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 5)
        self.assertEqual(kwargs["status"], "applied")
        packages = kwargs["installed_packages"]
        self.assertEqual(
            packages["platform_plugin"]["resolved_commit_sha"],
            "abc123",
        )
        self.assertEqual(packages["platform_plugin"]["version"], "0.4.5")
        self.assertEqual(len(packages["default_skills"]), 2)
        restart_gateway.assert_called_once()
        self.assertEqual(order, ["restart", "post"])

        state = self._read_state()
        self.assertEqual(state["last_revision"], 5)
        self.assertEqual(state["status"], "applied")
        self.assertIs(state["reported"], True)
        with open(self._plugin_override_path, encoding="utf-8") as fh:
            override = json.load(fh)
        self.assertEqual(override["repo_ref"], "v0.4.5")
        self.assertEqual(override["resolved_commit_sha"], "abc123")

    def test_gateway_restart_failure_reports_package_apply_failed(self) -> None:
        cmd = self._command(revision=9)
        marker = {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "repo_ref": "v0.4.5",
            "resolved_commit_sha": "abc123",
            "version": "0.4.5",
        }
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed"),
            patch.object(supervisor, "_read_installed_plugin_marker", return_value=marker),
            patch.object(supervisor, "_missing_default_skill_names", return_value=[]),
            patch.object(
                supervisor,
                "_restart_gateway_for_component_update",
                side_effect=RuntimeError("telegram did not reconnect"),
            ) as restart_gateway,
        ):
            supervisor.handle_apply_packages_command(cmd)

        restart_gateway.assert_called_once()
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 9)
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("gateway restart after package apply failed", kwargs["diagnostic"])
        self.assertIn("telegram did not reconnect", kwargs["diagnostic"])
        self.assertEqual(
            kwargs["installed_packages"]["platform_plugin"]["resolved_commit_sha"],
            "abc123",
        )
        state = self._read_state()
        self.assertEqual(state["last_revision"], 9)
        self.assertEqual(state["status"], "failed")
        self.assertIs(state["reported"], True)

    def test_post_failure_restarts_once_then_redelivery_reposts_only(self) -> None:
        cmd = self._command(revision=6)
        marker = {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "repo_ref": "v0.4.5",
            "resolved_commit_sha": "abc123",
            "version": "0.4.5",
        }
        self._posted.side_effect = RuntimeError("transient 503")
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer_1,
            patch.object(supervisor, "_read_installed_plugin_marker", return_value=marker),
            patch.object(supervisor, "_missing_default_skill_names", return_value=[]),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_1,
        ):
            supervisor.handle_apply_packages_command(cmd)
        installer_1.assert_called_once()
        restart_1.assert_called_once()
        self.assertIs(self._read_state()["reported"], False)

        self._posted.reset_mock()
        self._posted.side_effect = None
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer_2,
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_2,
        ):
            supervisor.handle_apply_packages_command(cmd)
        installer_2.assert_not_called()
        restart_2.assert_not_called()
        self._posted.assert_called_once()
        self.assertEqual(self._posted.call_args.kwargs["status"], "applied")
        self.assertIs(self._read_state()["reported"], True)

    def test_package_install_failure_reports_failed_without_restart(self) -> None:
        cmd = self._command(revision=7)
        with (
            patch.object(
                supervisor,
                "ensure_tinyhat_plugin_installed",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
        ):
            supervisor.handle_apply_packages_command(cmd)

        restart_gateway.assert_not_called()
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 7)
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("boom", kwargs["diagnostic"])

    def test_missing_default_skill_reports_failed_without_override_or_restart(
        self,
    ) -> None:
        cmd = self._command(revision=8)
        marker = {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "repo_ref": "v0.4.5",
            "resolved_commit_sha": "abc123",
            "version": "0.4.5",
        }
        with (
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(supervisor, "_read_installed_plugin_marker", return_value=marker),
            patch.object(
                supervisor,
                "_missing_default_skill_names",
                return_value=["tinyhat-software-updates"],
            ),
            patch.object(
                supervisor, "_restart_gateway_for_component_update"
            ) as restart_gateway,
        ):
            supervisor.handle_apply_packages_command(cmd)

        installer.assert_called_once_with(
            repo_url="https://github.com/tinyhat-ai/tinyhat.git",
            repo_ref="v0.4.5",
        )
        restart_gateway.assert_not_called()
        self.assertFalse(os.path.exists(self._plugin_override_path))
        self._posted.assert_called_once()
        kwargs = self._posted.call_args.kwargs
        self.assertEqual(kwargs["revision"], 8)
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("default skills missing", kwargs["diagnostic"])
        self.assertEqual(
            kwargs["installed_packages"]["platform_plugin"]["resolved_commit_sha"],
            "abc123",
        )
        state = self._read_state()
        self.assertEqual(state["last_revision"], 8)
        self.assertEqual(state["status"], "failed")
        self.assertIs(state["reported"], True)

    def test_malformed_command_is_ignored(self) -> None:
        supervisor.handle_apply_packages_command({"type": "apply_packages"})
        self._posted.assert_not_called()


class ComponentUpdateStatePathStabilityTests(unittest.TestCase):
    """Regression for tinyloophub/tinyloop#562.

    The component-update dedupe state file must resolve to the SAME absolute
    path before and after a supervisor restart. The runtime self-update
    re-checks-out the repo IN PLACE (``runtime_dir()``) and restarts the
    process; if the state path were tied to the runtime checkout dir or to the
    process cwd, the post-restart process would compute a different path, miss
    the persisted ``reported=false`` record, and re-run the update plus a
    second restart instead of reposting the cached result.

    These tests drive the REAL ``_component_update_state_path()`` (no
    monkeypatched constant) and vary exactly the things that move across a
    restart: the cwd, and what ``runtime_dir()`` resolves to.
    """

    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ.pop("TINYHAT_COMPONENT_UPDATE_STATE_PATH", None)
        self._cwd_backup = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd_backup)
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_default_path_is_absolute(self) -> None:
        self.assertTrue(os.path.isabs(supervisor._component_update_state_path()))

    def test_default_path_stable_across_cwd_change(self) -> None:
        # A restart can relaunch the supervisor from a different cwd; the
        # default path must not move with it.
        with tempfile.TemporaryDirectory() as cwd_a, tempfile.TemporaryDirectory() as cwd_b:
            os.chdir(cwd_a)
            p1 = supervisor._component_update_state_path()
            os.chdir(cwd_b)
            p2 = supervisor._component_update_state_path()
        self.assertEqual(p1, p2)

    def test_default_path_stable_across_runtime_recheckout(self) -> None:
        # Simulate the in-place re-checkout + restart by changing what
        # ``runtime_dir()`` resolves to between the two calls. The default
        # state path lives OUTSIDE the checkout, so it must be identical.
        with tempfile.TemporaryDirectory() as old_checkout, tempfile.TemporaryDirectory() as new_checkout:
            with patch.object(supervisor, "runtime_dir", return_value=old_checkout):
                before = supervisor._component_update_state_path()
            with patch.object(supervisor, "runtime_dir", return_value=new_checkout):
                after = supervisor._component_update_state_path()
        self.assertEqual(before, after)
        self.assertEqual(
            after,
            os.path.abspath(supervisor._DEFAULT_COMPONENT_UPDATE_STATE_PATH),
        )

    def test_override_inside_checkout_dir_falls_back_to_stable_default(self) -> None:
        # The footgun the reviewer flagged: an override pointing INSIDE the
        # runtime checkout dir would be erased by the in-place re-checkout, so
        # the dedupe state would not survive the restart. The supervisor must
        # reject it and fall back to the restart-stable default rather than
        # silently honour an unstable path.
        with tempfile.TemporaryDirectory() as checkout:
            inside = os.path.join(checkout, "state", "component-update-state.json")
            with patch.object(supervisor, "runtime_dir", return_value=checkout):
                os.environ["TINYHAT_COMPONENT_UPDATE_STATE_PATH"] = inside
                resolved = supervisor._component_update_state_path()
        self.assertEqual(
            resolved,
            os.path.abspath(supervisor._DEFAULT_COMPONENT_UPDATE_STATE_PATH),
        )
        self.assertNotEqual(resolved, os.path.abspath(inside))

    def test_override_outside_checkout_dir_is_honoured_and_absolutized(self) -> None:
        # A legitimate operator override outside the checkout is kept (and
        # absolutized so it is itself cwd-independent).
        with tempfile.TemporaryDirectory() as checkout, tempfile.TemporaryDirectory() as state_home:
            override = os.path.join(state_home, "component-update-state.json")
            with patch.object(supervisor, "runtime_dir", return_value=checkout):
                os.environ["TINYHAT_COMPONENT_UPDATE_STATE_PATH"] = override
                resolved = supervisor._component_update_state_path()
        self.assertEqual(resolved, os.path.abspath(override))


class ComponentUpdateRepostUsesRealStatePathTests(unittest.TestCase):
    """Regression for tinyloophub/tinyloop#562.

    The existing repost-after-restart test pins the state path via the
    ``TINYHAT_COMPONENT_UPDATE_STATE_PATH`` override, which would MASK a path
    that moves across a restart. This test instead drives the REAL
    ``_component_update_state_path()`` against a default that lives under a
    tmp dir, and changes the process cwd between the pre-restart write and the
    post-restart read. The post-restart process must still find the persisted
    ``reported=false`` record and repost it: zero re-apply, zero second
    restart.
    """

    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        # Force prod (non-dev) so the runtime path requests a restart, and
        # clear any inherited override so the REAL default-derivation runs --
        # just rooted under a writable tmp dir via the constant patch below.
        os.environ.pop("TINYHAT_COMPONENT_UPDATE_STATE_PATH", None)
        os.environ.pop("TINYHAT_DEV_RUNTIME", None)
        self._cwd_backup = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        self._default_path = os.path.join(
            self._tmp, "state", "component-update-state.json"
        )
        self._const_patcher = patch.object(
            supervisor, "_DEFAULT_COMPONENT_UPDATE_STATE_PATH", self._default_path
        )
        self._const_patcher.start()
        self._versions = patch.object(
            supervisor,
            "collect_component_versions",
            return_value={"runtime": {"version": "9.9.9", "sha": "s"}},
        )
        self._versions.start()

    def tearDown(self) -> None:
        patch.stopall()
        os.chdir(self._cwd_backup)
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_repost_after_restart_finds_record_when_cwd_changes(self) -> None:
        command = {
            "type": "update_component",
            "revision": 9,
            "targets": {"runtime": {"ref": "v9.9.9"}},
        }

        # --- pre-restart boot: runtime applies, the result POST fails, then
        #     the supervisor restarts. The unreported record is written via the
        #     REAL state path. ---
        with tempfile.TemporaryDirectory() as cwd_before:
            os.chdir(cwd_before)
            with (
                patch.object(
                    supervisor, "_update_runtime_component", return_value=(True, None)
                ),
                patch.object(
                    supervisor,
                    "_post_component_update_result",
                    side_effect=RuntimeError("net down"),
                ),
                patch.object(supervisor, "_restart_supervisor") as restart_1,
            ):
                supervisor.handle_update_component_command(command)
            restart_1.assert_called_once()
            pre = supervisor._read_component_update_state()
            self.assertEqual(pre["last_revision"], 9)
            self.assertIs(pre["reported"], False)

        # --- post-restart boot: the process is relaunched from a DIFFERENT
        #     cwd. It must resolve the SAME state path, find the unreported
        #     record, and repost -- no re-apply, no second restart. ---
        with tempfile.TemporaryDirectory() as cwd_after:
            os.chdir(cwd_after)
            with (
                patch.object(supervisor, "_update_runtime_component") as reapply,
                patch.object(supervisor, "_post_component_update_result") as post_2,
                patch.object(supervisor, "_restart_supervisor") as restart_2,
            ):
                supervisor.handle_update_component_command(command)
            post_2.assert_called_once()
            reapply.assert_not_called()
            restart_2.assert_not_called()
            self.assertIs(
                supervisor._read_component_update_state()["reported"], True
            )


class RepostSendsCachedAppliedVersionsTests(unittest.TestCase):
    """Regression for tinyloophub/tinyloop#562 (faithful repost payload).

    ``UpdateComponentCommandTests`` mocks ``_post_component_update_result``
    wholesale, so it never exercises the real POST body and could not catch
    the bug where the body recomputed ``collect_component_versions()`` at POST
    time. These tests instead let the REAL ``_post_component_update_result``
    run and mock only ``post_json`` (the network boundary), so they assert on
    the exact ``applied_versions`` that hit the wire.

    The durability contract: on a redelivery the repost must report EXACTLY
    the cached ``applied_versions`` from the persisted state -- not a fresh
    live snapshot -- so the report stays faithful to what was applied even
    when the live versions have since drifted (across a runtime self-update
    restart, or for a FAILED component whose cache holds the pre-failure
    versions).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._state_path = os.path.join(self._tmp, "component-update-state.json")
        self._env = patch.dict(
            os.environ,
            {"TINYHAT_COMPONENT_UPDATE_STATE_PATH": self._state_path},
            clear=False,
        )
        self._env.start()
        os.environ.pop("TINYHAT_DEV_RUNTIME", None)
        # Record every POST body the REAL _post_component_update_result emits.
        self._posts: list[tuple[str, dict]] = []
        self._post_json = patch.object(
            supervisor,
            "post_json",
            side_effect=lambda path, body: self._posts.append((path, body)) or {},
        )
        self._post_json.start()

    def tearDown(self) -> None:
        patch.stopall()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_repost_sends_cached_applied_versions_not_live(self) -> None:
        # --- First delivery: the update applies and records v1.0.0, but the
        # result POST raises (swallowed), so the revision is left unreported
        # with the cached applied_versions persisted. ---
        cached_versions = {"runtime": {"version": "v1.0.0", "sha": "aaa"}}
        cmd = {
            "type": "update_component",
            "revision": 200,
            "targets": {"runtime": {"ref": "v1.0.0"}},
        }
        first_post = {"raised": False}

        def _raise_then_record(path, body):
            # Fail the very first POST (first delivery) and record the rest.
            if not first_post["raised"]:
                first_post["raised"] = True
                raise RuntimeError("transient 503")
            self._posts.append((path, body))
            return {}

        with (
            patch.object(
                supervisor,
                "collect_component_versions",
                return_value=cached_versions,
            ),
            patch.object(
                supervisor, "_update_runtime_component", return_value=(True, None)
            ),
            patch.object(supervisor, "post_json", side_effect=_raise_then_record),
            patch.object(supervisor, "_restart_supervisor"),
        ):
            supervisor.handle_update_component_command(cmd)
        # The cached versions were persisted, unreported.
        state = supervisor._read_component_update_state()
        self.assertIs(state["reported"], False)
        self.assertEqual(state["applied_versions"], cached_versions)

        # --- Redelivery: a live recompute would now return a DIFFERENT value
        # (v9.9.9). The repost must still send the CACHED v1.0.0, proving it
        # reads the persisted cache rather than recomputing live. ---
        live_versions = {"runtime": {"version": "v9.9.9", "sha": "zzz"}}
        with (
            patch.object(
                supervisor,
                "collect_component_versions",
                return_value=live_versions,
            ),
            patch.object(supervisor, "_update_runtime_component") as reapply,
            patch.object(supervisor, "_restart_supervisor") as restart,
        ):
            supervisor.handle_update_component_command(cmd)
        # No re-apply / no restart on a repost.
        reapply.assert_not_called()
        restart.assert_not_called()
        # Exactly one POST landed (the repost), and it carried the CACHED
        # versions, NOT the live recompute.
        self.assertEqual(len(self._posts), 1)
        _path, body = self._posts[0]
        self.assertEqual(body["revision"], 200)
        self.assertEqual(body["status"], "applied")
        self.assertEqual(body["applied_versions"], cached_versions)
        self.assertNotEqual(body["applied_versions"], live_versions)
        # The repost was acknowledged, so the record is now reported.
        self.assertIs(
            supervisor._read_component_update_state()["reported"], True
        )

    def test_repost_of_failed_component_sends_cached_pre_failure_versions(
        self,
    ) -> None:
        # A FAILED component's cache holds the versions recorded at failure
        # time (the pre-failure snapshot). A redelivery must repost EXACTLY
        # those, never a fresh live snapshot.
        pre_failure_versions = {"plugin": {"version": "1.2.3", "sha": "old"}}
        supervisor._write_component_update_state(
            300,
            "failed",
            diagnostic="plugin update to v9.9.9 failed: boom",
            applied_versions=pre_failure_versions,
            reported=False,
        )
        cmd = {
            "type": "update_component",
            "revision": 300,
            "targets": {"plugin": {"ref": "v9.9.9"}},
        }
        # Live recompute would now differ -- the repost must ignore it.
        live_versions = {"plugin": {"version": "9.9.9", "sha": "new"}}
        with (
            patch.object(
                supervisor,
                "collect_component_versions",
                return_value=live_versions,
            ),
            patch.object(supervisor, "ensure_tinyhat_plugin_installed") as installer,
            patch.object(supervisor, "_restart_gateway_for_component_update") as gw,
            patch.object(supervisor, "_restart_supervisor") as restart,
        ):
            supervisor.handle_update_component_command(cmd)
        # No re-install, no restart on a repost.
        installer.assert_not_called()
        gw.assert_not_called()
        restart.assert_not_called()
        # The repost carried the cached failed status + pre-failure versions.
        self.assertEqual(len(self._posts), 1)
        _path, body = self._posts[0]
        self.assertEqual(body["revision"], 300)
        self.assertEqual(body["status"], "failed")
        self.assertIn("boom", body["diagnostic"])
        self.assertEqual(body["applied_versions"], pre_failure_versions)
        self.assertNotEqual(body["applied_versions"], live_versions)

    def test_first_delivery_posts_exactly_what_it_persists(self) -> None:
        # First delivery: the versions snapshot is computed once, and the
        # POSTed applied_versions are identical to the persisted cache by
        # construction (so a later repost is faithful).
        snapshot = {"runtime": {"version": "v2.0.0", "sha": "bbb"}}
        cmd = {
            "type": "update_component",
            "revision": 400,
            "targets": {"runtime": {"ref": "v2.0.0"}},
        }
        with (
            patch.object(
                supervisor, "collect_component_versions", return_value=snapshot
            ),
            patch.object(
                supervisor, "_update_runtime_component", return_value=(True, None)
            ),
            patch.object(supervisor, "_restart_supervisor"),
        ):
            supervisor.handle_update_component_command(cmd)
        # The single POST body matches the persisted cache exactly.
        self.assertEqual(len(self._posts), 1)
        _path, body = self._posts[0]
        self.assertEqual(body["applied_versions"], snapshot)
        self.assertEqual(
            supervisor._read_component_update_state()["applied_versions"], snapshot
        )


if __name__ == "__main__":
    unittest.main()


class TinyhatPluginRuntimeOwnershipTests(unittest.TestCase):
    """The isolated gateway must be able to READ the installed plugin.

    The supervisor installs the plugin privileged; since the workload
    isolation split the gateway runs as the unprivileged runtime user.
    Without an ownership sync the checkout + OpenClaw's extension copy
    stay root-owned and OpenClaw silently loads zero tinyhat tools.
    """

    def _run_install(self, tmpdir: str, *, chowned: list[tuple[str, int, int]]):
        repo_url = "https://example.com/tinyhat.git"
        repo_ref = "refs/tags/v0.5.0"
        plugin_sha = "abc123def4567890"
        plugin_dir = os.path.join(tmpdir, "platform-plugins", "tinyhat")
        env = {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": tmpdir,
            "TINYHAT_PLUGIN_CHECKOUT_DIR": plugin_dir,
            "TINYHAT_PLATFORM_PLUGIN_REPO_URL": repo_url,
            "TINYHAT_PLATFORM_PLUGIN_REPO_REF": repo_ref,
        }

        def fake_run(cmd, **kwargs):
            if cmd == ["git", "clone", repo_url, plugin_dir]:
                os.makedirs(os.path.join(plugin_dir, ".git"))
                with open(
                    os.path.join(plugin_dir, "openclaw.plugin.json"),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    json.dump({"id": "tinyhat"}, fh)
                with open(
                    os.path.join(plugin_dir, "package.json"),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    json.dump({"version": "0.5.0"}, fh)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd == ["git", "-C", plugin_dir, "checkout", repo_ref]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd == ["git", "-C", plugin_dir, "rev-parse", "HEAD"]:
                return SimpleNamespace(
                    returncode=0, stdout=f"{plugin_sha}\n", stderr=""
                )
            if cmd == ["openclaw", "plugins", "install", plugin_dir, "--force"]:
                # OpenClaw copies the extension into the state dir; the
                # CLI runs privileged, so in production these files end
                # up root-owned.
                ext_dir = os.path.join(tmpdir, "extensions", "tinyhat")
                os.makedirs(ext_dir, exist_ok=True)
                with open(
                    os.path.join(ext_dir, "openclaw.plugin.json"),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    json.dump({"id": "tinyhat"}, fh)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        def fake_chown(path, uid, gid):
            chowned.append((path, uid, gid))

        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(supervisor.subprocess, "run", side_effect=fake_run),
            patch.object(
                supervisor, "_runtime_ownership_ids", return_value=(4242, 4243)
            ),
            patch.object(supervisor.os, "chown", side_effect=fake_chown),
            patch.object(supervisor.os, "lchown", side_effect=fake_chown),
        ):
            self.assertTrue(supervisor.ensure_tinyhat_plugin_installed())
        return plugin_dir

    def test_install_hands_plugin_trees_to_runtime_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chowned: list[tuple[str, int, int]] = []
            plugin_dir = self._run_install(tmpdir, chowned=chowned)

            chowned_paths = {path for path, _uid, _gid in chowned}
            # The checkout tree (via its parent dir) is gateway-readable…
            self.assertIn(os.path.dirname(plugin_dir), chowned_paths)
            self.assertIn(
                os.path.join(plugin_dir, "openclaw.plugin.json"), chowned_paths
            )
            # …and so is OpenClaw's installed extension copy.
            self.assertIn(
                os.path.join(tmpdir, "extensions", "tinyhat", "openclaw.plugin.json"),
                chowned_paths,
            )
            self.assertTrue(
                all((uid, gid) == (4242, 4243) for _p, uid, gid in chowned)
            )

    def test_already_installed_path_repairs_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # First pass installs and records the marker.
            chowned_install: list[tuple[str, int, int]] = []
            plugin_dir = self._run_install(tmpdir, chowned=chowned_install)

            # Second pass takes the already-installed early return; it
            # must still repair ownership (supervisor restart on a
            # machine provisioned before the sync existed).
            chowned_repair: list[tuple[str, int, int]] = []

            def fake_chown(path, uid, gid):
                chowned_repair.append((path, uid, gid))

            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_RUNTIME_HOME": tmpdir,
                "TINYHAT_PLUGIN_CHECKOUT_DIR": plugin_dir,
                "TINYHAT_PLATFORM_PLUGIN_REPO_URL": "https://example.com/tinyhat.git",
                "TINYHAT_PLATFORM_PLUGIN_REPO_REF": "refs/tags/v0.5.0",
            }

            def fake_run(cmd, **kwargs):
                if cmd[:3] == ["git", "-C", plugin_dir] and "set-url" in cmd:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:3] == ["git", "-C", plugin_dir] and "fetch" in cmd:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "checkout", "refs/tags/v0.5.0"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd == ["git", "-C", plugin_dir, "rev-parse", "HEAD"]:
                    return SimpleNamespace(
                        returncode=0, stdout="abc123def4567890\n", stderr=""
                    )
                raise AssertionError(f"unexpected command: {cmd}")

            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(supervisor.subprocess, "run", side_effect=fake_run),
                patch.object(
                    supervisor, "_runtime_ownership_ids", return_value=(4242, 4243)
                ),
                patch.object(supervisor.os, "chown", side_effect=fake_chown),
                patch.object(supervisor.os, "lchown", side_effect=fake_chown),
                patch.object(
                    supervisor, "_is_tinyhat_plugin_registered", return_value=True
                ),
            ):
                self.assertTrue(supervisor.ensure_tinyhat_plugin_installed())

            repaired = {path for path, _uid, _gid in chowned_repair}
            self.assertIn(
                os.path.join(tmpdir, "extensions", "tinyhat", "openclaw.plugin.json"),
                repaired,
            )

    def test_no_runtime_user_keeps_current_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chowned: list[tuple[str, int, int]] = []

            def fake_chown(path, uid, gid):  # pragma: no cover - must not run
                chowned.append((path, uid, gid))

            with (
                patch.object(
                    supervisor, "_runtime_ownership_ids", return_value=None
                ),
                patch.object(supervisor.os, "chown", side_effect=fake_chown),
                patch.object(supervisor.os, "lchown", side_effect=fake_chown),
            ):
                supervisor._chown_runtime_owned_tree(tmpdir)
            self.assertEqual(chowned, [])

    def test_tree_chown_never_follows_symlinks(self) -> None:
        """A symlink inside the plugin tree must not chown its target.

        The tree content ultimately comes from a platform-pinned public
        repo; a hostile or mispinned checkout could plant symlinks at
        arbitrary host paths. The sync must lchown the link entry itself,
        never the target, and must not descend into symlinked dirs.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            outside_file = os.path.join(tmpdir, "outside-secret")
            outside_dir = os.path.join(tmpdir, "outside-dir")
            os.makedirs(outside_dir)
            with open(outside_file, "w", encoding="utf-8") as fh:
                fh.write("root-owned host file")
            with open(
                os.path.join(outside_dir, "inner"), "w", encoding="utf-8"
            ) as fh:
                fh.write("inside an outside dir")

            tree = os.path.join(tmpdir, "platform-plugins")
            os.makedirs(os.path.join(tree, "tinyhat"))
            file_link = os.path.join(tree, "tinyhat", "evil-file-link")
            dir_link = os.path.join(tree, "tinyhat", "evil-dir-link")
            os.symlink(outside_file, file_link)
            os.symlink(outside_dir, dir_link)

            lchowned: list[str] = []
            followed: list[str] = []

            def fake_lchown(path, uid, gid):
                lchowned.append(path)

            def fake_chown(path, uid, gid):  # pragma: no cover - must not run
                followed.append(path)

            with (
                patch.object(
                    supervisor, "_runtime_ownership_ids", return_value=(4242, 4243)
                ),
                patch.object(supervisor.os, "lchown", side_effect=fake_lchown),
                patch.object(supervisor.os, "chown", side_effect=fake_chown),
            ):
                supervisor._chown_runtime_owned_tree(tree)

            # The link entries themselves are handed over (lchown)…
            self.assertIn(file_link, lchowned)
            self.assertIn(dir_link, lchowned)
            # …but no follow-style chown happened at all, the symlink
            # targets were never touched, and the walk did not descend
            # into the symlinked directory.
            self.assertEqual(followed, [])
            self.assertNotIn(outside_file, lchowned)
            self.assertNotIn(outside_dir, lchowned)
            self.assertNotIn(os.path.join(outside_dir, "inner"), lchowned)
            self.assertNotIn(os.path.join(dir_link, "inner"), lchowned)


class PluginLoadDetectionTests(unittest.TestCase):
    """#77: an enabled plugin that never loaded must not report healthy.

    The detection consumes the load beacon the plugin writes since
    v0.5.0; older plugins and non-running gateways classify as
    "unknown" and never degrade health.
    """

    def _env(self, tmpdir: str) -> dict[str, str]:
        return {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": tmpdir,
            supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: os.path.join(
                tmpdir, "runtime-state.json"
            ),
            supervisor.TINYHAT_COMPUTER_ID_ENV: "cmp_test_77",
            supervisor.TINYHAT_GCE_INSTANCE_ID_ENV: "instance-77",
            supervisor.TINYHAT_GCE_METADATA_AVAILABLE_ENV: "",
        }

    def _write_marker(self, tmpdir: str, version: str) -> None:
        with open(
            os.path.join(tmpdir, "tinyhat-plugin.version"), "w", encoding="utf-8"
        ) as fh:
            json.dump(
                {
                    "repo_url": "https://example.com/tinyhat.git",
                    "repo_ref": f"refs/tags/v{version}",
                    "resolved_commit_sha": "a" * 40,
                    "version": version,
                },
                fh,
            )

    def _write_beacon(self, tmpdir: str, version: str) -> None:
        with open(
            os.path.join(tmpdir, supervisor.TINYHAT_PLUGIN_BEACON_FILENAME),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "plugin": "tinyhat",
                    "version": version,
                    "loaded_at": "2026-06-11T12:00:00Z",
                    "pid": 4321,
                    "node": "v22.0.0",
                },
                fh,
            )

    def _patches(self, tmpdir: str):
        return (
            patch.dict(os.environ, self._env(tmpdir), clear=False),
            patch.object(supervisor, "get_backend_base_url", return_value=""),
            patch.object(
                supervisor, "_read_runtime_repo_version", return_value="0.11.0"
            ),
            patch.object(
                supervisor, "_read_runtime_git_sha", return_value="b" * 40
            ),
        )

    def test_fresh_beacon_keeps_healthy_and_reports_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.5.0")
            self._write_beacon(tmpdir, "0.5.0")
            p1, p2, p3, p4 = self._patches(tmpdir)
            with p1, p2, p3, p4:
                supervisor._write_runtime_state(
                    "healthy", "ok", gateway_active=True
                )
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertEqual(payload["plugin"]["load_check"], "loaded")
            self.assertEqual(
                payload["plugin"]["beacon_loaded_at"], "2026-06-11T12:00:00Z"
            )
            self.assertIsNone(payload["last_error"])

    def test_missing_beacon_demotes_healthy_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.5.0")
            p1, p2, p3, p4 = self._patches(tmpdir)
            base = 1_750_000_000
            with p1, p2, p3, p4:
                with patch.object(supervisor.time, "time", return_value=base):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                    pending = supervisor.read_runtime_state()
                # Same boot, grace elapsed.
                with patch.object(
                    supervisor.time,
                    "time",
                    return_value=base + supervisor.PLUGIN_LOAD_GRACE_SECONDS + 1,
                ):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                    demoted = supervisor.read_runtime_state()
            self.assertEqual(pending["runtime_health"], "healthy")
            self.assertEqual(pending["plugin"]["load_check"], "pending")
            self.assertEqual(pending["plugin"]["missing_since_unix"], base)
            self.assertEqual(
                demoted["runtime_health"], "unsupported_openclaw_version"
            )
            self.assertEqual(demoted["plugin"]["load_check"], "not_loaded")
            self.assertEqual(demoted["plugin"]["reason"], "beacon_missing")
            self.assertEqual(demoted["plugin"]["missing_since_unix"], base)
            self.assertEqual(
                demoted["last_error"]["category"], "plugin_not_loaded"
            )
            self.assertIn("not loaded", demoted["detail"])

    def test_beacon_version_mismatch_counts_as_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.6.0")
            self._write_beacon(tmpdir, "0.5.0")  # stale: pre-update load
            p1, p2, p3, p4 = self._patches(tmpdir)
            base = 1_750_000_000
            with p1, p2, p3, p4:
                with patch.object(supervisor.time, "time", return_value=base):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                with patch.object(
                    supervisor.time,
                    "time",
                    return_value=base + supervisor.PLUGIN_LOAD_GRACE_SECONDS + 1,
                ):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                    payload = supervisor.read_runtime_state()
            self.assertEqual(
                payload["runtime_health"], "unsupported_openclaw_version"
            )
            self.assertEqual(
                payload["plugin"]["reason"], "beacon_version_mismatch"
            )

    def test_pre_beacon_plugin_reports_unknown_and_stays_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.4.5")
            p1, p2, p3, p4 = self._patches(tmpdir)
            with p1, p2, p3, p4:
                supervisor._write_runtime_state(
                    "healthy", "ok", gateway_active=True
                )
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertEqual(payload["plugin"]["load_check"], "unknown")
            self.assertEqual(
                payload["plugin"]["reason"], "plugin_predates_load_beacon"
            )

    def test_inactive_gateway_reports_unknown_without_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.5.0")
            p1, p2, p3, p4 = self._patches(tmpdir)
            with p1, p2, p3, p4:
                supervisor._write_runtime_state(
                    "healthy", "ok", gateway_active=False
                )
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertEqual(payload["plugin"]["load_check"], "unknown")
            self.assertNotIn("missing_since_unix", payload["plugin"])

    def test_non_healthy_states_are_never_demoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.5.0")
            p1, p2, p3, p4 = self._patches(tmpdir)
            base = 1_750_000_000
            with p1, p2, p3, p4:
                with patch.object(supervisor.time, "time", return_value=base):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                with patch.object(
                    supervisor.time,
                    "time",
                    return_value=base + supervisor.PLUGIN_LOAD_GRACE_SECONDS + 1,
                ):
                    supervisor._write_runtime_state(
                        "openclaw_not_ready", "starting", gateway_active=True
                    )
                    payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "openclaw_not_ready")
            self.assertEqual(payload["plugin"]["load_check"], "not_loaded")
            self.assertIsNotNone(payload["last_error"])
            self.assertNotEqual(
                payload["last_error"]["category"], "plugin_not_loaded"
            )

    def test_no_marker_attaches_no_plugin_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            p1, p2, p3, p4 = self._patches(tmpdir)
            with p1, p2, p3, p4:
                supervisor._write_runtime_state(
                    "healthy", "ok", gateway_active=True
                )
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertNotIn("plugin", payload)

    def test_malformed_beacon_counts_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.5.0")
            with open(
                os.path.join(
                    tmpdir, supervisor.TINYHAT_PLUGIN_BEACON_FILENAME
                ),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("{not json")
            check = None
            p1, p2, p3, p4 = self._patches(tmpdir)
            with p1, p2, p3, p4:
                check = supervisor._plugin_load_check(
                    {}, gateway_active=True, now=1_750_000_000
                )
            self.assertEqual(check["load_check"], "pending")
            self.assertEqual(check["reason"], "beacon_missing")

    def test_parse_plugin_version_edges(self) -> None:
        self.assertEqual(supervisor._parse_plugin_version("0.5.0"), (0, 5, 0))
        self.assertEqual(
            supervisor._parse_plugin_version("1.2.3-rc1"), (1, 2, 31)
        )
        self.assertIsNone(supervisor._parse_plugin_version(""))
        self.assertIsNone(supervisor._parse_plugin_version("main"))

    def test_matching_beacon_wins_even_for_pre_beacon_version_metadata(
        self,
    ) -> None:
        """A build of a beacon-capable plugin can still carry older version
        metadata (e.g. main before the release cut) — positive evidence
        must classify as loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.4.5")
            self._write_beacon(tmpdir, "0.4.5")
            p1, p2, p3, p4 = self._patches(tmpdir)
            with p1, p2, p3, p4:
                supervisor._write_runtime_state(
                    "healthy", "ok", gateway_active=True
                )
                payload = supervisor.read_runtime_state()
            self.assertEqual(payload["runtime_health"], "healthy")
            self.assertEqual(payload["plugin"]["load_check"], "loaded")

    def test_plugin_update_resets_the_missing_beacon_clock(self) -> None:
        """Review repro: a not_loaded verdict for one version must not
        leak its clock into the next installed version — each install
        gets its own grace window."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_marker(tmpdir, "0.5.0")
            p1, p2, p3, p4 = self._patches(tmpdir)
            base = 1_750_000_000
            with p1, p2, p3, p4:
                with patch.object(supervisor.time, "time", return_value=base):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                with patch.object(
                    supervisor.time,
                    "time",
                    return_value=base + supervisor.PLUGIN_LOAD_GRACE_SECONDS + 1,
                ):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                    demoted = supervisor.read_runtime_state()
                # Plugin updated to 0.6.0; its beacon has not landed yet.
                self._write_marker(tmpdir, "0.6.0")
                fresh_now = base + supervisor.PLUGIN_LOAD_GRACE_SECONDS + 2
                with patch.object(
                    supervisor.time, "time", return_value=fresh_now
                ):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                    fresh = supervisor.read_runtime_state()
                # And only after ITS OWN grace window does 0.6.0 demote.
                with patch.object(
                    supervisor.time,
                    "time",
                    return_value=fresh_now
                    + supervisor.PLUGIN_LOAD_GRACE_SECONDS
                    + 1,
                ):
                    supervisor._write_runtime_state(
                        "healthy", "ok", gateway_active=True
                    )
                    later = supervisor.read_runtime_state()
            self.assertEqual(
                demoted["runtime_health"], "unsupported_openclaw_version"
            )
            self.assertEqual(fresh["runtime_health"], "healthy")
            self.assertEqual(fresh["plugin"]["load_check"], "pending")
            self.assertEqual(fresh["plugin"]["installed_version"], "0.6.0")
            self.assertEqual(fresh["plugin"]["missing_since_unix"], fresh_now)
            self.assertEqual(
                later["runtime_health"], "unsupported_openclaw_version"
            )
            self.assertEqual(later["plugin"]["load_check"], "not_loaded")


class CleanShutdownGatewayTeardownTests(unittest.TestCase):
    """#685 (PR #81 review): with PartOf= gone the supervisor must stop an
    active gateway on a CLEAN exit — even when stopped before Phase D —
    but must NOT stop it on a crash/exception (continuity)."""

    def setUp(self) -> None:
        self._old_stop_holder = dict(supervisor._stop_holder)
        supervisor._stop_holder.update({"stop": False, "rebind": False})

    def tearDown(self) -> None:
        supervisor._stop_holder.clear()
        supervisor._stop_holder.update(self._old_stop_holder)

    def test_clean_stop_before_phase_d_stops_active_gateway(self) -> None:
        # Codex repro: a SIGTERM lands while the respawned supervisor is
        # still in Phase A/B, so _run_one_binding_cycle returns 0 before
        # its Phase C/D finally — with a gateway from the prior
        # crash-continuity instance still running.
        def fake_cycle() -> int:
            supervisor._stop_holder["stop"] = True
            return 0

        with (
            patch.object(supervisor, "_run_one_binding_cycle", side_effect=fake_cycle),
            patch.object(supervisor, "notify_supervisor_ready", return_value=True),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(supervisor, "stop_openclaw_gateway") as stop_gw,
        ):
            self.assertEqual(supervisor.main(), 0)
        stop_gw.assert_called_once()

    def test_clean_stop_with_no_active_gateway_does_not_call_stop(self) -> None:
        def fake_cycle() -> int:
            supervisor._stop_holder["stop"] = True
            return 0

        with (
            patch.object(supervisor, "_run_one_binding_cycle", side_effect=fake_cycle),
            patch.object(supervisor, "notify_supervisor_ready", return_value=True),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=False),
            patch.object(supervisor, "stop_openclaw_gateway") as stop_gw,
        ):
            self.assertEqual(supervisor.main(), 0)
        stop_gw.assert_not_called()

    def test_error_exit_does_not_trigger_clean_shutdown_teardown(self) -> None:
        # A non-zero cycle exit (broken/crash path) returns early; the
        # top-level clean-shutdown guard must NOT fire — error paths own
        # their own gateway handling and a crash must leave the gateway
        # for reattach.
        with (
            patch.object(supervisor, "_run_one_binding_cycle", return_value=1),
            patch.object(supervisor, "notify_supervisor_ready", return_value=True),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(supervisor, "stop_openclaw_gateway") as stop_gw,
        ):
            self.assertEqual(supervisor.main(), 1)
        stop_gw.assert_not_called()

    def test_guard_is_noop_when_stop_flag_not_set(self) -> None:
        # Defensive: the guard keys off the clean-stop flag, never the
        # gateway state alone, so it cannot bounce a gateway mid-run.
        supervisor._stop_holder["stop"] = False
        with (
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(supervisor, "stop_openclaw_gateway") as stop_gw,
        ):
            supervisor._stop_gateway_on_clean_shutdown()
        stop_gw.assert_not_called()

    def test_phase_a_ready_retry_returns_promptly_on_clean_stop(self) -> None:
        # Codex re-review repro: SIGTERM lands while Phase A is retrying an
        # unreachable platform. The retry loop must observe the stop flag
        # and return 0 BEFORE another long sleep/retry, so main() reaches
        # the clean-shutdown guard within TimeoutStopSec.
        import urllib.error

        post_calls = {"n": 0}

        def failing_ready_post(path, payload):
            post_calls["n"] += 1
            # The signal handler would set this mid-POST; simulate that.
            supervisor._stop_holder["stop"] = True
            raise urllib.error.HTTPError(path, 503, "platform down", {}, None)

        sleeps: list[float] = []

        with (
            patch.object(supervisor, "post_json", side_effect=failing_ready_post),
            patch.object(supervisor, "checkpoint_supervisor_progress", return_value=True),
            patch.object(supervisor.time, "sleep", side_effect=lambda s: sleeps.append(s)),
        ):
            result = supervisor._run_one_binding_cycle()

        self.assertEqual(result, 0)
        # Exactly one POST attempt — it must not keep retrying after stop.
        self.assertEqual(post_calls["n"], 1)
        # The interruptible sleep bailed immediately once stop was set, so
        # no full multi-second wait happened.
        self.assertLessEqual(sum(sleeps), 0.5)

    def test_phase_a_clean_stop_lets_main_stop_the_gateway(self) -> None:
        # End to end: a clean stop during a failing Phase A ready-post ->
        # cycle returns 0 -> main()'s guard stops the active gateway.
        import urllib.error

        def failing_ready_post(path, payload):
            supervisor._stop_holder["stop"] = True
            raise urllib.error.HTTPError(path, 503, "platform down", {}, None)

        with (
            patch.object(supervisor, "post_json", side_effect=failing_ready_post),
            patch.object(supervisor, "checkpoint_supervisor_progress", return_value=True),
            patch.object(supervisor, "notify_supervisor_ready", return_value=True),
            patch.object(supervisor.time, "sleep", return_value=None),
            patch.object(supervisor, "is_openclaw_gateway_active", return_value=True),
            patch.object(supervisor, "stop_openclaw_gateway") as stop_gw,
        ):
            self.assertEqual(supervisor.main(), 0)
        stop_gw.assert_called_once()

    def test_interruptible_sleep_bails_when_stop_set(self) -> None:
        slept: list[float] = []
        supervisor._stop_holder["stop"] = True
        with patch.object(supervisor.time, "sleep", side_effect=lambda s: slept.append(s)):
            supervisor._interruptible_sleep(30)
        self.assertEqual(slept, [])  # never sleeps once stop is already set
