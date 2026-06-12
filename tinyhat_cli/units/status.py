"""``tinyhat status`` — the one-look support answer.

Reads the daemon's last control-plane snapshot (the same redacted
``runtime_state_v1`` document mirrored to the platform) and pairs it
with live systemd unit states, so a support shell sees in one screen:
identity, health, gateway, plugin load, last error, recent events —
and whether that picture is current (envelope freshness fields).
"""

from __future__ import annotations

from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup


def run(ctx) -> dict[str, Any]:
    sup = _sup()
    state = ctx.state
    meta = ctx.snapshot

    identity = {
        "computer_id": state.get("computer_id"),
        "instance_id": state.get("instance_id"),
        "runtime_ref": state.get("runtime_ref"),
    }
    if not any(identity.values()):
        try:
            identity = sup._runtime_state_identity()
        except Exception:
            pass

    gateway_state = state.get("gateway") if isinstance(state.get("gateway"), dict) else {}
    supervisor_state = (
        state.get("supervisor") if isinstance(state.get("supervisor"), dict) else {}
    )
    events = state.get("runtime_events")
    events = events if isinstance(events, list) else []

    data: dict[str, Any] = {
        "identity": identity,
        "runtime_health": state.get("runtime_health"),
        "detail": state.get("detail"),
        "last_error": state.get("last_error"),
        "last_error_category": state.get("last_error_category"),
        "manual_recovery_required": state.get("manual_recovery_required"),
        "platform": state.get("platform"),
        "supervisor": {
            "unit": sup.SUPERVISOR_SYSTEMD_UNIT,
            "live_unit_state": meta.get("supervisor_unit_state"),
            "version": supervisor_state.get("version"),
            "status_from_state": supervisor_state.get("status"),
        },
        "gateway": {
            "unit": gateway_state.get("unit") or sup.GATEWAY_SYSTEMD_UNIT,
            "live_unit_state": meta.get("gateway_unit_state"),
            "status_from_state": gateway_state.get("status"),
            "active_from_state": gateway_state.get("active"),
            "restart_count_window": gateway_state.get("restart_count_window"),
            "action_from_state": gateway_state.get("action"),
        },
        "plugin": state.get("plugin"),
        "recent_events": events[-3:],
        "lifecycle": state.get("lifecycle"),
    }
    return data


def render(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    identity = data.get("identity") or {}
    lines.append(
        f"computer:  id={identity.get('computer_id') or '?'} "
        f"instance={identity.get('instance_id') or '?'}"
    )
    lines.append(f"runtime:   {identity.get('runtime_ref') or '?'}")
    health = data.get("runtime_health") or "unknown"
    lines.append(f"health:    {health} — {data.get('detail') or ''}".rstrip(" —"))
    supervisor_block = data.get("supervisor") or {}
    lines.append(
        f"supervisor unit: {supervisor_block.get('live_unit_state')} "
        f"({supervisor_block.get('unit')})"
    )
    gateway = data.get("gateway") or {}
    restart_part = (
        f" restarts-in-window={gateway.get('restart_count_window')}"
        if gateway.get("restart_count_window") is not None
        else ""
    )
    lines.append(
        f"gateway unit:    {gateway.get('live_unit_state')} "
        f"({gateway.get('unit')}); last-state={gateway.get('status_from_state')}"
        + restart_part
    )
    plugin = data.get("plugin") or {}
    if plugin:
        reason = f" reason={plugin.get('reason')}" if plugin.get("reason") else ""
        lines.append(
            f"plugin:    v{plugin.get('installed_version')} "
            f"load_check={plugin.get('load_check')}{reason}"
        )
    else:
        lines.append("plugin:    (not installed — installed on first agent bind)")
    last_error = data.get("last_error") or {}
    if last_error:
        lines.append(
            f"last error: [{last_error.get('category')}] {last_error.get('detail')}"
        )
    if data.get("manual_recovery_required"):
        lines.append("MANUAL RECOVERY REQUIRED — automatic recovery is blocked")
    platform = data.get("platform") or {}
    if isinstance(platform, dict) and platform.get("status") == "unreachable":
        lines.append(
            "platform:  UNREACHABLE at last post "
            f"({platform.get('last_error_at') or '?'})"
        )
    for event in data.get("recent_events") or []:
        lines.append(
            f"event:     {event.get('at', '?')} {event.get('type', '?')}"
            + (f" — {event['detail']}" if event.get("detail") else "")
        )
    lifecycle = data.get("lifecycle") or {}
    spans = lifecycle.get("spans") if isinstance(lifecycle, dict) else None
    if spans:
        rendered = ", ".join(
            f"{name.replace('_seconds', '')}={value}s" for name, value in spans.items()
        )
        lines.append(f"lifecycle: {rendered}")
    return lines
