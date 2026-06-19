"""Phase-D binding watchdog loop for the supervisor daemon."""

from __future__ import annotations

import time
import urllib.error
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup

UNIT_CATEGORY = "supervision"


def _handle_binding_watchdog_response(
    sup: Any, resp: dict[str, Any], binding: dict[str, Any]
) -> bool:
    if resp.get("assigned") is False:
        sup.log.info(
            "binding watchdog: platform reports assigned=false; triggering rebind"
        )
        sup._wipe_on_owner_release(reason="unassign")
        sup._stop_holder["rebind_reason"] = "platform_unassigned"
        sup._stop_holder["rebind"] = True
        sup._stop_holder["stop"] = True
        sup.notify_watchdog_checkpoint("phase-d-rebind-unassigned")
        return True

    new_binding = resp.get("binding") or {}
    new_sig = sup._binding_signature(new_binding)
    cached_sig = sup._stop_holder.get("signature")
    if cached_sig and new_sig != cached_sig:
        sup.log.info(
            "binding watchdog: identity changed (bot=@%s owner=%s); triggering rebind",
            new_binding.get("telegram_bot_username"),
            new_binding.get("telegram_owner_user_id"),
        )
        new_owner_sig = sup._owner_identity_signature(new_binding)
        cached_owner_sig = sup._stop_holder.get("owner_signature")
        if cached_owner_sig and new_owner_sig != cached_owner_sig:
            sup._wipe_on_owner_release(reason="reassign")
        sup._stop_holder["rebind_reason"] = "binding_identity_changed"
        sup._stop_holder["rebind"] = True
        sup._stop_holder["stop"] = True
        sup.notify_watchdog_checkpoint("phase-d-rebind-identity-changed")
        return True

    binding_command = resp.get("command") if isinstance(resp, dict) else None
    if isinstance(binding_command, dict):
        sup.log.info(
            "binding watchdog: handling piggybacked command type=%r",
            binding_command.get("type"),
        )
        sup.handle_heartbeat_command(binding_command, binding=binding)
        if sup._stop_holder["stop"]:
            return True
    return False


def run_binding_watchdog_heartbeat_loop(binding: dict[str, Any]) -> None:
    sup = _sup()
    while not sup._stop_holder["stop"]:
        gateway_alive = sup.is_openclaw_gateway_active()
        local_manifest = sup.local_watchdog_manifest_snapshot()
        gateway_cgroup = sup.gateway_cgroup_memory_snapshot()
        oom_delta_status = sup._record_gateway_oom_delta(gateway_cgroup)
        if oom_delta_status in {"hold_down", "manual"}:
            sup.log.warning(
                "gateway cgroup OOM policy requested %s; stopping gateway and "
                "restarting the binding cycle",
                oom_delta_status,
            )
            sup._stop_holder["rebind"] = oom_delta_status == "hold_down"
            if sup._stop_holder["rebind"]:
                sup._stop_holder["rebind_reason"] = "gateway_oom_hold_down"
            sup._stop_holder["stop"] = True
            sup.notify_watchdog_checkpoint(
                "phase-d-gateway-recovery-" + oom_delta_status
            )
            return
        if gateway_alive:
            sup._reset_gateway_recovery_after_stable_healthy(gateway_cgroup)
        component_versions = sup.collect_component_versions()
        metrics: dict[str, Any] = {
            "gateway_alive": gateway_alive,
            "supervisor_uptime_seconds": int(time.time()),
            "watchdog": {
                "loop_budget_seconds": sup.SUPERVISOR_LOOP_BUDGET_SECONDS,
                "max_checkpoint_gap_seconds": sup.WATCHDOG_MAX_CHECKPOINT_GAP_SECONDS,
                "local_manifest": local_manifest,
                "gateway_cgroup": gateway_cgroup,
            },
        }
        private_access = sup.private_access_report()
        if private_access is not None:
            metrics["private_access"] = private_access
        heartbeat_status = "not_attempted"
        binding_status = "not_attempted"
        active_reconfirm_status = "not_attempted"
        command = None
        try:
            heartbeat = sup.post_json(
                "/hapi/v1/computers/me/heartbeat",
                {
                    "metrics": metrics,
                    "component_versions": component_versions,
                },
            )
            heartbeat_status = "ok"
            command = heartbeat.get("command") if isinstance(heartbeat, dict) else None
        except Exception as exc:
            heartbeat_status = exc.__class__.__name__
            sup.log.warning("/me/heartbeat POST failed: %s", exc)
        sup.log.info(
            "watchdog checkpoint phase-d-platform-heartbeat: gateway_alive=%s "
            "heartbeat=%s",
            gateway_alive,
            heartbeat_status,
        )
        sup.notify_watchdog_checkpoint("phase-d-platform-heartbeat")
        if isinstance(command, dict):
            sup.handle_heartbeat_command(command, binding=binding)
            if sup._stop_holder["stop"]:
                return

        try:
            resp = sup.get_json(
                sup._binding_poll_path(wait_seconds=0, include_command=True)
            )
            binding_status = "ok"
            if _handle_binding_watchdog_response(sup, resp, binding):
                return
        except Exception as exc:
            binding_status = exc.__class__.__name__
            sup.log.warning("/me/binding watchdog GET failed: %s", exc)

        try:
            sup.post_json(
                "/hapi/v1/computers/me/state",
                {"state": "active", "detail": "watchdog re-confirm"},
            )
            active_reconfirm_status = "ok"
            sup.log.info(
                "watchdog: re-confirmed state=active (row was in assigned "
                "after a reassign)"
            )
        except urllib.error.HTTPError as http_exc:
            if http_exc.code != 400:
                sup.log.warning("watchdog state=active POST failed: %s", http_exc)
                active_reconfirm_status = f"HTTPError:{http_exc.code}"
            else:
                active_reconfirm_status = "already-active"
        except Exception as exc:
            active_reconfirm_status = exc.__class__.__name__
            sup.log.warning("watchdog state=active POST failed: %s", exc)
        sup.log.info(
            "watchdog checkpoint phase-d-heartbeat: gateway_alive=%s heartbeat=%s "
            "binding=%s active_reconfirm=%s",
            gateway_alive,
            heartbeat_status,
            binding_status,
            active_reconfirm_status,
        )
        sup.notify_watchdog_checkpoint("phase-d-heartbeat")
        heartbeat_deadline = time.monotonic() + sup.HEARTBEAT_INTERVAL_SECONDS
        while not sup._stop_holder["stop"]:
            remaining = heartbeat_deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_seconds = min(float(sup.BINDING_LONG_POLL_WAIT_SECONDS), remaining)
            try:
                resp = sup.get_json(
                    sup._binding_poll_path(
                        wait_seconds=wait_seconds,
                        include_command=True,
                    ),
                    timeout=sup.BINDING_LONG_POLL_REQUEST_TIMEOUT_SECONDS,
                )
                if _handle_binding_watchdog_response(sup, resp, binding):
                    return
            except Exception as exc:
                sup.log.warning("/me/binding command long-poll failed: %s", exc)
            if sup._stop_holder["stop"]:
                return
            time.sleep(min(1.0, max(0.0, heartbeat_deadline - time.monotonic())))
