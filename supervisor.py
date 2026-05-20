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
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

# Path conventions on the VM. Pinned here so the gateway systemd
# unit (written by bootstrap.sh) and this supervisor stay in
# lockstep.
OPENCLAW_CONFIG_PATH = "/etc/openclaw/openclaw.json"
OPENCLAW_STATE_DIR = "/var/lib/tinyhat-openclaw"
OPENCLAW_WORKSPACE_DIR = "/var/lib/tinyhat-openclaw/workspace"
OPENCLAW_GATEWAY_PORT = 18789
OPENCLAW_DEFAULT_MODEL = "openai/gpt-5.2"
GATEWAY_SYSTEMD_UNIT = "tinyhat-openclaw-gateway.service"

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
    """
    now = time.time()
    cached = _base_url_cache.get("value")
    ts = float(_base_url_cache.get("ts") or 0.0)
    if cached and (now - ts) < METADATA_TTL_SECONDS:
        return cached
    fallback = (os.environ.get("TINYHAT_PLATFORM_BASE_URL") or "").strip()
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


def write_openclaw_config(binding: dict) -> None:
    """Write the real OpenClaw gateway config for this binding."""
    owner_id = str(binding.get("telegram_owner_user_id") or "").strip()
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not owner_id:
        raise ValueError("binding is missing telegram_owner_user_id")
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")

    os.makedirs(os.path.dirname(OPENCLAW_CONFIG_PATH), exist_ok=True)
    os.makedirs(OPENCLAW_STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(OPENCLAW_WORKSPACE_DIR, mode=0o700, exist_ok=True)

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
                "workspace": OPENCLAW_WORKSPACE_DIR,
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
            "entries": {
                "telegram": {"enabled": True},
                "openai": openai_plugin,
            },
        },
        "session": {"dmScope": "per-channel-peer"},
    }
    if openrouter_enabled:
        config["env"] = {"OPENROUTER_API_KEY": openrouter_key}
    tmp = OPENCLAW_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    os.replace(tmp, OPENCLAW_CONFIG_PATH)
    os.chmod(OPENCLAW_CONFIG_PATH, 0o600)
    # Log only non-secret summary; never log the API key.
    log.info(
        "wrote OpenClaw config to %s (bot=@%s owner=%s model=%s openrouter=%s)",
        OPENCLAW_CONFIG_PATH,
        binding.get("telegram_bot_username"),
        owner_id,
        primary_model,
        "yes" if openrouter_enabled else "no",
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


def start_openclaw_gateway(binding: dict) -> float:
    """Start real OpenClaw under systemd.

    The Python supervisor owns binding coordination only. The
    OpenClaw gateway itself is a separate systemd unit so it has
    first-class lifecycle, logs, and crash restart semantics.
    """
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
    return (
        _run_systemctl(
            "is-active", "--quiet", GATEWAY_SYSTEMD_UNIT, check=False
        ).returncode
        == 0
    )


def probe_openclaw_gateway_health(started_at: float) -> tuple[bool, str]:
    """Inspect OpenClaw's own systemd logs for channel readiness.

    ``openclaw gateway health --url ...`` requires explicit gateway
    credentials even for this unauthenticated loopback setup, so the
    production readiness gate follows the systemd-owned gateway
    process and waits for the OpenClaw log lines that matter here:
    the gateway is ready and Telegram is connected for long polling.
    """
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
            last_probe = "systemd unit is not active"
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
        write_openclaw_config(binding)
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
            try:
                post_json(
                    "/hapi/v1/computers/me/heartbeat", {"metrics": metrics}
                )
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
