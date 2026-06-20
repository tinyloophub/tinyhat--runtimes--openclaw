"""Runtime filesystem paths.

Every default here is stable across bundles. Bundle-specific code should be
reached through ``/opt/tinyhat/current`` so activation and rollback can swap the
target without rewriting systemd units.
"""

from __future__ import annotations

import os
from pathlib import Path


def _path_from_env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _path_from_env_any(names: tuple[str, ...], default: str) -> Path:
    for name in names:
        configured = (os.environ.get(name) or "").strip()
        if configured:
            return Path(configured).expanduser()
    return Path(default).expanduser()


def _dev_runtime_home() -> str | None:
    if (os.environ.get("TINYHAT_DEV_RUNTIME") or "").strip() != "1":
        return None
    configured = (os.environ.get("TINYHAT_RUNTIME_HOME") or "").strip().rstrip("/")
    return configured or None


def _default_openclaw_state_dir() -> str:
    return _dev_runtime_home() or "/var/lib/tinyhat-openclaw"


def _default_openclaw_config_path() -> str:
    runtime_home = _dev_runtime_home()
    if runtime_home:
        return str(Path(runtime_home) / "openclaw" / "openclaw.json")
    return "/etc/openclaw/openclaw.json"


INSTALL_ROOT = _path_from_env("TINYHAT_RUNTIME_INSTALL_ROOT", "/opt/tinyhat")
BUNDLES_DIR = _path_from_env(
    "TINYHAT_RUNTIME_BUNDLES_DIR", str(INSTALL_ROOT / "bundles")
)
CURRENT_LINK = _path_from_env(
    "TINYHAT_RUNTIME_CURRENT_LINK", str(INSTALL_ROOT / "current")
)
STATE_ROOT = _path_from_env("TINYHAT_RUNTIME_STATE_ROOT", "/var/lib/tinyhat/runtime")
CONFIG_ROOT = _path_from_env("TINYHAT_RUNTIME_CONFIG_ROOT", "/etc/tinyhat")
LOG_ROOT = _path_from_env("TINYHAT_RUNTIME_LOG_ROOT", "/var/log/tinyhat")
COMMANDS_LOG_DIR = _path_from_env(
    "TINYHAT_RUNTIME_COMMANDS_LOG_DIR", str(LOG_ROOT / "commands")
)
DIAGNOSTICS_DIR = _path_from_env(
    "TINYHAT_RUNTIME_DIAGNOSTICS_DIR", str(LOG_ROOT / "diagnostics")
)
REBUILD_BACKUP_DIR = _path_from_env(
    "TINYHAT_RUNTIME_REBUILD_BACKUP_DIR", str(STATE_ROOT / "rebuild-backups")
)
IDENTITY_FILE = _path_from_env(
    "TINYHAT_RUNTIME_IDENTITY_FILE", str(STATE_ROOT / "identity.json")
)
ATTESTATION_FILE = _path_from_env(
    "TINYHAT_RUNTIME_ATTESTATION_FILE", str(STATE_ROOT / "attestation.json")
)
OPENCLAW_STATE_DIR = _path_from_env_any(
    ("TINYHAT_OPENCLAW_STATE_DIR", "OPENCLAW_STATE_DIR"),
    _default_openclaw_state_dir(),
)
OPENCLAW_CONFIG_PATH = _path_from_env_any(
    ("TINYHAT_OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG_PATH"),
    _default_openclaw_config_path(),
)
OPENCLAW_SECRETS_PATH = _path_from_env_any(
    (
        "TINYHAT_OPENCLAW_SECRETS_PATH",
        "TINYHAT_SECRETS_PATH",
        "OPENCLAW_SECRETS_PATH",
    ),
    "/etc/openclaw/tinyhat-secrets.json",
)
BUNDLE_OPENCLAW_DIR = _path_from_env(
    "TINYHAT_BUNDLE_OPENCLAW_DIR", str(CURRENT_LINK / "vendor" / "openclaw")
)
BUNDLE_OPENCLAW_BIN = _path_from_env(
    "TINYHAT_BUNDLE_OPENCLAW_BIN", str(BUNDLE_OPENCLAW_DIR / "bin")
)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
