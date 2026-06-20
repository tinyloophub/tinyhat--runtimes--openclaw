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
OPENCLAW_STATE_DIR = _path_from_env(
    "TINYHAT_OPENCLAW_STATE_DIR", "/var/lib/tinyhat-openclaw"
)
OPENCLAW_CONFIG_PATH = _path_from_env(
    "TINYHAT_OPENCLAW_CONFIG_PATH", "/etc/openclaw/openclaw.json"
)
OPENCLAW_SECRETS_PATH = _path_from_env(
    "TINYHAT_OPENCLAW_SECRETS_PATH", "/etc/openclaw/tinyhat-secrets.json"
)
BUNDLE_OPENCLAW_DIR = _path_from_env(
    "TINYHAT_BUNDLE_OPENCLAW_DIR", str(CURRENT_LINK / "vendor" / "openclaw")
)
BUNDLE_OPENCLAW_BIN = _path_from_env(
    "TINYHAT_BUNDLE_OPENCLAW_BIN", str(BUNDLE_OPENCLAW_DIR / "bin")
)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
