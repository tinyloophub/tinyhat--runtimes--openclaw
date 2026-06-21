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

# The supervisor reports metrics.private_access ONLY when this file
# exists with provider="tailscale"; the backend merges that report into
# the Computer row so the admin / Mini App terminal path becomes
# available. The prod bootstrap.sh writes the same ready/error/
# config_missing JSON here — the dev entrypoint must too, otherwise the
# backend never learns the dev node joined and the terminal path stays
# unavailable.
PRIVATE_ACCESS_STATUS_DIR="/var/lib/tinyhat-private-access"
PRIVATE_ACCESS_STATUS_FILE="${PRIVATE_ACCESS_STATUS_DIR}/bootstrap-status.json"

if [[ "${TINYHAT_PRIVATE_ACCESS_PROVIDER:-}" == "tailscale" ]]; then
  mkdir -p "${PRIVATE_ACCESS_STATUS_DIR}"
  if [[ -n "${TINYHAT_TAILSCALE_AUTH_KEY_FILE:-}" \
        && -f "${TINYHAT_TAILSCALE_AUTH_KEY_FILE}" ]]; then
    echo "[dev-entrypoint] starting tailscaled (userspace networking)..."
    # Userspace networking needs no NET_ADMIN / /dev/net/tun, so this runs
    # under Docker Desktop without extra container capabilities.
    mkdir -p /var/run/tailscale "${RUNTIME_HOME}/tailscale"
    tailscaled \
      --tun=userspace-networking \
      --state="${RUNTIME_HOME}/tailscale/tailscaled.state" \
      --statedir="${RUNTIME_HOME}/tailscale" \
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
    if tailscale "${up_args[@]}"; then
      printf '%s\n' '{"provider":"tailscale","state":"ready"}' \
        > "${PRIVATE_ACCESS_STATUS_FILE}"
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

if [[ "${TINYHAT_RUNTIME_MODE:-legacy_supervisor}" == "tiny_runtime" ]]; then
  echo "[dev-entrypoint] starting tiny_runtime platform loop as tinyhat..."
  export PYTHONPATH="/opt/tinyhat-runtime/tiny_runtime${PYTHONPATH:+:${PYTHONPATH}}"
  exec gosu tinyhat bash -lc '
    set -euo pipefail
    runtime_home="${TINYHAT_RUNTIME_HOME:-/home/tinyhat/runtime}"
    mkdir -p "${runtime_home}/openclaw" "${runtime_home}/tinyhat-control"
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
