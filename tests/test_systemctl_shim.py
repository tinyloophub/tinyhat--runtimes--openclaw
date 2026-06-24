"""Tests for the dev-only `systemctl` shim embedded in ``dev/entrypoint.sh``.

The shim emulates the gateway unit's stop/start so a no-systemd dev container
can complete the secret-apply rebind. The load-bearing contract (PR review,
runtimes/openclaw#170): ``stop`` must block until the matched gateway process
is actually gone and report failure if it cannot be reaped — otherwise the
rebind's start + health-check could pass against the still-alive old gateway
and falsely mark the new env applied.

Usage (from the repo root):
    python -m unittest tests.test_systemctl_shim -v
"""

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "dev" / "entrypoint.sh"
# Unique substring carried in the fake gateway's argv so the shim matches only it.
MARKER = "TINYHAT_SHIM_TEST_FAKE_GW"


def _extract_shim() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    match = re.search(r"<<'SYSTEMCTL_SHIM'\n(.*?)\nSYSTEMCTL_SHIM\n", text, re.DOTALL)
    if not match:
        raise AssertionError("SYSTEMCTL_SHIM heredoc not found in dev/entrypoint.sh")
    return match.group(1)


@unittest.skipUnless(
    Path("/proc").is_dir(), "shim scans /proc; runs on Linux (CI / dev container)"
)
class SystemctlShimStopTest(unittest.TestCase):
    def setUp(self) -> None:
        fh = tempfile.NamedTemporaryFile(
            "w", suffix="-systemctl", delete=False, encoding="utf-8"
        )
        fh.write(_extract_shim())
        fh.close()
        os.chmod(fh.name, 0o755)
        self._shim_path = fh.name
        self._procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        os.unlink(self._shim_path)

    def _spawn_fake(self, body: str) -> subprocess.Popen:
        # argv0 must be "python3" (the shim requires argv0 python*), with MARKER
        # in argv so /proc/<pid>/cmdline matches the overridden gateway pattern.
        proc = subprocess.Popen(
            ["python3", "-c", body, MARKER], executable=sys.executable
        )
        self._procs.append(proc)
        time.sleep(0.3)  # let it install its signal handler
        return proc

    def _run_stop(self, **env_extra: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "TINYHAT_DEV_GATEWAY_MATCH_SUBSTR": MARKER,
            **env_extra,
        }
        return subprocess.run(
            [self._shim_path, "stop", "tinyhat-runtime-gateway.service"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_stop_blocks_until_delayed_exit(self) -> None:
        # Exits ~1s after SIGTERM; stop must wait for it, not return immediately.
        body = (
            "import signal,sys,time; "
            "signal.signal(signal.SIGTERM, lambda *a: (time.sleep(1), sys.exit(0))); "
            "time.sleep(60)"
        )
        proc = self._spawn_fake(body)
        started = time.monotonic()
        result = self._run_stop(TINYHAT_DEV_GATEWAY_STOP_TERM_SECONDS="5")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(
            proc.poll(), "gateway must be gone once stop returns"
        )
        self.assertGreaterEqual(
            elapsed, 0.8, "stop returned before the gateway had exited"
        )

    def test_stop_escalates_to_sigkill_on_ignored_term(self) -> None:
        # Ignores SIGTERM; stop must escalate to SIGKILL within the window.
        body = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)"
        )
        proc = self._spawn_fake(body)
        result = self._run_stop(
            TINYHAT_DEV_GATEWAY_STOP_TERM_SECONDS="1",
            TINYHAT_DEV_GATEWAY_STOP_KILL_SECONDS="5",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            proc.poll(),
            -signal.SIGKILL,
            "gateway that ignored SIGTERM must be SIGKILLed by stop",
        )

    def test_stop_is_noop_for_unrelated_unit(self) -> None:
        # A non-gateway systemctl call is a dev no-op and must not touch the fake.
        body = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)"
        )
        proc = self._spawn_fake(body)
        env = {**os.environ, "TINYHAT_DEV_GATEWAY_MATCH_SUBSTR": MARKER}
        result = subprocess.run(
            [self._shim_path, "enable", "--now", "tailscaled"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(proc.poll(), "no-op systemctl call must not stop the fake")


if __name__ == "__main__":
    unittest.main()
