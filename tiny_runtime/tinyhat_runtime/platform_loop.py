"""Tinyhat platform loop for tiny_runtime Computers.

This loop owns platform coordination only: ready/active state, assignment
long-polling, command-ledger dispatch, and timing reports. It does not watch or
repair the OpenClaw process. OpenClaw liveness is owned by the gateway service;
this loop never restarts OpenClaw as part of assignment activation.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any

from . import openclaw_adapter, private_access, subscription_link
from .platform_client import (
    PlatformClient,
    backend_audience_from_env,
    default_platform_client,
    platform_base_url_from_env,
)
from .redaction import redact_text
from .runtime_commands import RuntimeCommandRunner

LOG = logging.getLogger("tinyhat-runtime-platform")

BINDING_WAIT_SECONDS = float(os.environ.get("TINYHAT_BINDING_WAIT_SECONDS", "25"))
IDLE_SLEEP_SECONDS = float(os.environ.get("TINYHAT_BINDING_IDLE_SLEEP_SECONDS", "1"))
HEARTBEAT_SECONDS = float(os.environ.get("TINYHAT_HEARTBEAT_SECONDS", "30"))
GATEWAY_READY_WAIT_SECONDS = float(
    os.environ.get("TINYHAT_GATEWAY_READY_WAIT_SECONDS", "90")
)
GATEWAY_READY_POLL_SECONDS = float(
    os.environ.get("TINYHAT_GATEWAY_READY_POLL_SECONDS", "0.5")
)


def _runtime_command_service_restart_enabled() -> bool:
    value = (
        os.environ.get("TINYHAT_RUNTIME_NO_SERVICE_RESTART")
        or os.environ.get("TINYHAT_RUNTIME_SKIP_SYSTEMD")
        or ""
    )
    return value.strip().lower() not in {"1", "true", "yes", "on"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(started: float, ended: float | None = None) -> int:
    end = time.monotonic() if ended is None else ended
    return max(1, int((end - started) * 1000))


def _phase(name: str, label: str, started: float, ended: float) -> dict[str, Any]:
    return {
        "phase": name,
        "label": label,
        "duration_ms": _elapsed_ms(started, ended),
    }


class TinyRuntimePlatformLoop:
    def __init__(self, *, client: PlatformClient | None = None) -> None:
        self.client = client or default_platform_client()
        self.stop_requested = False
        self.ready_reported = False

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self._report_ready()
        while not self.stop_requested:
            cycle_started_wall = time.time()
            cycle_started = time.monotonic()
            binding_started = time.monotonic()
            try:
                response = self._poll_binding()
            except Exception as exc:  # noqa: BLE001 - platform outage should not churn OpenClaw
                detail = f"binding poll failed: {redact_text(str(exc), limit=1000)}"
                LOG.warning(detail)
                self._safe_post_runtime_state(
                    "degraded_control_plane",
                    detail,
                    {
                        "assigned": False,
                        "binding_poll_failed": True,
                        "openclaw_control": "no_restart_requested",
                    },
                )
                self._safe_post_heartbeat(assigned=False, dispatch_command=True)
                time.sleep(max(1.0, IDLE_SLEEP_SECONDS))
                continue
            binding_received = time.monotonic()
            command = response.get("command")
            if isinstance(command, dict):
                self._dispatch_runtime_command(command)
            if response.get("assigned") is not True:
                self._report_ready()
                self._safe_post_heartbeat(assigned=False, dispatch_command=True)
                time.sleep(IDLE_SLEEP_SECONDS)
                continue
            binding = response.get("binding")
            if not isinstance(binding, dict):
                LOG.warning("binding response was assigned without a binding object")
                time.sleep(IDLE_SLEEP_SECONDS)
                continue
            phase_spans = [
                _phase(
                    "long_poll_receive",
                    "long-poll receive",
                    binding_started,
                    binding_received,
                )
            ]
            try:
                self._activate_binding(
                    binding,
                    cycle_started_wall=cycle_started_wall,
                    cycle_started=cycle_started,
                    binding_received=binding_received,
                    phase_spans=phase_spans,
                )
                self._active_loop(binding)
            except Exception as exc:  # noqa: BLE001 - keep loop visible, not crashy
                detail = f"binding activation failed: {redact_text(str(exc), limit=1000)}"
                LOG.exception(detail)
                self._safe_post_runtime_state(
                    "openclaw_not_ready",
                    detail,
                    {
                        "assigned": True,
                        "activation_failed": True,
                        "openclaw_control": "no_restart_requested",
                    },
                )
                time.sleep(max(1.0, IDLE_SLEEP_SECONDS))
        return 0

    def _on_signal(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True

    def _report_ready(self) -> None:
        try:
            self._post_runtime_state(
                "healthy",
                "control plane ready; awaiting binding",
                {"assigned": False},
            )
        except Exception as exc:  # noqa: BLE001 - do not mark hot-ready without the report
            LOG.info("ready report deferred until runtime-state succeeds: %s", exc)
            return
        if self.ready_reported:
            return
        try:
            state_posted = self._post_lifecycle_state(
                "ready",
                "tiny_runtime platform loop ready",
                already_state="ready",
                ready_allows_assigned_current=True,
            )
        except Exception as exc:  # noqa: BLE001 - readiness prerequisites can be gated
            LOG.info(
                "ready state deferred until prerequisites are ready: %s",
                redact_text(str(exc), limit=500),
            )
            return
        self.ready_reported = True
        if state_posted:
            LOG.info("confirmed state=ready")

    def _poll_binding(self) -> dict[str, Any]:
        path = (
            "/hapi/v1/computers/me/binding"
            f"?wait_seconds={BINDING_WAIT_SECONDS:g}&include_command=false"
        )
        return self.client.get_json(path, timeout=int(BINDING_WAIT_SECONDS + 10))

    def _activate_binding(
        self,
        binding: dict[str, Any],
        *,
        cycle_started_wall: float,
        cycle_started: float,
        binding_received: float,
        phase_spans: list[dict[str, Any]],
        preserve_existing_secrets: bool = True,
    ) -> None:
        config_started = time.monotonic()
        config_result = openclaw_adapter.apply_binding_config(
            binding,
            platform_base_url=platform_base_url_from_env(),
            backend_audience=backend_audience_from_env(),
            preserve_existing_secrets=preserve_existing_secrets,
        )
        if config_result.get("state") != "ready":
            raise RuntimeError(f"OpenClaw config patch failed: {config_result}")
        config_done = time.monotonic()
        phase_spans.append(
            _phase("binding_config_apply", "binding/config apply", config_started, config_done)
        )

        ready_started = time.monotonic()
        health = self._wait_for_gateway_ready()
        ready_done = time.monotonic()
        phase_spans.append(_phase("bot_ready", "bot-ready", ready_started, ready_done))

        active_started = time.monotonic()
        self._post_lifecycle_state(
            "active",
            "tiny_runtime OpenClaw ready",
            already_state="active",
        )
        active_done = time.monotonic()
        phase_spans.append(_phase("binding_ack", "binding ack", active_started, active_done))
        LOG.info("reported state=active")

        self._post_startup_timing(
            binding=binding,
            cycle_started_wall=cycle_started_wall,
            binding_received=binding_received,
            active_ack=active_done,
            phase_spans=phase_spans,
            metadata={
                "duration_anchor": "final_binding_receive_to_active_ack",
                "binding_wait_seconds": BINDING_WAIT_SECONDS,
                "gateway_restart": {
                    "state": "not_requested",
                    "reason": "tiny_runtime assignment does not control OpenClaw liveness",
                },
                "gateway_health": health,
                "runtime_loop": "tiny_runtime_platform_loop",
                "restart_policy": "no_assignment_restart",
            },
        )
        self._post_runtime_state(
            "healthy",
            "tiny_runtime OpenClaw ready",
            {
                "assigned": True,
                "gateway": health,
                "assignment_elapsed_ms": _elapsed_ms(cycle_started, active_done),
            },
        )
        self._report_subscription_runtime_verification(binding)

    def _active_loop(self, binding: dict[str, Any]) -> None:
        current_signature = self._binding_signature(binding)
        while not self.stop_requested:
            started = time.monotonic()
            heartbeat = self._post_heartbeat(assigned=True)
            command = heartbeat.get("command")
            if isinstance(command, dict):
                self._dispatch_runtime_command(command)
            response = self.client.get_json(
                "/hapi/v1/computers/me/binding?include_command=true",
                timeout=30,
            )
            if response.get("assigned") is not True:
                LOG.info("platform unassigned this Computer; returning to binding wait")
                self.ready_reported = False
                self._report_ready()
                return
            next_binding = response.get("binding")
            if isinstance(next_binding, dict) and self._binding_signature(next_binding) != current_signature:
                LOG.info("binding changed; applying new identity bind")
                preserve_existing_secrets = (
                    self._binding_owner_id(next_binding) == self._binding_owner_id(binding)
                )
                self._activate_binding(
                    next_binding,
                    cycle_started_wall=time.time(),
                    cycle_started=time.monotonic(),
                    binding_received=time.monotonic(),
                    phase_spans=[],
                    preserve_existing_secrets=preserve_existing_secrets,
                )
                current_signature = self._binding_signature(next_binding)
                binding = next_binding
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, HEARTBEAT_SECONDS - elapsed))

    def _wait_for_gateway_ready(self) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, GATEWAY_READY_WAIT_SECONDS)
        last_health: dict[str, Any] = {"state": "unavailable"}
        while True:
            last_health = openclaw_adapter.gateway_health()
            if last_health.get("state") == "healthy":
                return last_health
            if time.monotonic() >= deadline:
                raise RuntimeError(f"OpenClaw gateway was not ready: {last_health}")
            time.sleep(max(0.1, GATEWAY_READY_POLL_SECONDS))

    def _post_lifecycle_state(
        self,
        state: str,
        detail: str,
        *,
        already_state: str,
        ready_allows_assigned_current: bool = False,
    ) -> bool:
        try:
            self.client.post_json(
                "/hapi/v1/computers/me/state",
                {"state": state, "detail": detail},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - status check decides if this is stale
            try:
                status = self.client.get_json(
                    "/hapi/v1/computers/me/platform-status",
                    timeout=10,
                )
            except Exception:
                raise exc
            if status.get("state") == already_state:
                if state == "active" and status.get("assigned") is not True:
                    raise exc
                LOG.info(
                    "state=%s already satisfied by platform; continuing",
                    state,
                )
                return False
            if (
                ready_allows_assigned_current
                and state == "ready"
                and status.get("state") in {"assigned", "active"}
                and status.get("assigned") is True
            ):
                LOG.info(
                    "state=ready skipped; platform already %s",
                    status.get("state"),
                )
                return False
            raise exc

    def _dispatch_runtime_command(self, command: dict[str, Any]) -> None:
        runtime_command = command.get("command")
        if not isinstance(runtime_command, dict):
            runtime_command = {
                key: value
                for key, value in command.items()
                if key not in {"type", "revision"}
            }
        runner = RuntimeCommandRunner(
            platform_get_json=self.client.get_json,
            apply_runtime_config=self._apply_runtime_config,
            start_chatgpt_link=self._start_chatgpt_link,
            service_restart=_runtime_command_service_restart_enabled(),
        )
        result = runner.execute(runtime_command)
        self.client.post_json(
            "/hapi/v1/computers/me/runtime-command/result",
            {"result": result},
        )

    def _apply_runtime_config(
        self,
        *,
        revision: int,
        secrets: dict[str, str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        # This is intentionally no-restart. SecretRef-backed values are reloaded
        # through the official OpenClaw secrets surface; env-block changes must
        # move to OpenClaw hot secret refs rather than restarting the gateway.
        if not dry_run:
            openclaw_adapter.write_openclaw_secrets(secrets)
        reload_result = (
            {"skipped": True, "reason": "dry_run"}
            if dry_run
            else openclaw_adapter.secrets_reload()
        )
        if not dry_run and reload_result.get("state") != "ready":
            raise RuntimeError(
                "OpenClaw secrets reload failed: "
                + redact_text(json.dumps(reload_result, sort_keys=True), limit=1000)
            )
        return {
            "revision": revision,
            "secret_count": len(secrets),
            "reload": reload_result,
            "env_block_changed": False,
            "gateway_config_changed": False,
            "model_auth_signature_changed": False,
            "gateway_rebind_required": False,
        }

    def _start_chatgpt_link(self, command: dict[str, Any]) -> dict[str, Any]:
        # Runs OpenClaw's official device-code CLI in a background worker. The
        # worker posts public URL/code and terminal link state; OAuth tokens stay
        # on this Computer.
        return subscription_link.start_chatgpt_link(command, client=self.client)

    def _post_heartbeat(self, *, assigned: bool) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "runtime_generation": "tiny_runtime",
            "gateway_liveness_owner": "systemd",
            "openclaw_control": "official_cli_only",
            "assigned": bool(assigned),
        }
        private_report = private_access.private_access_report()
        if private_report is not None:
            metrics["private_access"] = private_report
        return self.client.post_json(
            "/hapi/v1/computers/me/heartbeat",
            {
                "metrics": metrics,
                "openclaw_status": openclaw_adapter.gateway_status(),
            },
        )

    def _safe_post_heartbeat(
        self, *, assigned: bool, dispatch_command: bool = False
    ) -> None:
        try:
            heartbeat = self._post_heartbeat(assigned=assigned)
        except Exception as exc:  # noqa: BLE001 - heartbeat must not churn OpenClaw
            LOG.warning(
                "heartbeat failed assigned=%s error=%s",
                assigned,
                redact_text(str(exc), limit=500),
            )
            return
        if dispatch_command:
            command = heartbeat.get("command")
            if isinstance(command, dict):
                try:
                    self._dispatch_runtime_command(command)
                except Exception as exc:  # noqa: BLE001 - command failures must not churn the loop
                    LOG.warning(
                        "runtime command dispatch failed after heartbeat: %s",
                        redact_text(str(exc), limit=500),
                    )

    def _report_subscription_runtime_verification(self, binding: dict[str, Any]) -> None:
        if str(binding.get("llm_auth_mode") or "") != "chatgpt_subscription":
            return
        expected = str(binding.get("llm_model_ref") or "openai/gpt-5.5").strip()
        status = openclaw_adapter.models_status()
        observed = _observed_model_ref_from_status(status)
        body: dict[str, Any] = {
            "expected_model_ref": expected,
            "observed_model_ref": observed,
            "command": "openclaw models status --json",
            "verified": observed == expected,
        }
        if status.get("state") != "ready":
            body["detail"] = redact_text(str(status.get("detail") or status), limit=512)
        elif observed is None:
            detail = (
                "openclaw models status did not expose a default model "
                f"(shape={_status_shape_for_log(status)})"
            )
            LOG.warning(detail)
            body["detail"] = detail
        try:
            self.client.post_json(
                "/hapi/v1/computers/me/subscription-link/runtime-verification",
                body,
            )
        except Exception as exc:  # noqa: BLE001 - readiness retry happens platform-side
            LOG.warning(
                "subscription runtime verification POST failed: %s",
                redact_text(str(exc), limit=500),
            )

    def _post_runtime_state(
        self,
        runtime_health: str,
        detail: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema": "runtime_state_v1",
            "schema_version": 1,
            "observed_at": _utc_now_iso(),
            "runtime_health": runtime_health,
            "runtime_state": runtime_health,
            "state": runtime_health,
            "detail": detail,
            "supervisor": {
                "present": False,
                "runtime_loop": "tiny_runtime_platform_loop",
            },
            "gateway": {
                "liveness_owner": "systemd",
                "restart_loop": False,
            },
            "openclaw": {
                "interface": "official_cli",
            },
        }
        if extra:
            gateway_extra = extra.get("gateway")
            payload.update(
                {key: value for key, value in extra.items() if key != "gateway"}
            )
            if isinstance(gateway_extra, dict):
                payload["gateway"] = {**payload["gateway"], **gateway_extra}
        private_report = private_access.private_access_report()
        if private_report is not None:
            payload["private_access"] = private_report
        self.client.post_json("/hapi/v1/computers/me/runtime-state", payload)

    def _safe_post_runtime_state(
        self,
        runtime_health: str,
        detail: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._post_runtime_state(runtime_health, detail, extra)
        except Exception as exc:  # noqa: BLE001 - observability must not break the loop
            LOG.warning(
                "runtime-state report failed health=%s detail=%s error=%s",
                runtime_health,
                detail,
                redact_text(str(exc), limit=500),
            )

    def _post_startup_timing(
        self,
        *,
        binding: dict[str, Any],
        cycle_started_wall: float,
        binding_received: float,
        active_ack: float,
        phase_spans: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> None:
        bot_id = str(binding.get("telegram_bot_user_id") or "unknown")
        source_ref = f"tiny-runtime-binding-cycle:{bot_id}:{int(cycle_started_wall * 1000)}"
        sample = {
            "metric_name": "assignment_to_serving_ms",
            "candidate_label": "runtime final binding receive to active ack",
            "source_kind": "runtime_report",
            "capacity_path": "hot_pool_running",
            "image_label": "tiny_runtime_bundle",
            "duration_ms": _elapsed_ms(binding_received, active_ack),
            "observed_at": _utc_now_iso(),
            "source_ref": source_ref,
            "phase_spans": phase_spans,
            "sample_metadata": metadata,
        }
        payload = {
            "schema": "runtime_state_v1",
            "schema_version": 1,
            "observed_at": _utc_now_iso(),
            "runtime_health": "healthy",
            "runtime_state": "healthy",
            "state": "healthy",
            "detail": "startup timing sample",
            "startup_timings": [sample],
        }
        self.client.post_json("/hapi/v1/computers/me/runtime-state", payload)

    @staticmethod
    def _binding_signature(binding: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(binding.get("telegram_bot_user_id") or ""),
            str(binding.get("telegram_owner_user_id") or ""),
            str(binding.get("llm_auth_mode") or ""),
            str(binding.get("llm_model_ref") or ""),
        )

    @staticmethod
    def _binding_owner_id(binding: dict[str, Any]) -> str:
        return str(binding.get("telegram_owner_user_id") or "")


def _observed_model_ref_from_status(status: dict[str, Any]) -> str | None:
    if not isinstance(status, dict):
        return None
    models = status.get("models")
    if isinstance(models, dict):
        value = _observed_model_ref_from_payload(models)
        if value:
            return value
    return _observed_model_ref_from_payload(status)


def _observed_model_ref_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("resolvedDefault", "defaultModel", "default", "currentModel"):
        value = _model_ref_from_value(payload.get(key))
        if value:
            return value
    nested = payload.get("models")
    if isinstance(nested, dict):
        value = _observed_model_ref_from_payload(nested)
        if value:
            return value
    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            if item.get("default") is True or item.get("isDefault") is True:
                value = _model_ref_from_value(item)
                if value:
                    return value
    return None


def _model_ref_from_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if not isinstance(value, dict):
        return None
    provider = str(value.get("provider") or "").strip()
    model = str(value.get("model") or value.get("modelId") or "").strip()
    if provider and model:
        return f"{provider}/{model}"
    for key in ("ref", "id", "label", "name"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    return None


def _status_shape_for_log(status: dict[str, Any]) -> str:
    if not isinstance(status, dict):
        return type(status).__name__
    keys = sorted(str(key) for key in status.keys())
    models = status.get("models")
    if isinstance(models, dict):
        model_shape = ",".join(sorted(str(key) for key in models.keys())[:8])
        return f"top={','.join(keys[:8])};models={model_shape}"
    if isinstance(models, list):
        return f"top={','.join(keys[:8])};models=list[{len(models)}]"
    return f"top={','.join(keys[:8])};models={type(models).__name__}"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("TINYHAT_RUNTIME_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return TinyRuntimePlatformLoop().run_forever()
