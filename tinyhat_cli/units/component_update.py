"""Component-update transaction helpers.

Moved from ``supervisor.py`` to keep the supervisor extraction budget
honest while preserving the old module-level API through delegating
re-exports. Cross-helper calls go through ``supervisor_module()`` so
test patches on ``supervisor.<name>`` still affect these paths.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")


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


def _commit_framework_install_transaction(transaction: dict[str, object]) -> None:
    """Discard the saved tree only after the gateway smoke has passed."""
    sup = _sup()
    backup_dir = str(transaction.get("backup_dir") or "")
    if not backup_dir:
        transaction["committed"] = True
        return
    try:
        sup._remove_filesystem_entry(backup_dir)
        transaction["committed"] = True
    except Exception as exc:  # noqa: BLE001 - stale backup is non-fatal
        log.warning(
            "component update: could not remove framework install backup %s: %s",
            backup_dir,
            exc,
        )


def _prepare_framework_install_transaction(version: str) -> dict[str, object]:
    sup = _sup()
    global_root = sup._npm_global_root()
    sup._repair_or_cleanup_framework_backups(global_root)
    sup._cleanup_stale_openclaw_npm_temp_dirs(global_root)

    package_dir = sup._framework_package_dir(global_root)
    backup_dir = ""
    if os.path.lexists(package_dir):
        backup_dir = os.path.join(
            global_root,
            f".tinyhat-openclaw-backup-{int(time.time())}-{os.getpid()}",
        )
        os.replace(package_dir, backup_dir)

    transaction: dict[str, object] = {
        "global_root": global_root,
        "package_dir": package_dir,
        "backup_dir": backup_dir,
        "target_version": version,
        "committed": False,
        "rolled_back": False,
        "restored_previous": False,
    }
    try:
        install = subprocess.run(
            ["npm", "install", "-g", "--no-fund", "--no-audit", f"openclaw@{version}"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        rollback = sup._rollback_framework_install_transaction(transaction)
        raise RuntimeError(
            f"framework npm install of openclaw@{version} timed out; {rollback}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        rollback = sup._rollback_framework_install_transaction(transaction)
        raise RuntimeError(f"framework npm install raised: {exc}; {rollback}") from exc
    if install.returncode != 0:
        detail = sup._sanitize_runtime_state_text(
            (install.stderr or install.stdout or "").strip(),
            limit=500,
        )
        rollback = sup._rollback_framework_install_transaction(transaction)
        raise RuntimeError(
            f"framework npm install failed: {detail or install.returncode}; {rollback}"
        )
    installed = sup._read_openclaw_framework_version()
    if installed != version:
        rollback = sup._rollback_framework_install_transaction(transaction)
        raise RuntimeError(
            "framework version mismatch after install: "
            f"wanted {version}, got {installed or 'unknown'}; {rollback}"
        )
    return transaction
