#!/usr/bin/env python3
"""Tinyhat Computer runtime supervisor.

This is the platform-owned process that runs on every Tinyhat
Computer (a private VM). It owns all platform communication for the
Computer lifecycle:

  - reports lifecycle state (``ready`` / ``active`` / ``broken``);
  - polls the platform for a binding (which Telegram bot + owner +
    optional provider credentials this Computer should run);
  - writes the framework (OpenClaw) config for that binding;
  - starts / monitors the framework gateway under systemd;
  - heartbeats while active and watches for re-binds / unassigns.

Configuration is read from the VM's instance metadata at runtime,
never baked into this file:

  - ``tinyhat-backend-audience`` — the JWT audience the platform's
    GCE-identity verifier requires (env fallback
    ``TINYHAT_BACKEND_AUDIENCE``).
  - ``tinyhat-platform-base-url`` — where to POST ``/me/*`` calls,
    re-read every loop so an admin URL change propagates without a
    VM restart (env fallback ``TINYHAT_PLATFORM_BASE_URL``).

Keeping this code in a standalone public repository (instead of an
inline startup-script heredoc) is the whole point of the runtime
repo: the Computer-side platform behaviour is versioned, auditable,
and reproducible from an explicit ref/tag/SHA.

Development mode (``TINYHAT_DEV_RUNTIME=1``)
============================================

Set ``TINYHAT_DEV_RUNTIME=1`` to run the supervisor without GCE
metadata, without systemd, and without a real GCE identity token —
the shape needed for a local Docker container talking to a
worktree's dev backend. In dev mode:

- The GCE metadata server is never contacted; ``TINYHAT_PLATFORM_BASE_URL``
  and ``TINYHAT_BACKEND_AUDIENCE`` env vars are read directly.
- The bearer token is a constant marker (``dev-runtime``). The
  platform's ``computer_identity_verifier`` already accepts any
  bearer when ``ENV=development`` AND ``DEV_AUTO_COMPUTER_ID=<row>``
  is set; that is the only safe pairing.
- The OpenClaw gateway is run as a subprocess managed by this
  supervisor (no ``systemctl`` / ``journalctl``).
- ``OPENCLAW_CONFIG_PATH`` / ``OPENCLAW_STATE_DIR`` move under
  ``$TINYHAT_RUNTIME_HOME`` (default ``/var/lib/tinyhat-openclaw``,
  but the dev Dockerfile points it at a writable workspace) so the
  container does not need root-owned ``/etc`` writes.

Dev mode is fail-closed against production: the runtime never sends
a real bearer in dev mode, and the platform-side bypass only fires
when ``ENV=development``. Running the dev image against a prod
backend therefore authenticates as nothing and is rejected.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

# Path conventions on the VM. Pinned here so the gateway systemd
# unit (written by bootstrap.sh) and this supervisor stay in
# lockstep. Dev mode (``TINYHAT_DEV_RUNTIME=1``) overrides each of
# these to a writable subdirectory of ``$TINYHAT_RUNTIME_HOME`` so
# the container does not need root ``/etc`` access.
_DEFAULT_OPENCLAW_CONFIG_PATH = "/etc/openclaw/openclaw.json"
_DEFAULT_OPENCLAW_STATE_DIR = "/var/lib/tinyhat-openclaw"
_DEFAULT_OPENCLAW_WORKSPACE_DIR = "/var/lib/tinyhat-openclaw/workspace"
_DEFAULT_TINYHAT_SECRETS_PATH = "/etc/openclaw/tinyhat-secrets.json"
OPENCLAW_GATEWAY_PORT = 18789
OPENCLAW_DEFAULT_MODEL = "openai/gpt-5.2"
TINYHAT_SECRETS_PROVIDER = "tinyhat"
TINYHAT_OPENAI_API_KEY_NAME = "OPENAI_API_KEY"
TINYHAT_OPENAI_API_KEY_POINTER = "/OPENAI_API_KEY"
TINYHAT_PLUGIN_ID = "tinyhat"
GATEWAY_SYSTEMD_UNIT = "tinyhat-openclaw-gateway.service"
PRIVATE_ACCESS_BOOTSTRAP_STATUS_PATH = (
    "/var/lib/tinyhat-private-access/bootstrap-status.json"
)

# Instance-metadata keys the platform writes at insert time and can
# update later via ``compute.instances.setMetadata``. Both are
# re-read on a short cache so admin changes propagate without a VM
# restart. Each falls back to an env var (set by bootstrap.sh) when
# the metadata server is unreachable or the key is missing.
METADATA_BASE_URL_KEY = "tinyhat-platform-base-url"
METADATA_AUDIENCE_KEY = "tinyhat-backend-audience"
METADATA_TTL_SECONDS = 30

BINDING_POLL_BASE_SECONDS = 3
BINDING_POLL_IDLE_CAP_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 30
GATEWAY_INACTIVE_GRACE_SECONDS = 30
OPENCLAW_SECRETS_RELOAD_ATTEMPTS = 3
OPENCLAW_SECRETS_RELOAD_TIMEOUT_SECONDS = 12
OPENCLAW_SECRETS_RELOAD_RETRY_DELAYS_SECONDS = (1, 2)

# Marker bearer used in dev mode so the request reaches the
# Computer-authenticated platform routes. The platform's verifier
# ignores the bearer body entirely when
# ``DEV_AUTO_COMPUTER_ID`` is set under ``ENV=development``; this
# string therefore carries no secret value.
DEV_RUNTIME_BEARER = "dev-runtime"


def _dev_mode() -> bool:
    """True when this supervisor is running against a dev backend.

    Set ``TINYHAT_DEV_RUNTIME=1`` in the container's environment to
    flip the systemd / metadata-server / GCE-identity-token paths to
    their local equivalents. Off by default — production behaviour
    is unchanged.
    """
    return (os.environ.get("TINYHAT_DEV_RUNTIME") or "").strip() == "1"


def _runtime_home() -> str:
    """Root for dev-mode writable state.

    Defaults to ``_DEFAULT_OPENCLAW_STATE_DIR`` for parity with prod
    paths; the dev Dockerfile points it at a workspace the
    unprivileged container user owns.
    """
    return (
        os.environ.get("TINYHAT_RUNTIME_HOME") or _DEFAULT_OPENCLAW_STATE_DIR
    ).rstrip("/")


def openclaw_config_path() -> str:
    if _dev_mode():
        return os.path.join(_runtime_home(), "openclaw", "openclaw.json")
    return _DEFAULT_OPENCLAW_CONFIG_PATH


def openclaw_state_dir() -> str:
    if _dev_mode():
        return _runtime_home()
    return _DEFAULT_OPENCLAW_STATE_DIR


def openclaw_workspace_dir() -> str:
    if _dev_mode():
        return os.path.join(_runtime_home(), "workspace")
    return _DEFAULT_OPENCLAW_WORKSPACE_DIR


def tinyhat_secrets_path() -> str:
    """Path where the supervisor writes Computer-scoped runtime secrets.

    Production intentionally uses OpenClaw's host-level config dir. Dev
    containers chown this one directory to the unprivileged runtime user
    so the local harness exercises the same file path without running the
    supervisor as root.
    """
    return (
        os.environ.get("TINYHAT_SECRETS_PATH") or _DEFAULT_TINYHAT_SECRETS_PATH
    ).strip()


def runtime_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def tinyhat_plugin_dir() -> str:
    return os.path.join(runtime_dir(), "plugins", TINYHAT_PLUGIN_ID)


def _openclaw_cli_env() -> dict[str, str]:
    return {
        **os.environ,
        "HOME": openclaw_state_dir(),
        "OPENCLAW_CONFIG_PATH": openclaw_config_path(),
        "OPENCLAW_STATE_DIR": openclaw_state_dir(),
    }


# Back-compat names kept for callers that reach in by attribute
# (tests + the prod bootstrap heredoc). These are the prod paths;
# dev callers must go through the helper functions above.
OPENCLAW_CONFIG_PATH = _DEFAULT_OPENCLAW_CONFIG_PATH
OPENCLAW_STATE_DIR = _DEFAULT_OPENCLAW_STATE_DIR
OPENCLAW_WORKSPACE_DIR = _DEFAULT_OPENCLAW_WORKSPACE_DIR
TINYHAT_SECRETS_PATH = _DEFAULT_TINYHAT_SECRETS_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s tinyhat-supervisor: %(message)s",
)
log = logging.getLogger("tinyhat-supervisor")


_base_url_cache = {"value": None, "ts": 0.0}
_audience_cache = {"value": None, "ts": 0.0}


def _read_metadata_value(key: str, timeout: int = 5) -> str:
    url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        f"instance/attributes/{key}"
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


def get_backend_base_url() -> str:
    """Resolve the platform base URL.

    Priority:
      1. ``tinyhat-platform-base-url`` instance metadata, cached for
         ``METADATA_TTL_SECONDS`` so we don't hammer the metadata
         server every loop.
      2. The ``TINYHAT_PLATFORM_BASE_URL`` env fallback bootstrap.sh
         wrote.

    Dev mode skips step 1 entirely (there is no metadata server in a
    local container) and reads the env var directly.
    """
    now = time.time()
    cached = _base_url_cache.get("value")
    ts = float(_base_url_cache.get("ts") or 0.0)
    if cached and (now - ts) < METADATA_TTL_SECONDS:
        return cached
    fallback = (os.environ.get("TINYHAT_PLATFORM_BASE_URL") or "").strip()
    if _dev_mode():
        value = ""
    else:
        try:
            value = _read_metadata_value(METADATA_BASE_URL_KEY)
        except Exception as exc:
            log.warning(
                "metadata read for %s failed: %s; using fallback",
                METADATA_BASE_URL_KEY,
                exc,
            )
            value = ""
    resolved = value or fallback
    if resolved != cached:
        log.info(
            "platform base URL = %s (metadata=%r fallback=%r)",
            resolved,
            value or None,
            fallback or None,
        )
    _base_url_cache["value"] = resolved
    _base_url_cache["ts"] = now
    return resolved


def get_backend_audience() -> str:
    """Resolve the JWT audience for the GCE identity token.

    Same precedence as :func:`get_backend_base_url`: instance
    metadata first (``tinyhat-backend-audience``), then the
    ``TINYHAT_BACKEND_AUDIENCE`` env fallback. The audience is far
    more stable than the base URL but is read the same way so the
    supervisor has zero baked-in deployment config.
    """
    now = time.time()
    cached = _audience_cache.get("value")
    ts = float(_audience_cache.get("ts") or 0.0)
    if cached and (now - ts) < METADATA_TTL_SECONDS:
        return cached
    fallback = (os.environ.get("TINYHAT_BACKEND_AUDIENCE") or "").strip()
    if _dev_mode():
        value = ""
    else:
        try:
            value = _read_metadata_value(METADATA_AUDIENCE_KEY)
        except Exception as exc:
            log.warning(
                "metadata read for %s failed: %s; using fallback",
                METADATA_AUDIENCE_KEY,
                exc,
            )
            value = ""
    resolved = value or fallback
    if resolved != cached:
        log.info("backend audience = %s", resolved)
    _audience_cache["value"] = resolved
    _audience_cache["ts"] = now
    return resolved


def fetch_identity_token() -> str:
    """Fetch a Google-signed VM identity JWT for this Computer.

    In dev mode there is no metadata server and no GCE identity to
    sign — return the constant marker bearer. The platform's
    ``computer_identity_verifier`` short-circuits on
    ``DEV_AUTO_COMPUTER_ID`` (only honoured under
    ``ENV=development``) and never inspects this string, so it
    carries no secret value.
    """
    if _dev_mode():
        return DEV_RUNTIME_BEARER
    audience = get_backend_audience()
    url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        "instance/service-accounts/default/identity"
        f"?audience={audience}&format=full"
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8").strip()


def post_json(path: str, body: dict) -> dict:
    token = fetch_identity_token()
    base = get_backend_base_url()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def get_json(path: str) -> dict:
    token = fetch_identity_token()
    base = get_backend_base_url()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def private_access_report() -> dict | None:
    """Return optional non-secret private-access status for heartbeat."""
    if not os.path.exists(PRIVATE_ACCESS_BOOTSTRAP_STATUS_PATH):
        return None
    bootstrap_status: dict = {}
    try:
        with open(PRIVATE_ACCESS_BOOTSTRAP_STATUS_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
            if isinstance(payload, dict):
                bootstrap_status = payload
    except Exception as exc:
        log.warning("could not read private access bootstrap status: %s", exc)

    if bootstrap_status.get("provider") != "tailscale":
        return None
    report = {
        "provider": "tailscale",
        "state": str(bootstrap_status.get("state") or "unreachable"),
    }
    if shutil.which("tailscale") is None:
        report.update(
            {
                "state": "not_installed",
                "diagnostic_code": "tailscale_cli_missing",
                "diagnostic": "tailscale CLI is not installed on this Computer",
            }
        )
        return report

    try:
        status = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        report.update(
            {
                "state": "error",
                "diagnostic_code": "tailscale_status_failed",
                "diagnostic": str(exc)[:500],
            }
        )
        return report

    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "").strip()
        report.update(
            {
                "state": "unreachable",
                "diagnostic_code": "tailscale_status_failed",
                "diagnostic": detail[:500] or "tailscale status failed",
            }
        )
        return report

    try:
        status_payload = json.loads(status.stdout or "{}")
    except json.JSONDecodeError as exc:
        report.update(
            {
                "state": "error",
                "diagnostic_code": "tailscale_status_json_invalid",
                "diagnostic": str(exc)[:500],
            }
        )
        return report

    self_node = status_payload.get("Self") or {}
    if not isinstance(self_node, dict):
        self_node = {}
    ips = self_node.get("TailscaleIPs") or []
    tailnet_ip = ""
    if isinstance(ips, list):
        tailnet_ip = next((str(ip) for ip in ips if str(ip).startswith("100.")), "")
    node_name = str(self_node.get("HostName") or "").strip()
    backend_state = str(status_payload.get("BackendState") or "").strip()
    report.update(
        {
            "state": "ready" if tailnet_ip else "unreachable",
            "node_name": node_name,
            "tailnet_ip": tailnet_ip,
            "diagnostic_code": "ready" if tailnet_ip else "missing_tailnet_ip",
            "diagnostic": backend_state or "tailscale status read",
        }
    )
    return report


def _tinyhat_plugin_version() -> str:
    package_json = os.path.join(tinyhat_plugin_dir(), "package.json")
    try:
        with open(package_json, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        raise RuntimeError(
            f"Tinyhat plugin package is missing at {package_json}"
        )
    return str(payload.get("version") or "unknown")


def _tinyhat_plugin_marker_path() -> str:
    return os.path.join(openclaw_state_dir(), "tinyhat-plugin.version")


def ensure_tinyhat_plugin_installed() -> bool:
    """Install the bundled Tinyhat tool plugin into OpenClaw.

    The plugin is shipped in the runtime repo and installed locally so
    OpenClaw can expose the Computer-scoped credential tools at
    gateway startup. It contains no Tinyhat credentials; requests
    authenticate with the same GCE Computer identity token boundary as
    the supervisor.
    """
    plugin_dir = tinyhat_plugin_dir()
    if not os.path.isdir(plugin_dir):
        raise RuntimeError(f"Tinyhat plugin not found at {plugin_dir}")
    version = _tinyhat_plugin_version()
    marker = _tinyhat_plugin_marker_path()
    installed_manifest = os.path.join(
        openclaw_state_dir(),
        "extensions",
        TINYHAT_PLUGIN_ID,
        "openclaw.plugin.json",
    )
    try:
        with open(marker, encoding="utf-8") as fh:
            marker_version = fh.read().strip()
    except FileNotFoundError:
        marker_version = ""
    if marker_version == version and os.path.exists(installed_manifest):
        log.info("Tinyhat plugin already installed (version=%s)", version)
        return True

    cmd = ["openclaw", "plugins", "install", plugin_dir, "--force"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        env=_openclaw_cli_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Tinyhat plugin install failed: {detail}")
    os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(version + "\n")
    log.info("installed Tinyhat plugin (version=%s)", version)
    return True


def try_install_tinyhat_plugin() -> bool:
    """Best-effort install for optional chat credential tools.

    The runtime must still boot without this plugin: core agent
    credentials flow through the supervisor's secret-file path, while
    the plugin only adds metadata-only helper tools for chat UX.
    """
    try:
        return ensure_tinyhat_plugin_installed()
    except Exception as exc:
        log.warning(
            "Tinyhat plugin unavailable; continuing without "
            "credential tools: %s",
            exc,
        )
        return False


def _atomic_write_json(path: str, payload: dict, *, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _secret_ref_for_openai_api_key() -> dict:
    return {
        "source": "file",
        "provider": TINYHAT_SECRETS_PROVIDER,
        "id": TINYHAT_OPENAI_API_KEY_POINTER,
    }


def _ensure_tinyhat_secret_provider_config(config: dict) -> None:
    secrets_cfg = config.setdefault("secrets", {})
    providers = secrets_cfg.setdefault("providers", {})
    providers[TINYHAT_SECRETS_PROVIDER] = {
        "source": "file",
        "path": tinyhat_secrets_path(),
        "mode": "json",
    }
    defaults = secrets_cfg.setdefault("defaults", {})
    defaults["file"] = TINYHAT_SECRETS_PROVIDER


def _sync_openai_api_key_ref(config: dict, secrets: dict[str, str]) -> None:
    models_cfg = config.setdefault("models", {})
    providers = models_cfg.setdefault("providers", {})
    openai_cfg = providers.setdefault("openai", {})
    expected_ref = _secret_ref_for_openai_api_key()
    if (secrets.get(TINYHAT_OPENAI_API_KEY_NAME) or "").strip():
        openai_cfg["apiKey"] = expected_ref
        return

    # Removing OPENAI_API_KEY should stop advertising Tinyhat's file
    # ref. Only remove the value when it is the one this supervisor
    # manages; leave unrelated operator-authored config alone.
    if openai_cfg.get("apiKey") == expected_ref:
        del openai_cfg["apiKey"]
    if not openai_cfg:
        del providers["openai"]
    if not providers:
        del models_cfg["providers"]
    if not models_cfg:
        del config["models"]


def sync_openclaw_secret_ref_config(secrets: dict[str, str]) -> None:
    """Update openclaw.json with Tinyhat's file SecretRef surfaces."""
    config_path = openclaw_config_path()
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        config = {}

    _ensure_tinyhat_secret_provider_config(config)
    _sync_openai_api_key_ref(config, secrets)
    _atomic_write_json(config_path, config)
    log.info(
        "synced OpenClaw SecretRef config (provider=%s openai_ref=%s)",
        TINYHAT_SECRETS_PROVIDER,
        "yes" if (secrets.get(TINYHAT_OPENAI_API_KEY_NAME) or "").strip() else "no",
    )


def _tinyhat_plugin_config() -> dict:
    """Return non-secret config for the bundled OpenClaw tool plugin."""
    plugin_config: dict = {"devMode": _dev_mode()}
    base_url = get_backend_base_url()
    if base_url:
        plugin_config["platformBaseUrl"] = base_url
    audience = get_backend_audience()
    if audience:
        plugin_config["backendAudience"] = audience
    if _dev_mode():
        plugin_config["devBearer"] = DEV_RUNTIME_BEARER
    return plugin_config


def write_tinyhat_secrets_file(secrets: dict[str, str]) -> None:
    """Write the latest Computer-scoped secret map for OpenClaw.

    Secret names are metadata; values are never logged. The file is an
    OpenClaw JSON file provider source, so refs such as
    ``/OPENAI_API_KEY`` resolve against the top-level object.
    """
    normalized = {
        str(name): str(value)
        for name, value in (secrets or {}).items()
        if str(name).strip()
    }
    path = tinyhat_secrets_path()
    _atomic_write_json(path, normalized)
    log.info(
        "wrote Tinyhat runtime secrets file to %s (keys=%d)",
        path,
        len(normalized),
    )


def read_tinyhat_secrets_file() -> dict[str, str]:
    """Read the last applied Computer-scoped runtime-secret map.

    This is used during supervisor restart / rebind to rebuild
    ``openclaw.json`` from the secrets already persisted on the
    Computer disk. Values remain process-local and are never logged.
    """
    path = tinyhat_secrets_path()
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("could not read Tinyhat runtime secrets file at %s: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        log.warning("Tinyhat runtime secrets file at %s is not a JSON object", path)
        return {}
    return {
        str(name): str(value)
        for name, value in payload.items()
        if str(name).strip()
    }


def _redact_known_secret_values(text: str, secrets: dict[str, str]) -> str:
    redacted = text
    for value in (secrets or {}).values():
        candidate = str(value or "")
        if len(candidate) >= 4:
            redacted = redacted.replace(candidate, "[redacted]")
    return redacted


def _diagnostic_from_exception(exc: Exception, secrets: dict[str, str]) -> str:
    return _redact_known_secret_values(str(exc), secrets)[:1023]


def _openclaw_reload_retryable(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "gateway did not respond" in lowered
        or "secrets runtime snapshot is not active" in lowered
    )


def _openclaw_reload_snapshot_inactive(detail: str) -> bool:
    return "secrets runtime snapshot is not active" in detail.lower()


def reload_openclaw_secrets(secrets: dict[str, str]) -> dict:
    """Ask the running gateway to refresh its SecretRef snapshot."""
    cmd = [
        "openclaw",
        "secrets",
        "reload",
        "--json",
    ]
    env = _openclaw_cli_env()
    last_detail = ""
    for attempt in range(1, OPENCLAW_SECRETS_RELOAD_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=OPENCLAW_SECRETS_RELOAD_TIMEOUT_SECONDS,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            last_detail = _redact_known_secret_values(
                f"openclaw secrets reload timed out after "
                f"{OPENCLAW_SECRETS_RELOAD_TIMEOUT_SECONDS}s",
                secrets,
            )
            if exc.stderr:
                last_detail += ": " + _redact_known_secret_values(
                    str(exc.stderr),
                    secrets,
                )
            if attempt < OPENCLAW_SECRETS_RELOAD_ATTEMPTS:
                delay_seconds = OPENCLAW_SECRETS_RELOAD_RETRY_DELAYS_SECONDS[
                    attempt - 1
                ]
                log.warning(
                    "openclaw secrets reload attempt %d timed out; "
                    "retrying in %ds",
                    attempt,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                continue
            break
        if result.returncode == 0:
            output = (result.stdout or "").strip()
            if not output:
                return {}
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return {"raw": _redact_known_secret_values(output, secrets)}

        last_detail = _redact_known_secret_values(
            (result.stderr or result.stdout or "").strip(),
            secrets,
        )
        if (
            attempt < OPENCLAW_SECRETS_RELOAD_ATTEMPTS
            and _openclaw_reload_retryable(last_detail)
        ):
            delay_seconds = OPENCLAW_SECRETS_RELOAD_RETRY_DELAYS_SECONDS[attempt - 1]
            log.warning(
                "openclaw secrets reload attempt %d failed during gateway "
                "settle; retrying in %ds: %s",
                attempt,
                delay_seconds,
                last_detail,
            )
            time.sleep(delay_seconds)
            continue
        break

    if last_detail and _openclaw_reload_snapshot_inactive(last_detail):
        log.info(
            "openclaw secrets reload skipped because no active secret "
            "snapshot exists yet; file provider config is synced"
        )
        return {
            "skipped": True,
            "reason": "secrets_runtime_snapshot_inactive",
        }

    raise RuntimeError(
        "openclaw secrets reload failed"
        + (f": {last_detail}" if last_detail else " (no detail)")
    )


def apply_runtime_secret_map(*, revision: int, secrets: dict[str, str]) -> dict:
    """Apply one latest runtime-secret revision to OpenClaw."""
    write_tinyhat_secrets_file(secrets)
    sync_openclaw_secret_ref_config(secrets)
    reload_result = reload_openclaw_secrets(secrets)
    return {
        "revision": revision,
        "secret_count": len(secrets or {}),
        "reload": reload_result,
    }


def write_openclaw_config(
    binding: dict,
    *,
    enable_tinyhat_plugin: bool = True,
) -> None:
    """Write the real OpenClaw gateway config for this binding."""
    owner_id = str(binding.get("telegram_owner_user_id") or "").strip()
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not owner_id:
        raise ValueError("binding is missing telegram_owner_user_id")
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")

    config_path = openclaw_config_path()
    state_dir = openclaw_state_dir()
    workspace_dir = openclaw_workspace_dir()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    os.makedirs(workspace_dir, mode=0o700, exist_ok=True)

    # OpenRouter runtime config when the platform delivered it on
    # this binding. OpenClaw's OpenRouter provider reads
    # ``OPENROUTER_API_KEY`` from config env and expects
    # provider-qualified model refs such as
    # ``openrouter/openai/...:free``.
    openrouter_key = str(binding.get("openrouter_api_key") or "").strip()
    openrouter_base = str(binding.get("openrouter_base_url") or "").strip()
    openrouter_model = str(binding.get("openrouter_default_model") or "").strip()

    def openrouter_model_ref(raw: str) -> str:
        model = (raw or "openai/gpt-oss-20b:free").strip()
        if model.startswith("openrouter/"):
            return model
        return "openrouter/" + model.lstrip("/")

    openrouter_enabled = bool(openrouter_key and openrouter_base)
    primary_model = (
        openrouter_model_ref(openrouter_model)
        if openrouter_enabled
        else OPENCLAW_DEFAULT_MODEL
    )
    openai_plugin = {"enabled": True}
    plugin_entries = {
        "telegram": {"enabled": True},
        "openai": openai_plugin,
    }
    if enable_tinyhat_plugin:
        plugin_entries[TINYHAT_PLUGIN_ID] = {
            "enabled": True,
            "config": _tinyhat_plugin_config(),
        }
    else:
        log.warning(
            "Tinyhat credential tools are disabled for this OpenClaw boot"
        )

    config = {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": OPENCLAW_GATEWAY_PORT,
            "auth": {"mode": "none"},
            "tailscale": {"mode": "off"},
        },
        "agents": {
            "defaults": {
                "workspace": workspace_dir,
                "model": {"primary": primary_model},
                "agentRuntime": {"id": "pi"},
            },
        },
        "channels": {
            "telegram": {
                "enabled": True,
                "dmPolicy": "allowlist",
                "groupPolicy": "disabled",
                "allowFrom": [owner_id],
                "botToken": bot_token,
            },
        },
        "commands": {
            "ownerAllowFrom": ["telegram:" + owner_id],
        },
        "plugins": {
            "entries": plugin_entries,
        },
        "session": {"dmScope": "per-channel-peer"},
    }
    _ensure_tinyhat_secret_provider_config(config)
    current_secrets = read_tinyhat_secrets_file()
    _sync_openai_api_key_ref(config, current_secrets)
    if openrouter_enabled:
        config["env"] = {"OPENROUTER_API_KEY": openrouter_key}
    _atomic_write_json(config_path, config)
    # Log only non-secret summary; never log the API key.
    log.info(
        "wrote OpenClaw config to %s "
        "(bot=@%s owner=%s model=%s openrouter=%s openai_ref=%s)",
        config_path,
        binding.get("telegram_bot_username"),
        owner_id,
        primary_model,
        "yes" if openrouter_enabled else "no",
        (
            "yes"
            if (current_secrets.get(TINYHAT_OPENAI_API_KEY_NAME) or "").strip()
            else "no"
        ),
    )


def delete_telegram_webhook(binding: dict) -> None:
    """Clear Tinyhat's fallback webhook before OpenClaw long-polls."""
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")
    payload = json.dumps({"drop_pending_updates": False}).encode("utf-8")
    last_error = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/deleteWebhook",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8") or "{}"
            data = json.loads(body)
            if data.get("ok") is not True:
                raise RuntimeError("Telegram deleteWebhook returned ok=false")
            log.info(
                "cleared Telegram webhook for OpenClaw long polling (bot=@%s)",
                binding.get("telegram_bot_username"),
            )
            return
        except Exception as exc:
            last_error = exc
            log.warning(
                "Telegram deleteWebhook failed before OpenClaw handoff "
                "(attempt %d): %s",
                attempt,
                exc,
            )
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError(
        f"Telegram deleteWebhook failed before OpenClaw handoff: {last_error}"
    )


def _run_systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError("systemctl " + " ".join(args) + " failed: " + detail)
    return result


# In dev mode the supervisor owns the OpenClaw gateway process
# directly instead of delegating to systemd. The Popen handle lives
# here so the four lifecycle entry points share state.
_dev_gateway: dict = {"proc": None, "log_path": None}


def _dev_gateway_log_path() -> str:
    return os.path.join(openclaw_state_dir(), "openclaw-gateway.log")


def _start_openclaw_gateway_dev(binding: dict) -> float:
    """Spawn ``openclaw gateway run`` as a child of this supervisor.

    Replaces the systemd ``restart`` path in dev mode. The
    subprocess's stdout / stderr stream into ``openclaw-gateway.log``
    under the state dir so the health probe in
    :func:`_probe_openclaw_gateway_health_dev` can read them; they
    do NOT flow to the container's stdout, so ``docker logs`` shows
    only the supervisor's own log lines. A maintainer who needs the
    gateway's output runs ``docker exec -it <container> tail -f
    $TINYHAT_RUNTIME_HOME/openclaw-gateway.log``. If a prior gateway
    is still alive, it is stopped first (idempotent restart).
    """
    if _dev_gateway["proc"] is not None and _dev_gateway["proc"].poll() is None:
        log.info("dev: stopping previous openclaw gateway before restart")
        _stop_openclaw_gateway_dev()
    state_dir = openclaw_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    log_path = _dev_gateway_log_path()
    # ``log_fh`` is kept open intentionally — subprocess.Popen
    # inherits it as its stdout/stderr and writes for the lifetime
    # of the gateway. Closing here would lose every log line.
    log_fh = open(log_path, "ab", buffering=0)  # noqa: SIM115
    cmd = [
        "openclaw",
        "gateway",
        "run",
        "--force",
        "--allow-unconfigured",
        "--port",
        str(OPENCLAW_GATEWAY_PORT),
        "--bind",
        "loopback",
        "--auth",
        "none",
        "--tailscale",
        "off",
        "--verbose",
    ]
    # OpenClaw reads its config path from ``OPENCLAW_CONFIG_PATH`` in
    # the process env (set below + by the prod systemd unit); the
    # ``gateway run`` subcommand does not accept a ``--config`` flag.
    log.info(
        "dev: starting OpenClaw gateway subprocess: bot=@%s owner=%s port=%s "
        "log=%s",
        binding.get("telegram_bot_username"),
        binding.get("telegram_owner_user_id"),
        OPENCLAW_GATEWAY_PORT,
        log_path,
    )
    proc = subprocess.Popen(
        cmd,
        cwd=state_dir,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env={
            **os.environ,
            "HOME": state_dir,
            "OPENCLAW_CONFIG_PATH": openclaw_config_path(),
            "OPENCLAW_STATE_DIR": state_dir,
        },
    )
    _dev_gateway["proc"] = proc
    _dev_gateway["log_path"] = log_path
    return time.time()


def _is_openclaw_gateway_active_dev() -> bool:
    proc = _dev_gateway.get("proc")
    return proc is not None and proc.poll() is None


def _probe_openclaw_gateway_health_dev(
    _started_at: float,
) -> tuple[bool, str]:
    log_path = _dev_gateway.get("log_path") or _dev_gateway_log_path()
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()
    except FileNotFoundError:
        return False, "gateway log file not created yet"
    gateway_ready = "[gateway] ready" in tail
    telegram_connected = "[telegram] connected to gateway" in tail
    if gateway_ready and telegram_connected:
        return True, "ok"
    missing = []
    if not gateway_ready:
        missing.append("gateway ready")
    if not telegram_connected:
        missing.append("telegram connected")
    return False, "waiting for OpenClaw " + ", ".join(missing)


def _stop_openclaw_gateway_dev() -> None:
    proc = _dev_gateway.get("proc")
    if proc is None:
        return
    if proc.poll() is not None:
        _dev_gateway["proc"] = None
        return
    log.info("dev: stopping openclaw gateway subprocess (pid=%s)", proc.pid)
    try:
        proc.terminate()
    except ProcessLookupError:
        _dev_gateway["proc"] = None
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("dev: gateway did not exit on SIGTERM, sending SIGKILL")
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("dev: gateway did not exit on SIGKILL either")
    _dev_gateway["proc"] = None


def start_openclaw_gateway(binding: dict) -> float:
    """Start real OpenClaw.

    In production the OpenClaw gateway runs as a separate systemd
    unit so it has first-class lifecycle, logs, and crash-restart
    semantics. In dev mode the supervisor runs it as a subprocess
    instead (no systemd in a typical dev container).
    """
    if _dev_mode():
        return _start_openclaw_gateway_dev(binding)
    started_at = time.time()
    log.info(
        "starting OpenClaw gateway unit: bot=@%s owner=%s port=%s",
        binding.get("telegram_bot_username"),
        binding.get("telegram_owner_user_id"),
        OPENCLAW_GATEWAY_PORT,
    )
    _run_systemctl("reset-failed", GATEWAY_SYSTEMD_UNIT, check=False)
    _run_systemctl("restart", GATEWAY_SYSTEMD_UNIT)
    return started_at


def is_openclaw_gateway_active() -> bool:
    if _dev_mode():
        return _is_openclaw_gateway_active_dev()
    return (
        _run_systemctl(
            "is-active", "--quiet", GATEWAY_SYSTEMD_UNIT, check=False
        ).returncode
        == 0
    )


def probe_openclaw_gateway_health(started_at: float) -> tuple[bool, str]:
    """Inspect OpenClaw's logs for channel readiness.

    ``openclaw gateway health --url ...`` requires explicit gateway
    credentials even for this unauthenticated loopback setup, so the
    readiness gate follows the gateway's own log output and waits
    for the two lines that matter here: the gateway is ready and
    Telegram is connected for long polling. In production those logs
    flow through journald; in dev mode they go to a flat file.
    """
    if _dev_mode():
        return _probe_openclaw_gateway_health_dev(started_at)
    since = f"@{int(started_at)}"
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                GATEWAY_SYSTEMD_UNIT,
                "--since",
                since,
                "--no-pager",
                "-n",
                "300",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "journalctl readiness probe timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"journalctl exited {result.returncode}"
    logs = result.stdout or ""
    gateway_ready = "[gateway] ready" in logs
    telegram_connected = "[telegram] connected to gateway" in logs
    if gateway_ready and telegram_connected:
        return True, "ok"
    missing = []
    if not gateway_ready:
        missing.append("gateway ready")
    if not telegram_connected:
        missing.append("telegram connected")
    return False, "waiting for OpenClaw " + ", ".join(missing)


def wait_for_openclaw_start(started_at: float) -> None:
    """Wait until OpenClaw reports the gateway is healthy."""
    deadline = time.time() + 90
    last_probe = ""
    while time.time() < deadline:
        if not is_openclaw_gateway_active():
            last_probe = (
                "openclaw subprocess exited"
                if _dev_mode()
                else "systemd unit is not active"
            )
            time.sleep(1)
            continue
        ok, detail = probe_openclaw_gateway_health(started_at)
        if ok:
            log.info("OpenClaw gateway readiness probe succeeded")
            return
        last_probe = detail
        time.sleep(1)
    raise RuntimeError(
        "openclaw gateway did not become healthy within 90s"
        + (f": {last_probe}" if last_probe else "")
    )


def stop_openclaw_gateway() -> None:
    if _dev_mode():
        _stop_openclaw_gateway_dev()
        return
    log.info("stopping OpenClaw gateway unit")
    _run_systemctl("stop", GATEWAY_SYSTEMD_UNIT, check=False)


# Module-level holder so the gateway loop + heartbeat thread can see
# the supervisor's stop / rebind flags.
#
# ``stop``      — set by SIGTERM/SIGINT or a fatal error.
# ``rebind``    — set by the heartbeat watchdog when it notices the
#                 platform has changed this Computer's binding
#                 (unassign, OR a different binding under the same
#                 ``assigned=true`` response). Causes ``main()`` to
#                 stop the gateway + jump back to Phase B without
#                 tearing down the supervisor process.
# ``signature`` — current binding's identity tuple set at the start
#                 of Phase D. The watchdog compares this against
#                 every fresh ``/me/binding`` response so a fast
#                 unassign + reassign that lands inside the heartbeat
#                 window still triggers a clean rebind.
_stop_holder = {"stop": False, "rebind": False, "signature": None}
_config_apply_state = {
    "failed_revision": None,
    "failed_diagnostic": None,
    "failed_reported": False,
}


def _binding_signature(binding: dict) -> tuple:
    """Identity tuple for an ``/me/binding`` payload.

    Any change in any field between two consecutive watchdog polls
    indicates the platform replaced the binding under us — admin
    re-assigned the same VPS (different bot, different account,
    different owner, or new vault row with a fresh token, or an
    OpenRouter child key + base URL + default model that appeared
    after a transient vault miss on the first poll). The supervisor
    must drop its in-memory state and re-run Phase B so openclaw.json
    gets rewritten with the now-present provider config.
    """
    return (
        str(binding.get("telegram_bot_user_id") or ""),
        str(binding.get("telegram_bot_username") or ""),
        str(binding.get("telegram_owner_user_id") or ""),
        str(binding.get("telegram_bot_token") or ""),
        str(binding.get("account_handle") or ""),
        str(binding.get("openrouter_api_key") or ""),
        str(binding.get("openrouter_base_url") or ""),
        str(binding.get("openrouter_default_model") or ""),
    )


def _command_revision(command: dict) -> int | None:
    try:
        revision = int(command.get("revision"))
    except (TypeError, ValueError):
        return None
    if revision < 0:
        return None
    return revision


def _post_config_apply_result(
    *,
    revision: int,
    status: str,
    diagnostic: str | None = None,
) -> None:
    body = {"revision": revision, "status": status}
    if diagnostic:
        body["diagnostic"] = diagnostic[:1023]
    post_json("/hapi/v1/computers/me/config/apply-result", body)


def _report_cached_failed_revision() -> None:
    revision = _config_apply_state.get("failed_revision")
    if revision is None or _config_apply_state.get("failed_reported"):
        return
    diagnostic = str(_config_apply_state.get("failed_diagnostic") or "")
    _post_config_apply_result(
        revision=int(revision),
        status="failed",
        diagnostic=diagnostic,
    )
    _config_apply_state["failed_reported"] = True


def handle_apply_config_command(command: dict) -> None:
    """Handle one heartbeat-delivered `apply_config` command.

    The heartbeat tells us only that config is stale. The Computer then
    pulls the latest runtime-secret map so multiple rapid saves collapse
    into one local apply attempt.
    """
    requested_revision = _command_revision(command)
    if requested_revision is None:
        log.warning("ignoring malformed apply_config command: %r", command)
        return

    if _config_apply_state.get("failed_revision") == requested_revision:
        log.info(
            "apply_config revision=%d was already attempted and failed; "
            "skipping local reapply until a newer revision arrives",
            requested_revision,
        )
        try:
            _report_cached_failed_revision()
        except Exception as exc:
            log.warning(
                "failed to re-post cached apply_config diagnostic for "
                "revision=%d: %s",
                requested_revision,
                exc,
            )
        return

    log.info("heartbeat command: applying runtime config revision=%d", requested_revision)
    secrets: dict[str, str] = {}
    revision = requested_revision
    try:
        payload = get_json("/hapi/v1/computers/me/runtime-secrets")
        revision = int(payload.get("revision") or requested_revision)
        raw_secrets = payload.get("secrets") or {}
        if not isinstance(raw_secrets, dict):
            raise ValueError("/me/runtime-secrets returned a non-object secrets map")
        secrets = {str(key): str(value) for key, value in raw_secrets.items()}
        result = apply_runtime_secret_map(revision=revision, secrets=secrets)
        _post_config_apply_result(
            revision=revision,
            status="applied",
            diagnostic=f"applied {result['secret_count']} runtime secret(s)",
        )
        _config_apply_state["failed_revision"] = None
        _config_apply_state["failed_diagnostic"] = None
        _config_apply_state["failed_reported"] = False
        log.info(
            "apply_config revision=%d applied (keys=%d)",
            revision,
            result["secret_count"],
        )
    except Exception as exc:
        diagnostic = _diagnostic_from_exception(exc, secrets)
        _config_apply_state["failed_revision"] = revision
        _config_apply_state["failed_diagnostic"] = diagnostic
        _config_apply_state["failed_reported"] = False
        log.exception("apply_config revision=%d failed: %s", revision, diagnostic)
        try:
            _post_config_apply_result(
                revision=revision,
                status="failed",
                diagnostic=diagnostic,
            )
            _config_apply_state["failed_reported"] = True
        except Exception as post_exc:
            log.warning(
                "failed to post apply_config failure result for revision=%d: %s",
                revision,
                post_exc,
            )


def main() -> int:
    log.info("supervisor starting")

    def _on_signal(signum, frame):
        _stop_holder["stop"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Outer rebind loop: every iteration is one full
    # bind->active->OpenClaw cycle. The heartbeat watchdog flips
    # ``rebind=True`` when the platform unassigns this Computer,
    # which falls through to a fresh Phase B without restarting
    # systemd.
    while not _stop_holder["stop"]:
        exit_code = _run_one_binding_cycle()
        if exit_code != 0:
            return exit_code
        if _stop_holder["stop"] and not _stop_holder["rebind"]:
            log.info("supervisor exiting cleanly")
            return 0
        # Rebind path: clear the per-cycle flags and loop.
        _stop_holder["stop"] = False
        _stop_holder["rebind"] = False
        log.info(
            "rebind: platform unassigned this Computer; awaiting a fresh "
            "/me/binding"
        )
    return 0


def _run_one_binding_cycle() -> int:
    # Phase A: report ready. Retry until it succeeds — if the
    # platform is transiently unreachable (ngrok blip, backend
    # restart) we MUST NOT proceed to Phase B without flipping
    # state. ``provisioning`` is not in ``/me/binding``'s
    # allow-list, so a stuck supervisor would 409-loop forever
    # otherwise.
    #
    # On supervisor restart the row may already be past
    # ``provisioning`` (admin retry, manual reset). The platform
    # refuses ``provisioning -> ready`` anything, but it also
    # refuses ``assigned -> ready`` from a Computer-actor — that's
    # an admin-only edge. We treat an HTTP 400 (illegal
    # transition) as a signal to skip ahead to Phase B and let
    # /me/binding decide what's next; that way a restart in
    # ready/assigned/active/broken does not infinite-loop.
    for attempt in range(1, 1000):
        try:
            post_json(
                "/hapi/v1/computers/me/state",
                {"state": "ready", "detail": "bootstrap complete"},
            )
            log.info("reported state=ready (attempt %d)", attempt)
            break
        except urllib.error.HTTPError as http_exc:
            if http_exc.code == 400:
                log.info(
                    "Phase A skipped: platform refused ready transition "
                    "(status 400) — row is likely already past "
                    "provisioning. Proceeding to Phase B."
                )
                break
            log.warning(
                "initial /me/state ready POST failed (attempt %d): %s",
                attempt,
                http_exc,
            )
            time.sleep(min(2 * attempt, 30))
        except Exception as exc:
            log.warning(
                "initial /me/state ready POST failed (attempt %d): %s",
                attempt,
                exc,
            )
            time.sleep(min(2 * attempt, 30))

    # Phase B: poll for binding
    poll = BINDING_POLL_BASE_SECONDS
    empty_count = 0
    binding = None
    while binding is None and not _stop_holder["stop"]:
        try:
            resp = get_json("/hapi/v1/computers/me/binding")
        except Exception as exc:
            log.warning("/me/binding GET failed: %s", exc)
            time.sleep(poll)
            continue
        if resp.get("assigned") is True and resp.get("binding"):
            binding = resp["binding"]
            break
        empty_count += 1
        if empty_count > 5:
            poll = BINDING_POLL_IDLE_CAP_SECONDS
        time.sleep(poll)
    if _stop_holder["stop"]:
        return 0

    # Phase C: persist binding + start OpenClaw + report active
    try:
        tinyhat_plugin_installed = try_install_tinyhat_plugin()
        write_openclaw_config(
            binding,
            enable_tinyhat_plugin=tinyhat_plugin_installed,
        )
        delete_telegram_webhook(binding)
        gateway_started_at = start_openclaw_gateway(binding)
        wait_for_openclaw_start(gateway_started_at)
    except Exception as exc:
        log.exception("OpenClaw gateway start failed: %s", exc)
        stop_openclaw_gateway()
        post_json(
            "/hapi/v1/computers/me/state",
            {"state": "broken", "detail": f"openclaw gateway start failed: {exc}"},
        )
        return 1

    try:
        post_json(
            "/hapi/v1/computers/me/state",
            {"state": "active", "detail": "openclaw gateway started"},
        )
        log.info("reported state=active")
    except Exception as exc:
        log.exception("active /me/state POST failed: %s", exc)

    # Stamp this cycle's binding signature so the watchdog thread
    # can detect a fast unassign + reassign that lands inside the
    # heartbeat window.
    _stop_holder["signature"] = _binding_signature(binding)
    log.info(
        "phase D: binding signature locked (bot=@%s owner=%s)",
        binding.get("telegram_bot_username"),
        binding.get("telegram_owner_user_id"),
    )

    # Phase D: heartbeat + binding-watch thread + OpenClaw gateway
    # monitor on the main thread. The thread watches the platform
    # for an unassign by re-polling /me/binding every heartbeat;
    # when ``assigned: false`` comes back it flips
    # ``_stop_holder["rebind"]`` so the gateway exits cleanly + the
    # outer ``main()`` loops back to a fresh Phase B.

    def _heartbeat_loop():
        while not _stop_holder["stop"]:
            gateway_alive = is_openclaw_gateway_active()
            metrics = {
                "gateway_alive": gateway_alive,
                "supervisor_uptime_seconds": int(time.time()),
            }
            private_access = private_access_report()
            if private_access is not None:
                metrics["private_access"] = private_access
            try:
                heartbeat = post_json(
                    "/hapi/v1/computers/me/heartbeat", {"metrics": metrics}
                )
                command = heartbeat.get("command") if isinstance(heartbeat, dict) else None
                if isinstance(command, dict) and command.get("type") == "apply_config":
                    handle_apply_config_command(command)
            except Exception as exc:
                log.warning("/me/heartbeat POST failed: %s", exc)
            # Watchdog: did the platform unassign us OR swap the
            # binding under us? Both cases must trigger rebind. The
            # unassign + immediate reassign path can land inside the
            # heartbeat window without ever surfacing assigned=false,
            # so checking the boolean alone is not enough — we also
            # compare the binding identity tuple against what Phase D
            # locked in.
            #
            # /me/binding is allowed for ready/assigned/active/broken
            # so it works in every state the heartbeat could find us
            # in.
            try:
                resp = get_json("/hapi/v1/computers/me/binding")
                if resp.get("assigned") is False:
                    log.info(
                        "binding watchdog: platform reports assigned=false; "
                        "triggering rebind"
                    )
                    _stop_holder["rebind"] = True
                    _stop_holder["stop"] = True
                    return
                new_binding = resp.get("binding") or {}
                new_sig = _binding_signature(new_binding)
                cached_sig = _stop_holder.get("signature")
                if cached_sig and new_sig != cached_sig:
                    log.info(
                        "binding watchdog: identity changed (bot=@%s owner=%s); "
                        "triggering rebind",
                        new_binding.get("telegram_bot_username"),
                        new_binding.get("telegram_owner_user_id"),
                    )
                    _stop_holder["rebind"] = True
                    _stop_holder["stop"] = True
                    return
            except Exception as exc:
                # Transient — don't trip rebind on a single GET
                # failure (the rest of the loop stays alive while the
                # platform comes back).
                log.warning("/me/binding watchdog GET failed: %s", exc)

            # Idempotent state=active re-confirm. After a SAME-bot
            # unassign + reassign the row goes ready -> assigned but
            # the supervisor never re-POSTs state=active (it ran that
            # POST once at Phase D), so the row stays stuck in
            # ``assigned`` even though OpenClaw is alive. The platform
            # refuses self-transitions (active -> active = 400), so
            # this is a no-op in steady state and only fires the real
            # ``assigned -> active`` edge when the row is actually
            # back in ``assigned``.
            try:
                post_json(
                    "/hapi/v1/computers/me/state",
                    {"state": "active", "detail": "watchdog re-confirm"},
                )
                log.info(
                    "watchdog: re-confirmed state=active (row was in assigned "
                    "after a reassign)"
                )
            except urllib.error.HTTPError as http_exc:
                if http_exc.code != 400:
                    log.warning(
                        "watchdog state=active POST failed: %s", http_exc
                    )
            except Exception as exc:
                log.warning("watchdog state=active POST failed: %s", exc)
            for _ in range(HEARTBEAT_INTERVAL_SECONDS):
                if _stop_holder["stop"]:
                    return
                time.sleep(1)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        inactive_for_seconds = 0
        while not _stop_holder["stop"]:
            if is_openclaw_gateway_active():
                inactive_for_seconds = 0
            else:
                inactive_for_seconds += 1
                if inactive_for_seconds == 1:
                    log.warning(
                        "OpenClaw gateway unit is not active; waiting for "
                        "systemd restart"
                    )
                if inactive_for_seconds >= GATEWAY_INACTIVE_GRACE_SECONDS:
                    raise RuntimeError(
                        "openclaw gateway unit stayed inactive for "
                        f"{GATEWAY_INACTIVE_GRACE_SECONDS}s"
                    )
            time.sleep(1)
    except Exception as exc:
        log.exception("OpenClaw gateway unhealthy: %s", exc)
        _stop_holder["stop"] = True
        try:
            post_json(
                "/hapi/v1/computers/me/state",
                {"state": "broken", "detail": f"openclaw gateway unhealthy: {exc}"},
            )
        except Exception:
            pass
        return 1
    finally:
        stop_openclaw_gateway()

    # Gateway monitor returned cleanly — either SIGTERM or the
    # binding watchdog tripped rebind. Wait for the heartbeat thread
    # to wind down before the outer loop decides what to do next.
    heartbeat_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
