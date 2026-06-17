"""Component-update transaction helpers.

Moved from ``supervisor.py`` to keep the supervisor extraction budget
honest while preserving the old module-level API through delegating
re-exports. Cross-helper calls go through ``supervisor_module()`` so
test patches on ``supervisor.<name>`` still affect these paths.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import subprocess
import time

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

UNIT_CATEGORY = "release-update-lifecycle"


def _tinyhat_plugin_source_override_path() -> str:
    sup = _sup()
    configured = (
        os.environ.get(sup.TINYHAT_PLUGIN_SOURCE_OVERRIDE_PATH_ENV) or ""
    ).strip()
    default_path = os.path.abspath(
        os.path.join(sup.openclaw_state_dir(), "tinyhat-plugin-source.json")
        if sup._dev_mode()
        else sup._DEFAULT_TINYHAT_PLUGIN_SOURCE_OVERRIDE_PATH
    )
    path = os.path.abspath(configured or default_path)
    checkout_dir = os.path.abspath(sup.runtime_dir())
    try:
        inside_checkout = os.path.commonpath([path, checkout_dir]) == checkout_dir
    except ValueError:
        inside_checkout = False
    if inside_checkout:
        log.warning(
            "%s=%s resolves inside the runtime checkout dir (%s); plugin "
            "update source overrides must survive runtime checkouts. Falling "
            "back to %s.",
            sup.TINYHAT_PLUGIN_SOURCE_OVERRIDE_PATH_ENV,
            path,
            checkout_dir,
            default_path,
        )
        return default_path
    return path


def _read_tinyhat_plugin_source_override() -> tuple[str, str] | None:
    payload = _read_tinyhat_plugin_source_override_payload()
    if payload is None:
        return None
    repo_url = str(payload.get("repo_url") or "").strip()
    repo_ref = str(payload.get("repo_ref") or "").strip()
    if not repo_url or not repo_ref:
        return None
    return repo_url, repo_ref


def _read_tinyhat_plugin_source_override_payload() -> dict | None:
    try:
        with open(_tinyhat_plugin_source_override_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _restore_tinyhat_plugin_source_override(payload: dict | None) -> None:
    sup = _sup()
    path = _tinyhat_plugin_source_override_path()
    if payload is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    repo_url = str(payload.get("repo_url") or "").strip()
    repo_ref = str(payload.get("repo_ref") or "").strip()
    if not repo_url or not repo_ref:
        raise RuntimeError("previous plugin source override is invalid")
    sup._atomic_write_json(path, payload, mode=0o600)


def _rollback_plugin_update_transaction(transaction: dict[str, object]) -> str:
    sup = _sup()
    repo_url = str(transaction.get("repo_url") or "").strip()
    repo_ref = str(transaction.get("repo_ref") or "").strip()
    previous_override = transaction.get("previous_override")
    if not repo_url or not repo_ref:
        transaction["rollback_failed"] = True
        return "plugin rollback failed: previous Tinyhat plugin source is missing"
    try:
        sup.ensure_tinyhat_plugin_installed(repo_url=repo_url, repo_ref=repo_ref)
        sup._restore_tinyhat_plugin_source_override(
            previous_override if isinstance(previous_override, dict) else None
        )
        transaction["rolled_back"] = True
        transaction["restored_previous"] = True
        return "plugin rollback restored previous Tinyhat plugin source"
    except Exception as exc:  # noqa: BLE001
        transaction["rollback_failed"] = True
        return f"plugin rollback failed: {exc}"


def _remove_filesystem_entry(path: str) -> None:
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
        return
    os.unlink(path)


def _npm_global_root() -> str:
    sup = _sup()
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("npm root -g timed out") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"npm root -g raised: {exc}") from exc
    if result.returncode != 0:
        detail = sup._sanitize_runtime_state_text(
            (result.stderr or result.stdout or "").strip(),
            limit=300,
        )
        raise RuntimeError(f"npm root -g failed: {detail or result.returncode}")
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    root = lines[-1] if lines else ""
    if not root:
        raise RuntimeError("npm root -g returned an empty path")
    return os.path.abspath(root)


def _framework_package_dir(global_root: str) -> str:
    return os.path.join(global_root, "openclaw")


def _framework_backup_dirs(global_root: str) -> list[str]:
    try:
        names = os.listdir(global_root)
    except OSError as exc:
        raise RuntimeError(f"could not inspect npm global root: {exc}") from exc
    return sorted(
        os.path.join(global_root, name)
        for name in names
        if name.startswith(".tinyhat-openclaw-backup-")
    )


def _cleanup_stale_framework_backup_artifacts(global_root: str) -> list[str]:
    """Remove non-repair backup artifacts from prior framework updates."""
    sup = _sup()
    removed: list[str] = []
    try:
        names = os.listdir(global_root)
    except OSError as exc:
        raise RuntimeError(f"could not inspect npm global root: {exc}") from exc
    for name in sorted(names):
        if not (
            name.startswith(".tinyhat-openclaw-committed-backup-")
            or name.startswith(".tinyhat-openclaw-copying-")
        ):
            continue
        path = os.path.join(global_root, name)
        try:
            sup._remove_filesystem_entry(path)
        except Exception as exc:  # noqa: BLE001 - stale cleanup is best effort
            log.warning(
                "component update: could not remove stale framework backup "
                "artifact %s: %s",
                path,
                exc,
            )
            continue
        removed.append(name)
    if removed:
        log.warning(
            "component update: removed stale framework backup artifacts: %s",
            ", ".join(removed[:10]),
        )
    return removed


def _repair_or_cleanup_framework_backups(global_root: str) -> list[str]:
    """Repair an interrupted Tinyhat-owned framework swap when possible."""
    sup = _sup()
    package_dir = sup._framework_package_dir(global_root)
    backups = sup._framework_backup_dirs(global_root)
    actions: list[str] = []
    restored: str | None = None
    if backups:
        restored = backups[-1]
        if os.path.lexists(package_dir):
            sup._remove_filesystem_entry(package_dir)
        os.replace(restored, package_dir)
        actions.append(f"restored {os.path.basename(restored)}")
    for backup in backups:
        if backup == restored:
            continue
        sup._remove_filesystem_entry(backup)
        actions.append(f"removed stale {os.path.basename(backup)}")
    return actions


def _cleanup_stale_openclaw_npm_temp_dirs(global_root: str) -> list[str]:
    """Remove npm's stale .openclaw-* dirs that cause ENOTEMPTY retries."""
    sup = _sup()
    removed: list[str] = []
    try:
        names = os.listdir(global_root)
    except OSError as exc:
        raise RuntimeError(f"could not inspect npm global root: {exc}") from exc
    for name in sorted(names):
        if not name.startswith(".openclaw-"):
            continue
        sup._remove_filesystem_entry(os.path.join(global_root, name))
        removed.append(name)
    if removed:
        log.warning(
            "component update: removed stale OpenClaw npm temp dirs: %s",
            ", ".join(removed[:10]),
        )
    return removed


def _move_framework_package_to_backup(package_dir: str, backup_dir: str) -> None:
    """Move the current OpenClaw tree into a repair-discoverable backup.

    Docker Desktop overlay/fakeowner paths can raise EXDEV for a directory
    rename even when the source and destination are under the same apparent
    parent. The fallback only publishes a repairable backup after the copy is
    complete; an interrupted copy leaves a non-discoverable scratch directory.
    """
    try:
        os.replace(package_dir, backup_dir)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    sup = _sup()
    parent, backup_name = os.path.split(backup_dir)
    backup_prefix = ".tinyhat-openclaw-backup-"
    scratch_suffix = (
        backup_name[len(backup_prefix) :]
        if backup_name.startswith(backup_prefix)
        else backup_name
    )
    scratch_dir = os.path.join(parent, f".tinyhat-openclaw-copying-{scratch_suffix}")
    try:
        if os.path.lexists(scratch_dir):
            sup._remove_filesystem_entry(scratch_dir)
        shutil.copytree(package_dir, scratch_dir, symlinks=True)
        os.replace(scratch_dir, backup_dir)
        sup._remove_filesystem_entry(package_dir)
    except Exception:
        if os.path.lexists(scratch_dir):
            try:
                sup._remove_filesystem_entry(scratch_dir)
            except Exception:  # noqa: BLE001 - preserve original failure
                pass
        raise


def _rollback_framework_install_transaction(transaction: dict[str, object]) -> str:
    """Restore the pre-update OpenClaw package tree after a failed smoke."""
    sup = _sup()
    package_dir = str(transaction.get("package_dir") or "")
    backup_dir = str(transaction.get("backup_dir") or "")
    try:
        if package_dir:
            sup._remove_filesystem_entry(package_dir)
        if backup_dir and os.path.lexists(backup_dir):
            os.replace(backup_dir, package_dir)
            transaction["rolled_back"] = True
            transaction["restored_previous"] = True
            return "framework rollback restored previous OpenClaw package tree"
        transaction["rolled_back"] = True
        transaction["restored_previous"] = False
        return "framework rollback removed partial OpenClaw package tree"
    except Exception as exc:  # noqa: BLE001
        transaction["rollback_failed"] = True
        return f"framework rollback failed: {exc}"


def _clean_framework_install_retry_artifacts(
    *,
    global_root: str,
    package_dir: str,
) -> None:
    """Remove only disposable OpenClaw npm install artifacts before retrying.

    The user's OpenClaw state, auth profiles, memory/workspace, and Tinyhat
    credentials live under the configured OpenClaw state/config directories,
    not in npm's global ``openclaw`` package tree. A failed framework install
    may leave that package tree internally inconsistent, so retries must start
    from a clean package path without touching user data.
    """
    sup = _sup()
    if os.path.lexists(package_dir):
        sup._remove_filesystem_entry(package_dir)
    sup._cleanup_stale_openclaw_npm_temp_dirs(global_root)
    try:
        subprocess.run(
            ["npm", "cache", "clean", "--force"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - cache cleanup is best effort
        log.warning("component update: npm cache clean failed before retry: %s", exc)


def _committed_framework_backup_dir(backup_dir: str) -> str:
    parent, name = os.path.split(backup_dir)
    prefix = ".tinyhat-openclaw-backup-"
    if not name.startswith(prefix):
        return backup_dir + ".committed"
    return os.path.join(
        parent,
        ".tinyhat-openclaw-committed-backup-" + name[len(prefix) :],
    )


def _commit_framework_install_transaction(transaction: dict[str, object]) -> None:
    """Discard the saved tree only after the gateway smoke has passed."""
    sup = _sup()

    def mark_committed() -> None:
        transaction["committed"] = True
        sup._openclaw_version_cache = False

    backup_dir = str(transaction.get("backup_dir") or "")
    if not backup_dir:
        mark_committed()
        return
    try:
        if os.path.lexists(backup_dir):
            committed_dir = _committed_framework_backup_dir(backup_dir)
            if committed_dir != backup_dir:
                os.replace(backup_dir, committed_dir)
                backup_dir = committed_dir
                transaction["backup_dir"] = committed_dir
        sup._remove_filesystem_entry(backup_dir)
        mark_committed()
    except Exception as exc:  # noqa: BLE001 - stale backup is non-fatal
        mark_committed()
        transaction["commit_cleanup_failed"] = True
        log.warning(
            "component update: could not remove framework install backup %s: %s",
            backup_dir,
            exc,
        )


def _prepare_framework_install_transaction(version: str) -> dict[str, object]:
    sup = _sup()
    global_root = sup._npm_global_root()
    _cleanup_stale_framework_backup_artifacts(global_root)
    sup._repair_or_cleanup_framework_backups(global_root)
    sup._cleanup_stale_openclaw_npm_temp_dirs(global_root)

    package_dir = sup._framework_package_dir(global_root)
    backup_dir = ""
    if os.path.lexists(package_dir):
        backup_dir = os.path.join(
            global_root,
            f".tinyhat-openclaw-backup-{int(time.time())}-{os.getpid()}",
        )
        _move_framework_package_to_backup(package_dir, backup_dir)

    transaction: dict[str, object] = {
        "global_root": global_root,
        "package_dir": package_dir,
        "backup_dir": backup_dir,
        "target_version": version,
        "committed": False,
        "rolled_back": False,
        "restored_previous": False,
    }
    last_error = ""
    last_exception: BaseException | None = None
    for attempt in range(1, 3):
        last_exception = None
        try:
            install = subprocess.run(
                [
                    "npm",
                    "install",
                    "-g",
                    "--no-fund",
                    "--no-audit",
                    f"openclaw@{version}",
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            last_exception = exc
            last_error = f"framework npm install of openclaw@{version} timed out"
        except Exception as exc:  # noqa: BLE001
            last_exception = exc
            last_error = f"framework npm install raised: {exc}"
        else:
            if install.returncode == 0:
                installed = sup._read_openclaw_framework_version()
                if installed == version:
                    return transaction
                last_error = (
                    "framework version mismatch after install: "
                    f"wanted {version}, got {installed or 'unknown'}"
                )
            else:
                detail = sup._sanitize_runtime_state_text(
                    (install.stderr or install.stdout or "").strip(),
                    limit=500,
                )
                last_error = (
                    f"framework npm install failed: {detail or install.returncode}"
                )
        if attempt < 2:
            log.warning(
                "component update: %s; retrying clean OpenClaw framework install",
                last_error,
            )
            _clean_framework_install_retry_artifacts(
                global_root=global_root,
                package_dir=package_dir,
            )
            continue

    rollback = sup._rollback_framework_install_transaction(transaction)
    message = f"{last_error}; {rollback}"
    if last_exception is not None:
        raise RuntimeError(message) from last_exception
    raise RuntimeError(message)
