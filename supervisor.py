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
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any

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

# ChatGPT BYO subscription (issue #23): when an `openai-codex` OAuth
# profile is present in this Computer's per-agent auth store, the
# supervisor swaps the default `pi`+OpenRouter config for the native
# Codex app-server harness pointed at `openai/gpt-5.5`. The OAuth
# credential is born on the Computer (either from the Mini App-driven
# heartbeat-command flow OR from the chat-driven plugin tool running
# the device-code CLI in-sandbox); the supervisor only reads the
# resulting file shape on disk.
CHATGPT_SUBSCRIPTION_MODEL = "openai/gpt-5.5"
CHATGPT_SUBSCRIPTION_PROVIDER = "openai-codex"
# Default agent id mirrors the platform's single-agent assumption today.
# The auth store is per-agent inside the per-Computer OpenClaw state dir.
DEFAULT_OPENCLAW_AGENT_ID = "main"
TINYHAT_SECRETS_PROVIDER = "tinyhat"
TINYHAT_OPENAI_API_KEY_NAME = "OPENAI_API_KEY"
TINYHAT_OPENAI_API_KEY_POINTER = "/OPENAI_API_KEY"
TINYHAT_OPENROUTER_API_KEY_NAME = "OPENROUTER_API_KEY"
# Env-block keys whose runtime values come from the binding payload (not the
# user-managed runtime-secrets vault). They are preserved across runtime-
# secret apply cycles so a Mini App entry can't accidentally shadow the
# platform-issued credential, and runtime-secret deletes never strip them.
BINDING_MANAGED_ENV_KEYS = frozenset({TINYHAT_OPENROUTER_API_KEY_NAME})
TINYHAT_PLUGIN_ID = "tinyhat"
TINYHAT_PLUGIN_REPO_URL_ENV = "TINYHAT_PLATFORM_PLUGIN_REPO_URL"
TINYHAT_PLUGIN_REPO_REF_ENV = "TINYHAT_PLATFORM_PLUGIN_REPO_REF"
TINYHAT_PLUGIN_REPO_URL_DEFAULT = "https://github.com/tinyhat-ai/tinyhat.git"
TINYHAT_PLUGIN_REPO_REF_DEFAULT = "main"
GATEWAY_SYSTEMD_UNIT = "tinyhat-openclaw-gateway.service"
# The systemd unit that runs THIS supervisor in production. Used by the
# runtime self-update path (the ``update_component`` command): after the
# runtime repo is checked out to a new ref, the still-running old
# supervisor process must be replaced by the freshly checked-out code, so
# we restart this unit (with an in-process ``os.execv`` fallback). Override
# via env if the deployment names the unit differently.
SUPERVISOR_SYSTEMD_UNIT = (
    os.environ.get("TINYHAT_SUPERVISOR_UNIT") or "tinyhat-supervisor.service"
).strip()
PRIVATE_ACCESS_BOOTSTRAP_STATUS_PATH = (
    "/var/lib/tinyhat-private-access/bootstrap-status.json"
)
# Where the in-place component-update dedupe state is persisted. Mirrors
# the per-revision dedupe of ``apply_config`` (``_config_apply_state``) but
# must survive a supervisor restart — the runtime self-update restarts this
# process, and the re-execed supervisor must not re-run the same revision.
_DEFAULT_COMPONENT_UPDATE_STATE_PATH = (
    "/var/lib/tinyhat/component-update-state.json"
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
OPENCLAW_SECRETS_RELOAD_TIMEOUT_SECONDS = 12
OPENCLAW_SECRETS_RELOAD_RETRY_DELAYS_SECONDS = (5, 10, 20, 30, 30)
OPENCLAW_SECRETS_RELOAD_ATTEMPTS = (
    len(OPENCLAW_SECRETS_RELOAD_RETRY_DELAYS_SECONDS) + 1
)

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


def tinyhat_plugin_checkout_dir() -> str:
    """Local checkout of the public Tinyhat OpenClaw plugin repo."""
    configured = (os.environ.get("TINYHAT_PLUGIN_CHECKOUT_DIR") or "").strip()
    if configured:
        return configured
    return os.path.join(openclaw_state_dir(), "platform-plugins", TINYHAT_PLUGIN_ID)


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


def _tinyhat_plugin_source() -> tuple[str, str]:
    """Return the public plugin repo/ref pinned by the platform manifest."""
    repo_url = (
        os.environ.get(TINYHAT_PLUGIN_REPO_URL_ENV) or TINYHAT_PLUGIN_REPO_URL_DEFAULT
    ).strip()
    repo_ref = (
        os.environ.get(TINYHAT_PLUGIN_REPO_REF_ENV) or TINYHAT_PLUGIN_REPO_REF_DEFAULT
    ).strip()
    return repo_url, repo_ref


def _tinyhat_plugin_version(plugin_dir: str) -> str:
    package_json = os.path.join(plugin_dir, "package.json")
    try:
        with open(package_json, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        version_txt = os.path.join(plugin_dir, "version.txt")
        try:
            with open(version_txt, encoding="utf-8") as fh:
                return fh.read().strip() or "unknown"
        except FileNotFoundError:
            return "unknown"
    return str(payload.get("version") or "unknown")


def _tinyhat_plugin_marker_path() -> str:
    return os.path.join(openclaw_state_dir(), "tinyhat-plugin.version")


def _read_runtime_repo_version() -> str:
    """Version string for this runtime checkout (the repo-root ``VERSION``).

    Returns ``""`` when the file is missing so the caller omits the
    runtime version rather than reporting a placeholder.
    """
    version_path = os.path.join(runtime_dir(), "VERSION")
    try:
        with open(version_path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _read_runtime_git_sha() -> str:
    """Full git SHA of this runtime checkout, or ``""`` when not a git tree.

    The production Computer clones the runtime repo at a pinned ref, so a
    ``rev-parse HEAD`` resolves the exact commit. A non-git deployment
    (e.g. a tarball drop) returns ``""`` and the caller sends a null sha.
    """
    try:
        result = subprocess.run(
            ["git", "-C", runtime_dir(), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _read_installed_plugin_marker() -> dict:
    """Read the Tinyhat plugin install marker (written at install time).

    Shape mirrors :func:`ensure_tinyhat_plugin_installed`'s payload:
    ``{repo_url, repo_ref, resolved_commit_sha, version}``. Returns an
    empty dict when the marker is missing or unreadable.
    """
    marker_path = _tinyhat_plugin_marker_path()
    try:
        with open(marker_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


_OPENCLAW_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)")


def _read_openclaw_framework_version() -> str:
    """Installed OpenClaw (framework) version via ``openclaw --version``.

    Best-effort: returns ``""`` when the CLI is absent or errors. The
    framework is an npm package with no git checkout, so its component
    sha is always ``None``.
    """
    try:
        result = subprocess.run(
            ["openclaw", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=_openclaw_cli_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    output = (result.stdout or result.stderr or "").strip()
    match = _OPENCLAW_VERSION_RE.search(output)
    if match:
        return match.group(1)
    return output.splitlines()[0].strip() if output else ""


def collect_component_versions() -> dict:
    """Best-effort snapshot of installed component versions for the heartbeat.

    Returns the shape the platform ingests on ``POST /me/heartbeat``::

        {"runtime":   {"version": <str|None>, "sha": <str|None>},
         "plugin":    {"version": <str|None>, "sha": <str|None>},
         "framework": {"version": <str|None>, "sha": None}}

    Each component is resolved under its own ``try``/``except`` so one
    failing source never suppresses the others, and the whole function
    degrades to ``{}`` on any unexpected error — the heartbeat must never
    throw because version collection failed. A component is omitted
    entirely when neither its version nor its sha can be resolved; the
    platform then falls back to its provisioning manifest for that
    component.
    """
    components: dict[str, dict[str, Any]] = {}
    try:
        # runtime: this repo's VERSION file + the checkout's git SHA.
        try:
            runtime_version = _read_runtime_repo_version()
            runtime_sha = _read_runtime_git_sha()
            if runtime_version or runtime_sha:
                components["runtime"] = {
                    "version": runtime_version or None,
                    "sha": runtime_sha or None,
                }
        except Exception as exc:
            log.warning("could not collect runtime component version: %s", exc)

        # plugin: read from the install marker the plugin installer wrote.
        # A "unknown" version (no package.json/version.txt at install) is
        # treated as not-reported so the platform uses its manifest value,
        # while the real resolved sha is still surfaced.
        try:
            marker = _read_installed_plugin_marker()
            plugin_version = str(marker.get("version") or "").strip()
            if plugin_version.lower() == "unknown":
                plugin_version = ""
            plugin_sha = str(marker.get("resolved_commit_sha") or "").strip()
            if plugin_version or plugin_sha:
                components["plugin"] = {
                    "version": plugin_version or None,
                    "sha": plugin_sha or None,
                }
        except Exception as exc:
            log.warning("could not collect plugin component version: %s", exc)

        # framework: OpenClaw npm package version. No git sha for an npm pkg.
        try:
            framework_version = _read_openclaw_framework_version()
            if framework_version:
                components["framework"] = {
                    "version": framework_version,
                    "sha": None,
                }
        except Exception as exc:
            log.warning("could not collect framework component version: %s", exc)
    except Exception as exc:  # pragma: no cover - belt and suspenders
        log.warning("component version collection failed entirely: %s", exc)
        return {}
    return components


def ensure_tinyhat_plugin_installed() -> bool:
    """Install the public Tinyhat tool plugin into OpenClaw.

    The runtime repo does not own the plugin implementation. It only
    clones the public ``tinyhat-ai/tinyhat`` source pinned by the
    platform manifest and asks OpenClaw to install that checkout.
    The plugin contains no Tinyhat credentials; requests authenticate
    with the same Computer identity-token boundary as the supervisor.
    """
    repo_url, repo_ref = _tinyhat_plugin_source()
    plugin_dir = tinyhat_plugin_checkout_dir()
    os.makedirs(os.path.dirname(plugin_dir), mode=0o700, exist_ok=True)
    if os.path.isdir(os.path.join(plugin_dir, ".git")):
        remote = subprocess.run(
            ["git", "-C", plugin_dir, "remote", "set-url", "origin", repo_url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if remote.returncode != 0:
            detail = (remote.stderr or remote.stdout or "").strip()
            raise RuntimeError(f"Tinyhat plugin git remote update failed: {detail}")
        fetch = subprocess.run(
            ["git", "-C", plugin_dir, "fetch", "--tags", "--prune", "origin"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if fetch.returncode != 0:
            detail = (fetch.stderr or fetch.stdout or "").strip()
            raise RuntimeError(f"Tinyhat plugin git fetch failed: {detail}")
    else:
        if os.path.exists(plugin_dir):
            shutil.rmtree(plugin_dir)
        clone = subprocess.run(
            ["git", "clone", repo_url, plugin_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if clone.returncode != 0:
            detail = (clone.stderr or clone.stdout or "").strip()
            raise RuntimeError(f"Tinyhat plugin git clone failed: {detail}")

    checkout = subprocess.run(
        ["git", "-C", plugin_dir, "checkout", repo_ref],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if checkout.returncode != 0:
        detail = (checkout.stderr or checkout.stdout or "").strip()
        raise RuntimeError(f"Tinyhat plugin git checkout failed: {detail}")

    rev_parse = subprocess.run(
        ["git", "-C", plugin_dir, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if rev_parse.returncode != 0:
        detail = (rev_parse.stderr or rev_parse.stdout or "").strip()
        raise RuntimeError(f"Tinyhat plugin revision lookup failed: {detail}")
    plugin_sha = (rev_parse.stdout or "").strip()
    manifest_path = os.path.join(plugin_dir, "openclaw.plugin.json")
    if not os.path.exists(manifest_path):
        raise RuntimeError(f"Tinyhat plugin manifest is missing at {manifest_path}")

    version = _tinyhat_plugin_version(plugin_dir)
    marker_payload = {
        "repo_url": repo_url,
        "repo_ref": repo_ref,
        "resolved_commit_sha": plugin_sha,
        "version": version,
    }
    marker = _tinyhat_plugin_marker_path()
    installed_manifest = os.path.join(
        openclaw_state_dir(),
        "extensions",
        TINYHAT_PLUGIN_ID,
        "openclaw.plugin.json",
    )
    try:
        with open(marker, encoding="utf-8") as fh:
            marker_json = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        marker_json = {}
    if marker_json == marker_payload and os.path.exists(installed_manifest):
        log.info(
            "Tinyhat plugin already installed (ref=%s sha=%s version=%s)",
            repo_ref,
            plugin_sha[:12],
            version,
        )
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
        json.dump(marker_payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    log.info(
        "installed Tinyhat plugin (repo=%s ref=%s sha=%s version=%s)",
        repo_url,
        repo_ref,
        plugin_sha[:12],
        version,
    )
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


def _runtime_secret_env_entries(secrets: dict[str, str]) -> dict[str, str]:
    """Return the subset of runtime secrets that should land in ``config["env"]``.

    OpenClaw populates the gateway's ``process.env`` from plaintext
    ``config["env"]`` entries at boot via ``applyConfigEnvVars`` — that
    is the same lever the bash tool then reads for its child shells. Any
    user-saved runtime secret that the agent shell should be able to read
    (e.g. ``EXA_API_KEY``, third-party tokens) must therefore appear here.

    Two classes of names are filtered out:

    - ``OPENAI_API_KEY`` is wired into ``models.providers.openai.apiKey``
      as a file SecretRef. The OpenAI provider resolves it through the
      Tinyhat file provider — no plaintext env entry is needed, and adding
      one would shadow the SecretRef wiring.
    - Anything in :data:`BINDING_MANAGED_ENV_KEYS` (currently
      ``OPENROUTER_API_KEY``) comes from the platform-issued binding
      payload, not from the user. The binding-derived value is layered
      back on top in :func:`_apply_runtime_secret_env_block` so a user
      can't accidentally shadow it with a Mini App entry.
    """
    filtered: dict[str, str] = {}
    for raw_name, raw_value in (secrets or {}).items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        if name == TINYHAT_OPENAI_API_KEY_NAME:
            continue
        if name in BINDING_MANAGED_ENV_KEYS:
            continue
        value = str(raw_value or "")
        if not value.strip():
            continue
        filtered[name] = value
    return filtered


def _apply_runtime_secret_env_block(
    config: dict, secrets: dict[str, str]
) -> None:
    """Mirror user-managed runtime secrets into ``config["env"]``.

    Binding-managed keys (see :data:`BINDING_MANAGED_ENV_KEYS`) that
    already live in ``config["env"]`` are preserved verbatim — those
    values are owned by :func:`write_openclaw_config` and reflect the
    latest binding payload from the platform. All other prior entries
    are treated as stale runtime secrets and dropped, so a deleted Mini
    App secret actually disappears from the gateway env on next boot.
    """
    existing = config.get("env") or {}
    preserved = {
        key: existing[key]
        for key in BINDING_MANAGED_ENV_KEYS
        if isinstance(existing.get(key), str) and existing[key].strip()
    }
    secret_entries = _runtime_secret_env_entries(secrets)
    # Binding-managed wins on conflict so a user-set OPENROUTER_API_KEY in
    # the Mini App never overrides the platform-issued one.
    merged = {**secret_entries, **preserved}
    if merged:
        config["env"] = merged
    else:
        config.pop("env", None)


def _signal_rebind_for_secrets() -> None:
    """Ask the supervisor's main loop to restart the gateway.

    ``applyConfigEnvVars`` only runs at OpenClaw gateway boot, so a
    change to ``config["env"]`` does not reach the bash tool's
    ``process.env`` until the gateway restarts. Reuse the existing
    rebind machinery (stop → poll ``/me/binding`` → fresh config → fresh
    gateway) rather than inventing a parallel restart path; the binding
    watchdog and gateway-health probe already understand that flow.
    """
    log.info(
        "runtime-secret env block changed; signaling gateway rebind so "
        "applyConfigEnvVars picks up the new keys"
    )
    _stop_holder["rebind"] = True
    _stop_holder["stop"] = True


def sync_openclaw_secret_ref_config(secrets: dict[str, str]) -> bool:
    """Update openclaw.json with Tinyhat's file SecretRef surfaces.

    Returns ``True`` if the runtime-secret entries in ``config["env"]``
    changed (a gateway restart is required for ``applyConfigEnvVars`` to
    re-populate ``process.env``), ``False`` otherwise. Changes that only
    affect SecretRef-resolved fields like ``models.providers.openai.apiKey``
    are handled by ``openclaw secrets reload`` without a restart and
    return ``False``.
    """
    config_path = openclaw_config_path()
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        config = {}

    previous_env = dict(config.get("env") or {})
    _ensure_tinyhat_secret_provider_config(config)
    _sync_openai_api_key_ref(config, secrets)
    _apply_runtime_secret_env_block(config, secrets)
    current_env = dict(config.get("env") or {})
    _atomic_write_json(config_path, config)
    env_block_changed = previous_env != current_env
    log.info(
        "synced OpenClaw SecretRef config (provider=%s openai_ref=%s "
        "env_block_changed=%s env_keys=%d)",
        TINYHAT_SECRETS_PROVIDER,
        "yes" if (secrets.get(TINYHAT_OPENAI_API_KEY_NAME) or "").strip() else "no",
        "yes" if env_block_changed else "no",
        len(current_env),
    )
    return env_block_changed


def _tinyhat_plugin_config() -> dict:
    """Return non-secret config for the public OpenClaw tool plugin."""
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
    return "gateway did not respond" in lowered


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
        if last_detail and _openclaw_reload_snapshot_inactive(last_detail):
            log.info(
                "openclaw secrets reload skipped because no active secret "
                "snapshot exists yet; file provider config is synced"
            )
            return {
                "skipped": True,
                "reason": "secrets_runtime_snapshot_inactive",
            }
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
    """Apply one latest runtime-secret revision to OpenClaw.

    ``env_block_changed`` is returned alongside the reload result so the
    caller can decide whether a gateway restart is needed. Runtime
    secrets that show up in the gateway's ``process.env`` only land
    there at boot (via OpenClaw's ``applyConfigEnvVars``), so any change
    to ``config["env"]`` requires :func:`_signal_rebind_for_secrets`.
    Pure SecretRef changes (e.g. an ``OPENAI_API_KEY``-only edit) leave
    ``env_block_changed`` false and the gateway keeps running.
    """
    write_tinyhat_secrets_file(secrets)
    env_block_changed = sync_openclaw_secret_ref_config(secrets)
    reload_result = reload_openclaw_secrets(secrets)
    return {
        "revision": revision,
        "secret_count": len(secrets or {}),
        "reload": reload_result,
        "env_block_changed": env_block_changed,
    }


def openclaw_auth_profiles_path(*, agent_id: str = DEFAULT_OPENCLAW_AGENT_ID) -> str:
    """Resolve the per-agent OAuth auth-store path inside this Computer.

    Mirrors OpenClaw's own layout: under ``OPENCLAW_STATE_DIR`` (the
    supervisor resolves this to ``/var/lib/tinyhat-openclaw`` in
    production, or the per-worktree dev dir when ``--dev`` is in
    effect), each agent has its own ``agents/<id>/agent/`` directory
    with an ``auth-profiles.json`` file (mode 0600) that stores OAuth
    bundles per provider id. The chat-driven device-code login (via
    the Tinyhat plugin's ``tinyhat_open_chatgpt_subscription_link``
    tool) writes here; the Mini App-driven flow also writes here once
    the supervisor's PTY subprocess completes its poll. Per-agent
    isolation means a reassign / recycle of the Computer wipes this
    file (issue #23).
    """
    return os.path.join(
        openclaw_state_dir(), "agents", agent_id, "agent", "auth-profiles.json"
    )


def read_chatgpt_subscription_profile(
    *, agent_id: str = DEFAULT_OPENCLAW_AGENT_ID
) -> dict | None:
    """Return the ``openai-codex:*`` profile entry, if present.

    Returns the first matching profile dict from
    ``auth-profiles.json`` (typically there's exactly one — OpenClaw's
    profile id is ``openai-codex:<email>`` and the device-code flow
    only mints one per Computer). Returns ``None`` when the file is
    missing, malformed, or has no ``openai-codex:*`` entries.

    The OAuth token fields (``access``, ``refresh``, ``id``) ARE present
    in the returned dict — callers must NOT log them. The supervisor's
    own use of this function is metadata-only (presence check + email
    for logging); the actual OAuth refresh is OpenClaw's own concern.
    """
    path = openclaw_auth_profiles_path(agent_id=agent_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    for profile_id, profile in profiles.items():
        if not isinstance(profile_id, str) or not isinstance(profile, dict):
            continue
        if profile_id.startswith(f"{CHATGPT_SUBSCRIPTION_PROVIDER}:"):
            # Return a shallow copy with the profile id so callers can
            # log it without re-reading.
            out = dict(profile)
            out["__profile_id"] = profile_id
            return out
    return None


def wipe_chatgpt_subscription_profile(
    *, agent_id: str = DEFAULT_OPENCLAW_AGENT_ID
) -> list[str]:
    """Delete the ``openai-codex:*`` entries from the per-agent auth store.

    Used on unassign / reassign / recycle (the chat plugin's
    ``tinyhat_revert_to_platform_credits`` tool also performs this
    wipe directly from inside the sandbox; the supervisor's path here
    is the admin-driven case where the platform tells the Computer to
    drop the credential without the agent being involved).

    Returns the list of profile ids removed (empty when no matching
    profile existed). Preserves any non-``openai-codex`` profiles in
    the file. Writes atomically via a ``.tmp`` rename so a partial
    write can't strand other-provider entries.
    """
    path = openclaw_auth_profiles_path(agent_id=agent_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    removed: list[str] = []
    for profile_id in list(profiles.keys()):
        if isinstance(profile_id, str) and profile_id.startswith(
            f"{CHATGPT_SUBSCRIPTION_PROVIDER}:"
        ):
            del profiles[profile_id]
            removed.append(profile_id)
    if not removed:
        return []
    version = data.get("version") if isinstance(data.get("version"), int) else 1
    next_data = {"version": version, "profiles": profiles}
    tmp_path = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(next_data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    return removed


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
        model = (raw or "deepseek/deepseek-v4-flash:free").strip()
        if model.startswith("openrouter/"):
            return model
        return "openrouter/" + model.lstrip("/")

    def openrouter_model_package() -> dict:
        package = binding.get("openrouter_model_package")
        return package if isinstance(package, dict) else {}

    def openrouter_model_refs_by_role(package: dict) -> dict[str, str]:
        models = package.get("models")
        if not isinstance(models, dict):
            return {}
        refs: dict[str, str] = {}
        for role, model in models.items():
            if not isinstance(role, str) or not isinstance(model, str):
                continue
            refs[role] = openrouter_model_ref(model)
        return refs

    def openrouter_enabled_model_catalog(package: dict) -> dict:
        refs_by_role = openrouter_model_refs_by_role(package)
        enabled_roles = package.get("enabled_roles")
        if not isinstance(enabled_roles, list) or not refs_by_role:
            return {primary_model: {"alias": "default"}}
        catalog: dict[str, dict[str, str]] = {}
        for role in enabled_roles:
            if not isinstance(role, str):
                continue
            ref = refs_by_role.get(role)
            if not ref:
                continue
            catalog[ref] = {"alias": role.replace("_", "-")}
        catalog.setdefault(primary_model, {"alias": "default"})
        return catalog

    def openrouter_model_fallbacks(package: dict) -> list[str]:
        refs_by_role = openrouter_model_refs_by_role(package)
        enabled_roles = set(package.get("enabled_roles") or [])
        default_role = str(package.get("default_role") or "")
        if default_role == "power":
            candidates = ("default", "cheap")
        elif default_role == "default":
            candidates = ("cheap",)
        else:
            candidates = ()
        return [
            refs_by_role[role]
            for role in candidates
            if role in enabled_roles and refs_by_role.get(role) != primary_model
        ]

    openrouter_enabled = bool(openrouter_key and openrouter_base)
    model_package = openrouter_model_package() if openrouter_enabled else {}

    # ── ChatGPT BYO subscription branch (issue #23) ─────────────────
    # The platform may advertise `llm_auth_mode = chatgpt_subscription`
    # on the binding to signal "the owner has opted in" (Mini App
    # path), but the source of truth for "is a credential actually
    # present" is the per-agent OAuth auth store on disk. The chat
    # plugin's tool writes there directly; the Mini App's
    # heartbeat-command flow has the supervisor write there. Either
    # way, the supervisor flips to subscription-mode config only when
    # the credential is on disk — otherwise an opted-in but
    # not-yet-linked Computer would lose its OpenRouter fallback
    # before the user has even approved.
    binding_llm_auth_mode = str(binding.get("llm_auth_mode") or "platform_credits")
    binding_llm_model_ref = str(binding.get("llm_model_ref") or "").strip()
    subscription_profile = read_chatgpt_subscription_profile()
    use_chatgpt_subscription = (
        binding_llm_auth_mode == "chatgpt_subscription"
        and subscription_profile is not None
    )

    if use_chatgpt_subscription:
        primary_model = binding_llm_model_ref or CHATGPT_SUBSCRIPTION_MODEL
    else:
        primary_model = (
            openrouter_model_ref(openrouter_model)
            if openrouter_enabled
            else OPENCLAW_DEFAULT_MODEL
        )
    text_model_config: dict[str, object] = {"primary": primary_model}
    if openrouter_enabled and not use_chatgpt_subscription:
        fallbacks = openrouter_model_fallbacks(model_package)
        if fallbacks:
            text_model_config["fallbacks"] = fallbacks
    elif use_chatgpt_subscription and openrouter_enabled:
        # Cross-provider fallback when the platform-credits OpenRouter
        # rail is still available on the binding — covers the case
        # where the subscription hits a per-account rate window
        # (5h / weekly) and the agent should keep replying via the
        # funded path instead of going dark. Tinyloop's preflight
        # spike confirmed OpenClaw's `models.*.fallbacks` accepts
        # cross-provider refs as a pure config field, no runtime
        # controller required.
        text_model_config["fallbacks"] = [openrouter_model_ref(openrouter_model)]

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

    agents_defaults: dict[str, object] = {
        "workspace": workspace_dir,
        "model": text_model_config,
    }
    # The `pi` agent runtime is the v0.5.x default — used for OpenRouter
    # / API-key routes. Subscription mode (openai/gpt-5.5 + the
    # native Codex app-server harness) is selected automatically by
    # OpenClaw when an `openai-codex` profile is present, so we drop
    # the explicit `pi` pin here — pinning it would force OpenClaw's
    # built-in runtime instead of the Codex harness and silently
    # negate the linked subscription.
    if not use_chatgpt_subscription:
        agents_defaults["agentRuntime"] = {"id": "pi"}

    config = {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": OPENCLAW_GATEWAY_PORT,
            "auth": {"mode": "none"},
            "tailscale": {"mode": "off"},
        },
        "agents": {"defaults": agents_defaults},
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
    if openrouter_enabled and not use_chatgpt_subscription:
        config["agents"]["defaults"]["models"] = openrouter_enabled_model_catalog(
            model_package
        )
    current_secrets = read_tinyhat_secrets_file()
    if not use_chatgpt_subscription:
        # Subscription mode owns its auth via the per-agent
        # `auth-profiles.json`; we explicitly leave the
        # OpenAI-API-key SecretRef out so OpenClaw doesn't try to
        # bypass the OAuth profile with a stale API key.
        _sync_openai_api_key_ref(config, current_secrets)
    # Seed binding-managed env entries first so they are preserved when
    # runtime secrets are layered on top — see _apply_runtime_secret_env_block.
    if openrouter_enabled:
        # Keep OPENROUTER_API_KEY in env even in subscription mode so
        # the cross-provider fallback above has a working auth path.
        config["env"] = {TINYHAT_OPENROUTER_API_KEY_NAME: openrouter_key}
    _apply_runtime_secret_env_block(config, current_secrets)
    _atomic_write_json(config_path, config)
    # Log only non-secret summary; never log the API key or OAuth token.
    log.info(
        "wrote OpenClaw config to %s "
        "(bot=@%s owner=%s model=%s subscription=%s openrouter=%s openai_ref=%s)",
        config_path,
        binding.get("telegram_bot_username"),
        owner_id,
        primary_model,
        (
            subscription_profile.get("__profile_id", "yes")
            if use_chatgpt_subscription and subscription_profile
            else "no"
        ),
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
    after a transient vault miss on the first poll), OR the owner
    flipped the LLM auth mode (issue #23 — adding
    ``llm_auth_mode`` / ``llm_model_ref`` so a platform-credits ->
    chatgpt_subscription flip triggers rebind and the supervisor
    rewrites openclaw.json instead of keeping the old
    ``pi`` + OpenRouter config running). The supervisor must drop
    its in-memory state and re-run Phase B so openclaw.json gets
    rewritten with the now-present provider config.
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
        json.dumps(binding.get("openrouter_model_package") or {}, sort_keys=True),
        # ChatGPT BYO subscription (issue #23) — mode/model_ref live
        # on the binding so the watchdog notices a flip and triggers
        # a rebind (otherwise write_openclaw_config never runs after
        # the platform changes the auth mode).
        str(binding.get("llm_auth_mode") or "platform_credits"),
        str(binding.get("llm_model_ref") or ""),
    )


def _owner_identity_signature(binding: dict) -> tuple:
    """Owner-identity subset of the binding signature (issue #23).

    Same shape as ``_binding_signature`` but trimmed to the fields
    that change ONLY on owner-identity changes — i.e. an admin
    reassign / unassign / recycle hands the Computer to a different
    user. Mode flips (``llm_auth_mode`` / ``llm_model_ref``) and
    OpenRouter key rotation for the SAME owner do not move this
    tuple, so they don't trigger the per-agent OAuth auth-store
    wipe — only owner-identity changes do (issue #23 wipe contract).
    """
    return (
        str(binding.get("telegram_bot_user_id") or ""),
        str(binding.get("telegram_bot_username") or ""),
        str(binding.get("telegram_owner_user_id") or ""),
        str(binding.get("account_handle") or ""),
    )


def _wipe_on_owner_release(*, reason: str) -> None:
    """Wipe the per-agent OAuth auth-store when the Computer changes hands.

    Issue #23 — admin-driven unassign / reassign / recycle must not
    leak the previous owner's OAuth credential to the next owner. The
    watchdog calls this from both the ``assigned=false`` branch
    (platform-driven unassign) and the owner-identity-changed branch
    (admin reassign to a different account/owner). Phase B also calls
    it on cold-start when it observes ``assigned=false`` with an
    orphaned profile on disk (PR #24 review at 01:19Z — second
    attack path).

    Three operations, in order:

    1. **Bump the binding generation** so any in-flight device-code
       worker thread under the previous binding observes the change
       on its next loop iteration and exits without posting.
    2. **SIGTERM any active CLI subprocesses** so a late OAuth-profile
       write from the previous owner's worker cannot survive into the
       next owner's auth store.
    3. **Wipe the auth-profiles file** itself.

    Steps 1+2 are the late-arriving-worker fix from the 01:19Z review.
    Step 3 is the original wipe (issue #23 / step #2 of the prior
    review). All three are best-effort: failures are warning-logged
    but never raised so the rebind path keeps moving.
    """
    _bump_binding_generation(reason=reason)
    _cancel_active_subscription_link_workers()
    try:
        removed = wipe_chatgpt_subscription_profile()
    except Exception as exc:
        log.warning(
            "binding watchdog: subscription auth-store wipe failed on %s: %s",
            reason,
            exc,
        )
        return
    if removed:
        log.info(
            "binding watchdog: wiped subscription auth-store on %s (profiles=%s)",
            reason,
            removed,
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


# In-memory record of ChatGPT-subscription link sessions we've
# already kicked off in this supervisor lifetime. Keeps idempotency
# tight when the platform re-delivers the heartbeat command before
# the result POST has landed (issue #23). Cleared on supervisor
# restart, which is the right behavior — after restart the platform
# can re-trigger and we'll spawn a fresh CLI for the same session id.
_subscription_link_sessions_started: set[str] = set()

# Cross-owner credential-leak guard (PR #24 review at 01:19Z).
#
# Two failure modes Codex reproduced on the prior head:
#
# 1. **Late-arriving worker writes profile after wipe.** Owner A
#    starts the device-code flow; the daemon worker keeps polling
#    auth.openai.com after the supervisor wipes for an
#    unassign/reassign; owner A approves 30s later; the still-alive
#    worker's CLI subprocess writes the OAuth profile back to the
#    auth store the next owner is about to inherit.
#
# 2. **Cold-start picks up an orphaned profile.** A previous owner
#    left a profile on disk, the supervisor restarted while the row
#    was unassigned, Phase B sleeps on ``assigned=false`` without
#    wiping, the next assigned owner's first
#    ``write_openclaw_config`` picks up the prior profile.
#
# Both are real cross-owner credential leaks. The shared guard is a
# monotonically increasing ``binding generation``: a new generation
# is minted on every "the owner changed under us" event — Phase D's
# initial binding lock-in, the watchdog's reassign / unassign
# branches, and Phase B's cold-start observation of
# ``assigned=false`` (which retroactively orphans whatever profile
# is on disk).
#
# Active workers stamp their starting generation and check it before
# every POST + at every loop iteration. If the supervisor's current
# generation has moved past the worker's, the worker SIGTERMs its
# CLI subprocess and exits without posting any terminal status (the
# wipe path is the one that talks to the platform about owner
# release; the worker bowing out silently is the right shape).
_binding_generation: int = 0
_binding_generation_lock = threading.Lock()
# Registry: session_id -> (pid, generation) so the wipe path can
# SIGTERM in-flight CLI subprocesses too. Cleared by the worker
# itself on exit.
_subscription_link_active_workers: dict[str, dict[str, int]] = {}
_subscription_link_active_workers_lock = threading.Lock()


def _current_binding_generation() -> int:
    with _binding_generation_lock:
        return _binding_generation


def _bump_binding_generation(*, reason: str) -> int:
    """Move the supervisor to a fresh binding generation.

    Called on every owner-release / new-owner event. Any in-flight
    subscription-link worker thread that started under an older
    generation will exit on its next loop iteration without posting
    a terminal status, so a late OAuth-profile write from the
    previous owner's CLI cannot survive into the next owner's
    auth-profiles.json.
    """
    global _binding_generation
    with _binding_generation_lock:
        _binding_generation += 1
        new_gen = _binding_generation
    log.info(
        "binding generation bumped to %d (reason=%s)", new_gen, reason
    )
    return new_gen


def _cancel_active_subscription_link_workers() -> None:
    """SIGTERM any in-flight device-code CLIs.

    Paired with ``_bump_binding_generation()`` on owner-release paths
    (PR #24 review). Killing the CLI prevents it from writing an
    OAuth profile after we've wiped the file, and the generation
    bump ensures any worker that's about to call ``_post_failed`` /
    ``_post_linked`` skips the POST instead of confusing the platform
    with a status update on the previous owner's session.
    """
    with _subscription_link_active_workers_lock:
        snapshot = list(_subscription_link_active_workers.items())
    for session_id, info in snapshot:
        pid = info.get("pid")
        if not pid:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            log.info(
                "subscription-link: SIGTERM in-flight CLI on owner release "
                "session_id=%s pid=%s",
                session_id,
                pid,
            )
        except ProcessLookupError:
            pass
        except OSError as exc:
            log.warning(
                "subscription-link: failed to SIGTERM in-flight CLI "
                "session_id=%s pid=%s: %s",
                session_id,
                pid,
                exc,
            )


def _strip_ansi_for_cli_capture(text: str) -> str:
    """Strip ANSI / OSC sequences from CLI output for URL/code matching."""
    cleaned = _ANSI_CSI_RE.sub("", text)
    cleaned = _ANSI_OSC_RE.sub("", cleaned)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


def _post_subscription_link_result(
    *,
    session_id: str,
    status: str,
    verification_url: str | None = None,
    user_code: str | None = None,
    expires_at: str | None = None,
    error: str | None = None,
) -> None:
    body: dict[str, Any] = {"session_id": session_id, "status": status}
    if verification_url:
        body["verification_url"] = verification_url
    if user_code:
        body["user_code"] = user_code
    if expires_at:
        body["expires_at"] = expires_at
    if error:
        body["error"] = error[:1023]
    try:
        post_json("/hapi/v1/computers/me/subscription-link-result", body)
    except Exception as exc:
        log.warning(
            "subscription-link-result POST failed (session_id=%s status=%s): %s",
            session_id,
            status,
            exc,
        )


def _run_chatgpt_device_code_login_in_thread(
    *,
    session_id: str,
    starting_generation: int | None = None,
    openclaw_bin: str = "openclaw",
    url_emit_timeout_s: float = 20.0,
    overall_timeout_s: float = 900.0,  # 15 min = device-code expiry
) -> None:
    """Worker thread: spawn the device-code CLI in a PTY + report progress.

    Issue #23 — runtime half of the chat / Mini App ChatGPT BYO flow.
    The OpenClaw CLI's ``models auth login --device-code`` requires an
    interactive TTY even with the headless flag (preflight memo §Q1),
    so we spawn it under a PTY via ``pty.fork`` and read the bytes
    OpenClaw would have written to a real terminal. The CLI emits
    a panel containing ``URL: <auth.openai.com/...>`` and
    ``Code: XXXX-YYYYY`` once the device-code request lands; once the
    user approves at auth.openai.com it emits ``OpenAI device code
    complete`` and writes the OAuth profile to disk.

    The supervisor POSTs **exactly one terminal lifecycle state** back
    to the platform per invocation:

    - ``linked`` once the CLI prints the success marker, OR the
      auth-profile shows up on disk after the child exits;
    - ``failed`` for: CLI exits before URL/code (Codex review on
      PR #24), no URL/code by the URL-emit deadline, child exits
      after URL/code but no profile written, overall 15-min window
      expires.

    Plus ``pending`` once URL+code are parsed (so the Mini App / chat
    tool can render them) — that's not terminal; the terminal POST
    follows later.

    Runs in a thread because the heartbeat loop must return within
    a few seconds — the device-code flow takes minutes (the user has
    to open a browser and tap Approve). The terminal POST clears
    ``model_auth_status=pending`` on the backend so the platform
    stops re-emitting the heartbeat command and the user can retry.
    """
    import pty
    import select
    import time as _time

    # PR #24 review at 01:41Z — defensive default + pre-fork check.
    # If the dispatcher didn't pass `starting_generation` (legacy call
    # path / direct test invocation), fall back to capturing now. The
    # production dispatcher always passes it explicitly.
    if starting_generation is None:
        starting_generation = _current_binding_generation()

    log.info(
        "subscription-link: starting device-code login subprocess "
        "session_id=%s starting_generation=%d",
        session_id,
        starting_generation,
    )

    # Pre-fork supersession check: if owner-release fired between
    # dispatcher's Thread.start() and us actually running, bail out
    # WITHOUT forking. Deregister so the active-workers map stays
    # clean.
    if _current_binding_generation() != starting_generation:
        log.info(
            "subscription-link: superseded before fork — exiting "
            "session_id=%s (starting=%d current=%d)",
            session_id,
            starting_generation,
            _current_binding_generation(),
        )
        with _subscription_link_active_workers_lock:
            _subscription_link_active_workers.pop(session_id, None)
        return

    try:
        pid, fd = pty.fork()
    except OSError as exc:
        log.warning("pty.fork failed for session_id=%s: %s", session_id, exc)
        with _subscription_link_active_workers_lock:
            _subscription_link_active_workers.pop(session_id, None)
        _post_subscription_link_result(
            session_id=session_id,
            status="failed",
            error=f"could not allocate a pseudo-terminal for the OpenClaw CLI: {exc}",
        )
        return

    # Worker is past the fork; update the registry entry with the
    # real PID so `_cancel_active_subscription_link_workers` can
    # SIGTERM us if owner-release fires after this point. (Before
    # this update the dispatcher pre-registered with pid=None; the
    # cancellation helper skips those entries — the pre-fork check
    # above and the main-loop check below are the safety nets for
    # that brief window.)
    with _subscription_link_active_workers_lock:
        entry = _subscription_link_active_workers.get(session_id)
        if entry is not None:
            entry["pid"] = pid

    if pid == 0:
        # Child: exec the CLI in the PTY. Env is inherited from the
        # supervisor process so the resulting auth profile lands in
        # this Computer's per-agent auth store.
        try:
            os.execvpe(
                openclaw_bin,
                [
                    openclaw_bin,
                    "models",
                    "auth",
                    "login",
                    "--provider",
                    "openai-codex",
                    "--device-code",
                ],
                {**os.environ, **_openclaw_cli_env()},
            )
        except OSError as exc:
            # exec failed; print so the parent's stdout-reader can
            # see the message, then exit non-zero.
            sys.stderr.write(f"openclaw exec failed: {exc}\n")
            os._exit(127)
        os._exit(0)

    # Parent: read stdout from the PTY in a loop. Match URL + Code,
    # POST pending. Then keep reading until "OpenAI device code
    # complete" or the child exits, and POST exactly one terminal
    # result regardless of which way we exit the loop.
    started_at = _time.monotonic()
    buffer = ""
    url_line_re = re.compile(r"URL:\s*(https?://\S+)")
    code_line_re = re.compile(r"Code:\s*([A-Za-z0-9]{4,5}-[A-Za-z0-9]{4,6})")
    url_value: str | None = None
    code_value: str | None = None
    pending_reported = False
    terminal_posted = False
    child_exit_code: int | None = None

    # Cross-owner credential-leak guard (PR #24 reviews at 01:19Z +
    # 01:41Z). The `starting_generation` parameter is the binding
    # generation in effect when the dispatcher decided to spawn this
    # worker — captured synchronously up there, NOT here, so a race
    # between Thread.start() and this line can't let us stamp a
    # post-release generation as our own. On each loop iteration we
    # check whether the supervisor has moved past us (owner release /
    # reassign / cold-start orphan-wipe) — if so we kill the CLI and
    # exit silently without posting a terminal status. The wipe path
    # is the one talking to the platform about owner release; the
    # worker bowing out silently is the right shape (a stale linked/
    # failed POST under the old session_id would be ignored by the
    # platform's session-id check anyway, but silence is cleaner).

    def _is_superseded() -> bool:
        return _current_binding_generation() != starting_generation

    def _child_alive() -> bool:
        nonlocal child_exit_code
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return False
        if wpid == 0:
            return True
        # Decode the status — only the exit-code byte is non-secret
        # diagnostic context we ever surface.
        if os.WIFEXITED(status):
            child_exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            child_exit_code = -os.WTERMSIG(status)
        return False

    def _post_failed(reason: str) -> None:
        nonlocal terminal_posted
        if terminal_posted:
            return
        if _is_superseded():
            # Owner released or rebound under us; the wipe path is the
            # one talking to the platform about this transition.
            log.info(
                "subscription-link: skipping failed POST — superseded "
                "session_id=%s",
                session_id,
            )
            terminal_posted = True
            return
        _post_subscription_link_result(
            session_id=session_id, status="failed", error=reason
        )
        terminal_posted = True

    def _post_linked() -> None:
        nonlocal terminal_posted
        if terminal_posted:
            return
        if _is_superseded():
            log.info(
                "subscription-link: skipping linked POST — superseded "
                "session_id=%s",
                session_id,
            )
            terminal_posted = True
            return
        _post_subscription_link_result(session_id=session_id, status="linked")
        terminal_posted = True

    def _kill_child() -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _cli_tail() -> str:
        stripped = _strip_ansi_for_cli_capture(buffer)[-400:]
        return stripped or "(empty)"

    try:
        while True:
            elapsed = _time.monotonic() - started_at

            # PR #24 review at 01:19Z: bail out fast on supersession
            # so a late OAuth-profile write from this worker's CLI
            # cannot survive into the next owner's auth store.
            # The wipe path bumps the generation BEFORE wiping the
            # file, then SIGTERMs our child; observing the bump here
            # is the latest chance to also re-wipe in case the CLI
            # managed to write a profile in the gap between the
            # generation bump and SIGTERM landing.
            if _is_superseded():
                log.info(
                    "subscription-link: worker superseded by binding rebind "
                    "session_id=%s; killing CLI and exiting",
                    session_id,
                )
                _kill_child()
                # Re-wipe defensively in case the CLI raced the
                # SIGTERM and wrote the OAuth profile just before
                # dying.
                try:
                    removed = wipe_chatgpt_subscription_profile()
                    if removed:
                        log.info(
                            "subscription-link: re-wiped late profile from "
                            "superseded worker session_id=%s profiles=%s",
                            session_id,
                            removed,
                        )
                except Exception as exc:
                    log.warning(
                        "subscription-link: late re-wipe failed for "
                        "superseded worker session_id=%s: %s",
                        session_id,
                        exc,
                    )
                terminal_posted = True  # short-circuit terminal POSTs
                break

            # Overall window — OpenAI device codes expire after 15min.
            if elapsed > overall_timeout_s:
                log.info(
                    "subscription-link: device-code subprocess timed out after %.0fs "
                    "session_id=%s",
                    elapsed,
                    session_id,
                )
                _kill_child()
                _post_failed(
                    "Device code timed out before the user approved at "
                    "auth.openai.com (15-minute window). Ask to retry."
                )
                break

            # No URL+code by the emit deadline — the CLI almost
            # certainly errored before issuing the device code
            # (network, OpenAI 4xx, disabled-for-account, etc.).
            if not pending_reported and elapsed > url_emit_timeout_s:
                log.warning(
                    "subscription-link: no URL/code from CLI after %.0fs "
                    "session_id=%s; reporting failed",
                    elapsed,
                    session_id,
                )
                _post_failed(
                    "openclaw did not return a device code. Check that "
                    "device-code login is enabled in your ChatGPT security "
                    "settings (Settings -> Security -> Enable device code "
                    "authorization for Codex). Recent CLI output: "
                    f"{_cli_tail()}"
                )
                _kill_child()
                break

            ready, _, _ = select.select([fd], [], [], 0.5)
            if fd in ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    buffer += _strip_ansi_for_cli_capture(
                        chunk.decode("utf-8", errors="replace")
                    )
                    if not pending_reported:
                        if not url_value:
                            m = url_line_re.search(buffer)
                            if m:
                                url_value = m.group(1)
                        if not code_value:
                            m = code_line_re.search(buffer)
                            if m:
                                code_value = m.group(1)
                        if url_value and code_value:
                            log.info(
                                "subscription-link: parsed URL+code for "
                                "session_id=%s; posting pending",
                                session_id,
                            )
                            _post_subscription_link_result(
                                session_id=session_id,
                                status="pending",
                                verification_url=url_value,
                                user_code=code_value,
                            )
                            pending_reported = True
                    if not terminal_posted and (
                        "OpenAI device code complete" in buffer
                        or "Auth profile: openai-codex:" in buffer
                    ):
                        log.info(
                            "subscription-link: detected device-code complete for "
                            "session_id=%s; posting linked",
                            session_id,
                        )
                        _post_linked()
                        # Let the CLI exit naturally so the auth-
                        # profile write finishes before we close the
                        # PTY in the finally block.

            if not _child_alive():
                # Drain any remaining stdout before deciding.
                try:
                    rest = os.read(fd, 4096)
                except OSError:
                    rest = b""
                if rest:
                    buffer += _strip_ansi_for_cli_capture(
                        rest.decode("utf-8", errors="replace")
                    )
                # If we already posted linked we're done. Otherwise
                # decide based on whether URL/code ever made it out:
                if terminal_posted:
                    break
                if pending_reported:
                    # CLI exited after issuing URL/code but before we
                    # caught the success marker. Re-check disk —
                    # OpenClaw may have finished writing the profile
                    # in the gap.
                    if read_chatgpt_subscription_profile() is not None:
                        log.info(
                            "subscription-link: CLI exited but auth-profile "
                            "present on disk; posting linked session_id=%s",
                            session_id,
                        )
                        _post_linked()
                    else:
                        _post_failed(
                            "Device-code login subprocess exited before the "
                            "auth profile was written "
                            f"(exit code: {child_exit_code}). Recent CLI "
                            f"output: {_cli_tail()}"
                        )
                else:
                    # Codex review on PR #24 — CLI exited BEFORE
                    # URL/code were issued (broken openclaw_bin,
                    # device-code disabled for the account, immediate
                    # provider error). Must post a terminal failure
                    # so the platform clears model_auth_status=pending
                    # and the user can retry — otherwise the row stays
                    # stuck and the in-memory session_id dedup blocks
                    # the next heartbeat redelivery.
                    log.warning(
                        "subscription-link: CLI exited before issuing URL/code "
                        "session_id=%s exit_code=%s",
                        session_id,
                        child_exit_code,
                    )
                    _post_failed(
                        "The OpenClaw device-code login subprocess exited "
                        f"before issuing a code (exit code: {child_exit_code}). "
                        "Check that device-code login is enabled in your "
                        "ChatGPT security settings (Settings -> Security -> "
                        "Enable device code authorization for Codex), then "
                        f"ask to retry. Recent CLI output: {_cli_tail()}"
                    )
                break
    except Exception as exc:  # noqa: BLE001 — worker thread, must not crash silently
        log.exception(
            "subscription-link: worker thread crashed session_id=%s", session_id
        )
        _post_failed(
            f"Internal supervisor error while running the device-code login: {exc}"
        )
    finally:
        with _subscription_link_active_workers_lock:
            _subscription_link_active_workers.pop(session_id, None)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


# ANSI / OSC regexes used by the device-code CLI capture path.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_OSC_RE = re.compile(r"\x1b\][0-9;].*?(?:\x07|\x1b\\)")


def handle_start_chatgpt_link_command(command: dict) -> None:
    """Handle one heartbeat-delivered `start_chatgpt_link` command.

    Spawns the device-code login subprocess in a worker thread so the
    heartbeat loop returns within its normal window. Idempotent per
    session_id within a supervisor lifetime — re-delivery of the same
    command (which the backend keeps emitting until ``status=pending``
    is reported) does NOT spawn a second CLI.

    Captures the binding generation synchronously here (in the
    dispatcher's thread) and passes it into the worker. The previous
    version captured inside the worker, which left a race window
    between thread.start() and the capture: if owner-release ran in
    that gap, the worker would stamp the already-bumped generation
    as its own and never observe supersession (PR #24 review at
    01:41Z — Codex reproduced this by injecting a wipe between
    pty.fork() and the worker's generation capture).

    Also registers the worker in `_subscription_link_active_workers`
    BEFORE Thread.start() so `_cancel_active_subscription_link_workers`
    can at least see the entry exists, even though `pid` is unknown
    until pty.fork() returns. The worker fills in `pid` once the
    child is forked. SIGTERM is skipped on entries with `pid=None`
    (the worker's pre-fork + main-loop supersession checks are the
    backstop for that brief window).
    """
    import threading

    session_id = str(command.get("session_id") or "").strip()
    if not session_id:
        log.warning("ignoring malformed start_chatgpt_link command: %r", command)
        return
    if session_id in _subscription_link_sessions_started:
        log.info(
            "start_chatgpt_link: session_id=%s already in flight; ignoring re-delivery",
            session_id,
        )
        return
    _subscription_link_sessions_started.add(session_id)

    # Stamp the binding generation we were dispatched under, and
    # pre-register the worker (pid unknown — the forked child will
    # update it). Doing this synchronously here means a release that
    # fires between this point and the worker thread actually
    # running will be observable via the captured generation.
    starting_generation = _current_binding_generation()
    with _subscription_link_active_workers_lock:
        _subscription_link_active_workers[session_id] = {
            "pid": None,
            "generation": starting_generation,
        }

    threading.Thread(
        target=_run_chatgpt_device_code_login_in_thread,
        kwargs={
            "session_id": session_id,
            "starting_generation": starting_generation,
        },
        name=f"chatgpt-link-{session_id[:8]}",
        daemon=True,
    ).start()


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
            "apply_config revision=%d applied (keys=%d env_changed=%s)",
            revision,
            result["secret_count"],
            "yes" if result.get("env_block_changed") else "no",
        )
        # The platform now knows the new revision is applied. Restart the
        # gateway so OpenClaw's applyConfigEnvVars picks up the new env
        # block; otherwise the agent shell tool's process.env stays stale
        # and a user-added secret like EXA_API_KEY never reaches `$EXA_API_KEY`.
        # Skip the restart when only SecretRef-backed config changed (e.g.
        # an OPENAI_API_KEY-only edit) since `openclaw secrets reload`
        # already refreshed the runtime snapshot for that path.
        if result.get("env_block_changed"):
            _signal_rebind_for_secrets()
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


# --------------------------------------------------------------------------
# In-place component update (runtime / plugin / framework)
# --------------------------------------------------------------------------
#
# On a heartbeat the platform may hand back an ``update_component`` command
# naming a target release for any subset of the three components that make
# up a running Computer::
#
#     {"type": "update_component", "revision": <int>, "targets": {
#         "runtime":   {"ref": "<git tag>"},        # optional
#         "plugin":    {"ref": "<git tag>"},        # optional
#         "framework": {"version": "<npm version>"} # optional
#     }}
#
# Only the components present in ``targets`` are touched. The update is
# strictly IN PLACE: we never recreate the box and never wipe the BYO-ChatGPT
# OAuth auth store. That store is owner-scoped and only cleared on an owner
# change (see ``_wipe_on_owner_release``, called solely from the binding
# watchdog's ``assigned=false`` / owner-signature-changed branches); a
# same-owner component update never goes near it.
#
# After acting, the supervisor POSTs the outcome to
# ``/hapi/v1/computers/me/component-update/apply-result`` (mirroring the
# ``apply_config`` result POST), and the next regular heartbeat re-reports
# the now-running versions via ``collect_component_versions``.
#
# Components are updated in the order plugin -> framework -> runtime so the
# riskiest step (the supervisor updating the very repo it runs from, which
# requires restarting this process) runs LAST; a plugin/framework failure
# therefore never strands a half-updated runtime.


def _component_update_state_path() -> str:
    return (
        os.environ.get("TINYHAT_COMPONENT_UPDATE_STATE_PATH")
        or _DEFAULT_COMPONENT_UPDATE_STATE_PATH
    ).strip()


def _read_component_update_state() -> dict:
    """Read the per-revision component-update dedupe record.

    Returns an empty dict when the file is missing or unreadable so a first
    update (or a wiped box) is never blocked.
    """
    try:
        with open(_component_update_state_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_component_update_state(revision: int, status: str) -> None:
    """Persist the last attempted component-update revision + outcome.

    Best-effort: a write failure is warning-logged but never raised — the
    update result itself has already been (or is about to be) POSTed, and a
    missing dedupe record at worst re-runs an idempotent update.
    """
    try:
        _atomic_write_json(
            _component_update_state_path(),
            {"last_revision": int(revision), "status": str(status)},
            mode=0o600,
        )
    except Exception as exc:  # noqa: BLE001 - never fatal
        log.warning("failed to persist component-update state: %s", exc)


def _restart_gateway_for_component_update() -> None:
    """Restart the OpenClaw gateway so a plugin/framework update takes effect.

    Reuses the supervisor's existing restart primitive rather than inventing
    a parallel path: setting the rebind flag makes ``main()`` stop the
    gateway and re-run Phase B (re-read ``/me/binding`` -> rewrite
    openclaw.json -> restart the gateway with the correct binding). This is
    the same lever ``_signal_rebind_for_secrets`` pulls for an env-block
    change, so it respects whatever the binding watchdog is doing and does
    not fight a config-apply that is mid-flight.

    Note this does NOT reload ``supervisor.py`` itself — a runtime
    self-update needs ``_restart_supervisor`` instead.
    """
    log.info(
        "component update: signaling gateway rebind so the updated "
        "plugin/framework is picked up"
    )
    _stop_holder["rebind"] = True
    _stop_holder["stop"] = True


def _update_plugin_component(ref: str) -> tuple[bool, str | None]:
    """Update the Tinyhat plugin to ``ref`` in place; restart the gateway.

    ``ensure_tinyhat_plugin_installed`` reads the target ref from the
    ``TINYHAT_PLATFORM_PLUGIN_REPO_REF`` env var, so we override it for the
    duration of the call (git fetch + checkout + ``openclaw plugins install
    --force`` + marker write all key off that ref). The resolved version /
    sha are then read back from the install marker.

    Returns ``(ok, diagnostic)``; never raises.
    """
    previous = os.environ.get(TINYHAT_PLUGIN_REPO_REF_ENV)
    os.environ[TINYHAT_PLUGIN_REPO_REF_ENV] = ref
    try:
        ensure_tinyhat_plugin_installed()
    except Exception as exc:  # noqa: BLE001 - keep the update non-fatal
        return False, f"plugin update to {ref} failed: {exc}"
    finally:
        if previous is None:
            os.environ.pop(TINYHAT_PLUGIN_REPO_REF_ENV, None)
        else:
            os.environ[TINYHAT_PLUGIN_REPO_REF_ENV] = previous
    marker = _read_installed_plugin_marker()
    sha = str(marker.get("resolved_commit_sha") or "")
    log.info("component update: plugin now at ref=%s sha=%s", ref, sha[:12])
    _restart_gateway_for_component_update()
    return True, None


def _update_framework_component(version: str) -> tuple[bool, str | None]:
    """Update the OpenClaw framework (npm package) to ``version`` in place.

    Mirrors the bootstrap install style (``npm install -g`` with the
    non-interactive flags). Verifies the installed version matches the
    target via ``_read_openclaw_framework_version`` before declaring
    success, then restarts the gateway. Returns ``(ok, diagnostic)``;
    never raises.
    """
    try:
        install = subprocess.run(
            ["npm", "install", "-g", "--no-fund", "--no-audit", f"openclaw@{version}"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"framework npm install of openclaw@{version} timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"framework npm install raised: {exc}"
    if install.returncode != 0:
        detail = (install.stderr or install.stdout or "").strip()
        return False, f"framework npm install failed: {detail[:200]}"
    installed = _read_openclaw_framework_version()
    if installed != version:
        return (
            False,
            "framework version mismatch after install: "
            f"wanted {version}, got {installed or 'unknown'}",
        )
    log.info("component update: framework now at %s", version)
    _restart_gateway_for_component_update()
    return True, None


def _update_runtime_component(ref: str) -> tuple[bool, str | None]:
    """Check out the runtime repo to ``ref`` in place (the self-update).

    The supervisor updates the very repo it runs from. The running process
    keeps executing the OLD code until it is restarted, so the CALLER is
    responsible for posting the applied-result BEFORE triggering the restart
    (see ``handle_update_component_command``) — that way the platform records
    success even if the restart terminates this process mid-flight.

    Dev mode (``TINYHAT_DEV_RUNTIME=1``): the supervisor source is typically
    bind-mounted from the host checkout, so an in-container ``git checkout``
    would not reflect what is actually running (and could collide with the
    bind mount). We therefore SKIP the checkout and report success with a
    clear diagnostic rather than failing hard.

    Returns ``(ok, diagnostic)``; never raises. On any failure we stay on the
    current ref (git checkout is atomic per-ref, so a failed checkout leaves
    the working tree untouched) — the box is never left without a working
    supervisor.
    """
    if _dev_mode():
        log.info("dev runtime: skipping runtime self-update (source is bind-mounted)")
        return True, "dev runtime: runtime self-update skipped (bind-mounted source)"
    d = runtime_dir()
    try:
        fetch = subprocess.run(
            ["git", "-C", d, "fetch", "--tags", "--prune"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if fetch.returncode != 0:
            detail = (fetch.stderr or fetch.stdout or "").strip()
            return False, f"runtime fetch failed: {detail[:200]}"
        checkout = subprocess.run(
            ["git", "-C", d, "checkout", ref],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if checkout.returncode != 0:
            detail = (checkout.stderr or checkout.stdout or "").strip()
            return False, f"runtime checkout of {ref} failed: {detail[:200]}"
    except subprocess.TimeoutExpired:
        return False, f"runtime update to {ref} timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"runtime update raised: {exc}"
    new_version = _read_runtime_repo_version()
    new_sha = _read_runtime_git_sha()
    log.info(
        "component update: runtime checked out to ref=%s (version=%s sha=%s)",
        ref,
        new_version or "unknown",
        new_sha[:12] or "unknown",
    )
    return True, None


def _restart_supervisor() -> None:
    """Restart THIS supervisor so a freshly checked-out runtime takes effect.

    Production: restart the supervisor's systemd unit. systemd starts a new
    supervisor process from the new repo state, so this call may not return
    (the restart can terminate us mid-call) — which is exactly why the
    applied-result is POSTed before this runs. If ``systemctl`` returns (it
    failed, or the unit name is wrong), we fall back to re-execing ourselves
    in place via ``os.execv`` so the updated ``supervisor.py`` is still
    loaded.

    Dev mode: no-op — the dev harness owns the process lifecycle and the
    source is bind-mounted, so there is nothing to restart here.
    """
    if _dev_mode():
        log.info("dev runtime: supervisor restart is a no-op")
        return
    log.info(
        "component update: restarting supervisor unit %s to load updated runtime",
        SUPERVISOR_SYSTEMD_UNIT,
    )
    try:
        result = _run_systemctl("restart", SUPERVISOR_SYSTEMD_UNIT, check=False)
        # If systemctl returned, the restart did not replace us. Fall back to
        # a clean in-process re-exec so the new code is still picked up.
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            log.warning("supervisor systemctl restart failed: %s", detail[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("supervisor systemctl restart raised: %s", exc)
    try:
        log.info("component update: re-execing supervisor in place")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    except Exception as exc:  # noqa: BLE001 - last resort; never crash the box
        log.warning("supervisor re-exec failed: %s", exc)


def _post_component_update_result(
    *,
    revision: int,
    status: str,
    diagnostic: str | None = None,
) -> None:
    """POST the component-update outcome to the platform.

    Mirrors ``_post_config_apply_result`` but on the component-update
    endpoint, and additionally carries ``applied_versions`` (read back from
    ``collect_component_versions`` after the update) so the platform can
    confirm exactly what landed.
    """
    body: dict = {
        "revision": revision,
        "status": status,
        "diagnostic": diagnostic[:1023] if diagnostic else None,
        "applied_versions": collect_component_versions(),
    }
    post_json("/hapi/v1/computers/me/component-update/apply-result", body)


def handle_update_component_command(command: dict) -> None:
    """Handle one heartbeat-delivered ``update_component`` command.

    See the section header above for the command shape and ordering
    rationale. Defensive throughout: a Computer in the field must never
    brick itself, so every component step is wrapped and a failure is
    REPORTED (``status=failed``) rather than raised.

    Per-revision dedupe: the platform re-sends the command until it sees the
    target revision reflected in a heartbeat, so the same command can arrive
    multiple times. We persist each attempted revision and skip a
    re-delivery, mirroring the ``apply_config`` dedupe (and the persisted
    file survives the runtime self-update's process restart).
    """
    revision = _command_revision(command)
    targets = command.get("targets")
    if revision is None or not isinstance(targets, dict):
        log.warning("ignoring malformed update_component command: %r", command)
        return

    state = _read_component_update_state()
    if state.get("last_revision") == revision:
        log.info(
            "update_component revision=%d already attempted (status=%s); skipping",
            revision,
            state.get("status"),
        )
        return

    log.info("heartbeat command: applying component update revision=%d", revision)
    diagnostics: list[str] = []
    all_ok = True

    # Order: plugin -> framework -> runtime. The runtime self-update is the
    # riskiest (rewrites the repo the supervisor runs from and must restart
    # the process), so it runs last — a plugin/framework failure never
    # strands a half-updated runtime.

    plugin_target = targets.get("plugin")
    if isinstance(plugin_target, dict) and str(plugin_target.get("ref") or "").strip():
        ok, diag = _update_plugin_component(str(plugin_target["ref"]).strip())
        all_ok = all_ok and ok
        if diag:
            diagnostics.append(diag)

    framework_target = targets.get("framework")
    if isinstance(framework_target, dict) and str(
        framework_target.get("version") or ""
    ).strip():
        ok, diag = _update_framework_component(str(framework_target["version"]).strip())
        all_ok = all_ok and ok
        if diag:
            diagnostics.append(diag)

    # Runtime is handled specially: because the running process is still the
    # OLD code until restarted, on a SUCCESSFUL real (non-dev) checkout we
    # post the applied-result FIRST, then restart the supervisor process.
    runtime_target = targets.get("runtime")
    runtime_requested = isinstance(runtime_target, dict) and bool(
        str(runtime_target.get("ref") or "").strip()
    )
    runtime_needs_restart = False
    if runtime_requested:
        ok, diag = _update_runtime_component(str(runtime_target["ref"]).strip())
        all_ok = all_ok and ok
        if diag:
            diagnostics.append(diag)
        # Only a real (non-dev) successful checkout requires a process
        # restart to take effect. Dev mode returns ok with a "skipped"
        # diagnostic and must not restart.
        runtime_needs_restart = ok and not _dev_mode()

    status = "applied" if all_ok else "failed"
    diagnostic = "; ".join(diagnostics) if diagnostics else None

    # Record the attempted revision (success or fail) for dedupe BEFORE any
    # restart, so the re-execed supervisor does not re-run this revision on
    # its next heartbeat.
    _write_component_update_state(revision, status)

    # Post the result before restarting for a runtime self-update (the
    # restart may terminate this process). For non-runtime updates this is
    # the normal post-after-work path.
    try:
        _post_component_update_result(
            revision=revision, status=status, diagnostic=diagnostic
        )
    except Exception as exc:
        log.warning(
            "failed to post component update result for revision=%d: %s",
            revision,
            exc,
        )

    if runtime_needs_restart:
        _restart_supervisor()


def handle_heartbeat_command(command: dict) -> None:
    """Dispatch a heartbeat-delivered command to its handler.

    The platform piggybacks at most one command on a heartbeat response.
    Unknown command types are ignored (forward-compatibility: an older
    runtime must tolerate a newer platform emitting a command it does not
    understand yet).
    """
    cmd_type = command.get("type")
    if cmd_type == "apply_config":
        handle_apply_config_command(command)
    elif cmd_type == "start_chatgpt_link":
        handle_start_chatgpt_link_command(command)
    elif cmd_type == "update_component":
        handle_update_component_command(command)
    else:
        log.info("ignoring unknown heartbeat command type: %r", cmd_type)


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
    # PR #24 review at 01:19Z — cold-start guard: a profile on disk
    # without a current owner is orphaned by definition. Wipe at most
    # once per Phase B entry to avoid pummeling the filesystem on
    # every poll iteration of an idle Computer.
    cold_start_wipe_attempted = False
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
        # Cold-start owner-release (Codex 01:19Z + 01:32Z reviews —
        # two attack paths that share the same root cause).
        #
        # Run UNCONDITIONALLY on the first ``assigned=false`` poll
        # per Phase B entry — not just when a profile already exists.
        # The 01:32Z review surfaced the remaining gap in the gated
        # version: a device-code worker from the previous binding
        # can be in-flight (the owner hasn't approved yet) when
        # Phase B observes the unassign. Gating on profile presence
        # would skip the bump+cancel here, the worker would keep
        # polling auth.openai.com, and when the user finally
        # approved the CLI would write ``openai-codex:old@example.com``
        # to the same auth store the next owner is about to inherit.
        #
        # _wipe_on_owner_release is the right entry point for this:
        # it bumps the binding generation (so any worker observing
        # supersession exits silently on its next loop iteration),
        # SIGTERMs in-flight CLI subprocesses (so a racing OAuth-
        # profile write can't land in the gap), and only THEN wipes
        # the file (a no-op when no profile is present, which is
        # the common case here). Runs at most once per Phase B
        # entry — subsequent polls of an idle Computer don't re-
        # touch the filesystem or re-bump the generation.
        if not cold_start_wipe_attempted:
            cold_start_wipe_attempted = True
            try:
                log.info(
                    "phase B cold-start: running owner-release path "
                    "(supersedes any in-flight subscription workers, "
                    "wipes any orphaned auth-profile)"
                )
                _wipe_on_owner_release(reason="cold-start-orphan")
            except Exception as exc:
                log.warning(
                    "phase B cold-start owner-release failed: %s", exc
                )
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
    # heartbeat window. The owner-identity subset is stamped
    # separately so the watchdog can decide whether to wipe the
    # per-agent OAuth auth-store on rebind (issue #23 — owner
    # change = wipe; mode flip for the same owner = don't wipe).
    _stop_holder["signature"] = _binding_signature(binding)
    _stop_holder["owner_signature"] = _owner_identity_signature(binding)
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
                    "/hapi/v1/computers/me/heartbeat",
                    {
                        "metrics": metrics,
                        "component_versions": collect_component_versions(),
                    },
                )
                command = heartbeat.get("command") if isinstance(heartbeat, dict) else None
                if isinstance(command, dict):
                    handle_heartbeat_command(command)
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
                    # Issue #23: platform-driven unassign hands the
                    # Computer back to the pool. Wipe the previous
                    # owner's per-agent OAuth credential before the
                    # supervisor releases control, so the next owner
                    # can't inherit a linked-subscription state from
                    # the prior binding.
                    _wipe_on_owner_release(reason="unassign")
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
                    # Issue #23: only wipe when the OWNER changed,
                    # not when the same owner flipped llm_auth_mode
                    # or rotated their OpenRouter key. A mode flip
                    # for the same owner triggers a rebind (so the
                    # supervisor rewrites openclaw.json) but the
                    # OAuth credential they just linked should
                    # survive into the next config.
                    new_owner_sig = _owner_identity_signature(new_binding)
                    cached_owner_sig = _stop_holder.get("owner_signature")
                    if cached_owner_sig and new_owner_sig != cached_owner_sig:
                        _wipe_on_owner_release(reason="reassign")
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
