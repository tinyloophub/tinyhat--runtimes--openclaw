"""The only tiny_runtime module allowed to invoke OpenClaw commands."""

from __future__ import annotations

import json
import os
import subprocess
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
) -> AdapterResult:
    completed = runner(
        ["openclaw", *tuple(args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env or openclaw_env(),
    )
    return AdapterResult(
        command=("openclaw", *tuple(args)),
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def inspect_plugin(plugin_id: str, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("plugins", "inspect", plugin_id, "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "plugin": redact_json(result.json_payload())}


def gateway_health(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("gateway", "health", "--json"), runner=runner)
    if not result.ok:
        return {"state": "unhealthy", "detail": result.public_summary()}
    return {"state": "healthy", "gateway": redact_json(result.json_payload())}


def gateway_run() -> int:
    return subprocess.call(["openclaw", "gateway", "run"], env=openclaw_env())


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


def adapter_attestation(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    return {
        "schema": "openclaw_adapter_attestation_v1",
        "plugin": inspect_plugin("tinyhat", runner=runner),
        "gateway": gateway_health(runner=runner),
        "models": models_status(runner=runner),
    }
