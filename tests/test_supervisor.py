"""Regression tests for the Tinyhat Computer runtime supervisor.

Usage:
    python -m unittest tests.test_supervisor -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import supervisor


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
            [5, 10, 20, 30, 30],
        )


if __name__ == "__main__":
    unittest.main()
