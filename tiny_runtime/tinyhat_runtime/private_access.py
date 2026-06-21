"""Tiny runtime private-access enrollment and reporting.

The platform mints short-lived enrollment material; the VM consumes it locally
and reports only non-secret Tailscale state back on heartbeats.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .redaction import redact_text

STATUS_PATH = Path("/var/lib/tinyhat-private-access/bootstrap-status.json")
TAILSCALE_STATUS_TIMEOUT_SECONDS = 10
TAILSCALE_UP_TIMEOUT_SECONDS = 120

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _status_path() -> Path:
    return Path(os.environ.get("TINYHAT_PRIVATE_ACCESS_STATUS_PATH") or STATUS_PATH)


def _write_status(
    payload: dict[str, Any], *, path: Path | None = None
) -> dict[str, Any]:
    target = path or _status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        target.chmod(0o644)
    except OSError:
        pass
    return payload


def _truthy_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _run(
    args: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def enroll_from_env(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Enroll the VM from explicit environment-provided private-access material.

    Normal Computer startup uses ``enroll_from_payload`` through the
    Computer-authenticated platform API. This env path is reserved for explicit
    repair/dev invocations that intentionally provide the provider material
    out-of-band.
    """

    return enroll_from_config(
        provider=os.environ.get("TINYHAT_PRIVATE_ACCESS_PROVIDER"),
        auth_key=os.environ.get("TINYHAT_TAILSCALE_AUTH_KEY"),
        node_name=os.environ.get("TINYHAT_TAILSCALE_NODE_NAME"),
        tags=os.environ.get("TINYHAT_TAILSCALE_TAGS"),
        ssh_enabled=_truthy_env("TINYHAT_TAILSCALE_SSH", default=True),
        runner=runner,
    )


def enroll_from_payload(
    payload: dict[str, Any],
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Enroll using a one-time platform-issued payload.

    Runtime commands use this path so the Tailscale auth key is pulled directly
    by the authenticated Computer and never lands in the platform command
    ledger or the local command SQLite mirror.
    """

    tags = payload.get("tailscale_tags")
    if isinstance(tags, list):
        tags = ",".join(str(tag).strip() for tag in tags if str(tag).strip())
    return enroll_from_config(
        provider=payload.get("provider"),
        auth_key=payload.get("tailscale_auth_key"),
        node_name=payload.get("tailscale_node_name"),
        tags=tags,
        ssh_enabled=bool(payload.get("tailscale_ssh", True)),
        runner=runner,
    )


def enroll_from_config(
    *,
    provider: Any,
    auth_key: Any,
    node_name: Any,
    tags: Any = None,
    ssh_enabled: bool = True,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Enroll into the configured private-access provider."""

    provider = str(provider or "").strip().lower()
    if provider != "tailscale":
        return _write_status(
            {
                "provider": provider or "disabled",
                "state": "disabled",
                "diagnostic": "private access disabled",
            }
        )

    auth_key = str(auth_key or "").strip()
    node_name = str(node_name or "").strip()
    tags = str(tags or "").strip()
    if not auth_key or not node_name:
        return _write_status(
            {
                "provider": "tailscale",
                "state": "config_missing",
                "diagnostic": "missing auth key or node name",
            }
        )

    if shutil.which("tailscale") is None:
        install = runner(
            ["bash", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if install.returncode != 0:
            return _write_status(
                {
                    "provider": "tailscale",
                    "state": "error",
                    "diagnostic": (
                        "tailscale install failed: "
                        + redact_text((install.stderr or install.stdout or "").strip())
                    )[:500],
                }
            )

    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        started = _run(
            [systemctl, "enable", "--now", "tailscaled"],
            runner=runner,
            timeout=60,
        )
        if started.returncode != 0:
            return _write_status(
                {
                    "provider": "tailscale",
                    "state": "error",
                    "diagnostic": (
                        "tailscaled start failed: "
                        + redact_text((started.stderr or started.stdout or "").strip())
                    )[:500],
                }
            )

    secret_dir = _status_path().parent / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    try:
        secret_dir.chmod(0o700)
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(
        "w",
        prefix="tailscale-auth.",
        dir=secret_dir,
        delete=False,
    ) as handle:
        auth_file = Path(handle.name)
        handle.write(auth_key)
    try:
        auth_file.chmod(0o600)
        up_args = [
            "tailscale",
            "up",
            f"--auth-key=file:{auth_file}",
            f"--hostname={node_name}",
        ]
        if ssh_enabled:
            up_args.append("--ssh")
        if tags:
            up_args.append(f"--advertise-tags={tags}")
        _run(
            ["tailscale", "logout"],
            runner=runner,
            timeout=TAILSCALE_STATUS_TIMEOUT_SECONDS,
        )
        result = _run(up_args, runner=runner, timeout=TAILSCALE_UP_TIMEOUT_SECONDS)
    finally:
        try:
            auth_file.unlink()
        except OSError:
            pass

    if result.returncode != 0:
        return _write_status(
            {
                "provider": "tailscale",
                "state": "error",
                "node_name": node_name,
                "ssh_enabled": ssh_enabled,
                "diagnostic": (
                    "tailscale up failed: "
                    + redact_text((result.stderr or result.stdout or "").strip())
                )[:500],
            }
        )

    return _write_status(
        {
            "provider": "tailscale",
            "state": "ready",
            "node_name": node_name,
            "ssh_enabled": ssh_enabled,
            "diagnostic": "tailscale enrollment completed",
        }
    )


def _load_bootstrap_status() -> dict[str, Any]:
    try:
        payload = json.loads(_status_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def private_access_report(*, runner: Runner = subprocess.run) -> dict[str, Any] | None:
    """Return a heartbeat-safe private-access report."""

    bootstrap = _load_bootstrap_status()
    provider = (
        str(
            bootstrap.get("provider")
            or os.environ.get("TINYHAT_PRIVATE_ACCESS_PROVIDER")
            or ""
        )
        .strip()
        .lower()
    )
    if provider != "tailscale":
        return None

    base: dict[str, Any] = {
        "provider": "tailscale",
        "node_name": bootstrap.get("node_name")
        or os.environ.get("TINYHAT_TAILSCALE_NODE_NAME")
        or None,
    }
    if shutil.which("tailscale") is None:
        return {
            **base,
            "state": "not_installed",
            "diagnostic_code": "tailscale_cli_missing",
            "diagnostic": "tailscale CLI is not installed",
        }

    result = _run(
        ["tailscale", "status", "--json"],
        runner=runner,
        timeout=TAILSCALE_STATUS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return {
            **base,
            "state": "unreachable",
            "diagnostic_code": "tailscale_status_failed",
            "diagnostic": redact_text((result.stderr or result.stdout or "").strip())[
                :500
            ],
        }

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            **base,
            "state": "error",
            "diagnostic_code": "tailscale_status_json_invalid",
            "diagnostic": str(exc)[:500],
        }
    if not isinstance(payload, dict):
        return {
            **base,
            "state": "error",
            "diagnostic_code": "tailscale_status_json_invalid",
            "diagnostic": "tailscale status returned a non-object payload",
        }

    self_node = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    ips = self_node.get("TailscaleIPs")
    tailnet_ip = (
        next((str(ip).strip() for ip in ips if str(ip).strip()), None)
        if isinstance(ips, list)
        else None
    )
    node_name = (
        str(self_node.get("HostName") or "").strip()
        or str(base.get("node_name") or "").strip()
        or None
    )
    backend_state = str(payload.get("BackendState") or "").strip()
    ready = bool(tailnet_ip) and (
        backend_state.lower() == "running" or self_node.get("Online") is True
    )
    return {
        **base,
        "node_name": node_name,
        "tailnet_ip": tailnet_ip,
        "state": "ready" if ready else "unreachable",
        "diagnostic_code": "ready" if ready else "tailscale_not_running",
        "diagnostic": backend_state or "tailscale status read",
    }
