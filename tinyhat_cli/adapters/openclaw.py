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
import os
import re
import subprocess

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

_OPENCLAW_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)")
OPENCLAW_SQLITE_AUTH_STORE_MIN_VERSION = (2026, 6, 6)
OPENCLAW_AUTH_STORE_MIGRATION_TIMEOUT_SECONDS = 120


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
            **sup._runtime_user_subprocess_kwargs(),
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


def _parse_openclaw_version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"\bOpenClaw\s+(\d{4})\.(\d{1,2})\.(\d{1,2})\b", text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _current_openclaw_version_tuple() -> tuple[int, int, int] | None:
    sup = _sup()
    if sup._openclaw_version_cache is not False:
        return sup._openclaw_version_cache
    try:
        result = subprocess.run(
            ["openclaw", "--version"],
            capture_output=True,
            text=True,
            timeout=sup.OPENCLAW_CLI_VERSION_TIMEOUT_SECONDS,
            env=sup._openclaw_cli_env(),
        )
    except Exception as exc:  # noqa: BLE001 - version only gates compatibility
        log.warning("OpenClaw version check for auth migration failed: %s", exc)
        sup._openclaw_version_cache = None
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        log.warning(
            "OpenClaw version check for auth migration exited %s: %s",
            result.returncode,
            sup._sanitize_runtime_state_text(detail, limit=255),
        )
        sup._openclaw_version_cache = None
        return None
    parsed = sup._parse_openclaw_version_tuple(result.stdout or result.stderr or "")
    sup._openclaw_version_cache = parsed
    return parsed


def _current_openclaw_requires_sqlite_auth_store() -> bool:
    sup = _sup()
    version = sup._current_openclaw_version_tuple()
    return bool(version and version >= sup.OPENCLAW_SQLITE_AUTH_STORE_MIN_VERSION)


def _legacy_auth_store_paths(
    *, agent_id: str | None = None
) -> list[str]:
    sup = _sup()
    resolved_agent_id = agent_id or sup.DEFAULT_OPENCLAW_AGENT_ID
    state_dir = sup.openclaw_state_dir()
    candidates = [
        sup.openclaw_auth_profiles_path(agent_id=resolved_agent_id),
        os.path.join(
            sup.openclaw_agent_state_dir(agent_id=resolved_agent_id),
            "auth-state.json",
        ),
        os.path.join(state_dir, "auth-profiles.json"),
    ]
    out: list[str] = []
    for path in candidates:
        if path not in out:
            out.append(path)
    return out


def _has_legacy_auth_store(
    *, agent_id: str | None = None
) -> bool:
    sup = _sup()
    resolved_agent_id = agent_id or sup.DEFAULT_OPENCLAW_AGENT_ID
    return any(
        os.path.exists(path)
        for path in sup._legacy_auth_store_paths(agent_id=resolved_agent_id)
    )


def repair_openclaw_auth_store_for_upgrade(
    *,
    agent_id: str | None = None,
    force: bool = False,
) -> bool:
    """Let OpenClaw migrate legacy auth JSON into its canonical store."""
    sup = _sup()
    resolved_agent_id = agent_id or sup.DEFAULT_OPENCLAW_AGENT_ID
    if not force:
        if not sup._has_legacy_auth_store(agent_id=resolved_agent_id):
            return False
        if not sup._current_openclaw_requires_sqlite_auth_store():
            return False
    key = sup.openclaw_agent_state_dir(agent_id=resolved_agent_id)
    if not force and key in sup._auth_store_migration_attempted:
        return False
    if not force:
        sup._auth_store_migration_attempted.add(key)

    log.info(
        "running OpenClaw auth-store migration doctor for agent %s",
        resolved_agent_id,
    )
    try:
        result = subprocess.run(
            [
                "openclaw",
                "doctor",
                "--fix",
                "--non-interactive",
                "--yes",
            ],
            capture_output=True,
            text=True,
            timeout=sup.OPENCLAW_AUTH_STORE_MIGRATION_TIMEOUT_SECONDS,
            env=sup._openclaw_cli_env(),
            **sup._runtime_user_subprocess_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001 - fail closed below
        log.warning(
            "OpenClaw auth-store migration doctor failed to run: %s",
            sup._sanitize_runtime_state_text(str(exc), limit=255),
        )
        return False
    sup._prepare_openclaw_agent_auth_store_ownership(agent_id=resolved_agent_id)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        log.warning(
            "OpenClaw auth-store migration doctor exited %s: %s",
            result.returncode,
            sup._sanitize_runtime_state_text(detail, limit=255),
        )
        return False
    log.info("OpenClaw auth-store migration doctor completed")
    return True


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


def openclaw_plugin_registry_entry(plugin_id: str) -> tuple[dict | None, str | None]:
    """The framework registry's view of one plugin, with an honest miss reason.

    Unlike :func:`_inspect_openclaw_plugin` (the install-time gate, which
    collapses every failure to ``None``), the capability verification must
    distinguish *the registry answered "not registered"* (a real shortfall
    signal) from *the registry could not be asked* (fall back to the
    self-check mechanism instead of inventing a verdict).

    Returns ``(entry, None)`` on success, ``(None, "not_registered")``
    when OpenClaw answers but the plugin is absent/unparseable, and
    ``(None, "cli_unavailable")`` when the CLI cannot be executed.
    """
    sup = _sup()
    try:
        result = subprocess.run(
            ["openclaw", "plugins", "inspect", plugin_id, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=sup._openclaw_cli_env(),
            **sup._runtime_user_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("openclaw registry inspect unavailable for %s: %s", plugin_id, exc)
        return None, "cli_unavailable"
    if result.returncode != 0:
        return None, "not_registered"
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, "not_registered"
    plugin = sup._openclaw_plugin_from_inspect_payload(plugin_id, payload)
    if plugin is None:
        return None, "not_registered"
    return plugin, None


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
            **sup._runtime_user_subprocess_kwargs(),
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


def configured_telegram_binding() -> dict | None:
    """The Telegram binding facts the box's OpenClaw config holds.

    The gateway-restart transaction needs the bot token to clear the
    Telegram webhook before OpenClaw long-polls; the daemon gets it
    from its live platform binding, but the CLI runs without one. The
    deployed OpenClaw config is the on-box source of those facts, and
    config-path access belongs to this adapter. Returns ``None`` when
    no config exists (an unbound box restarts without the webhook leg).
    """
    sup = _sup()
    try:
        with open(sup.openclaw_config_path(), encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    channels = config.get("channels")
    telegram = channels.get("telegram") if isinstance(channels, dict) else None
    if not isinstance(telegram, dict):
        return None
    token = str(telegram.get("botToken") or "").strip()
    allow_from = telegram.get("allowFrom")
    owner = ""
    if isinstance(allow_from, list) and allow_from:
        owner = str(allow_from[0] or "").strip()
    if not token:
        return None
    return {
        "telegram_bot_token": token,
        "telegram_owner_user_id": owner or None,
    }
