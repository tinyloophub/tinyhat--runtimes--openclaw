"""``tinyhat health`` — the runtime-health projection, recomputed live.

Uses the SAME extracted units as the daemon's write path: the live
gateway activity check, the live plugin load-beacon check, and the
shared healthy-demotion rule — so a support shell and the platform
admin page can never disagree about what "healthy" means. The state
file's value is reported alongside so a divergence (daemon stale, live
check worse) is visible rather than papered over.
"""

from __future__ import annotations

import time
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup

UNIT_CATEGORY = "diagnostics"


def run(ctx) -> dict[str, Any]:
    sup = _sup()
    state = ctx.state
    now = int(time.time())

    health_from_state = state.get("runtime_health")
    base_health = (
        health_from_state if isinstance(health_from_state, str) else "unknown"
    )

    try:
        gateway_active_live: bool | None = sup.is_openclaw_gateway_active()
    except Exception:
        gateway_active_live = None

    try:
        plugin_check = sup._plugin_load_check(
            state,
            gateway_active=gateway_active_live,
            now=now,
        )
    except Exception:
        plugin_check = None

    try:
        capabilities, framework_compat = sup.capability_verification(now=now)
    except Exception:
        capabilities, framework_compat = None, None

    demotion = sup.capability_demotion(
        base_health,
        plugin_check,
        capabilities,
        framework_compat,
    )
    demoted = demotion is not None
    effective_health = demotion[0] if demotion is not None else base_health
    demotion_category = demotion[1] if demotion is not None else None
    demotion_detail = demotion[2] if demotion is not None else None

    openclaw_state = state.get("openclaw") if isinstance(state.get("openclaw"), dict) else {}
    openclaw_ready = openclaw_state.get("ready")
    recovery = sup._runtime_state_gateway_recovery(state)

    data: dict[str, Any] = {
        "runtime_health": effective_health,
        "runtime_health_from_state": health_from_state,
        "demoted_by_live_check": demoted,
        "supervisor_status": (
            sup._runtime_supervisor_status(effective_health)
            if effective_health != "unknown"
            else "unknown"
        ),
        "gateway_status": (
            sup._gateway_status(
                effective_health,
                gateway_active=gateway_active_live,
                openclaw_ready=openclaw_ready if isinstance(openclaw_ready, bool) else None,
            )
            if effective_health != "unknown"
            else "unknown"
        ),
        "gateway_active_live": gateway_active_live,
        "openclaw_ready_from_state": (
            openclaw_ready if isinstance(openclaw_ready, bool) else None
        ),
        "plugin_check": plugin_check,
        "capabilities": capabilities,
        "framework_compat": framework_compat,
        "demotion_detail": demotion_detail,
        "last_error": state.get("last_error"),
        "last_error_category": state.get("last_error_category")
        or demotion_category,
        "manual_recovery_required": state.get("manual_recovery_required"),
        "gateway_recovery": {
            "failures_in_window": sup._gateway_restart_count_window(recovery, now=now),
            "hold_down_cycles": recovery.get("hold_down_cycles"),
            "hold_down_until_unix": recovery.get("hold_down_until_unix"),
        },
    }
    return data


def render(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    health = data.get("runtime_health") or "unknown"
    lines.append(f"runtime health:  {health}")
    if data.get("demoted_by_live_check"):
        lines.append(
            "  (demoted live: state file said "
            f"{data.get('runtime_health_from_state')!r} — "
            f"{data.get('demotion_detail') or 'capability check failed'})"
        )
    elif data.get("runtime_health_from_state") not in (None, health):
        lines.append(f"  (state file said: {data.get('runtime_health_from_state')})")
    lines.append(f"supervisor:      {data.get('supervisor_status')}")
    gateway_active = data.get("gateway_active_live")
    active_text = {True: "active", False: "inactive", None: "unknown"}[
        gateway_active if gateway_active in (True, False) else None
    ]
    lines.append(f"gateway:         {data.get('gateway_status')} (live: {active_text})")
    ready = data.get("openclaw_ready_from_state")
    if ready is not None:
        lines.append(f"openclaw ready:  {ready} (from last daemon state)")
    plugin_check = data.get("plugin_check") or {}
    if plugin_check:
        reason = (
            f" reason={plugin_check.get('reason')}" if plugin_check.get("reason") else ""
        )
        lines.append(
            f"plugin:          v{plugin_check.get('installed_version')} "
            f"load_check={plugin_check.get('load_check')}{reason} (live check)"
        )
    else:
        lines.append("plugin:          (not installed — nothing to check)")
    capabilities = data.get("capabilities") or {}
    if capabilities:
        lines.append(
            f"capabilities:    {capabilities.get('status')} "
            f"[{capabilities.get('mechanism')}] "
            f"tools {capabilities.get('registered_tools')}/{capabilities.get('declared_tools')} "
            f"skills {capabilities.get('mounted_skills')}/{capabilities.get('declared_skills')}"
            " (live check)"
        )
        missing = capabilities.get("missing") or []
        if missing:
            suffix = " …" if capabilities.get("missing_truncated") else ""
            lines.append(f"  missing:       {', '.join(missing)}{suffix}")
    framework_compat = data.get("framework_compat") or {}
    if framework_compat.get("framework_in_range") is False:
        lines.append(
            f"framework:       {framework_compat.get('framework_installed')} is OUTSIDE "
            f"the plugin's supported range (>= {framework_compat.get('framework_minimum')}"
            + (
                f" <= {framework_compat.get('framework_maximum')}"
                if framework_compat.get("framework_maximum")
                else ""
            )
            + ")"
        )
    recovery = data.get("gateway_recovery") or {}
    lines.append(
        f"recovery:        failures-in-window={recovery.get('failures_in_window')} "
        f"hold-down-cycles={recovery.get('hold_down_cycles') or 0}"
    )
    last_error = data.get("last_error") or {}
    if last_error:
        lines.append(
            f"last error:      [{last_error.get('category')}] {last_error.get('detail')}"
        )
    if data.get("manual_recovery_required"):
        lines.append("MANUAL RECOVERY REQUIRED — automatic recovery is blocked")
    return lines
