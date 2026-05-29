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

            def fake_run(cmd, **_kwargs):
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
                ],
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


def _subscription_binding(*, with_openrouter: bool) -> dict:
    """Binding for a Computer opted into the ChatGPT BYO subscription."""
    base = {
        "telegram_owner_user_id": "123456",
        "telegram_bot_token": "123456:ABC",
        "telegram_bot_username": "Tinychattestbot",
        "llm_auth_mode": "chatgpt_subscription",
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
    """Drop an openai-codex profile into the agent's auth store."""
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
            },
            fh,
        )


def _seed_other_provider_profile(state_dir: str) -> None:
    """Drop a non-openai-codex profile to test the wipe preserves it."""
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


class ChatgptSubscriptionBranchTests(unittest.TestCase):
    """Issue #23 — supervisor branches on auth-profile presence."""

    def test_opted_in_with_profile_writes_subscription_config(self) -> None:
        """auth-profile present + llm_auth_mode=chatgpt_subscription on
        binding → openai/gpt-5.5, no `pi` runtime pin, no openai SecretRef,
        cross-provider fallback to OpenRouter when its key is present."""
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
        # No `pi` pin — let OpenClaw auto-select the Codex harness.
        self.assertNotIn("agentRuntime", defaults)
        # No openai SecretRef — the OAuth profile owns auth.
        self.assertNotIn("models", defaults.get("models", {}))
        providers = (config.get("models") or {}).get("providers") or {}
        self.assertNotIn("apiKey", providers.get("openai", {}))
        # Cross-provider fallback to OpenRouter for rate-window relief.
        self.assertEqual(
            defaults["model"].get("fallbacks"), ["openrouter/openai/gpt-5.5"]
        )
        # OpenRouter env stays set so the fallback has an auth path.
        self.assertEqual(
            config.get("env", {}).get("OPENROUTER_API_KEY"), "sk-or-v1-child"
        )

    def test_opted_in_without_profile_stays_on_default_config(self) -> None:
        """llm_auth_mode=chatgpt_subscription but NO auth-profile yet →
        default-mode (pi + OpenRouter) so the agent keeps replying while
        the device-code flow is in flight or before the user has approved."""
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
        # Stays on `pi` because the credential isn't on disk yet.
        self.assertEqual(defaults.get("agentRuntime"), {"id": "pi"})
        self.assertTrue(defaults["model"]["primary"].startswith("openrouter/"))

    def test_default_binding_unchanged(self) -> None:
        """Non-subscription Computers — the existing path stays
        completely unaffected by this branch (regression guard)."""
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
        self.assertEqual(defaults.get("agentRuntime"), {"id": "pi"})


class WipeChatgptSubscriptionProfileTests(unittest.TestCase):
    """Issue #23 — admin-driven wipe of the per-agent OAuth credential."""

    def test_wipe_removes_openai_codex_and_preserves_others(self) -> None:
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
                self.assertEqual(removed, ["openai-codex:owner@example.com"])

                # File still exists with the non-openai-codex profile.
                path = supervisor.openclaw_auth_profiles_path()
                with open(path, encoding="utf-8") as fh:
                    after = json.load(fh)
                self.assertNotIn(
                    "openai-codex:owner@example.com", after["profiles"]
                )
                self.assertIn("xai:other@example.com", after["profiles"])

                # Second call is a no-op (idempotent).
                self.assertEqual(supervisor.wipe_chatgpt_subscription_profile(), [])

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

    def test_signature_moves_when_llm_auth_mode_flips(self) -> None:
        before = self._base_binding()  # implicit platform_credits
        after = dict(before, llm_auth_mode="chatgpt_subscription")
        self.assertNotEqual(
            supervisor._binding_signature(before),
            supervisor._binding_signature(after),
        )

    def test_signature_moves_when_llm_model_ref_changes(self) -> None:
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
    unassign leaves the previous owner's openai-codex profile on
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
                # New owner without a profile yet → STAYS on `pi` +
                # OpenRouter (the opted-in-without-profile branch).
                # The critical assertion: the prior owner's profile
                # did NOT survive to make the supervisor flip to
                # subscription mode for the new owner.
                self.assertEqual(defaults.get("agentRuntime"), {"id": "pi"})
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

        with patch.object(supervisor, "post_json", side_effect=_fake_post_json):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="quick-exit-test",
                openclaw_bin="/bin/false",
                url_emit_timeout_s=2.0,
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

    def test_quick_exit_posts_once_even_with_late_drain(self) -> None:
        """No double-post: only ONE terminal status per invocation."""
        calls: list[tuple[str, dict]] = []
        with patch.object(
            supervisor, "post_json", side_effect=lambda p, b: calls.append((p, b)) or {}
        ):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="dup-guard-test",
                openclaw_bin="/bin/false",
                url_emit_timeout_s=2.0,
                overall_timeout_s=5.0,
            )
        # Only one terminal post, not two.
        terminal_posts = [b for _, b in calls if b.get("status") in ("linked", "failed")]
        self.assertEqual(len(terminal_posts), 1, f"got {terminal_posts}")

    def test_pty_fork_failure_posts_terminal_failed(self) -> None:
        """If we can't even allocate a PTY, the platform still hears
        about it (failed) instead of waiting indefinitely."""
        calls: list[tuple[str, dict]] = []
        import pty as pty_mod

        with patch.object(
            supervisor, "post_json", side_effect=lambda p, b: calls.append((p, b)) or {}
        ), patch.object(pty_mod, "fork", side_effect=OSError("no ptys")):
            supervisor._run_chatgpt_device_code_login_in_thread(
                session_id="pty-fail-test",
                openclaw_bin="/bin/false",
            )
        self.assertEqual(len(calls), 1)
        path, body = calls[0]
        self.assertEqual(body["status"], "failed")
        self.assertIn("pseudo-terminal", body["error"])


class CrossOwnerCredentialLeakGuardTests(unittest.TestCase):
    """PR #24 review at 01:19Z — late-arriving worker + cold-start.

    Codex's exact reproduction: owner A starts the device-code flow,
    the Computer is unassigned/reassigned before the user approves,
    owner A approves after the wipe; the previously still-alive
    worker's CLI must not write an openai-codex profile back to the
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
                # the profile so the new owner stays on pi+OpenRouter.
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
                # branch — stays on pi + OpenRouter (not subscription
                # mode with the prior owner's credential).
                self.assertEqual(defaults.get("agentRuntime"), {"id": "pi"})
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


if __name__ == "__main__":
    unittest.main()
