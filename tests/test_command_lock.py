"""Global command lock + command-result spool + `commands` ring tests.

CI-runnable (non-root) legs of the M2 proof gates: the lock unit and
spool unit run against temp directories via their env overrides; the
seven LIVE concurrency cases run on a real VM via
``dev/systemd-proof/lock_proof.sh`` and are not duplicated here.

Usage:
    python -m unittest tests.test_command_lock -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import supervisor
from tinyhat_cli import entrypoint
from tinyhat_cli.units import command_lock, command_spool, gateway_restart
from tests.test_tinyhat_cli import _SENTINEL_CORPUS, _corpus_leaks


class _LockEnvironment:
    """Temp control-plane tree wired through the env overrides."""

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self.tmpdir = self._stack.enter_context(tempfile.TemporaryDirectory())
        self.state_path = os.path.join(self.tmpdir, "runtime-state.json")
        self.lock_dir = os.path.join(self.tmpdir, "command-lock")
        self.results_dir = os.path.join(self.tmpdir, "command-results")
        self._stack.enter_context(
            patch.dict(
                os.environ,
                {
                    supervisor.TINYHAT_RUNTIME_STATE_PATH_ENV: self.state_path,
                    command_lock.TINYHAT_COMMAND_LOCK_DIR_ENV: self.lock_dir,
                    command_spool.TINYHAT_COMMAND_RESULTS_DIR_ENV: self.results_dir,
                },
                clear=False,
            )
        )
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()

    def lock_status(self) -> dict:
        with open(os.path.join(self.lock_dir, "lock.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def spool_files(self) -> list[str]:
        spool = os.path.join(self.results_dir, "spool")
        try:
            return sorted(
                name for name in os.listdir(spool) if name.endswith(".json")
            )
        except OSError:
            return []


def _ring_record(key: str, *, outcome: str = "succeeded", **extra) -> dict:
    record = {
        "name": "gateway restart",
        "class": "operate",
        "outcome": outcome,
        "started_at_unix": 1_760_000_000,
        "finished_at_unix": 1_760_000_100,
        "idempotency_key": key,
        "summary": f"result {key}",
    }
    record.update(extra)
    return record


class CommandLockTransactionTests(unittest.TestCase):
    def test_acquire_writes_contract_status_record(self) -> None:
        with _LockEnvironment() as env:
            txn = command_lock.acquire(
                "gateway restart", holder="cli", idempotency_key="k1",
                timeout_seconds=120,
            )
            try:
                status = env.lock_status()
            finally:
                txn.finish("succeeded", "done", _ring_record("k1"))
                txn.release()
            self.assertEqual(status["schema"], "command_lock_v1")
            for field in (
                "holder",
                "pid",
                "pid_start_time",
                "uid",
                "command",
                "idempotency_key",
                "child_pgid",
                "operation_phase",
                "operation_started_at_unix",
                "operation_deadline_unix",
                "operation_marker_unix",
                "acquired_at_unix",
                "deadline_unix",
                "generation",
            ):
                self.assertIn(field, status)
            self.assertEqual(status["holder"], "cli")
            self.assertEqual(status["command"], "gateway restart")
            self.assertEqual(status["generation"], 1)
            self.assertEqual(
                status["operation_deadline_unix"] - status["acquired_at_unix"],
                120,
            )
            self.assertEqual(env.lock_status()["operation_phase"], "terminal")

    def test_contender_gets_typed_busy_and_never_mutates(self) -> None:
        with _LockEnvironment():
            holder = command_lock.acquire(
                "gateway restart", holder="cli", idempotency_key="k1"
            )
            try:
                with self.assertRaises(command_lock.CommandLockBusy) as caught:
                    command_lock.acquire(
                        "gateway restart", holder="daemon", idempotency_key="k2"
                    )
                self.assertIn("cli pid", caught.exception.reason)
                self.assertIn("gateway restart", caught.exception.reason)
                self.assertEqual(caught.exception.status["idempotency_key"], "k1")
            finally:
                holder.finish("succeeded", "done", _ring_record("k1"))
                holder.release()
            # Released: the next contender acquires under generation 2.
            txn = command_lock.acquire(
                "gateway restart", holder="daemon", idempotency_key="k3"
            )
            self.assertEqual(txn.generation, 2)
            self.assertIsNone(txn.stale_previous)
            txn.finish("succeeded", "done", _ring_record("k3"))
            txn.release()

    def test_stale_takeover_exposes_previous_non_terminal_record(self) -> None:
        with _LockEnvironment() as env:
            txn = command_lock.acquire(
                "gateway restart", holder="cli", idempotency_key="lost"
            )
            txn.set_phase("readiness_wait", marker_unix=1_760_000_000)
            # Simulate the runner dying WITHOUT terminal write: close the
            # fd directly (the kernel frees the flock) but leave lock.json.
            os.close(txn.fd)
            txn.fd = None
            command_lock._active.transaction = None

            takeover = command_lock.acquire(
                "gateway restart", holder="daemon", idempotency_key="next"
            )
            try:
                self.assertIsNotNone(takeover.stale_previous)
                self.assertEqual(
                    takeover.stale_previous["operation_phase"], "readiness_wait"
                )
                self.assertEqual(
                    takeover.stale_previous["operation_marker_unix"], 1_760_000_000
                )
                self.assertEqual(takeover.generation, 2)
            finally:
                takeover.finish("succeeded", "done", _ring_record("next"))
                takeover.release()
            self.assertEqual(env.lock_status()["operation_phase"], "terminal")

    def test_run_subprocess_records_pgid_and_inherits_fd(self) -> None:
        with _LockEnvironment():
            txn = command_lock.acquire(
                "gateway restart", holder="cli", idempotency_key="k1"
            )
            try:
                # The child sees the mutex fd open (inheritance marker).
                result = txn.run_subprocess(
                    [
                        "python3",
                        "-c",
                        f"import os; os.fstat({txn.fd}); print(os.getpgrp())",
                    ],
                )
                self.assertEqual(result.returncode, 0)
                child_pgid = int(result.stdout.strip())
                self.assertNotEqual(child_pgid, os.getpgrp())
                self.assertIsNone(txn.child_pgid)  # cleared after completion
            finally:
                txn.finish("succeeded", "done", _ring_record("k1"))
                txn.release()

    def test_deadline_kills_the_child_process_group(self) -> None:
        with _LockEnvironment():
            txn = command_lock.acquire(
                "gateway restart", holder="cli", idempotency_key="k1",
                timeout_seconds=1,
            )
            try:
                started = time.time()
                with self.assertRaises(subprocess.TimeoutExpired):
                    txn.run_subprocess(["sleep", "30"])
                self.assertLess(time.time() - started, 10)
                self.assertTrue(txn.timed_out_children)
            finally:
                txn.finish(
                    "timed_out", "deadline", _ring_record("k1", outcome="timed_out")
                )
                txn.release()

    def test_exit_without_terminal_normalizes_to_failed(self) -> None:
        with _LockEnvironment() as env:
            with self.assertRaises(RuntimeError):
                with command_lock.acquire(
                    "gateway restart", holder="cli", idempotency_key="k1"
                ):
                    raise RuntimeError("runner blew up")
            status = env.lock_status()
            self.assertEqual(status["operation_phase"], "terminal")
            self.assertEqual(status["outcome"], "failed")

    def test_idempotency_store_roundtrip_and_caps(self) -> None:
        with _LockEnvironment():
            command_lock.store_result("replay-me", _ring_record("replay-me"))
            stored = command_lock.load_result("replay-me")
            self.assertEqual(stored["idempotency_key"], "replay-me")
            self.assertIsNone(command_lock.load_result("never-stored"))
            for index in range(command_lock.IDEMPOTENCY_MAX_RECORDS + 7):
                command_lock.store_result(f"key-{index:03d}", _ring_record(f"key-{index:03d}"))
            results_dir = command_lock._results_dir()
            self.assertLessEqual(
                len([n for n in os.listdir(results_dir) if n.endswith(".json")]),
                command_lock.IDEMPOTENCY_MAX_RECORDS,
            )
            # Age cap: a record older than 24h is pruned on the next store.
            old = os.path.join(results_dir, "key-007.json")
            if os.path.exists(old):
                stale_mtime = time.time() - command_lock.IDEMPOTENCY_MAX_AGE_SECONDS - 60
                os.utime(old, (stale_mtime, stale_mtime))
                command_lock.store_result("fresh", _ring_record("fresh"))
                self.assertFalse(os.path.exists(old))

    def test_lock_unavailable_without_root_or_override(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(command_lock.TINYHAT_COMMAND_LOCK_DIR_ENV, None)
            with patch.object(command_lock.os, "geteuid", return_value=1000, create=True):
                self.assertFalse(command_lock.lock_available_to_this_process())
        with _LockEnvironment():
            self.assertTrue(command_lock.lock_available_to_this_process())


class CommandSpoolTests(unittest.TestCase):
    def test_append_then_shared_reader_roundtrip(self) -> None:
        with _LockEnvironment() as env:
            path = command_spool.append_result(
                _ring_record("k1", phase_reached="terminal", extra_field="dropped")
            )
            self.assertTrue(os.path.exists(path))
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
            results = command_spool.read_results()
            self.assertEqual(len(results), 1)
            record = results[0][1]
            self.assertEqual(record["idempotency_key"], "k1")
            # Only the ring projection is transported.
            self.assertNotIn("extra_field", record)
            self.assertNotIn("phase_reached", record)
            self.assertEqual(env.spool_files(), [os.path.basename(path)])

    def test_record_size_is_bounded(self) -> None:
        with _LockEnvironment():
            path = command_spool.append_result(
                _ring_record("k-big", summary="s" * 5000)
            )
            self.assertLessEqual(
                os.stat(path).st_size, command_spool.MAX_RECORD_BYTES
            )
            record = command_spool.read_results()[0][1]
            self.assertLessEqual(
                len(record["summary"].encode("utf-8")),
                command_spool.SUMMARY_MAX_BYTES,
            )

    def test_redaction_failure_is_fail_closed(self) -> None:
        with _LockEnvironment() as env:
            with patch.object(
                command_spool,
                "sanitize_json_tree",
                side_effect=RuntimeError("sanitizer exploded"),
            ):
                with self.assertRaises(command_spool.SpoolRedactionError):
                    command_spool.append_result(_ring_record("k1"))
            self.assertEqual(env.spool_files(), [])

    def test_spool_prunes_oldest_first(self) -> None:
        with _LockEnvironment() as env:
            for index in range(command_spool.MAX_SPOOL_RECORDS + 5):
                command_spool.append_result(
                    _ring_record(
                        f"key-{index:03d}",
                        finished_at_unix=1_760_000_000 + index,
                    )
                )
            files = env.spool_files()
            self.assertEqual(len(files), command_spool.MAX_SPOOL_RECORDS)
            # The oldest five (000–004) were pruned.
            self.assertNotIn("001760000000-key-000.json", files)
            self.assertIn(
                f"{1_760_000_000 + command_spool.MAX_SPOOL_RECORDS + 4:012d}-"
                f"key-{command_spool.MAX_SPOOL_RECORDS + 4:03d}.json",
                files,
            )

    def test_corrupt_records_quarantine_bounded(self) -> None:
        with _LockEnvironment() as env:
            spool = os.path.join(env.results_dir, "spool")
            os.makedirs(spool, exist_ok=True)
            for index in range(command_spool.MAX_QUARANTINE_RECORDS + 3):
                with open(
                    os.path.join(spool, f"{index:012d}-corrupt.json"),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    fh.write("{not json")
            command_spool.append_result(_ring_record("good"))
            results = command_spool.read_results()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1]["idempotency_key"], "good")
            quarantine = os.path.join(env.results_dir, "quarantine")
            self.assertLessEqual(
                len(os.listdir(quarantine)), command_spool.MAX_QUARANTINE_RECORDS
            )

    def test_prune_folded_removes_only_named_paths(self) -> None:
        with _LockEnvironment():
            first = command_spool.append_result(_ring_record("k1"))
            second = command_spool.append_result(
                _ring_record("k2", finished_at_unix=1_760_000_200)
            )
            command_spool.prune_folded([first])
            remaining = [path for path, _record in command_spool.read_results()]
            self.assertEqual(remaining, [second])


class CommandsRingFoldTests(unittest.TestCase):
    def test_fold_merges_dedupes_and_caps_at_five(self) -> None:
        with _LockEnvironment():
            existing = {
                "commands": [
                    _ring_record("old-1", finished_at_unix=1_760_000_000),
                    _ring_record("old-2", finished_at_unix=1_760_000_001),
                ]
            }
            for index in range(50):
                command_spool.append_result(
                    _ring_record(
                        f"spool-{index:02d}",
                        finished_at_unix=1_760_000_010 + index,
                        summary="s" * 256,
                    )
                )
            ring, folded, fresh = supervisor.fold_command_results(existing)
            self.assertEqual(len(ring), 5)
            self.assertEqual(len(folded), 50)
            self.assertEqual(len(fresh), 50)
            self.assertEqual(
                [entry["idempotency_key"] for entry in ring],
                [f"spool-{index:02d}" for index in range(45, 50)],
            )
            # Read-only callers (tinyhat status) left the spool intact.
            self.assertEqual(len(command_spool.read_results()), 50)

    def test_daemon_write_folds_ring_prunes_spool_and_mirrors_events(self) -> None:
        with _LockEnvironment() as env:
            command_spool.append_result(
                _ring_record(
                    "lost-op",
                    outcome="succeeded",
                    runner_lost=True,
                    stale_takeover=True,
                    finished_at_unix=1_760_000_300,
                )
            )
            with (
                patch.object(supervisor, "_runtime_state_recent_log_excerpts", return_value={"bootstrap": [], "supervisor": [], "gateway": []}),
                patch.object(supervisor, "_plugin_load_check", return_value=None),
                patch.object(supervisor, "_post_runtime_state_to_platform", return_value=False),
            ):
                supervisor._write_runtime_state(
                    "healthy",
                    "steady",
                    gateway_active=True,
                    openclaw_ready=True,
                )
            with open(env.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            ring = state["commands"]
            self.assertEqual(len(ring), 1)
            self.assertEqual(ring[0]["idempotency_key"], "lost-op")
            self.assertTrue(ring[0]["runner_lost"])
            self.assertIn(
                "command_lock_stale_takeover",
                [event["type"] for event in state["runtime_events"]],
            )
            # Folded → pruned: the spool drained into the ring.
            self.assertEqual(env.spool_files(), [])
            # A second write keeps the ring from the existing state.
            with (
                patch.object(supervisor, "_runtime_state_recent_log_excerpts", return_value={"bootstrap": [], "supervisor": [], "gateway": []}),
                patch.object(supervisor, "_plugin_load_check", return_value=None),
                patch.object(supervisor, "_post_runtime_state_to_platform", return_value=False),
            ):
                supervisor._write_runtime_state("healthy", "steady again")
            with open(env.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            self.assertEqual(len(state["commands"]), 1)


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = entrypoint.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class GatewayRestartOperateCliTests(unittest.TestCase):
    def _patched_transaction(self, **overrides):
        defaults = {
            "delete_telegram_webhook": patch.object(supervisor, "delete_telegram_webhook"),
            "start_openclaw_gateway": patch.object(
                supervisor, "start_openclaw_gateway", return_value=1_760_000_000.0
            ),
            "wait_for_openclaw_start": patch.object(
                supervisor, "wait_for_openclaw_start"
            ),
            "geteuid": patch.object(os, "geteuid", return_value=0),
            "binding": patch(
                "tinyhat_cli.adapters.openclaw.configured_telegram_binding",
                return_value={
                    "telegram_bot_token": "7654321098:AAEhBOweik6ad9r_QXMENQjcG4euXckBVlo"
                },
            ),
        }
        defaults.update(overrides)
        return defaults

    def test_operate_success_envelope_schema_valid_and_spooled(self) -> None:
        from tinyhat_cli import schemas

        with _LockEnvironment() as env:
            patches = self._patched_transaction()
            with contextlib.ExitStack() as stack:
                mocks = {name: stack.enter_context(p) for name, p in patches.items()}
                code, out, _err = _run_cli(["gateway", "restart", "--json"])
            self.assertEqual(code, 0)
            envelope = json.loads(out)
            self.assertEqual(schemas.validate_envelope("gateway restart", envelope), [])
            self.assertEqual(envelope["data"]["outcome"], "succeeded")
            self.assertFalse(envelope["data"]["replayed"])
            mocks["delete_telegram_webhook"].assert_called_once()
            mocks["start_openclaw_gateway"].assert_called_once()
            mocks["wait_for_openclaw_start"].assert_called_once_with(1_760_000_000.0)
            # Operate results always reach the spool…
            self.assertEqual(len(env.spool_files()), 1)
            # …and the terminal lock record is on disk.
            self.assertEqual(env.lock_status()["operation_phase"], "terminal")

    def test_repeated_run_restarts_again_with_fresh_key(self) -> None:
        with _LockEnvironment() as env:
            patches = self._patched_transaction()
            with contextlib.ExitStack() as stack:
                mocks = {name: stack.enter_context(p) for name, p in patches.items()}
                code_one, out_one, _ = _run_cli(["gateway", "restart", "--json"])
                code_two, out_two, _ = _run_cli(["gateway", "restart", "--json"])
            self.assertEqual((code_one, code_two), (0, 0))
            key_one = json.loads(out_one)["data"]["idempotency_key"]
            key_two = json.loads(out_two)["data"]["idempotency_key"]
            self.assertNotEqual(key_one, key_two)
            self.assertEqual(mocks["start_openclaw_gateway"].call_count, 2)
            self.assertEqual(len(env.spool_files()), 2)

    def test_explicit_idempotency_key_replays_without_execution(self) -> None:
        with _LockEnvironment():
            command_lock.store_result(
                "stored-key", _ring_record("stored-key", outcome="succeeded")
            )
            patches = self._patched_transaction()
            with contextlib.ExitStack() as stack:
                mocks = {name: stack.enter_context(p) for name, p in patches.items()}
                code, out, _err = _run_cli(
                    ["gateway", "restart", "--json", "--idempotency-key", "stored-key"]
                )
            self.assertEqual(code, 0)
            data = json.loads(out)["data"]
            self.assertTrue(data["replayed"])
            self.assertEqual(data["outcome"], "succeeded")
            mocks["start_openclaw_gateway"].assert_not_called()
            mocks["delete_telegram_webhook"].assert_not_called()

    def test_busy_lock_answers_typed_with_exit_75(self) -> None:
        with _LockEnvironment():
            holder = command_lock.acquire(
                "gateway restart", holder="daemon", idempotency_key="held"
            )
            try:
                patches = self._patched_transaction()
                with contextlib.ExitStack() as stack:
                    for p in patches.values():
                        stack.enter_context(p)
                    code, _out, err = _run_cli(["gateway", "restart"])
            finally:
                holder.finish("succeeded", "done", _ring_record("held"))
                holder.release()
            self.assertEqual(code, entrypoint.EXIT_BUSY)
            payload = json.loads(err)
            self.assertEqual(payload["error"]["type"], "busy")
            self.assertIn("daemon pid", payload["error"]["detail"])

    def test_non_root_refused_before_any_lock_or_mutation(self) -> None:
        with _LockEnvironment() as env:
            with patch.object(os, "geteuid", return_value=1000):
                code, _out, err = _run_cli(["gateway", "restart"])
            self.assertEqual(code, entrypoint.EXIT_NOT_ROOT)
            self.assertEqual(json.loads(err)["error"]["type"], "not_root")
            self.assertEqual(env.spool_files(), [])
            self.assertFalse(os.path.exists(os.path.join(env.lock_dir, "lock.json")))

    def test_dev_container_refused_typed(self) -> None:
        with _LockEnvironment():
            with (
                patch.object(os, "geteuid", return_value=0),
                patch.object(supervisor, "_dev_mode", return_value=True),
            ):
                code, _out, err = _run_cli(["gateway", "restart"])
            self.assertEqual(code, entrypoint.EXIT_UNSUPPORTED)
            self.assertEqual(
                json.loads(err)["error"]["type"], "unsupported_environment"
            )

    def test_daemon_defers_then_acquires_when_holder_releases(self) -> None:
        """Case-1 shape, CI-sized: the daemon path waits out a holder."""
        with _LockEnvironment():
            holder = command_lock.acquire(
                "gateway restart", holder="cli", idempotency_key="human"
            )

            def _release_soon() -> None:
                holder.finish("succeeded", "done", _ring_record("human"))
                holder.release()

            import threading

            releaser = threading.Timer(1.5, _release_soon)
            releaser.start()
            ticks: list[float] = []
            with (
                patch.object(supervisor, "delete_telegram_webhook"),
                patch.object(
                    supervisor, "start_openclaw_gateway", return_value=1.0
                ),
                patch.object(supervisor, "wait_for_openclaw_start"),
            ):
                result = gateway_restart.run_locked_gateway_restart(
                    {"telegram_bot_token": "x"},
                    holder="daemon",
                    wait_for_lock_seconds=15,
                    on_lock_wait=lambda: ticks.append(time.time()),
                )
            releaser.join()
            self.assertEqual(result.outcome, "succeeded")
            self.assertGreaterEqual(len(ticks), 1)


class OperateRedactionSentinelTests(unittest.TestCase):
    def test_operate_outputs_spool_and_fold_leak_nothing(self) -> None:
        planted = " | ".join(text for _label, text, _secret in _SENTINEL_CORPUS)
        with _LockEnvironment() as env:
            with (
                patch.object(os, "geteuid", return_value=0),
                patch(
                    "tinyhat_cli.adapters.openclaw.configured_telegram_binding",
                    return_value={"telegram_bot_token": "x"},
                ),
                patch.object(supervisor, "delete_telegram_webhook"),
                patch.object(
                    supervisor, "start_openclaw_gateway", return_value=1.0
                ),
                patch.object(
                    supervisor,
                    "wait_for_openclaw_start",
                    side_effect=RuntimeError(planted),
                ),
            ):
                code, out, err = _run_cli(["gateway", "restart", "--json"])
            self.assertEqual(code, 0)
            for _label, _plaintext, secret in _SENTINEL_CORPUS:
                self.assertNotIn(secret, out)
                self.assertNotIn(secret, err)
            # The spool record carries the sanitized summary only.
            spool_blob = ""
            for name in env.spool_files():
                with open(
                    os.path.join(env.results_dir, "spool", name), encoding="utf-8"
                ) as fh:
                    spool_blob += fh.read()
            self.assertTrue(spool_blob)
            leaks = [
                label
                for label, _plaintext, secret in _SENTINEL_CORPUS
                if secret in spool_blob
            ]
            self.assertEqual(leaks, [], f"spool leaked: {leaks}")
            # …and the folded commands[].summary stays clean too.
            with (
                patch.object(supervisor, "_runtime_state_recent_log_excerpts", return_value={"bootstrap": [], "supervisor": [], "gateway": []}),
                patch.object(supervisor, "_plugin_load_check", return_value=None),
                patch.object(supervisor, "_post_runtime_state_to_platform", return_value=False),
            ):
                supervisor._write_runtime_state("healthy", "fold")
            with open(env.state_path, encoding="utf-8") as fh:
                folded = fh.read()
            fold_leaks = [
                label
                for label, _plaintext, secret in _SENTINEL_CORPUS
                if secret in folded
            ]
            self.assertEqual(fold_leaks, [], f"fold leaked: {fold_leaks}")

    def test_operate_sentinel_can_fail(self) -> None:
        """A spool write that bypasses append_result MUST be caught."""
        with _LockEnvironment() as env:
            spool = os.path.join(env.results_dir, "spool")
            os.makedirs(spool, exist_ok=True)
            raw_secret = _SENTINEL_CORPUS[0][1]
            with open(
                os.path.join(spool, "000000000001-bypass.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump({"name": "x", "outcome": "failed", "summary": raw_secret}, fh)
            blob = ""
            for name in env.spool_files():
                with open(os.path.join(spool, name), encoding="utf-8") as fh:
                    blob += fh.read()
            leaks = [
                label
                for label, _plaintext, secret in _SENTINEL_CORPUS
                if secret in blob
            ]
            self.assertEqual(
                leaks,
                ["anthropic-key-bare"],
                "the operate sentinel failed to flag a deliberately raw "
                "spool record — the checker is vacuous",
            )


class WorstCaseSpoolMergeBudgetTests(unittest.TestCase):
    def test_fifty_record_spool_merge_still_caps_the_ring_at_five(self) -> None:
        """The §-budget premise: a full spool can never bloat the ring."""
        with _LockEnvironment():
            for index in range(command_spool.MAX_SPOOL_RECORDS):
                command_spool.append_result(
                    _ring_record(
                        f"k-{index:02d}-{'x' * 32}",
                        finished_at_unix=1_760_000_000 + index,
                        summary="s" * 256,
                    )
                )
            ring, _folded, _fresh = supervisor.fold_command_results({})
            self.assertEqual(len(ring), 5)
            # Five max-summary entries stay a small, bounded slice of the
            # 12,288-byte post budget (the full-payload bound including
            # this ring is proven by test_tinyhat_cli's worst-case
            # fixture; the budgeter's lever 2 trims the ring tail first).
            encoded = len(json.dumps(ring).encode("utf-8"))
            self.assertLessEqual(encoded, 5 * command_spool.MAX_RECORD_BYTES // 3)


if __name__ == "__main__":
    unittest.main()
