"""``tinyhat whoami`` — prove the GCE/Computer binding visibly and cheaply.

Identity only, never credentials: platform computer id, GCE instance
id, hostname, runtime ref, the platform origin this box reports to,
tailnet enrollment, and whether an agent is bound (owner id — never the
bot token). Everything still passes the output sanitizer.
"""

from __future__ import annotations

import json
import socket
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup


def _binding_visibility() -> dict[str, Any]:
    """Non-secret binding facts from the OpenClaw config, if present."""
    sup = _sup()
    block: dict[str, Any] = {"bound": False, "telegram_owner_user_id": None}
    try:
        with open(sup.openclaw_config_path(), encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return block
    if not isinstance(config, dict):
        return block
    telegram = ((config.get("channels") or {}).get("telegram")) or {}
    if not isinstance(telegram, dict):
        return block
    block["bound"] = telegram.get("enabled") is True
    allow_from = telegram.get("allowFrom")
    if isinstance(allow_from, list) and allow_from:
        block["telegram_owner_user_id"] = str(allow_from[0])
    return block


def run(ctx) -> dict[str, Any]:
    sup = _sup()

    try:
        identity = sup._runtime_state_identity()
    except Exception:
        identity = {"computer_id": None, "instance_id": None, "runtime_ref": None}

    try:
        platform_base_url = sup.get_backend_base_url() or None
    except Exception:
        platform_base_url = None
    try:
        backend_audience = sup.get_backend_audience() or None
    except Exception:
        backend_audience = None

    try:
        private_access = sup.private_access_report()
    except Exception:
        private_access = None
    if isinstance(private_access, dict):
        private_access = {
            key: private_access.get(key)
            for key in ("provider", "state", "node_name", "tailnet_ip")
            if key in private_access
        }

    data: dict[str, Any] = {
        "computer_id": identity.get("computer_id"),
        "instance_id": identity.get("instance_id"),
        "hostname": socket.gethostname(),
        "runtime_ref": identity.get("runtime_ref"),
        "platform_base_url": platform_base_url,
        "backend_audience": backend_audience,
        "gce_metadata_available": bool(sup._gce_metadata_available()),
        "dev_mode": bool(sup._dev_mode()),
        "private_access": private_access,
        "binding": _binding_visibility(),
    }
    return data


def render(data: dict[str, Any]) -> list[str]:
    lines = [
        f"computer id:   {data.get('computer_id') or '(unknown)'}",
        f"gce instance:  {data.get('instance_id') or '(unknown)'}",
        f"hostname:      {data.get('hostname')}",
        f"runtime:       {data.get('runtime_ref') or '(unknown)'}",
        f"platform:      {data.get('platform_base_url') or '(unresolved)'}",
        f"audience:      {data.get('backend_audience') or '(unresolved)'}",
    ]
    private_access = data.get("private_access")
    if isinstance(private_access, dict) and private_access:
        lines.append(
            f"tailnet:       {private_access.get('node_name') or '?'} "
            f"({private_access.get('tailnet_ip') or '?'}) "
            f"state={private_access.get('state')}"
        )
    else:
        lines.append("tailnet:       (no private-access enrollment found)")
    binding = data.get("binding") or {}
    if binding.get("bound"):
        lines.append(
            "binding:       bound "
            f"(telegram owner user id {binding.get('telegram_owner_user_id')})"
        )
    else:
        lines.append("binding:       not bound (no agent assigned)")
    if data.get("dev_mode"):
        lines.append("mode:          DEV runtime (TINYHAT_DEV_RUNTIME=1)")
    return lines
