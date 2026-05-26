"""Regression tests for the Tinyhat Computer runtime supervisor.

Usage:
    python -m unittest tests.test_supervisor -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import supervisor


def _write_config_in_temp_runtime(binding: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {
            "TINYHAT_DEV_RUNTIME": "1",
            "TINYHAT_RUNTIME_HOME": tmpdir,
            "TINYHAT_SECRETS_PATH": os.path.join(
                tmpdir,
                "tinyhat-secrets.json",
            ),
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
                "openrouter/deepseek/deepseek-v4-flash": {"alias": "cheap"},
                "openrouter/deepseek/deepseek-v4-pro": {"alias": "default"},
                "openrouter/moonshotai/kimi-k2.6": {"alias": "power"},
            },
        )
        self.assertEqual(config["env"], {"OPENROUTER_API_KEY": "sk-or-v1-child"})

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
                "openrouter/deepseek/deepseek-v4-flash:free": {
                    "alias": "free-demo"
                },
            },
        )

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


if __name__ == "__main__":
    unittest.main()
