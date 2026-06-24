#!/usr/bin/env bash
# Tinyhat Computer runtime — dev-container entrypoint.
#
# Optionally brings the Computer up as a real Tailscale node (userspace
# networking + Tailscale SSH) before starting the supervisor, so a local
# dev Computer is reachable for SSH / the managed terminal the same way a
# managed cloud Computer is — instead of a docker-exec-only fallback.
# When the Tailscale env is absent (a plain `docker run`), this is a
# no-op and goes straight to the supervisor.
#
# It runs as root only long enough to start tailscaled — Tailscale SSH
# execs login shells, which needs root — then drops to the unprivileged
# `tinyhat` user (gosu) for the supervisor + OpenClaw, matching the
# non-Tailscale dev image.
set -euo pipefail

RUNTIME_HOME="${TINYHAT_RUNTIME_HOME:-/home/tinyhat/runtime}"
TINY_RUNTIME_BUNDLE_DIR="/opt/tinyhat-runtime/tiny_runtime"
TAILSCALE_STATE_DIR="${TINYHAT_TAILSCALE_STATE_DIR:-/var/lib/tinyhat-tailscale}"

# The supervisor reports metrics.private_access ONLY when this file
# exists with provider="tailscale"; the backend merges that report into
# the Computer row so the admin / Mini App terminal path becomes
# available. The prod bootstrap.sh writes the same ready/error/
# config_missing JSON here — the dev entrypoint must too, otherwise the
# backend never learns the dev node joined and the terminal path stays
# unavailable.
PRIVATE_ACCESS_STATUS_DIR="/var/lib/tinyhat-private-access"
PRIVATE_ACCESS_STATUS_FILE="${PRIVATE_ACCESS_STATUS_DIR}/bootstrap-status.json"

prepare_tiny_runtime_bundle() {
  local install_root="${TINYHAT_RUNTIME_INSTALL_ROOT:-/opt/tinyhat}"
  local runtime_ref="${TINYHAT_RUNTIME_REF:-docker-dev}"
  local openclaw_ref
  local plugin_ref

  export TINYHAT_RUNTIME_INSTALL_ROOT="${install_root}"
  export TINYHAT_RUNTIME_BUNDLES_DIR="${TINYHAT_RUNTIME_BUNDLES_DIR:-${install_root}/bundles}"
  export TINYHAT_RUNTIME_CURRENT_LINK="${TINYHAT_RUNTIME_CURRENT_LINK:-${install_root}/current}"
  # In the dev container OpenClaw's source state lives under $RUNTIME_HOME.
  # Keep rebuild backups outside that tree because OpenClaw intentionally
  # refuses to write a backup archive inside the source being backed up.
  export TINYHAT_RUNTIME_REBUILD_BACKUP_DIR="${TINYHAT_RUNTIME_REBUILD_BACKUP_DIR:-/tmp/tinyhat-rebuild-backups}"

  mkdir -p \
    "${TINYHAT_RUNTIME_INSTALL_ROOT}" \
    "${TINYHAT_RUNTIME_BUNDLES_DIR}" \
    "$(dirname "${TINYHAT_RUNTIME_CURRENT_LINK}")" \
    "${TINYHAT_RUNTIME_REBUILD_BACKUP_DIR}"
  chown -R tinyhat:tinyhat \
    "${TINYHAT_RUNTIME_INSTALL_ROOT}" \
    "${TINYHAT_RUNTIME_REBUILD_BACKUP_DIR}"

  openclaw_ref="$(
    python3 - <<'PY'
import json

with open("/opt/tinyhat-runtime/tiny_runtime/bake/bundle.lock", encoding="utf-8") as fh:
    print(json.load(fh)["dependencies"]["openclaw"]["resolved"])
PY
  )"
  plugin_ref="$(
    python3 - <<'PY'
import json

with open("/opt/tinyhat-runtime/tiny_runtime/bake/bundle.lock", encoding="utf-8") as fh:
    print(json.load(fh)["dependencies"]["tinyhat_openclaw_plugin"]["ref"])
PY
  )"

  gosu tinyhat env \
    PYTHONPATH="${TINY_RUNTIME_BUNDLE_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 -m tinyhat_runtime.main bundle write \
      --bundle-dir "${TINY_RUNTIME_BUNDLE_DIR}" \
      --runtime-ref "${runtime_ref}" \
      --openclaw-ref "${openclaw_ref}" \
      --plugin-ref "${plugin_ref}" >/dev/null

  gosu tinyhat env \
    TINYHAT_RUNTIME_SKIP_SYSTEMD=1 \
    TINYHAT_RUNTIME_BUNDLE_DIR="${TINY_RUNTIME_BUNDLE_DIR}" \
    TINYHAT_RUNTIME_INSTALL_ROOT="${TINYHAT_RUNTIME_INSTALL_ROOT}" \
    TINYHAT_RUNTIME_BUNDLES_DIR="${TINYHAT_RUNTIME_BUNDLES_DIR}" \
    TINYHAT_RUNTIME_CURRENT_LINK="${TINYHAT_RUNTIME_CURRENT_LINK}" \
    "${TINY_RUNTIME_BUNDLE_DIR}/install.sh" >/tmp/tinyhat-dev-bundle-install.json

  echo "[dev-entrypoint] tiny_runtime bundle installed: $(cat /tmp/tinyhat-dev-bundle-install.json)"
}

install_dev_systemctl_shim() {
  # tiny_runtime's gateway-control verbs (the secret-apply rebind that makes a
  # newly-saved user secret reach the agent's exec shell, plus force-upgrade)
  # stop/start the gateway via `systemctl`, which assumes systemd. A plain
  # Docker dev container has no systemd, so those calls ENOENT and the
  # secret-apply rebind can never advance applied_config_revision -> the
  # "✅ <NAME> is now available on your Computer" confirmation never fires (a
  # real systemd Computer completes it). Emulate just the gateway unit's
  # stop/start/restart as plain process management (procps-free: /proc scan +
  # kill, setsid relaunch) so the dev container behaves like a systemd
  # Computer for the one operation that needs a real gateway restart. Every
  # other systemctl call is a dev no-op. The match is deliberately narrow
  # (argv0 python* AND "-m tinyhat_runtime.main gateway run"), and PID 1 / the
  # shim itself are never targeted, so it cannot kill the entrypoint.
  cat > /usr/local/bin/systemctl <<'SYSTEMCTL_SHIM'
#!/usr/bin/env bash
set -uo pipefail
action="${1:-}"
unit="${2:-}"
log="${TINYHAT_RUNTIME_HOME:-/home/tinyhat/runtime}/openclaw-gateway.log"

_is_gateway() {
  case "$1" in
    python*"-m tinyhat_runtime.main gateway run"*) return 0 ;;
    *) return 1 ;;
  esac
}

_stop_gateway() {
  self=$$
  for d in /proc/[0-9]*; do
    pid="${d##*/}"
    [ "$pid" = "1" ] && continue
    [ "$pid" = "$self" ] && continue
    [ -r "${d}/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "${d}/cmdline" 2>/dev/null)
    if _is_gateway "$cmd"; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
}

_start_gateway() {
  setsid bash -c "exec python3 -m tinyhat_runtime.main gateway run >>'${log}' 2>&1" \
    </dev/null >/dev/null 2>&1 &
}

case "${unit%.service}" in
  tinyhat-runtime-gateway)
    case "${action}" in
      stop) _stop_gateway ;;
      start) _start_gateway ;;
      restart) _stop_gateway; sleep 1; _start_gateway ;;
    esac
    ;;
esac
exit 0
SYSTEMCTL_SHIM
  chmod 0755 /usr/local/bin/systemctl
  echo "[dev-entrypoint] installed dev systemctl shim for gateway control"
}

if [[ "${TINYHAT_PRIVATE_ACCESS_PROVIDER:-}" == "tailscale" ]]; then
  mkdir -p "${PRIVATE_ACCESS_STATUS_DIR}"
  if [[ -n "${TINYHAT_TAILSCALE_AUTH_KEY_FILE:-}" \
        && -f "${TINYHAT_TAILSCALE_AUTH_KEY_FILE}" ]]; then
    echo "[dev-entrypoint] starting tailscaled (userspace networking)..."
    # Userspace networking needs no NET_ADMIN / /dev/net/tun, so this runs
    # under Docker Desktop without extra container capabilities.
    mkdir -p /var/run/tailscale "${TAILSCALE_STATE_DIR}"
    if [[ -d "${RUNTIME_HOME}/tailscale" && "${TAILSCALE_STATE_DIR}" != "${RUNTIME_HOME}/tailscale" ]]; then
      # Older dev harnesses put Tailscale state under the OpenClaw state tree.
      # Keep transport state out of app-layer rebuild backups.
      rm -rf -- "${RUNTIME_HOME}/tailscale"
    fi
    tailscaled \
      --tun=userspace-networking \
      --state="${TAILSCALE_STATE_DIR}/tailscaled.state" \
      --statedir="${TAILSCALE_STATE_DIR}" \
      >"${RUNTIME_HOME}/tailscaled.log" 2>&1 &

    # Wait for the daemon socket (default path) before `tailscale up`.
    for _ in $(seq 1 50); do
      [[ -S /var/run/tailscale/tailscaled.sock ]] && break
      sleep 0.2
    done

    up_args=(
      "up"
      "--auth-key=file:${TINYHAT_TAILSCALE_AUTH_KEY_FILE}"
      "--ssh"
      "--operator=tinyhat"
      "--accept-dns=false"
    )
    if [[ -n "${TINYHAT_TAILSCALE_NODE_NAME:-}" ]]; then
      up_args+=("--hostname=${TINYHAT_TAILSCALE_NODE_NAME}")
    fi
    if [[ -n "${TINYHAT_TAILSCALE_TAGS:-}" ]]; then
      up_args+=("--advertise-tags=${TINYHAT_TAILSCALE_TAGS}")
    fi

    echo "[dev-entrypoint] tailscale up --ssh (node=${TINYHAT_TAILSCALE_NODE_NAME:-auto})..."
    # `if` condition so a non-zero exit doesn't trip `set -e`; record the
    # outcome in the status file the supervisor reports either way.
    tailscale logout >/dev/null 2>&1 || true
    if tailscale "${up_args[@]}"; then
      python3 - <<'PY' > "${PRIVATE_ACCESS_STATUS_FILE}"
import json
import os

print(json.dumps({
    "provider": "tailscale",
    "state": "ready",
    "node_name": os.environ.get("TINYHAT_TAILSCALE_NODE_NAME") or None,
}, sort_keys=True))
PY
      echo "[dev-entrypoint] tailscale up OK — node is on the tailnet with SSH enabled"
    else
      printf '%s\n' \
        '{"provider":"tailscale","state":"error","diagnostic":"tailscale up failed"}' \
        > "${PRIVATE_ACCESS_STATUS_FILE}"
      echo "[dev-entrypoint] WARN: tailscale up failed; continuing without private access" >&2
    fi
  else
    printf '%s\n' \
      '{"provider":"tailscale","state":"config_missing","diagnostic":"missing auth key"}' \
      > "${PRIVATE_ACCESS_STATUS_FILE}"
    echo "[dev-entrypoint] WARN: private access provider=tailscale but no auth-key file; skipping Tailscale" >&2
  fi
fi

if [[ -d "${PRIVATE_ACCESS_STATUS_DIR}" ]]; then
  chown -R tinyhat:tinyhat "${PRIVATE_ACCESS_STATUS_DIR}"
fi

runtime_mode="${TINYHAT_RUNTIME_MODE:-${TINYHAT_RUNTIME_IMAGE_MODE:-legacy_supervisor}}"

if [[ "${runtime_mode}" == "tiny_runtime" ]]; then
  echo "[dev-entrypoint] starting tiny_runtime platform loop as tinyhat..."
  prepare_tiny_runtime_bundle
  export PYTHONPATH="${TINY_RUNTIME_BUNDLE_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
  export TINYHAT_RUNTIME_NO_SERVICE_RESTART="${TINYHAT_RUNTIME_NO_SERVICE_RESTART:-1}"
  install_dev_systemctl_shim
  exec gosu tinyhat bash -lc '
    set -euo pipefail
    runtime_home="${TINYHAT_RUNTIME_HOME:-/home/tinyhat/runtime}"
    mkdir -p "${runtime_home}/openclaw" "${runtime_home}/tinyhat-control"
    python3 -m tinyhat_runtime.main platform warm-config >/dev/null
    gateway_log="${runtime_home}/openclaw-gateway.log"
    : > "${gateway_log}"
    echo "[dev-entrypoint] tiny_runtime gateway log: ${gateway_log}"
    python3 -m tinyhat_runtime.main gateway run >>"${gateway_log}" 2>&1 &
    gateway_pid="$!"
    trap '\''kill "${gateway_pid}" 2>/dev/null || true; wait "${gateway_pid}" 2>/dev/null || true'\'' EXIT INT TERM
    python3 -m tinyhat_runtime.main platform loop
  '
fi

echo "[dev-entrypoint] starting supervisor as tinyhat..."
exec gosu tinyhat python3 /opt/tinyhat-runtime/supervisor.py
