"""ChatGPT/Codex subscription linking for tiny_runtime.

The runtime uses OpenClaw's official model-auth CLI and posts only public
device-code lifecycle state back to Tinyloop. OAuth tokens stay on the VM.
"""

from __future__ import annotations

import logging
import os
import pty
import re
import select
import subprocess
import threading
import time
from typing import Any, Callable

from . import openclaw_adapter
from .platform_client import PlatformClient
from .redaction import redact_text

LOG = logging.getLogger("tinyhat-runtime-subscription-link")

URL_EMIT_TIMEOUT_SECONDS = float(os.environ.get("TINYHAT_CHATGPT_CODE_TIMEOUT", "45"))
OVERALL_TIMEOUT_SECONDS = float(os.environ.get("TINYHAT_CHATGPT_LINK_TIMEOUT", "900"))
OPENAI_AUTH_URL_RE = re.compile(r"(https://auth\.openai\.com/\S+)")
CODE_RE = re.compile(
    r"(?:Code|code|User code|user code)\s*[:=]\s*([A-Za-z0-9]{4,8}-[A-Za-z0-9]{4,8})"
)
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_RE = re.compile(r"\x1b\][0-9;].*?(?:\x07|\x1b\\)")

_active_sessions: set[str] = set()
_active_lock = threading.Lock()

Launcher = Callable[[Callable[[], None]], None]


def strip_terminal_control(text: str) -> str:
    text = ANSI_OSC_RE.sub("", text)
    return ANSI_CSI_RE.sub("", text)


def extract_public_device_code(buffer: str) -> tuple[str | None, str | None]:
    clean = strip_terminal_control(buffer)
    url_match = OPENAI_AUTH_URL_RE.search(clean)
    code_match = CODE_RE.search(clean)
    return (
        url_match.group(1).rstrip(".,)") if url_match else None,
        code_match.group(1).strip() if code_match else None,
    )


def start_chatgpt_link(
    command: dict[str, Any],
    *,
    client: PlatformClient,
    launcher: Launcher | None = None,
) -> dict[str, Any]:
    session_id = str(command.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    with _active_lock:
        if session_id in _active_sessions:
            return {
                "state": "already_running",
                "session_id": session_id,
                "worker": "tiny_runtime_device_code",
            }
        _active_sessions.add(session_id)

    def target() -> None:
        try:
            run_device_code_flow(session_id=session_id, client=client)
        finally:
            with _active_lock:
                _active_sessions.discard(session_id)

    if launcher is None:
        thread = threading.Thread(
            target=target,
            name=f"tinyhat-chatgpt-link-{session_id[:8]}",
            daemon=True,
        )
        thread.start()
    else:
        launcher(target)
    return {
        "state": "started",
        "session_id": session_id,
        "worker": "tiny_runtime_device_code",
    }


def run_device_code_flow(
    *,
    session_id: str,
    client: PlatformClient,
    url_emit_timeout_seconds: float = URL_EMIT_TIMEOUT_SECONDS,
    overall_timeout_seconds: float = OVERALL_TIMEOUT_SECONDS,
) -> None:
    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    buffer = ""
    pending_sent = False
    started = time.monotonic()
    try:
        process = openclaw_adapter.spawn_models_auth_login_device_code(
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)
        slave_fd = -1
        while True:
            elapsed = time.monotonic() - started
            if elapsed > overall_timeout_seconds:
                _terminate_process(process)
                _post_link_result(
                    client,
                    session_id=session_id,
                    status="failed",
                    error=(
                        "Device-code login timed out before the user approved at "
                        "auth.openai.com."
                    ),
                )
                return

            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    buffer += chunk.decode("utf-8", errors="replace")
                    if not pending_sent:
                        url, code = extract_public_device_code(buffer)
                        if url and code:
                            _post_link_result(
                                client,
                                session_id=session_id,
                                status="pending",
                                verification_url=url,
                                user_code=code,
                            )
                            pending_sent = True

            returncode = process.poll()
            if returncode is not None:
                buffer += _drain_fd(master_fd)
                linked = returncode == 0 or _looks_successful(buffer)
                if linked:
                    _post_link_result(client, session_id=session_id, status="linked")
                    return
                detail = strip_terminal_control(buffer)[-500:].strip()
                _post_link_result(
                    client,
                    session_id=session_id,
                    status="failed",
                    error=(
                        "OpenClaw device-code login failed "
                        f"(exit code: {returncode}). Recent CLI output: "
                        f"{redact_text(detail)}"
                    )[:1000],
                )
                return

            if not pending_sent and elapsed > url_emit_timeout_seconds:
                _terminate_process(process)
                detail = strip_terminal_control(buffer)[-500:].strip()
                _post_link_result(
                    client,
                    session_id=session_id,
                    status="failed",
                    error=(
                        "OpenClaw did not return a ChatGPT device code in time. "
                        "Check that device-code login is enabled in ChatGPT "
                        "Security settings, then retry. Recent CLI output: "
                        f"{redact_text(detail)}"
                    )[:1000],
                )
                return
    except Exception as exc:  # noqa: BLE001 - background worker boundary
        if process is not None:
            _terminate_process(process)
        LOG.exception("ChatGPT device-code worker failed session_id=%s", session_id)
        _post_link_result(
            client,
            session_id=session_id,
            status="failed",
            error=f"Runtime failed to start ChatGPT device-code login: {exc}",
        )
    finally:
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass


def _looks_successful(buffer: str) -> bool:
    lowered = strip_terminal_control(buffer).lower()
    return "device code complete" in lowered or "authorization complete" in lowered


def _drain_fd(fd: int) -> str:
    chunks: list[str] = []
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if fd not in ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk.decode("utf-8", errors="replace"))
    return "".join(chunks)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:  # noqa: BLE001 - best effort cleanup
        try:
            process.kill()
        except Exception:
            pass


def _post_link_result(
    client: PlatformClient,
    *,
    session_id: str,
    status: str,
    verification_url: str | None = None,
    user_code: str | None = None,
    error: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "session_id": session_id,
        "status": status,
    }
    if verification_url:
        body["verification_url"] = verification_url
    if user_code:
        body["user_code"] = user_code
    if error:
        body["error"] = redact_text(error, limit=1000)
    client.post_json("/hapi/v1/computers/me/subscription-link-result", body)
