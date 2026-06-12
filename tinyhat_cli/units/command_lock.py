"""Global command lock — one mutation at a time, per Computer.

The mutex is the **fd**, not a pidfile: ``mutex.lock`` is opened with
``O_CREAT`` and held with ``flock(LOCK_EX)`` for the full mutating
command. The fd is deliberately marked inheritable and passed into
every mutation subprocess (spawned in its own process group), so the
kernel keeps the lock held by the open file description until the
**entire mutating process tree** has exited — not merely until the
runner dies. ``lock.json`` is a status record for humans and stale
recovery; it is never the source of mutual exclusion.

Stale takeover is therefore safe exactly when ``flock`` says so: the
fd being free is a kernel guarantee that the previous mutating tree is
gone. The new holder then inspects the previous non-terminal
``lock.json`` and must reconcile that operation to a terminal outcome
(``succeeded`` / ``failed`` / ``timed_out``, with ``runner_lost``
metadata) before mutating — the command-class-specific reconcile lives
with the command unit (see ``gateway_restart``).

Privilege: the lock directory is root-owned ``0700`` under the
control-plane tree. Only root can mutate the runtime (systemd), so the
lock is only meaningful — and only attempted — for root processes;
tests exercise it via the ``TINYHAT_COMMAND_LOCK_DIR`` override.

Idempotency results live under ``results/<key>.json`` (bounded to
:data:`IDEMPOTENCY_MAX_RECORDS` / :data:`IDEMPOTENCY_MAX_AGE_SECONDS`).
A replayed key returns the stored result without re-execution; replay
is always explicit (the CLI mints a fresh key per invocation).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

COMMAND_LOCK_SCHEMA = "command_lock_v1"
TINYHAT_COMMAND_LOCK_DIR_ENV = "TINYHAT_COMMAND_LOCK_DIR"

# Per-class timeout defaults (§ per-command declarations). The runner
# enforces the deadline; systemd's per-unit job serialization absorbs
# any post-timeout overlap at the systemd layer.
DEFAULT_MUTATING_TIMEOUT_SECONDS = 300
KILL_GRACE_SECONDS = 5.0

IDEMPOTENCY_MAX_RECORDS = 50
IDEMPOTENCY_MAX_AGE_SECONDS = 24 * 3600

STALE_TAKEOVER_EVENT = "command_lock_stale_takeover"

# Only the thread that holds the lock routes its subprocess children
# through the transaction (fd inheritance + pgid bookkeeping). Other
# daemon threads keep plain subprocess semantics for their probes.
_active = threading.local()


def active_transaction() -> "CommandLockTransaction | None":
    """The transaction held by the CURRENT thread, if any."""
    return getattr(_active, "transaction", None)


def command_lock_dir() -> str:
    configured = (os.environ.get(TINYHAT_COMMAND_LOCK_DIR_ENV) or "").strip()
    if configured:
        return configured
    sup = _sup()
    return os.path.join(
        os.path.dirname(sup.runtime_state_path()), "command-lock"
    )


def lock_available_to_this_process() -> bool:
    """Whether this process participates in the global lock at all.

    Mutations are root-only (systemd + the root-owned control-plane
    tree), so a non-root process can never hold the real lock; it also
    must not fail on being unable to create root-owned directories.
    The explicit env override opts tests/harnesses in regardless.
    """
    if (os.environ.get(TINYHAT_COMMAND_LOCK_DIR_ENV) or "").strip():
        return True
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _mutex_path() -> str:
    return os.path.join(command_lock_dir(), "mutex.lock")


def _status_path() -> str:
    return os.path.join(command_lock_dir(), "lock.json")


def _results_dir() -> str:
    return os.path.join(command_lock_dir(), "results")


def _pid_start_time(pid: int) -> str | None:
    """``/proc/<pid>/stat`` field 22 — the liveness/identity discriminator."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens; split after the LAST ')'.
    tail = stat.rsplit(")", 1)[-1].split()
    # tail[0] is field 3 (state); field 22 (starttime) is tail[19].
    return tail[19] if len(tail) > 19 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock_status() -> dict[str, Any] | None:
    try:
        with open(_status_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class CommandLockBusy(RuntimeError):
    """Typed busy answer: someone else's mutation is still in flight."""

    def __init__(self, reason: str, status: dict[str, Any] | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _busy_reason(status: dict[str, Any] | None) -> str:
    if not status:
        return "command lock is held (no status record readable)"
    holder = status.get("holder") or "unknown"
    command = status.get("command") or "unknown"
    pid = status.get("pid")
    pid_alive = isinstance(pid, int) and _pid_alive(pid)
    if not pid_alive:
        pgid = status.get("child_pgid")
        return (
            f"busy: mutation child still completing (pgid {pgid}) — "
            f"runner pid {pid} of '{command}' is gone but the lock fd is "
            "still held by its process tree"
        )
    return f"busy: {holder} pid {pid} is running '{command}'"


class CommandLockTransaction:
    """One lock-held mutating command, acquire → phases → terminal."""

    def __init__(
        self,
        command: str,
        *,
        holder: str,
        idempotency_key: str,
        timeout_seconds: int = DEFAULT_MUTATING_TIMEOUT_SECONDS,
    ) -> None:
        self.command = command
        self.holder = holder
        self.idempotency_key = idempotency_key
        self.timeout_seconds = int(timeout_seconds)
        self.fd: int | None = None
        self.generation: int | None = None
        self.acquired_at_unix: int | None = None
        self.deadline_unix: int | None = None
        self.operation_phase = "starting"
        self.operation_marker_unix: int | None = None
        self.child_pgid: int | None = None
        self.previous_status: dict[str, Any] | None = None
        self.stale_previous: dict[str, Any] | None = None
        self.timed_out_children = False

    # ── acquisition ──────────────────────────────────────────────────

    def acquire(self) -> "CommandLockTransaction":
        sup = _sup()
        lock_dir = command_lock_dir()
        sup._prepare_control_plane_state_dir(lock_dir)
        fd = os.open(_mutex_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            status = read_lock_status()
            raise CommandLockBusy(_busy_reason(status), status) from None
        self.fd = fd
        os.set_inheritable(fd, True)
        previous = read_lock_status()
        self.previous_status = previous
        if previous and previous.get("operation_phase") != "terminal":
            # fd free + non-terminal record = the previous mutating tree
            # died without finishing. The caller must reconcile it to a
            # terminal outcome before mutating (command-class-specific).
            self.stale_previous = previous
        try:
            self.generation = int(previous.get("generation") or 0) + 1 if previous else 1
        except (TypeError, ValueError):
            self.generation = 1
        now = int(time.time())
        self.acquired_at_unix = now
        self.deadline_unix = now + self.timeout_seconds
        self._write_status()
        _active.transaction = self
        return self

    def __enter__(self) -> "CommandLockTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # A runner that errors out without a terminal verdict still
        # normalizes: the operation failed.
        if self.fd is not None and self.operation_phase != "terminal":
            detail = f"runner error: {exc}" if exc else "runner exited mid-operation"
            try:
                self.set_phase("terminal", outcome="failed", detail=detail)
            except Exception:  # noqa: BLE001 - releasing matters more
                pass
        self.release()

    # ── status record ────────────────────────────────────────────────

    def _record(self, **extra: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": COMMAND_LOCK_SCHEMA,
            "holder": self.holder,
            "pid": os.getpid(),
            "pid_start_time": _pid_start_time(os.getpid()),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "command": self.command,
            "idempotency_key": self.idempotency_key,
            "child_pgid": self.child_pgid,
            "operation_phase": self.operation_phase,
            "operation_started_at_unix": self.acquired_at_unix,
            "operation_deadline_unix": self.deadline_unix,
            "operation_marker_unix": self.operation_marker_unix,
            "acquired_at_unix": self.acquired_at_unix,
            "deadline_unix": self.deadline_unix,
            "generation": self.generation,
        }
        record.update(extra)
        return record

    def _write_status(self, **extra: Any) -> None:
        sup = _sup()
        sup._atomic_write_json(_status_path(), self._record(**extra), mode=0o600)

    def set_phase(
        self,
        phase: str,
        *,
        marker_unix: int | None = None,
        outcome: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.operation_phase = phase
        if marker_unix is not None:
            self.operation_marker_unix = int(marker_unix)
        extra: dict[str, Any] = {}
        if outcome is not None:
            extra["outcome"] = outcome
        if detail is not None:
            sup = _sup()
            extra["detail"] = sup._sanitize_runtime_state_text(detail, limit=256)
        self._write_status(**extra)

    # ── mutation children ────────────────────────────────────────────

    def remaining_seconds(self) -> float:
        if self.deadline_unix is None:
            return float(self.timeout_seconds)
        return self.deadline_unix - time.time()

    def run_subprocess(
        self,
        argv: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """``subprocess.run``-shaped, but lock-aware.

        The child gets the mutex fd (inheritable) and its own process
        group; ``child_pgid`` is recorded in ``lock.json`` as soon as
        the child is spawned, and the effective timeout never exceeds
        the operation deadline. On timeout the WHOLE child group gets
        SIGTERM → grace → SIGKILL, then ``TimeoutExpired`` is raised
        with the same contract callers of ``subprocess.run`` expect.
        """
        remaining = self.remaining_seconds()
        if remaining <= 0:
            self.timed_out_children = True
            raise subprocess.TimeoutExpired(argv, 0)
        effective_timeout = min(timeout, remaining) if timeout else remaining
        if self.fd is None:
            raise RuntimeError("command lock transaction is not acquired")
        popen = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
            pass_fds=(self.fd,),
            start_new_session=True,
        )
        self.child_pgid = popen.pid
        self._write_status()
        try:
            stdout, stderr = popen.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            self.timed_out_children = True
            self.kill_child_group()
            try:
                stdout, stderr = popen.communicate(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            raise subprocess.TimeoutExpired(
                argv, effective_timeout, output=stdout, stderr=stderr
            ) from None
        finally:
            self.child_pgid = None
            self._write_status()
        return subprocess.CompletedProcess(argv, popen.returncode, stdout, stderr)

    def kill_child_group(self) -> None:
        pgid = self.child_pgid
        if not pgid:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + KILL_GRACE_SECONDS
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # ── terminal ─────────────────────────────────────────────────────

    def finish(self, outcome: str, detail: str, result_record: dict[str, Any]) -> None:
        store_result(self.idempotency_key, result_record)
        self.set_phase("terminal", outcome=outcome, detail=detail)

    def release(self) -> None:
        if getattr(_active, "transaction", None) is self:
            _active.transaction = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def acquire(
    command: str,
    *,
    holder: str,
    idempotency_key: str,
    timeout_seconds: int = DEFAULT_MUTATING_TIMEOUT_SECONDS,
    wait_seconds: float = 0,
    on_wait: Callable[[], None] | None = None,
) -> CommandLockTransaction:
    """Acquire the global lock, optionally deferring while it's busy.

    ``wait_seconds > 0`` is the daemon's defer-don't-race mode: poll the
    mutex (never mutate while busy) and call ``on_wait`` each second so
    the caller can keep feeding its watchdog.
    """
    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        txn = CommandLockTransaction(
            command,
            holder=holder,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )
        try:
            return txn.acquire()
        except CommandLockBusy:
            if time.time() >= deadline:
                raise
            if on_wait is not None:
                try:
                    on_wait()
                except Exception:  # noqa: BLE001 - keep waiting anyway
                    pass
            time.sleep(1)


# ── idempotency results store ────────────────────────────────────────


def store_result(idempotency_key: str, record: dict[str, Any]) -> None:
    sup = _sup()
    results_dir = _results_dir()
    sup._prepare_control_plane_state_dir(results_dir)
    # Stamp the exact key into the record so load_result can verify it:
    # the filename is a digest, never the key itself.
    stamped = dict(record)
    stamped.setdefault("idempotency_key", str(idempotency_key))
    sup._atomic_write_json(
        os.path.join(results_dir, _result_filename(idempotency_key)),
        stamped,
        mode=0o600,
    )
    _prune_results(results_dir)


def load_result(idempotency_key: str) -> dict[str, Any] | None:
    path = os.path.join(_results_dir(), _result_filename(idempotency_key))
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    # Replay returns a stored result only for the EXACT key it was
    # stored under — a digest collision or a record written under a
    # different scheme must never replay as someone else's result.
    stored_key = payload.get("idempotency_key")
    if stored_key is not None and str(stored_key) != str(idempotency_key):
        return None
    return payload


def _result_filename(idempotency_key: str) -> str:
    """Fixed-length, collision-free filename for any caller-supplied key.

    ``--idempotency-key`` is operator-controlled free text. Deriving the
    path by stripping characters would let distinct keys collide
    (``a/b`` vs ``ab``) and let a long key blow the filename limit AFTER
    the mutation already ran — so the path is a digest of the exact
    key, and the key itself lives (verified) inside the record.
    """
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _prune_results(results_dir: str) -> None:
    try:
        entries = [
            os.path.join(results_dir, name)
            for name in os.listdir(results_dir)
            if name.endswith(".json")
        ]
    except OSError:
        return
    now = time.time()
    dated: list[tuple[float, str]] = []
    for path in entries:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if now - mtime > IDEMPOTENCY_MAX_AGE_SECONDS:
            _unlink_quiet(path)
            continue
        dated.append((mtime, path))
    dated.sort()
    while len(dated) > IDEMPOTENCY_MAX_RECORDS:
        _, oldest = dated.pop(0)
        _unlink_quiet(oldest)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
