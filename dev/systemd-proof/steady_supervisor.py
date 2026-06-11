#!/usr/bin/env python3
"""Steady-state supervisor driver for the #685 systemd proof harness.

Runs the REAL supervisor watchdog/reattach/health code under the REAL
systemd supervisor unit, without the platform binding loop (which is
environment-independent Python already covered by the supervisor unit
tests). It imports ``supervisor`` and drives exactly the active-phase
forward-progress pass:

  notify_supervisor_ready()              # real READY=1 to systemd
  loop every PERIOD seconds:
    active  = is_openclaw_gateway_active()           # real systemctl is-active
    ready,_ = probe_current_openclaw_gateway_health() # real journal readiness probe
    health  = "healthy" if (active and ready) else "openclaw_not_ready"
    _write_runtime_state(health, ...,                 # real health + state writer
                         gateway_active=active, openclaw_ready=ready)
    checkpoint_supervisor_progress("steady", inspect_gateway=True)  # real WATCHDOG=1

This is what makes the systemd watchdog real: while this process makes
forward progress it feeds ``WATCHDOG=1`` through the real
``sd_notify`` path; SIGSTOP it and the notifications stop, so systemd's
``WatchdogSec`` fires and ``Restart=on-failure`` respawns it — and the
gateway, an independent unit, keeps running across the respawn
(reattach continuity). The no-early-healthy demo holds the gateway
active-but-not-ready and asserts the health writer never emits
``healthy``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

PERIOD_SECONDS = float(os.environ.get("PROOF_PERIOD_SECONDS") or "5")


def _load_supervisor():
    path = os.environ.get("PROOF_SUPERVISOR_PATH") or "/opt/tinyhat-runtime/supervisor.py"
    spec = importlib.util.spec_from_file_location("supervisor", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["supervisor"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    sup = _load_supervisor()
    sup.log.info("proof steady driver starting (period=%.1fs)", PERIOD_SECONDS)
    # Real READY=1 — this is what flips the systemd notify unit to active.
    sup.notify_supervisor_ready()
    while True:
        active = sup.is_openclaw_gateway_active()
        try:
            ready, detail = sup.probe_current_openclaw_gateway_health()
        except Exception as exc:  # never let a probe error wedge the loop
            ready, detail = False, f"probe error: {exc}"
        if active and ready:
            health, state_detail = "healthy", "gateway ready"
        elif active:
            health, state_detail = "openclaw_not_ready", f"gateway not ready: {detail}"
        else:
            health, state_detail = "openclaw_not_ready", "gateway inactive"
        sup._write_runtime_state(
            health,
            state_detail,
            gateway_active=active,
            openclaw_ready=(ready if active else None),
        )
        # Real forward-progress checkpoint -> real WATCHDOG=1.
        sup.checkpoint_supervisor_progress("steady", inspect_gateway=True)
        time.sleep(PERIOD_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
