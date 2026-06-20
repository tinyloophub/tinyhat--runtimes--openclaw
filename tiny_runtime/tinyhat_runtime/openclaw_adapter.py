"""The only tiny_runtime module allowed to invoke OpenClaw commands."""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import paths
from .redaction import redact_json, redact_text


@dataclass(frozen=True)
class AdapterResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json_payload(self) -> dict[str, Any]:
        if not self.stdout.strip():
            return {}
        payload = json.loads(self.stdout)
        if not isinstance(payload, dict):
            raise ValueError("OpenClaw JSON response must be an object")
        return payload

    def public_summary(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": redact_text(self.stdout, limit=1000),
            "stderr": redact_text(self.stderr, limit=1000),
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


def openclaw_env(
    *,
    state_dir: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, str]:
    resolved_state = state_dir or paths.OPENCLAW_STATE_DIR
    resolved_config = config_path or paths.OPENCLAW_CONFIG_PATH
    base_env = dict(os.environ)
    base_env["PATH"] = f"{paths.BUNDLE_OPENCLAW_BIN}{os.pathsep}{base_env.get('PATH', '')}"
    return {
        **base_env,
        "HOME": str(resolved_state),
        "OPENCLAW_BUNDLE_DIR": str(paths.BUNDLE_OPENCLAW_DIR),
        "OPENCLAW_STATE_DIR": str(resolved_state),
        "OPENCLAW_CONFIG_PATH": str(resolved_config),
    }


def run_openclaw(
    args: Sequence[str],
    *,
    timeout: int | None = 30,
    runner: Runner = subprocess.run,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> AdapterResult:
    argv = ("openclaw", *tuple(args))
    try:
        completed = runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env or openclaw_env(),
            input=input_text,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AdapterResult(command=argv, returncode=127, stdout="", stderr=str(exc))
    return AdapterResult(
        command=argv,
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def inspect_plugin(plugin_id: str, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("plugins", "inspect", plugin_id, "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "plugin": redact_json(result.json_payload())}


def install_plugin(source: str, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(
        ("plugins", "install", source, "--force"),
        timeout=120,
        runner=runner,
    )
    if not result.ok:
        return {"state": "failed", "detail": result.public_summary()}
    return {"state": "ready", "detail": result.public_summary()}


def config_patch(
    patch: dict[str, Any],
    *,
    dry_run: bool = False,
    replace_paths: Sequence[str] = (),
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    args: list[str] = ["config", "patch", "--stdin"]
    if dry_run:
        args.extend(["--dry-run", "--json"])
    for path in replace_paths:
        args.extend(["--replace-path", path])
    result = run_openclaw(
        tuple(args),
        runner=runner,
        input_text=json.dumps(patch, sort_keys=True),
    )
    if not result.ok:
        return {"state": "failed", "detail": result.public_summary()}
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            payload = redact_json(result.json_payload())
        except json.JSONDecodeError:
            payload = {"stdout": redact_text(result.stdout)}
    return {"state": "ready", "patch": payload, "detail": result.public_summary()}


def gateway_health(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("gateway", "health", "--json"), runner=runner)
    if not result.ok:
        return {"state": "unhealthy", "detail": result.public_summary()}
    return {"state": "healthy", "gateway": redact_json(result.json_payload())}


def gateway_status(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("gateway", "status", "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "gateway": redact_json(result.json_payload())}


def gateway_run() -> int:
    try:
        return subprocess.call(
            [
                "openclaw",
                "gateway",
                "run",
                "--allow-unconfigured",
                "--bind",
                "loopback",
                "--auth",
                "none",
                "--tailscale",
                "off",
            ],
            env=openclaw_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(redact_text(str(exc)), flush=True)
        return 127


def models_status(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("models", "status", "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "models": redact_json(result.json_payload())}


def secrets_reload(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("secrets", "reload", "--json"), runner=runner)
    if not result.ok:
        return {"state": "failed", "detail": result.public_summary()}
    return {"state": "ready", "secrets": redact_json(result.json_payload())}


def backup_create(
    *,
    output_path: Path,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Create and verify an OpenClaw backup archive on this Computer."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_openclaw(
        (
            "backup",
            "create",
            "--output",
            str(output_path),
            "--verify",
            "--json",
        ),
        timeout=300,
        runner=runner,
    )
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            payload = redact_json(result.json_payload())
        except (json.JSONDecodeError, ValueError):
            payload = {"stdout": redact_text(result.stdout)}
    archive_exists = output_path.exists()
    return {
        "state": "ready"
        if result.ok and archive_exists and payload.get("verified") is True
        else "failed",
        "archive_path": str(output_path) if archive_exists else None,
        "archive_bytes": output_path.stat().st_size if archive_exists else None,
        "backup": payload,
        "detail": result.public_summary(),
    }


def doctor_repair(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Run OpenClaw's non-interactive safe repair path."""
    result = run_openclaw(
        ("doctor", "--fix", "--non-interactive", "--yes"),
        timeout=300,
        runner=runner,
    )
    return {
        "state": "ready" if result.ok else "failed",
        "detail": result.public_summary(),
    }


def status_json(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("status", "--json"), timeout=30, runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    try:
        status_payload = redact_json(result.json_payload())
    except (json.JSONDecodeError, ValueError):
        status_payload = {"stdout": redact_text(result.stdout)}
    return {
        "state": "ready",
        "status": status_payload,
        "detail": result.public_summary(),
    }


def _tinyhat_file_secret_ref(pointer: str) -> dict[str, str]:
    return {"source": "file", "provider": "tinyhat", "id": pointer}


def _tinyhat_plugin_config(
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if platform_base_url:
        config["platformBaseUrl"] = platform_base_url
    if backend_audience:
        config["backendAudience"] = backend_audience
    return config


def warm_image_config_patch(
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
) -> dict[str, Any]:
    tinyhat_plugin_config = _tinyhat_plugin_config(
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )
    return {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": int(os.environ.get("TINYHAT_OPENCLAW_GATEWAY_PORT", "18789")),
            "auth": {"mode": "none"},
            "tailscale": {"mode": "off"},
        },
        "agents": {
            "defaults": {
                "workspace": str(paths.OPENCLAW_STATE_DIR / "workspace"),
                "model": {"primary": "openai/gpt-5.5"},
                "compaction": {"reserveTokensFloor": 20000},
            }
        },
        "channels": {
            "telegram": {
                "enabled": False,
                "dmPolicy": "disabled",
                "groupPolicy": "disabled",
            }
        },
        "plugins": {
            "entries": {
                "telegram": {"enabled": True},
                "openai": {"enabled": True},
                "codex": {"enabled": True},
                "codex-supervisor": {"enabled": True},
                "tinyhat": {
                    "enabled": True,
                    "config": tinyhat_plugin_config,
                },
            }
        },
        "secrets": {
            "providers": {
                "tinyhat": {
                    "source": "file",
                    "path": str(paths.OPENCLAW_SECRETS_PATH),
                    "mode": "json",
                }
            },
            "defaults": {"file": "tinyhat"},
        },
        "session": {"dmScope": "per-channel-peer"},
    }


def apply_warm_image_config(
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    return config_patch(
        warm_image_config_patch(
            platform_base_url=platform_base_url,
            backend_audience=backend_audience,
        ),
        replace_paths=("channels.telegram",),
        runner=runner,
    )


def _deep_merge_json_objects(
    existing: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in overlay.items():
        previous = merged.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_json_objects(previous, value)
        else:
            merged[key] = value
    return merged


def _read_existing_secrets(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _count_secret_leaves(payload: Any) -> int:
    if isinstance(payload, dict):
        return sum(_count_secret_leaves(value) for value in payload.values())
    return 1


def write_openclaw_secrets(
    secrets: dict[str, Any],
    *,
    merge: bool = True,
) -> dict[str, Any]:
    paths.OPENCLAW_SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_existing_secrets(paths.OPENCLAW_SECRETS_PATH) if merge else {}
    payload = _deep_merge_json_objects(existing, secrets)
    tmp_path = paths.OPENCLAW_SECRETS_PATH.with_name(
        f".{paths.OPENCLAW_SECRETS_PATH.name}.tmp"
    )
    tmp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(paths.OPENCLAW_SECRETS_PATH)
    return {
        "state": "ready",
        "secret_count": _count_secret_leaves(secrets),
        "merged": merge,
    }


def binding_secrets_payload(binding: dict[str, Any]) -> dict[str, Any]:
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")
    openrouter_key = str(binding.get("openrouter_api_key") or "").strip()
    return {
        "channels": {"telegram": {"botToken": bot_token}},
        "providers": {"openrouter": {"apiKey": openrouter_key}},
    }


def binding_config_patch(
    binding: dict[str, Any],
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
) -> dict[str, Any]:
    owner_id = str(binding.get("telegram_owner_user_id") or "").strip()
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not owner_id:
        raise ValueError("binding is missing telegram_owner_user_id")
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")

    openrouter_key = str(binding.get("openrouter_api_key") or "").strip()
    openrouter_model = str(binding.get("openrouter_default_model") or "").strip()
    llm_model_ref = str(binding.get("llm_model_ref") or "").strip()
    llm_auth_mode = str(binding.get("llm_auth_mode") or "platform_credits").strip()

    primary_model = (
        llm_model_ref
        if llm_auth_mode == "chatgpt_subscription" and llm_model_ref
        else f"openrouter/{openrouter_model.lstrip('/')}"
        if openrouter_model
        else "openai/gpt-5.5"
    )
    tinyhat_plugin_config = _tinyhat_plugin_config(
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )

    patch: dict[str, Any] = {
        "agents": {"defaults": {"model": {"primary": primary_model}}},
        "channels": {
            "telegram": {
                "enabled": True,
                "dmPolicy": "allowlist",
                "groupPolicy": "disabled",
                "allowFrom": [owner_id],
                "botToken": _tinyhat_file_secret_ref("/channels/telegram/botToken"),
                "execApprovals": {"approvers": [owner_id]},
            }
        },
        "plugins": {
            "entries": {
                "tinyhat": {"config": tinyhat_plugin_config},
            }
        },
        "secrets": {
            "providers": {
                "tinyhat": {
                    "source": "file",
                    "path": str(paths.OPENCLAW_SECRETS_PATH),
                    "mode": "json",
                }
            },
            "defaults": {"file": "tinyhat"},
        },
        "session": {"dmScope": "per-channel-peer"},
    }
    if openrouter_key:
        patch["models"] = {
            "providers": {
                "openrouter": {
                    "apiKey": _tinyhat_file_secret_ref("/providers/openrouter/apiKey")
                }
            }
        }
    return patch


def apply_binding_config(
    binding: dict[str, Any],
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
    preserve_existing_secrets: bool = True,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    patch = binding_config_patch(
        binding,
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )
    secrets_result = write_openclaw_secrets(
        binding_secrets_payload(binding),
        merge=preserve_existing_secrets,
    )
    patch_result = config_patch(
        patch,
        replace_paths=("channels.telegram",),
        runner=runner,
    )
    patch_result["secrets"] = secrets_result
    return patch_result


def _redact_zip_member(name: str, data: bytes) -> bytes:
    lowered = name.lower()
    if lowered.endswith(".json"):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return redact_text(data.decode("utf-8", errors="replace")).encode("utf-8")
        return (
            json.dumps(redact_json(payload), indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return b"[binary omitted by Tinyhat diagnostics redaction]\n"
    if "\x00" in text:
        return b"[binary omitted by Tinyhat diagnostics redaction]\n"
    return redact_text(text).encode("utf-8")


def redact_diagnostics_zip(path: Path) -> list[str]:
    tmp_path = path.with_name(f".{path.name}.redacted.tmp")
    entries: list[str] = []
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(
        tmp_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as dst:
        for info in src.infolist():
            if info.is_dir():
                continue
            entries.append(info.filename)
            dst.writestr(info.filename, _redact_zip_member(info.filename, src.read(info)))
    os.replace(tmp_path, path)
    return sorted(entries)


def export_diagnostics(
    *,
    output_path: Path,
    runner: Runner = subprocess.run,
    log_bytes: int = 1_000_000,
    log_lines: int = 5_000,
    timeout_ms: int = 3_000,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_openclaw(
        (
            "gateway",
            "diagnostics",
            "export",
            "--json",
            "--output",
            str(output_path),
            "--log-bytes",
            str(log_bytes),
            "--log-lines",
            str(log_lines),
            "--timeout",
            str(timeout_ms),
        ),
        timeout=max(5, int(timeout_ms / 1000) + 5),
        runner=runner,
    )
    if not output_path.exists():
        return {
            "state": "failed",
            "output_path": str(output_path),
            "detail": result.public_summary(),
        }
    entries = redact_diagnostics_zip(output_path)
    export_payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                export_payload = redact_json(parsed)
        except json.JSONDecodeError:
            export_payload = {"stdout": redact_text(result.stdout)}
    return {
        "state": "ready" if result.ok else "ready_with_warnings",
        "output_path": str(output_path),
        "zip_bytes": output_path.stat().st_size,
        "entries": entries,
        "export": export_payload,
        "detail": result.public_summary(),
    }


def adapter_attestation(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    return {
        "schema": "openclaw_adapter_attestation_v1",
        "plugin": inspect_plugin("tinyhat", runner=runner),
        "gateway": gateway_health(runner=runner),
        "models": models_status(runner=runner),
    }
