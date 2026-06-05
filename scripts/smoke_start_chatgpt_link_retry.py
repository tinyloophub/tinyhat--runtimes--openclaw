#!/usr/bin/env python3
"""Smoke start_chatgpt_link retry recovery with a fake OpenClaw CLI.

This is intentionally outside unittest: it runs the real heartbeat handler,
forks a real subprocess under a PTY, and lets the supervisor POST lifecycle
results to a localhost backend. The fake OpenClaw CLI only controls the two
pre-code failure shapes that are hard to force against a live account.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import supervisor  # noqa: E402


class _RecordingServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.records: list[tuple[str, dict]] = []


class _RecordingHandler(BaseHTTPRequestHandler):
    server: _RecordingServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw_body.decode("utf-8") or "{}")
        self.server.records.append((self.path, body))
        self._send_json({})

    def do_GET(self) -> None:
        self._send_json({})

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextlib.contextmanager
def _recording_backend() -> Iterator[tuple[_RecordingServer, str]]:
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@contextlib.contextmanager
def _temporary_env(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_fake_openclaw(bin_dir: Path) -> Path:
    openclaw = bin_dir / "openclaw"
    openclaw.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import signal
import sys
import time

args = sys.argv[1:]

if args == ["plugins", "inspect", "openai", "--json"]:
    print(json.dumps({{"plugin": {{"id": "openai", "enabled": True, "status": "loaded", "providerIds": ["openai"]}}}}))
    raise SystemExit(0)

if (
    len(args) >= 6
    and args[:3] == ["models", "auth", "login"]
    and "--provider" in args
    and "--device-code" in args
):
    attempts_path = Path(os.environ["SMOKE_ATTEMPTS_PATH"])
    try:
        attempts = int(attempts_path.read_text(encoding="utf-8") or "0")
    except FileNotFoundError:
        attempts = 0
    attempts += 1
    attempts_path.write_text(str(attempts), encoding="utf-8")

    scenario = os.environ["SMOKE_SCENARIO"]
    if attempts == 1 and scenario == "exit-then-success":
        print("simulated pre-code CLI exit", file=sys.stderr, flush=True)
        raise SystemExit(42)
    if attempts == 1 and scenario == "hang-then-success":
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
        while True:
            time.sleep(60)

    print("URL: https://auth.openai.com/codex/device", flush=True)
    print(f"Code: {{os.environ['SMOKE_USER_CODE']}}", flush=True)
    print("OpenAI device code complete", flush=True)
    raise SystemExit(0)

print("unexpected openclaw invocation: " + " ".join(args), file=sys.stderr, flush=True)
raise SystemExit(64)
""",
        encoding="utf-8",
    )
    openclaw.chmod(0o755)
    return openclaw


def _reset_supervisor_state() -> None:
    supervisor._base_url_cache.update({"value": None, "ts": 0.0})
    supervisor._audience_cache.update({"value": None, "ts": 0.0})
    supervisor._subscription_link_sessions_started.clear()
    with supervisor._subscription_link_active_workers_lock:
        supervisor._subscription_link_active_workers.clear()
    with supervisor._binding_generation_lock:
        supervisor._binding_generation = 0


def _result_bodies(server: _RecordingServer, session_id: str) -> list[dict]:
    return [
        body
        for path, body in list(server.records)
        if path == "/hapi/v1/computers/me/subscription-link-result"
        and body.get("session_id") == session_id
    ]


def _wait_for_terminal_result(
    server: _RecordingServer, session_id: str, *, timeout_s: float
) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    bodies: list[dict] = []
    while time.monotonic() < deadline:
        bodies = _result_bodies(server, session_id)
        if bodies and bodies[-1].get("status") in {"linked", "failed"}:
            return bodies
        time.sleep(0.05)
    return bodies


def _wait_for_worker_idle(session_id: str, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with supervisor._subscription_link_active_workers_lock:
            active = session_id in supervisor._subscription_link_active_workers
        if not active:
            return True
        time.sleep(0.05)
    return False


def _run_scenario(scenario: str, user_code: str) -> None:
    with tempfile.TemporaryDirectory(prefix="tinyhat-chatgpt-link-smoke-") as tmp:
        tmp_path = Path(tmp)
        bin_dir = tmp_path / "bin"
        runtime_home = tmp_path / "runtime"
        bin_dir.mkdir()
        runtime_home.mkdir()
        _write_fake_openclaw(bin_dir)
        attempts_path = tmp_path / "attempts.txt"

        with _recording_backend() as (server, base_url):
            _reset_supervisor_state()
            env = {
                "TINYHAT_DEV_RUNTIME": "1",
                "TINYHAT_PLATFORM_BASE_URL": base_url,
                "TINYHAT_BACKEND_AUDIENCE": base_url,
                "TINYHAT_RUNTIME_HOME": str(runtime_home),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                supervisor.CHATGPT_DEVICE_CODE_URL_EMIT_TIMEOUT_ENV: "0.25",
                supervisor.CHATGPT_DEVICE_CODE_URL_EMIT_ATTEMPTS_ENV: "2",
                supervisor.CHATGPT_DEVICE_CODE_RETRY_DELAY_ENV: "0",
                supervisor.CHATGPT_DEVICE_CODE_OVERALL_TIMEOUT_ENV: "5",
                "SMOKE_ATTEMPTS_PATH": str(attempts_path),
                "SMOKE_SCENARIO": scenario,
                "SMOKE_USER_CODE": user_code,
            }
            session_id = f"smoke-{scenario}"
            with _temporary_env(env):
                supervisor.handle_start_chatgpt_link_command(
                    {"type": "start_chatgpt_link", "session_id": session_id}
                )
                bodies = _wait_for_terminal_result(
                    server, session_id, timeout_s=8.0
                )
                if not _wait_for_worker_idle(session_id, timeout_s=2.0):
                    raise AssertionError(f"{scenario}: worker did not go idle")

        statuses = [body.get("status") for body in bodies]
        if statuses != ["pending", "linked"]:
            raise AssertionError(
                f"{scenario}: expected pending then linked, got {bodies!r}"
            )
        if bodies[0].get("verification_url") != "https://auth.openai.com/codex/device":
            raise AssertionError(f"{scenario}: pending result has wrong URL")
        if bodies[0].get("user_code") != user_code:
            raise AssertionError(f"{scenario}: pending result has wrong code")
        attempts = attempts_path.read_text(encoding="utf-8")
        if attempts != "2":
            raise AssertionError(f"{scenario}: expected 2 CLI attempts, got {attempts}")
        print(f"PASS {scenario}: pending+linked after {attempts} CLI attempts")


def main() -> int:
    _run_scenario("exit-then-success", "EXIT-OKAY")
    _run_scenario("hang-then-success", "HANG-OKAY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
