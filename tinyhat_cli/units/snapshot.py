"""Control-plane snapshot — the freshness fields every diagnose output carries.

A dead daemon's last state must never render as current: every
``tinyhat`` command reports ``state_as_of`` (when the control-plane
snapshot it read was written) and ``supervisor_alive`` (a live systemd
check), computed here once per invocation.
"""

from __future__ import annotations

import os
import time
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup

UNIT_CATEGORY = "diagnostics"


def _unit_state(unit: str) -> str:
    """``systemctl is-active`` value, or ``unavailable`` off-systemd."""
    sup = _sup()
    if sup._dev_mode():
        return "unavailable"
    result = sup._run_systemctl("is-active", unit, check=False)
    if result.returncode == 127 or (result.returncode != 0 and not (result.stdout or "").strip()):
        text = (result.stdout or result.stderr or "").strip()
        return text or "unavailable"
    return (result.stdout or "").strip() or "unknown"


def control_plane_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(meta, state)``.

    ``meta`` carries the freshness fields for the output envelope plus
    the live unit states the handlers reuse; ``state`` is the raw local
    runtime-state document (already redacted at write time).
    """
    sup = _sup()
    now = int(time.time())
    state = sup.read_runtime_state()
    path = sup.runtime_state_path()
    try:
        mtime: int | None = int(os.stat(path).st_mtime)
    except OSError:
        mtime = None

    state_as_of = state.get("observed_at")
    if not isinstance(state_as_of, str) or not state_as_of.strip():
        state_as_of = (
            sup._runtime_state_observed_at(mtime) if isinstance(mtime, int) else None
        )

    age_anchor = state.get("updated_at_unix")
    if not isinstance(age_anchor, int):
        age_anchor = mtime
    state_age_seconds = max(0, now - age_anchor) if isinstance(age_anchor, int) else None

    supervisor_unit_state = _unit_state(sup.SUPERVISOR_SYSTEMD_UNIT)
    gateway_unit_state = _unit_state(sup.GATEWAY_SYSTEMD_UNIT)
    if supervisor_unit_state == "unavailable":
        supervisor_alive: bool | None = None
    else:
        supervisor_alive = supervisor_unit_state == "active"

    meta: dict[str, Any] = {
        "state_path": path,
        "state_present": bool(state),
        "state_as_of": state_as_of,
        "state_age_seconds": state_age_seconds,
        "supervisor_alive": supervisor_alive,
        "supervisor_unit_state": supervisor_unit_state,
        "gateway_unit_state": gateway_unit_state,
    }
    return meta, state


def freshness_lines(meta: dict[str, Any]) -> list[str]:
    """The shared human-output footer."""
    age = meta.get("state_age_seconds")
    age_text = f" ({age}s ago)" if isinstance(age, int) else ""
    alive = meta.get("supervisor_alive")
    alive_text = {True: "yes", False: "NO", None: "unknown (no systemd)"}[
        alive if alive in (True, False) else None
    ]
    lines = [
        f"state as of:      {meta.get('state_as_of') or '(no local state found)'}{age_text}",
        f"supervisor alive: {alive_text}"
        + (
            f" (unit: {meta.get('supervisor_unit_state')})"
            if meta.get("supervisor_unit_state") not in (None, "unavailable")
            else ""
        ),
    ]
    if alive is False:
        lines.append(
            "warning: the supervisor daemon is not running — the state above "
            "is its LAST snapshot, not a live view"
        )
    return lines
