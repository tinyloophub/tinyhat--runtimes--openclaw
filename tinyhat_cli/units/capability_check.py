"""Capability-check unit — "installed" is not "loaded".

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``).
OpenClaw skips an enabled extension it cannot read or import WITHOUT
failing the gateway, and ``openclaw plugins inspect`` reports
registration, not loadability. The plugin ships a load beacon since
v0.5.0: when its extension module evaluates successfully it writes
``tinyhat-plugin-loaded.json`` (plugin, version, loaded_at, pid, node)
into the OpenClaw state dir. This unit reads that beacon to tell when
an enabled plugin never actually loaded — the silent capability loss
behind the v0.11.13 ownership regression.

Consumed by the daemon's runtime-state projection (health demotion)
and by ``tinyhat health`` (live re-check) — one shared path.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

TINYHAT_PLUGIN_BEACON_FILENAME = "tinyhat-plugin-loaded.json"
# First plugin version that writes the beacon. Older plugins cannot be
# distinguished from a load failure, so they report "unknown" instead of
# degrading runtime health.
TINYHAT_PLUGIN_BEACON_MIN_VERSION = (0, 5, 0)
# How long an enabled, beacon-capable plugin may stay beacon-less after
# the gateway is active before the runtime reports it as not loaded.
# Generous against slow extension startup; tiny against the hours/days a
# real silent loss would otherwise persist.
PLUGIN_LOAD_GRACE_SECONDS = 180
_PLUGIN_BEACON_MAX_BYTES = 8192


def tinyhat_plugin_beacon_path() -> str:
    """Where the plugin's load beacon lands (gateway-written)."""
    sup = _sup()
    return os.path.join(sup.openclaw_state_dir(), sup.TINYHAT_PLUGIN_BEACON_FILENAME)


def _parse_plugin_version(value: Any) -> tuple[int, ...] | None:
    """Parse ``0.5.0``-style versions; ``None`` for anything else."""
    text = str(value or "").strip()
    if not text:
        return None
    parts: list[int] = []
    for segment in text.split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _read_plugin_load_beacon() -> dict | None:
    """Read the beacon file; ``None`` when missing/oversized/invalid."""
    sup = _sup()
    path = sup.tinyhat_plugin_beacon_path()
    try:
        if os.path.getsize(path) > sup._PLUGIN_BEACON_MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _plugin_load_check(
    existing_state: dict[str, Any],
    *,
    gateway_active: bool | None,
    now: int,
) -> dict[str, Any] | None:
    """Classify whether the enabled Tinyhat plugin actually loaded.

    Returns ``None`` when no plugin is installed (nothing to check).
    Otherwise a payload block::

        {"installed_version", "load_check", "reason"?,
         "beacon_loaded_at"?, "missing_since_unix"?}

    ``load_check`` values:

    - ``loaded`` — a beacon for the installed plugin version exists;
    - ``not_loaded`` — beacon-capable plugin, gateway active, and no
      matching beacon for longer than the grace window (degrades
      runtime health to ``unsupported_openclaw_version``);
    - ``pending`` — same, but still inside the grace window;
    - ``unknown`` — the check cannot conclude (pre-beacon plugin
      version, gateway not active) and must never degrade health.

    Known limitation (documented, accepted): the beacon is matched by
    plugin version, not gateway start time, so a plugin that loaded
    once and silently broke mid-life without a version change is not
    detected until the next plugin update or reprovision. The incident
    class this targets breaks at boot/install time.
    """
    sup = _sup()
    marker = sup._read_installed_plugin_marker()
    installed_version = str(marker.get("version") or "").strip()
    if not installed_version:
        return None
    check: dict[str, Any] = {"installed_version": installed_version}
    # Positive evidence first: a beacon matching the installed version is
    # definitive proof of load, whatever the version metadata says (a
    # pre-release build of a beacon-capable plugin can still carry an
    # older version string).
    beacon = sup._read_plugin_load_beacon()
    if (
        isinstance(beacon, dict)
        and str(beacon.get("version") or "").strip() == installed_version
    ):
        check["load_check"] = "loaded"
        loaded_at = str(beacon.get("loaded_at") or "").strip()
        if loaded_at:
            check["beacon_loaded_at"] = loaded_at
        return check
    # Absence is only meaningful for plugin versions that are known to
    # write the beacon; older plugins cannot be distinguished from a
    # load failure and must never degrade health.
    parsed = sup._parse_plugin_version(installed_version)
    if parsed is None or parsed < sup.TINYHAT_PLUGIN_BEACON_MIN_VERSION:
        check["load_check"] = "unknown"
        check["reason"] = "plugin_predates_load_beacon"
        return check
    check["reason"] = (
        "beacon_version_mismatch" if isinstance(beacon, dict) else "beacon_missing"
    )
    if gateway_active is not True:
        # The plugin cannot have loaded into a gateway that is not
        # running; report unknown rather than start the clock.
        check["load_check"] = "unknown"
        return check
    # The missing-beacon clock is scoped to the installed version: a
    # plugin install/update must get its own grace window instead of
    # inheriting a stale verdict from the previous version. The reason
    # may flip between beacon_missing and beacon_version_mismatch for
    # the same unloaded install; both mean "this version has not
    # loaded", so they share one clock.
    prior = existing_state.get("plugin")
    prior_version = (
        str(prior.get("installed_version") or "").strip()
        if isinstance(prior, dict)
        else ""
    )
    missing_since = (
        prior.get("missing_since_unix")
        if isinstance(prior, dict) and prior_version == installed_version
        else None
    )
    if not isinstance(missing_since, int):
        missing_since = now
    check["missing_since_unix"] = missing_since
    if now - missing_since >= sup.PLUGIN_LOAD_GRACE_SECONDS:
        check["load_check"] = "not_loaded"
    else:
        check["load_check"] = "pending"
    return check
