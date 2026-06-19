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
    args: list[str] = ["config", "patch", "--stdin", "--json"]
    if dry_run:
        args.append("--dry-run")
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
    openrouter_base = str(binding.get("openrouter_base_url") or "").strip()
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
    env_block: dict[str, str] = {}
    if openrouter_key:
        env_block["OPENROUTER_API_KEY"] = openrouter_key

    tinyhat_plugin_config: dict[str, Any] = {}
    if platform_base_url:
        tinyhat_plugin_config["platformBaseUrl"] = platform_base_url
    if backend_audience:
        tinyhat_plugin_config["backendAudience"] = backend_audience

    patch: dict[str, Any] = {
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
                "model": {"primary": primary_model},
                "compaction": {"reserveTokensFloor": 20000},
            }
        },
        "channels": {
            "telegram": {
                "enabled": True,
                "dmPolicy": "allowlist",
                "groupPolicy": "disabled",
                "allowFrom": [owner_id],
                "botToken": bot_token,
            }
        },
        "commands": {"ownerAllowFrom": [f"telegram:{owner_id}"]},
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
    if openrouter_base:
        patch.setdefault("models", {}).setdefault("providers", {})["openrouter"] = {
            "baseURL": openrouter_base
        }
    if env_block:
        patch["env"] = env_block
    return patch


def apply_binding_config(
    binding: dict[str, Any],
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    patch = binding_config_patch(
        binding,
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )
    return config_patch(
        patch,
        replace_paths=("plugins.entries", "channels.telegram", "commands.ownerAllowFrom"),
        runner=runner,
    )


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
