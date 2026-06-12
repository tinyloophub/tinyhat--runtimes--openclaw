"""Capability-check unit — "installed" is not "loaded", "loaded" is not "complete".

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``).
OpenClaw skips an enabled extension it cannot read or import WITHOUT
failing the gateway, and ``openclaw plugins inspect`` reports
registration, not loadability. The plugin ships a load beacon since
v0.5.0: when its extension module evaluates successfully it writes
``tinyhat-plugin-loaded.json`` (plugin, version, loaded_at, pid, node)
into the OpenClaw state dir. This unit reads that beacon to tell when
an enabled plugin never actually loaded — the silent capability loss
behind the v0.11.13 ownership regression.

The v0.12.0 M3 extension adds the **declared-vs-registered capability
verification**: the installed plugin's manifest declares its tools,
skills, and supported framework range (``contracts.tools`` /
``contracts.skills`` / ``contracts.framework``), and
:func:`capability_verification` compares that declaration against the
framework registry (``mechanism: "inspect"``, the M0-ratified primary)
or, when the registry cannot be asked, against the load beacon
(``mechanism: "self_check"`` — never inventing missing names). The
result is the additive ``capabilities`` block of ``runtime_state_v1``::

    {declared_tools, registered_tools, declared_skills, mounted_skills,
     missing: [<=10 names], missing_truncated, checked_at_unix,
     mechanism: "inspect"|"self_check",
     status: "ok"|"shortfall"|"unverifiable"}

``unverifiable`` is reserved for a plugin with no declared manifest to
check (pre-manifest versions). Any shortfall maps to degraded health
through :func:`tinyhat_cli.units.runtime_state.capability_demotion` —
never silent ``healthy``.

Consumed by the daemon's runtime-state projection (health demotion)
and by ``tinyhat health`` (live re-check) — one shared path.
"""

from __future__ import annotations

import json
import logging
import os
import stat as stat_module
import time
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

UNIT_CATEGORY = "framework-compatibility"

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


# ── v0.12.0 M3: declared-vs-registered capability verification ────────

# Re-verify at most this often on the daemon's write path; a fresh
# gateway start (the lifecycle ``gateway_ready_at_unix`` mark) or a
# plugin version change always invalidates the cache.
CAPABILITY_VERIFICATION_TTL_SECONDS = 300
# The runtime_state_v1 contract caps the missing-name list; the payload
# budgeter may trim it further (setting missing_truncated).
CAPABILITIES_MISSING_MAX_NAMES = 10
_CAPABILITY_MANIFEST_MAX_BYTES = 262144

_capability_verification_cache: dict[str, Any] = {
    "capabilities": None,
    "framework": None,
    "checked_at": 0,
    "plugin_version": None,
    "gateway_ready_generation": 0,
}

# Updating gateway-readiness signal for the cache — deliberately NOT a
# lifecycle mark: ``_mark_lifecycle`` records first-boot-wins
# timestamps (``setdefault``), so ``gateway_ready_at_unix`` never
# advances on later gateway restarts in the same daemon. This counter
# increments on EVERY readiness event, so "re-checked after every
# gateway start" survives restarts and same-second readiness.
_gateway_ready_generation = 0


def note_gateway_ready() -> None:
    """Record a gateway readiness event (called from the shared
    readiness wait on every successful probe, daemon and restart
    transaction alike)."""
    global _gateway_ready_generation
    _gateway_ready_generation += 1


def _reset_capability_verification_cache() -> None:
    global _gateway_ready_generation
    _gateway_ready_generation = 0
    _capability_verification_cache.update(
        {
            "capabilities": None,
            "framework": None,
            "checked_at": 0,
            "plugin_version": None,
            "gateway_ready_generation": 0,
        }
    )


def _clean_name_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name for name in value if isinstance(name, str) and name]


def _installed_plugin_dir() -> str:
    """OpenClaw's installed extension copy — what the gateway loads."""
    sup = _sup()
    return os.path.join(sup.openclaw_state_dir(), "extensions", sup.TINYHAT_PLUGIN_ID)


def read_declared_capabilities() -> dict[str, Any] | None:
    """The installed plugin's declared capability surface.

    Reads ``openclaw.plugin.json`` from the installed extension copy
    (what the gateway actually loads), falling back to the platform
    checkout. Returns ``None`` when neither manifest exists (plugin not
    installed, or installed before manifests shipped); otherwise::

        {"tools": [...], "skills": [...], "skill_roots": [...],
         "framework": {...} | None, "source_dir": <dir read from>}

    ``skills`` prefers the explicit ``contracts.skills`` declaration;
    for manifests that predate it, the skill directories shipped under
    the declared roots stand in (a pinned git tree, so directory
    presence is the declaration).
    """
    sup = _sup()
    for plugin_dir in (
        sup._installed_plugin_dir(),
        sup.tinyhat_plugin_checkout_dir(),
    ):
        manifest_path = os.path.join(plugin_dir, "openclaw.plugin.json")
        try:
            if os.path.getsize(manifest_path) > _CAPABILITY_MANIFEST_MAX_BYTES:
                continue
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        contracts = manifest.get("contracts")
        contracts = contracts if isinstance(contracts, dict) else {}
        skill_roots = _clean_name_list(manifest.get("skills")) or ["skills"]
        declared_skills = _clean_name_list(contracts.get("skills"))
        if not declared_skills:
            declared_skills = _enumerate_skill_dirs(plugin_dir, skill_roots)
        framework = contracts.get("framework")
        return {
            "tools": _clean_name_list(contracts.get("tools")),
            "skills": declared_skills,
            "skill_roots": skill_roots,
            "framework": framework if isinstance(framework, dict) else None,
            "source_dir": plugin_dir,
        }
    return None


def _enumerate_skill_dirs(plugin_dir: str, skill_roots: list[str]) -> list[str]:
    found: list[str] = []
    for root in skill_roots:
        root_dir = os.path.join(plugin_dir, root)
        try:
            entries = sorted(os.listdir(root_dir))
        except OSError:
            continue
        for name in entries:
            if os.path.isfile(os.path.join(root_dir, name, "SKILL.md")):
                found.append(name)
    return found


def _mode_grants_read(st: os.stat_result, uid: int, gid: int, *, traverse: bool) -> bool:
    """Permission math: can ``uid``/``gid`` read (and traverse) this entry?"""
    mode = st.st_mode
    want = stat_module.S_IROTH | (stat_module.S_IXOTH if traverse else 0)
    if st.st_uid == uid:
        want = stat_module.S_IRUSR | (stat_module.S_IXUSR if traverse else 0)
    elif st.st_gid == gid:
        want = stat_module.S_IRGRP | (stat_module.S_IXGRP if traverse else 0)
    return (mode & want) == want


def _workload_readable(path: str, *, directory: bool) -> bool:
    """Whether the gateway's workload user can read this path.

    The #683-class regression: a privileged install leaves the plugin
    tree root-owned, the unprivileged gateway cannot read it, and
    OpenClaw silently skips the plugin. When no workload user is
    configured (dev mode, pre-isolation images) this degrades to "the
    current process can read it".
    """
    sup = _sup()
    ownership = sup._runtime_ownership_ids()
    try:
        st = os.stat(path)
    except OSError:
        return False
    if ownership is None:
        return os.access(path, os.R_OK | (os.X_OK if directory else 0))
    uid, gid = ownership
    return _mode_grants_read(st, uid, gid, traverse=directory)


def _mounted_skills(declared: dict[str, Any]) -> tuple[int, list[str]]:
    """Count declared skills present AND workload-readable on disk.

    OpenClaw mounts plugin skills from the declared roots of the
    installed extension copy; the registry does not expose skill
    mounts, so presence + workload readability of ``<root>/<name>/
    SKILL.md`` is the runtime's honest observable for "mounted".
    Readability is checked along the whole ancestor chain the gateway
    must traverse — the install-managed parent, the extension dir, and
    the declared skill root — not just the leaf: a root-owned ``0700``
    parent with a world-readable leaf still hides the skill from the
    unprivileged gateway (the privileged checker itself traverses
    everything, so a leaf-only stat cannot see that).
    Returns ``(mounted_count, missing_names)``.
    """
    source_dir = str(declared.get("source_dir") or "")
    skill_roots = declared.get("skill_roots") or []
    source_chain_ok = _workload_readable(
        os.path.dirname(source_dir), directory=True
    ) and _workload_readable(source_dir, directory=True)
    root_ok = {
        root: source_chain_ok
        and _workload_readable(os.path.join(source_dir, root), directory=True)
        for root in skill_roots
    }
    mounted = 0
    missing: list[str] = []
    for name in declared.get("skills") or []:
        readable = False
        for root in skill_roots:
            skill_dir = os.path.join(source_dir, root, name)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            if (
                root_ok.get(root, False)
                and _workload_readable(skill_dir, directory=True)
                and _workload_readable(skill_md, directory=False)
            ):
                readable = True
                break
        if readable:
            mounted += 1
        else:
            missing.append(f"skill:{name}")
    return mounted, missing


def _parse_framework_version(value: Any) -> tuple[int, ...] | None:
    sup = _sup()
    return sup._parse_plugin_version(value)


def _framework_compat(declared: dict[str, Any]) -> dict[str, Any] | None:
    """Installed framework version vs the plugin's declared range.

    Returns ``None`` when the plugin declares no range. ``in_range`` is
    ``None`` (unknown — never degrades health) when either side cannot
    be read or parsed.
    """
    sup = _sup()
    framework = declared.get("framework")
    if not isinstance(framework, dict):
        return None
    minimum = str(framework.get("minimum") or "").strip()
    maximum = str(framework.get("maximum") or "").strip()
    try:
        installed = str(sup._read_openclaw_framework_version() or "").strip()
    except Exception:  # noqa: BLE001 - diagnostics must stay best-effort
        installed = ""
    compat: dict[str, Any] = {
        "framework_installed": installed or None,
        "framework_minimum": minimum or None,
    }
    if maximum:
        compat["framework_maximum"] = maximum
    installed_parsed = _parse_framework_version(installed)
    minimum_parsed = _parse_framework_version(minimum)
    maximum_parsed = _parse_framework_version(maximum) if maximum else None
    if installed_parsed is None or minimum_parsed is None:
        compat["framework_in_range"] = None
        return compat
    in_range = installed_parsed >= minimum_parsed
    if in_range and maximum_parsed is not None:
        in_range = installed_parsed <= maximum_parsed
    compat["framework_in_range"] = in_range
    return compat


def capability_verification(*, now: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Verify declared capabilities against what actually registered.

    Returns ``(capabilities_block, framework_compat)`` — both ``None``
    when no plugin manifest is on the box (nothing to check, mirroring
    the absent ``plugin`` block). The block follows the
    ``runtime_state_v1`` contract documented in the module docstring.

    Mechanism selection: the framework registry is the ratified primary
    (``inspect``); when the CLI cannot be asked the load beacon stands
    in (``self_check``) and the verdict carries only observed names —
    a missing/stale beacon reports counts, never invented names.
    """
    sup = _sup()
    declared = sup.read_declared_capabilities()
    if declared is None:
        return None, None
    declared_tools = declared["tools"]
    declared_skills = declared["skills"]
    framework = sup._framework_compat(declared)
    block: dict[str, Any] = {
        "declared_tools": len(declared_tools),
        "registered_tools": 0,
        "declared_skills": len(declared_skills),
        "mounted_skills": 0,
        "missing": [],
        "missing_truncated": False,
        "checked_at_unix": now,
    }
    if not declared_tools and not declared_skills:
        # A manifest with no declared surface: nothing to verify and
        # nothing to invent. Pre-manifest plugins land here.
        block["mechanism"] = "self_check"
        block["status"] = "unverifiable"
        return block, framework

    mounted_count, missing_skills = sup._mounted_skills(declared)
    block["mounted_skills"] = mounted_count

    # Mechanism selection — claim `inspect` ONLY on positive tool-level
    # registry data. Proven live on a bound GCE canary (OpenClaw
    # 2026.6.6): the CLI-side registry derives its index from the
    # bundled extension set and can omit a config-enabled, gateway-
    # loaded install-dir plugin entirely ("Plugin not found" while the
    # agent demonstrably has the tools). A registry miss — or an entry
    # without toolNames — is therefore NOT evidence of a shortfall; the
    # load beacon is, in both directions.
    entry, _miss_reason = sup.openclaw_plugin_registry_entry(sup.TINYHAT_PLUGIN_ID)
    registered_names = (
        _clean_name_list(entry.get("toolNames")) if entry is not None else []
    )
    missing: list[str] = []
    unverifiable = False
    if registered_names:
        block["mechanism"] = "inspect"
        block["registered_tools"] = len(registered_names)
        missing.extend(
            name for name in declared_tools if name not in registered_names
        )
    else:
        # self_check: a fresh beacon for the installed version covers
        # the declared manifest (that version's manifest IS the
        # declaration); a missing/stale beacon on a beacon-capable
        # plugin is a shortfall reported by counts — never invented
        # names. Plugin versions that predate the beacon cannot be
        # distinguished from a load failure, so they report
        # `unverifiable` and never degrade health (the same version
        # gate `_plugin_load_check` applies).
        block["mechanism"] = "self_check"
        marker = sup._read_installed_plugin_marker()
        installed_version = str(marker.get("version") or "").strip()
        beacon = sup._read_plugin_load_beacon()
        beacon_fresh = (
            isinstance(beacon, dict)
            and str(beacon.get("version") or "").strip() == installed_version
            and bool(installed_version)
        )
        if beacon_fresh:
            beacon_declared = (
                beacon.get("declared") if isinstance(beacon.get("declared"), dict) else {}
            )
            beacon_tools = _clean_name_list(beacon_declared.get("tools"))
            if beacon_tools:
                block["registered_tools"] = len(beacon_tools)
                missing.extend(
                    name for name in declared_tools if name not in beacon_tools
                )
            else:
                # Older beacons carry no declared listing; the version
                # match itself is the coverage proof.
                block["registered_tools"] = len(declared_tools)
        else:
            parsed = sup._parse_plugin_version(installed_version)
            if parsed is None or parsed < sup.TINYHAT_PLUGIN_BEACON_MIN_VERSION:
                unverifiable = True
            block["registered_tools"] = 0

    missing.extend(missing_skills)
    if len(missing) > CAPABILITIES_MISSING_MAX_NAMES:
        block["missing"] = missing[:CAPABILITIES_MISSING_MAX_NAMES]
        block["missing_truncated"] = True
    else:
        block["missing"] = missing
    if unverifiable and not missing_skills:
        # No registry data, no beacon expected from this plugin
        # generation, and the on-disk skills tree is intact: there is
        # no mechanism to verify against — never invent a verdict.
        block["status"] = "unverifiable"
        return block, framework
    shortfall = (
        bool(missing)
        or block["registered_tools"] < block["declared_tools"]
        or block["mounted_skills"] < block["declared_skills"]
    )
    block["status"] = "shortfall" if shortfall else "ok"
    return block, framework


def capability_verification_cached(
    *, now: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """TTL-cached :func:`capability_verification` for the write path.

    The registry inspection is a subprocess; the daemon's state writes
    are frequent. Re-verify when the TTL lapses, when the gateway
    became ready after the last check — tracked by an updating
    readiness *generation*, not the first-boot-wins lifecycle mark, so
    every gateway restart (and a readiness landing in the same second
    as a pre-ready check) invalidates — or when the installed plugin
    version changed. ``checked_at_unix`` stays the time of the real
    check.
    """
    sup = _sup()
    cache = _capability_verification_cache
    marker = sup._read_installed_plugin_marker()
    installed_version = str(marker.get("version") or "").strip()
    checked_at = cache["checked_at"]
    # A backwards-moving clock (NTP step, test time control) must
    # invalidate rather than pin a stale verdict forever.
    fresh = (
        checked_at
        and 0 <= now - checked_at < CAPABILITY_VERIFICATION_TTL_SECONDS
        and cache["plugin_version"] == installed_version
        and cache["gateway_ready_generation"] == _gateway_ready_generation
    )
    if fresh:
        return cache["capabilities"], cache["framework"]
    capabilities, framework = sup.capability_verification(now=now)
    cache.update(
        {
            "capabilities": capabilities,
            "framework": framework,
            "checked_at": now,
            "plugin_version": installed_version,
            "gateway_ready_generation": _gateway_ready_generation,
        }
    )
    return capabilities, framework
