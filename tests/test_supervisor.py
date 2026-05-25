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
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
