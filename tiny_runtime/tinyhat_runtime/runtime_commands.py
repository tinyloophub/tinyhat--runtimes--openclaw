"""Typed tiny_runtime command execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Sequence

from . import attestation, bundle, launcher, openclaw_adapter, paths
from .command_ledger import (
    TERMINAL_STATUSES,
    CommandLedger,
    utc_now_iso,
    validate_command_id,
)
from .identity import load_identity_document
from .redaction import redact_json, redact_text

COMMAND_RESULT_SCHEMA = "tiny_runtime_command_result_v1"
ALLOWED_COMMAND_KINDS = frozenset(
    {
        "activate_bundle",
        "rollback_bundle",
        "export_diagnostics",
        "apply_config",
        "link_chatgpt",
        "rebuild_app_layer",
    }
)
FAILURE_CODES = frozenset(
    {
        "activation_failed",
        "app_layer_rebuild_failed",
        "attestation_failed",
        "attestation_mismatch",
        "backup_failed",
        "bundle_not_found",
        "bundle_verification_failed",
        "canceled",
        "config_apply_failed",
        "diagnostics_export_failed",
        "idempotency_mismatch",
        "invalid_command",
        "link_chatgpt_failed",
        "rollback_failed",
        "unsupported_kind",
    }
)


class RuntimeCommandError(ValueError):
    """Raised for invalid command specs before irreversible work starts."""


def _default_gateway_stop_command() -> tuple[str, ...]:
    return ("systemctl", "stop", "tinyhat-runtime-gateway.service")


def _default_gateway_start_command() -> tuple[str, ...]:
    return ("systemctl", "start", "tinyhat-runtime-gateway.service")


def _default_health_command() -> tuple[str, ...]:
    return (str(paths.CURRENT_LINK / "bin" / "tinyhat-runtime"), "gateway", "health")


def _normalize_failure_code(value: str) -> str:
    return value if value in FAILURE_CODES else "invalid_command"


def _no_restart_failure_payload(exc: Exception) -> dict[str, Any]:
    return {
        "detail": redact_text(str(exc), limit=1000),
        "restart_requested": False,
        "systemd_restart_requested": False,
    }


def _safe_command_sequence(value: Sequence[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(str(item) for item in value)


class RuntimeCommandRunner:
    """Execute the narrow runtime command set from the platform ledger."""

    def __init__(
        self,
        *,
        ledger: CommandLedger | None = None,
        bundles_dir: Path = paths.BUNDLES_DIR,
        current_link: Path = paths.CURRENT_LINK,
        diagnostics_dir: Path = paths.DIAGNOSTICS_DIR,
        stop_command: Sequence[str] | None = None,
        start_command: Sequence[str] | None = None,
        health_command: Sequence[str] | None = None,
        service_restart: bool = True,
        platform_get_json: Callable[[str], dict[str, Any]] | None = None,
        apply_runtime_config: Callable[..., dict[str, Any]] | None = None,
        start_chatgpt_link: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        rebuild_backup_dir: Path = paths.REBUILD_BACKUP_DIR,
    ) -> None:
        self.ledger = ledger or CommandLedger()
        self.bundles_dir = bundles_dir
        self.current_link = current_link
        self.diagnostics_dir = diagnostics_dir
        self.rebuild_backup_dir = rebuild_backup_dir
        self.platform_get_json = platform_get_json
        self.apply_runtime_config = apply_runtime_config
        self.start_chatgpt_link = start_chatgpt_link
        self.stop_command = (
            _safe_command_sequence(stop_command)
            if stop_command is not None
            else _default_gateway_stop_command()
            if service_restart
            else None
        )
        self.start_command = (
            _safe_command_sequence(start_command)
            if start_command is not None
            else _default_gateway_start_command()
            if service_restart
            else None
        )
        self.health_command = (
            _safe_command_sequence(health_command)
            if health_command is not None
            else _default_health_command()
        )

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        command_id = validate_command_id(str(command.get("command_id") or ""))
        idempotency_key = str(command.get("idempotency_key") or "")
        kind = str(command.get("kind") or "")
        existing = self.ledger.load(command_id)
        if existing is not None:
            existing_key = str(existing.get("idempotency_key") or "")
            if existing_key and idempotency_key and existing_key != idempotency_key:
                return {
                    "schema": COMMAND_RESULT_SCHEMA,
                    "command_id": command_id,
                    "idempotency_key": idempotency_key,
                    "kind": kind,
                    "status": "failed",
                    "phase": "validate",
                    "failure_code": "idempotency_mismatch",
                    "observed_at": utc_now_iso(),
                    "result": {
                        "detail": "idempotency_key mismatch for existing local mirror",
                        "existing_status": existing.get("status"),
                    },
                }
            if existing.get("status") in TERMINAL_STATUSES:
                result = existing.get("result")
                if not isinstance(result, dict):
                    result = {}
                result = dict(result)
                result["duplicate"] = True
                result["existing_status"] = existing.get("status")
                return {
                    "schema": COMMAND_RESULT_SCHEMA,
                    "command_id": command_id,
                    "idempotency_key": idempotency_key,
                    "kind": kind or str(existing.get("kind") or ""),
                    "status": str(existing.get("status")),
                    "phase": str(existing.get("phase") or "duplicate"),
                    "failure_code": existing.get("failure_code"),
                    "observed_at": utc_now_iso(),
                    "result": result,
                }
        self.ledger.mirror(command)

        if kind not in ALLOWED_COMMAND_KINDS:
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="validate",
                failure_code="unsupported_kind",
                result={"detail": f"unsupported command kind: {kind}"},
            )
        if not idempotency_key:
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="validate",
                failure_code="invalid_command",
                result={"detail": "idempotency_key is required"},
            )

        self.ledger.update(command_id, status="running", phase="start")
        if self._cancel_requested(command):
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="canceled",
                phase="safe_cancel",
                failure_code="canceled",
                result={"detail": "cancel requested before irreversible work"},
            )

        try:
            if kind == "activate_bundle":
                return self._activate_bundle(command)
            if kind == "rollback_bundle":
                return self._rollback_bundle(command)
            if kind == "export_diagnostics":
                return self._export_diagnostics(command)
            if kind == "apply_config":
                return self._apply_config(command)
            if kind == "link_chatgpt":
                return self._link_chatgpt(command)
            if kind == "rebuild_app_layer":
                return self._rebuild_app_layer(command)
        except (bundle.BundleVerificationError, RuntimeCommandError) as exc:
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="validate",
                failure_code=(
                    "bundle_verification_failed"
                    if isinstance(exc, bundle.BundleVerificationError)
                    else "invalid_command"
                ),
                result={"detail": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - command boundary
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="execute",
                failure_code="invalid_command",
                result={"detail": redact_text(str(exc), limit=1000)},
            )

        raise AssertionError("unreachable")

    def _activate_bundle(self, command: dict[str, Any]) -> dict[str, Any]:
        spec = _spec(command)
        command_id, idempotency_key, kind = _ids(command)
        expected_bundle_id = _required_string(spec, "bundle_id")
        bundle_dir = self._bundle_dir_for_id(expected_bundle_id)
        if not bundle_dir.exists():
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="stage",
                failure_code="bundle_not_found",
                result={"bundle_id": expected_bundle_id, "bundle_dir": str(bundle_dir)},
            )

        self.ledger.update(command_id, status="running", phase="verify")
        manifest = bundle.load_manifest(bundle_dir)
        bundle.verify_manifest(bundle_dir, manifest)
        if manifest.get("bundle_id") != expected_bundle_id:
            raise RuntimeCommandError("bundle_id does not match staged manifest")

        self.ledger.update(command_id, status="running", phase="flip_before_start")
        result = launcher.activate_bundle(
            bundle_dir,
            current_link=self.current_link,
            stop_command=self.stop_command,
            start_command=self.start_command,
            health_command=self.health_command,
        )
        result_payload = {
            "activation": result.__dict__,
            "bundle_id": expected_bundle_id,
        }
        if not result.activated:
            status = "rolled_back" if result.rolled_back else "failed"
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status=status,
                phase=result.phase,
                failure_code="activation_failed",
                result=result_payload,
            )

        self.ledger.update(command_id, status="running", phase="attest")
        try:
            attestation_doc = self._current_attestation()
        except Exception as exc:  # noqa: BLE001 - rollback gate
            rollback = self._rollback_to_previous(result.previous_target)
            result_payload["attestation_error"] = redact_text(str(exc), limit=1000)
            result_payload["rollback"] = rollback.__dict__ if rollback else None
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="rolled_back" if rollback and rollback.activated else "failed",
                phase="attestation_gate",
                failure_code=(
                    "attestation_failed" if rollback and rollback.activated else "rollback_failed"
                ),
                result=result_payload,
            )
        result_payload["attestation"] = attestation_doc
        if attestation_doc.get("bundle_id") != expected_bundle_id:
            rollback = self._rollback_to_previous(result.previous_target)
            result_payload["rollback"] = rollback.__dict__ if rollback else None
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="rolled_back" if rollback and rollback.activated else "failed",
                phase="attestation_gate",
                failure_code=(
                    "attestation_mismatch" if rollback and rollback.activated else "rollback_failed"
                ),
                result=result_payload,
            )

        return self._finish(
            command_id=command_id,
            idempotency_key=idempotency_key,
            kind=kind,
            status="applied",
            phase="attested",
            result=result_payload,
        )

    def _rollback_bundle(self, command: dict[str, Any]) -> dict[str, Any]:
        spec = _spec(command)
        command_id, idempotency_key, kind = _ids(command)
        target_bundle_id = _required_string(spec, "bundle_id")
        bundle_dir = self._bundle_dir_for_id(target_bundle_id)
        if not bundle_dir.exists():
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="stage",
                failure_code="bundle_not_found",
                result={"bundle_id": target_bundle_id, "bundle_dir": str(bundle_dir)},
            )
        self.ledger.update(command_id, status="running", phase="rollback")
        result = launcher.activate_bundle(
            bundle_dir,
            current_link=self.current_link,
            stop_command=self.stop_command,
            start_command=self.start_command,
            health_command=self.health_command,
        )
        return self._finish(
            command_id=command_id,
            idempotency_key=idempotency_key,
            kind=kind,
            status="applied" if result.activated else "failed",
            phase="rolled_back" if result.activated else result.phase,
            failure_code=None if result.activated else "rollback_failed",
            result={"activation": result.__dict__, "bundle_id": target_bundle_id},
        )

    def _export_diagnostics(self, command: dict[str, Any]) -> dict[str, Any]:
        command_id, idempotency_key, kind = _ids(command)
        self.ledger.update(command_id, status="running", phase="export_diagnostics")
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.diagnostics_dir / f"{command_id}.zip"
        payload = openclaw_adapter.export_diagnostics(output_path=output_path)
        status = (
            "applied"
            if payload.get("state") in {"ready", "ready_with_warnings"}
            else "failed"
        )
        return self._finish(
            command_id=command_id,
            idempotency_key=idempotency_key,
            kind=kind,
            status=status,
            phase="exported" if status == "applied" else "export_failed",
            failure_code=None if status == "applied" else "diagnostics_export_failed",
            result=payload,
        )

    def _apply_config(self, command: dict[str, Any]) -> dict[str, Any]:
        spec = _spec(command)
        command_id, idempotency_key, kind = _ids(command)
        desired_revision = _required_int(spec, "desired_config_revision")
        if spec.get("hot_required") is False:
            raise RuntimeCommandError("apply_config.hot_required must be true")
        if self.platform_get_json is None:
            raise RuntimeCommandError("platform_get_json callback is required")
        if self.apply_runtime_config is None:
            raise RuntimeCommandError("apply_runtime_config callback is required")

        self.ledger.update(command_id, status="running", phase="pull_runtime_secrets")
        try:
            payload = self.platform_get_json("/hapi/v1/computers/me/runtime-secrets")
        except RuntimeCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - platform boundary
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="pull_runtime_secrets",
                failure_code="config_apply_failed",
                result=_no_restart_failure_payload(exc),
            )
        revision = int(payload.get("revision") or desired_revision)
        raw_secrets = payload.get("secrets") or {}
        if not isinstance(raw_secrets, dict):
            raise RuntimeCommandError("/me/runtime-secrets returned a non-object secrets map")
        secrets = {str(key): str(value) for key, value in raw_secrets.items()}

        self.ledger.update(command_id, status="running", phase="check_hot_support")
        try:
            result = self.apply_runtime_config(
                revision=revision,
                secrets=secrets,
                dry_run=True,
            )
        except RuntimeCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - runtime config boundary
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="check_hot_support",
                failure_code="config_apply_failed",
                result=_no_restart_failure_payload(exc),
            )
        result_payload = {
            "desired_config_revision": desired_revision,
            "applied_revision": revision,
            "secret_count": int(result.get("secret_count") or len(secrets)),
            "reload": result.get("reload") or {},
            "env_block_changed": bool(result.get("env_block_changed")),
            "gateway_config_changed": bool(result.get("gateway_config_changed")),
            "model_auth_signature_changed": bool(
                result.get("model_auth_signature_changed")
            ),
            "gateway_rebind_requested": False,
            "restart_requested": False,
            "systemd_restart_requested": False,
        }

        self.ledger.update(command_id, status="running", phase="hot_reload")
        try:
            result = self.apply_runtime_config(
                revision=revision,
                secrets=secrets,
                dry_run=False,
            )
        except RuntimeCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - runtime config boundary
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="hot_reload",
                failure_code="config_apply_failed",
                result=_no_restart_failure_payload(exc),
            )
        result_payload.update(
            {
                "secret_count": int(result.get("secret_count") or len(secrets)),
                "reload": result.get("reload") or {},
                "env_block_changed": bool(result.get("env_block_changed")),
                "gateway_config_changed": bool(result.get("gateway_config_changed")),
                "model_auth_signature_changed": bool(
                    result.get("model_auth_signature_changed")
                ),
                "gateway_rebind_requested": False,
                "restart_requested": False,
                "systemd_restart_requested": False,
            }
        )
        return self._finish(
            command_id=command_id,
            idempotency_key=idempotency_key,
            kind=kind,
            status="applied",
            phase="hot_reloaded",
            result=result_payload,
        )

    def _link_chatgpt(self, command: dict[str, Any]) -> dict[str, Any]:
        spec = _spec(command)
        command_id, idempotency_key, kind = _ids(command)
        session_id = _required_string(spec, "session_id")
        provider = str(spec.get("provider") or "openai").strip()
        model_ref = str(spec.get("model_ref") or "openai/gpt-5.5").strip()
        auth_flow = str(spec.get("auth_flow") or "device_code").strip()
        required_auth_path = str(
            spec.get("required_final_auth_path") or "chatgpt_subscription"
        ).strip()
        if provider != "openai":
            raise RuntimeCommandError("link_chatgpt.provider must be openai")
        if auth_flow != "device_code":
            raise RuntimeCommandError("link_chatgpt.auth_flow must be device_code")
        if required_auth_path != "chatgpt_subscription":
            raise RuntimeCommandError(
                "link_chatgpt.required_final_auth_path must be chatgpt_subscription"
            )
        if self.start_chatgpt_link is None:
            raise RuntimeCommandError("start_chatgpt_link callback is required")

        self.ledger.update(command_id, status="running", phase="start_device_code")
        try:
            start = self.start_chatgpt_link(
                {
                    "type": "start_chatgpt_link",
                    "session_id": session_id,
                    "provider": provider,
                    "model_ref": model_ref,
                    "reason": "runtime_command_link_chatgpt",
                }
            ) or {
                "state": "started",
                "session_id": session_id,
                "worker": "openclaw_device_code",
            }
        except RuntimeCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - device-code worker boundary
            payload = _no_restart_failure_payload(exc)
            payload.update(
                {
                    "session_id": session_id,
                    "provider": provider,
                    "model_ref": model_ref,
                    "auth_flow": auth_flow,
                    "required_final_auth_path": required_auth_path,
                }
            )
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="start_device_code",
                failure_code="link_chatgpt_failed",
                result=payload,
            )
        return self._finish(
            command_id=command_id,
            idempotency_key=idempotency_key,
            kind=kind,
            status="applied",
            phase="device_code_started",
            result={
                "session_id": session_id,
                "provider": provider,
                "model_ref": model_ref,
                "auth_flow": auth_flow,
                "required_final_auth_path": required_auth_path,
                "restart_requested": False,
                "systemd_restart_requested": False,
                "start": start,
            },
        )

    def _rebuild_app_layer(self, command: dict[str, Any]) -> dict[str, Any]:
        spec = _spec(command)
        command_id, idempotency_key, kind = _ids(command)
        requested_bundle_id = str(spec.get("bundle_id") or "").strip()
        bundle_dir = (
            self._bundle_dir_for_id(requested_bundle_id)
            if requested_bundle_id
            else self.current_link
        )
        if not bundle_dir.exists():
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="stage",
                failure_code="bundle_not_found",
                result={"bundle_id": requested_bundle_id or None},
            )

        self.ledger.update(command_id, status="running", phase="verify_current_bundle")
        manifest = bundle.load_manifest(bundle_dir)
        bundle.verify_manifest(bundle_dir, manifest)
        bundle_id = str(manifest.get("bundle_id") or "")
        if requested_bundle_id and bundle_id != requested_bundle_id:
            raise RuntimeCommandError("bundle_id does not match staged manifest")

        self.ledger.update(command_id, status="running", phase="snapshot")
        self.rebuild_backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.rebuild_backup_dir.chmod(0o700)
        except OSError:
            pass
        backup_path = self.rebuild_backup_dir / f"{command_id}-openclaw-backup.tar.gz"
        backup_result = openclaw_adapter.backup_create(output_path=backup_path)
        result_payload: dict[str, Any] = {
            "bundle_id": bundle_id,
            "reason": str(spec.get("reason") or "admin_rebuild_app_layer"),
            "backup": backup_result,
            "restart_requested": True,
            "systemd_restart_requested": self.stop_command is not None
            or self.start_command is not None,
            "restart_reason": "explicit_rebuild_app_layer_command",
            "automatic_restart_loop": False,
        }
        if backup_result.get("state") != "ready":
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="snapshot",
                failure_code="backup_failed",
                result=result_payload,
            )

        self.ledger.update(command_id, status="running", phase="reactivate_bundle")
        activation = launcher.activate_bundle(
            bundle_dir,
            current_link=self.current_link,
            stop_command=self.stop_command,
            start_command=self.start_command,
            health_command=self.health_command,
        )
        result_payload["activation"] = activation.__dict__
        if not activation.activated:
            status = "rolled_back" if activation.rolled_back else "failed"
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status=status,
                phase=activation.phase,
                failure_code="app_layer_rebuild_failed",
                result=result_payload,
            )

        self.ledger.update(command_id, status="running", phase="openclaw_doctor")
        doctor = openclaw_adapter.doctor_repair()
        result_payload["doctor"] = doctor
        if doctor.get("state") != "ready":
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="openclaw_doctor",
                failure_code="app_layer_rebuild_failed",
                result=result_payload,
            )

        self.ledger.update(command_id, status="running", phase="openclaw_status")
        result_payload["status"] = openclaw_adapter.status_json()

        self.ledger.update(command_id, status="running", phase="attest")
        try:
            result_payload["attestation"] = self._current_attestation()
        except Exception as exc:  # noqa: BLE001 - final safety gate
            result_payload["attestation_error"] = redact_text(str(exc), limit=1000)
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="attestation_gate",
                failure_code="attestation_failed",
                result=result_payload,
            )
        if result_payload["attestation"].get("bundle_id") != bundle_id:
            return self._finish(
                command_id=command_id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="failed",
                phase="attestation_gate",
                failure_code="attestation_mismatch",
                result=result_payload,
            )

        return self._finish(
            command_id=command_id,
            idempotency_key=idempotency_key,
            kind=kind,
            status="applied",
            phase="attested",
            result=result_payload,
        )

    def _bundle_dir_for_id(self, bundle_id: str) -> Path:
        if not bundle_id.startswith("sha256:"):
            raise RuntimeCommandError("bundle_id must use sha256:<hex> format")
        digest = bundle_id.removeprefix("sha256:")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise RuntimeCommandError("bundle_id digest must be 64 lowercase hex chars")
        return self.bundles_dir / digest

    def _current_attestation(self) -> dict[str, Any]:
        manifest = bundle.load_manifest(self.current_link)
        bundle.verify_manifest(self.current_link, manifest)
        identity_doc = (
            load_identity_document(paths.IDENTITY_FILE)
            if paths.IDENTITY_FILE.exists()
            else {}
        )
        return attestation.build_attestation(
            bundle_manifest=manifest,
            identity_doc=identity_doc,
            openclaw=openclaw_adapter.adapter_attestation(),
        )

    def _rollback_to_previous(self, previous_target: str | None) -> launcher.ActivationResult | None:
        if not previous_target:
            return None
        return launcher.activate_bundle(
            Path(previous_target),
            current_link=self.current_link,
            stop_command=self.stop_command,
            start_command=self.start_command,
            health_command=self.health_command,
        )

    @staticmethod
    def _cancel_requested(command: dict[str, Any]) -> bool:
        return bool(command.get("cancel_requested") is True or command.get("cancel_requested_at"))

    def _finish(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        kind: str,
        status: str,
        phase: str,
        failure_code: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        failure = _normalize_failure_code(failure_code) if failure_code else None
        payload = {
            "schema": COMMAND_RESULT_SCHEMA,
            "command_id": command_id,
            "idempotency_key": idempotency_key,
            "kind": kind,
            "status": status,
            "phase": phase,
            "failure_code": failure,
            "observed_at": utc_now_iso(),
            "result": redact_json(result or {}),
        }
        self.ledger.update(
            command_id,
            status=status,
            phase=phase,
            failure_code=failure,
            result=payload["result"],
        )
        return payload


def load_command_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeCommandError("command file must contain a JSON object")
    return payload


def _spec(command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") or {}
    if not isinstance(spec, dict):
        raise RuntimeCommandError("command spec must be a JSON object")
    return spec


def _ids(command: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(command.get("command_id") or ""),
        str(command.get("idempotency_key") or ""),
        str(command.get("kind") or ""),
    )


def _required_string(spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeCommandError(f"spec.{key} is required")
    return value


def _required_int(spec: dict[str, Any], key: str) -> int:
    value = spec.get(key)
    if isinstance(value, bool):
        raise RuntimeCommandError(f"spec.{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeCommandError(f"spec.{key} is required") from exc
    if parsed < 0:
        raise RuntimeCommandError(f"spec.{key} must be non-negative")
    return parsed
