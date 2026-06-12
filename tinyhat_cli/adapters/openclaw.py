"""The single OpenClaw adapter boundary (required reads).

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``).
Everything the extracted units need from the OpenClaw framework — the
installed version and the plugin/provider registry inspection — goes
through this module and nothing else in the ``tinyhat_cli`` package
touches the ``openclaw`` binary, its config path, or its process
environment. ``tests/test_extraction_guards.py`` enforces that
boundary for the package.

Legacy callsites still inside ``supervisor.py`` (gateway start,
plugin install, secrets reload) are NOT part of this slice's boundary;
they migrate in later strangling steps.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

_OPENCLAW_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)")


def _openclaw_cli_env() -> dict[str, str]:
    import os

    sup = _sup()
    return {
        **os.environ,
        "HOME": sup.openclaw_state_dir(),
        "OPENCLAW_CONFIG_PATH": sup.openclaw_config_path(),
        "OPENCLAW_STATE_DIR": sup.openclaw_state_dir(),
    }


def _read_openclaw_framework_version() -> str:
    """Installed OpenClaw (framework) version via ``openclaw --version``.

    Best-effort: returns ``""`` when the CLI is absent or errors. The
    framework is an npm package with no git checkout, so its component
    sha is always ``None``.
    """
    sup = _sup()
    try:
        result = subprocess.run(
            ["openclaw", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=sup._openclaw_cli_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    output = (result.stdout or result.stderr or "").strip()
    match = _OPENCLAW_VERSION_RE.search(output)
    if match:
        return match.group(1)
    return output.splitlines()[0].strip() if output else ""


def _openclaw_plugin_from_inspect_payload(plugin_id: str, payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    plugin = payload.get("plugin")
    if not isinstance(plugin, dict):
        plugin = payload
    resolved_id = str(plugin.get("id") or plugin.get("pluginId") or "").strip()
    if resolved_id != plugin_id:
        return None
    return plugin


def _inspect_openclaw_plugin(plugin_id: str) -> dict | None:
    """Return OpenClaw's plugin-registry entry, or None when missing/broken."""
    sup = _sup()
    try:
        result = subprocess.run(
            ["openclaw", "plugins", "inspect", plugin_id, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=sup._openclaw_cli_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not inspect OpenClaw plugin %s: %s", plugin_id, exc)
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        log.warning(
            "OpenClaw plugin %s is not registered: %s",
            plugin_id,
            detail[:500] if detail else f"openclaw exited {result.returncode}",
        )
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        log.warning("OpenClaw plugin %s inspect returned invalid JSON: %s", plugin_id, exc)
        return None
    plugin = sup._openclaw_plugin_from_inspect_payload(plugin_id, payload)
    if plugin is None:
        log.warning(
            "OpenClaw plugin inspect returned unexpected payload for %s",
            plugin_id,
        )
        return None
    dependency_status = plugin.get("dependencyStatus")
    if (
        isinstance(dependency_status, dict)
        and dependency_status.get("requiredInstalled") is False
    ):
        log.warning("OpenClaw plugin %s has missing dependencies", plugin_id)
        return None
    return plugin
