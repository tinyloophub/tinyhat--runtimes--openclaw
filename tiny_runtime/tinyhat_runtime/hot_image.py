"""Bake-time helpers for hot tiny_runtime images."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from . import openclaw_adapter, paths

CODEX_PLUGIN_ID = "codex"
CODEX_PLUGIN_PACKAGE = os.environ.get("CODEX_SUBSCRIPTION_PLUGIN_PACKAGE", "@openclaw/codex")
TINYHAT_PLUGIN_ID = "tinyhat"
TINYHAT_PLUGIN_REPO_URL = os.environ.get(
    "TINYHAT_PLATFORM_PLUGIN_REPO_URL",
    "https://github.com/tinyhat-ai/tinyhat.git",
)
TINYHAT_PLUGIN_REPO_REF = os.environ.get("TINYHAT_PLATFORM_PLUGIN_REPO_REF", "main")


def _run(args: Sequence[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{args[0]} command failed: {detail}")
    return (result.stdout or "").strip()


def _try_run(args: Sequence[str], *, cwd: Path | None = None) -> str | None:
    try:
        return _run(args, cwd=cwd)
    except RuntimeError:
        return None


def _plugin_checkout_dir() -> Path:
    configured = os.environ.get("TINYHAT_PLUGIN_CHECKOUT_DIR")
    if configured:
        return Path(configured)
    return paths.OPENCLAW_STATE_DIR / "platform-plugins" / TINYHAT_PLUGIN_ID


def _checkout_tinyhat_plugin() -> tuple[Path, str]:
    checkout = _plugin_checkout_dir()
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if (checkout / ".git").exists():
        _run(["git", "fetch", "--tags", "--prune", "origin"], cwd=checkout)
    else:
        if checkout.exists():
            raise RuntimeError(f"plugin checkout exists but is not a git repo: {checkout}")
        _run(["git", "clone", TINYHAT_PLUGIN_REPO_URL, str(checkout)])
    _run(["git", "checkout", TINYHAT_PLUGIN_REPO_REF], cwd=checkout)
    _fast_forward_remote_branch_ref(checkout)
    resolved_sha = _run(["git", "rev-parse", "HEAD"], cwd=checkout)
    return checkout, resolved_sha


def _fast_forward_remote_branch_ref(checkout: Path) -> None:
    remote_ref = f"origin/{TINYHAT_PLUGIN_REPO_REF}"
    resolved_remote = _try_run(
        ["git", "rev-parse", "--verify", f"{remote_ref}^{{commit}}"],
        cwd=checkout,
    )
    if resolved_remote:
        _run(["git", "reset", "--hard", remote_ref], cwd=checkout)


def _plugin_version(checkout: Path) -> str:
    package_json = checkout / "package.json"
    if not package_json.exists():
        return "unknown"
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unknown"
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version or "unknown")


def _write_tinyhat_marker(*, checkout: Path, resolved_sha: str) -> None:
    marker = paths.OPENCLAW_STATE_DIR / "tinyhat-plugin.version"
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": "tinyhat_plugin_install_marker_v1",
        "plugin_id": TINYHAT_PLUGIN_ID,
        "repo_url": TINYHAT_PLUGIN_REPO_URL,
        "repo_ref": TINYHAT_PLUGIN_REPO_REF,
        "resolved_commit_sha": resolved_sha,
        "version": _plugin_version(checkout),
        "checkout_dir": str(checkout),
    }
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def reown_plugin_trees_to_root(
    runner: Sequence | None = None,
) -> dict[str, Any]:
    """Re-own the installed plugin trees to root, world-readable.

    OpenClaw 2026.6.9+ BLOCKS any plugin whose install path is not owned by root
    (an anti-tamper check). The runtime owns the OpenClaw state dir as the
    unprivileged gateway user, so the codex npm project and the tinyhat extension
    end up uid!=0 and get blocked — ``inspect_plugin`` then reports the plugin as
    "not found", the codex preinstall gate fails, and the box bricks mid-upgrade
    (the #112 incident). Re-owning the plugin trees to root with ``a+rX`` keeps
    them loadable by the gateway while satisfying OpenClaw's ownership check.

    Best-effort: only root can chown to root, which the bootstrap is; in non-root
    dev/test it is a harmless no-op.
    """
    run = runner if callable(runner) else subprocess.run
    state = paths.OPENCLAW_STATE_DIR
    trees = [
        state / "extensions",
        state / "platform-plugins",
        state / "npm" / "projects",
    ]
    results: dict[str, Any] = {}
    for tree in trees:
        if not tree.exists():
            continue
        try:
            run(
                ["chown", "-R", "root:root", str(tree)],
                capture_output=True,
                text=True,
                check=False,
            )
            run(
                ["chmod", "-R", "a+rX", str(tree)],
                capture_output=True,
                text=True,
                check=False,
            )
            results[tree.name] = "reowned_root"
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal
            results[tree.name] = f"skipped: {exc}"
    return results


def preinstall_hot_image_plugins() -> dict[str, Any]:
    codex_install = openclaw_adapter.install_plugin(CODEX_PLUGIN_PACKAGE)
    if codex_install.get("state") != "ready":
        raise RuntimeError(f"Codex plugin install failed: {codex_install}")
    # Re-own to root BEFORE the inspect gate: OpenClaw blocks non-root-owned
    # plugins, which would otherwise surface here as "unavailable" and brick the
    # box (the #112 codex-ownership incident).
    reown_plugin_trees_to_root()
    codex_inspect = openclaw_adapter.inspect_plugin(CODEX_PLUGIN_ID)
    if codex_inspect.get("state") != "ready":
        raise RuntimeError(f"Codex plugin unavailable after install: {codex_inspect}")

    checkout, resolved_sha = _checkout_tinyhat_plugin()
    tinyhat_install = openclaw_adapter.install_plugin(str(checkout))
    if tinyhat_install.get("state") != "ready":
        raise RuntimeError(f"Tinyhat plugin install failed: {tinyhat_install}")
    reown_plugin_trees_to_root()
    tinyhat_inspect = openclaw_adapter.inspect_plugin(TINYHAT_PLUGIN_ID)
    if tinyhat_inspect.get("state") != "ready":
        raise RuntimeError(f"Tinyhat plugin unavailable after install: {tinyhat_inspect}")
    warm_config = openclaw_adapter.apply_warm_image_config()
    if warm_config.get("state") != "ready":
        raise RuntimeError(f"Warm OpenClaw config failed: {warm_config}")
    _write_tinyhat_marker(checkout=checkout, resolved_sha=resolved_sha)
    return {
        "codex": codex_inspect,
        "tinyhat": {
            "inspect": tinyhat_inspect,
            "resolved_commit_sha": resolved_sha,
            "checkout_dir": str(checkout),
        },
        "warm_config": warm_config,
    }
