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

if [[ "${TINYHAT_PRIVATE_ACCESS_PROVIDER:-}" == "tailscale" \
      && -n "${TINYHAT_TAILSCALE_AUTH_KEY_FILE:-}" \
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
  if tailscale "${up_args[@]}"; then
    echo "[dev-entrypoint] tailscale up OK — node is on the tailnet with SSH enabled"
  else
    echo "[dev-entrypoint] WARN: tailscale up failed; continuing without private access" >&2
  fi
fi

echo "[dev-entrypoint] starting supervisor as tinyhat..."
exec gosu tinyhat python3 /opt/tinyhat-runtime/supervisor.py
