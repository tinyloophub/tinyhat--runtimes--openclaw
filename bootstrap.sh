#!/usr/bin/env bash
# Tinyhat Computer runtime bootstrap.
#
# Invoked by the VM's thin GCE startup script after the runtime repo
# has been cloned and the framework (OpenClaw) has been installed.
# This is the runtime's own install command: it installs the
# supervisor + framework gateway as systemd units and starts the
# supervisor.
#
# Idempotent: re-running re-writes the units and reloads systemd
# without breaking a running supervisor.
#
# Optional fallback config (used only when the GCE metadata server
# is unreachable) is read from the environment and written to
# /etc/tinyhat/runtime.env:
#   TINYHAT_BACKEND_AUDIENCE   — JWT audience for the identity token
#   TINYHAT_PLATFORM_BASE_URL  — platform origin for /me/* calls
#
# This file ships in the standalone public Tinyhat Computer runtime
# repository. It must not import from or assume the Tinyhat monorepo.

set -euo pipefail

RUNTIME_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR_PATH="${RUNTIME_DIR}/supervisor.py"
SUPERVISOR_UNIT="/etc/systemd/system/tinyhat-openclaw.service"
GATEWAY_UNIT="/etc/systemd/system/tinyhat-openclaw-gateway.service"
RUNTIME_ENV_FILE="/etc/tinyhat/runtime.env"

OPENCLAW_CONFIG_PATH="/etc/openclaw/openclaw.json"
OPENCLAW_STATE_DIR="/var/lib/tinyhat-openclaw"
OPENCLAW_GATEWAY_PORT="18789"

echo "[tinyhat-runtime] bootstrap starting from ${RUNTIME_DIR}"

if [[ ! -f "${SUPERVISOR_PATH}" ]]; then
  echo "[tinyhat-runtime] ERROR: supervisor.py not found at ${SUPERVISOR_PATH}" >&2
  exit 1
fi

mkdir -p /opt/tinyhat /etc/openclaw /etc/tinyhat

# Fallback runtime config. The supervisor prefers GCE instance
# metadata (tinyhat-backend-audience / tinyhat-platform-base-url)
# and only reads this file if the metadata server is unreachable.
{
  echo "TINYHAT_BACKEND_AUDIENCE=${TINYHAT_BACKEND_AUDIENCE:-}"
  echo "TINYHAT_PLATFORM_BASE_URL=${TINYHAT_PLATFORM_BASE_URL:-}"
} > "${RUNTIME_ENV_FILE}"
chmod 0644 "${RUNTIME_ENV_FILE}"

# The OpenClaw gateway: a separate systemd unit so it has
# first-class lifecycle, logs, and crash-restart semantics. Started
# and stopped only by the supervisor; restarted by systemd if
# OpenClaw crashes.
cat > "${GATEWAY_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=OPENCLAW_CONFIG_PATH=${OPENCLAW_CONFIG_PATH}
Environment=OPENCLAW_STATE_DIR=${OPENCLAW_STATE_DIR}
Environment=HOME=${OPENCLAW_STATE_DIR}
WorkingDirectory=${OPENCLAW_STATE_DIR}
ExecStart=/usr/bin/env openclaw gateway run --force --allow-unconfigured --port ${OPENCLAW_GATEWAY_PORT} --bind loopback --auth none --tailscale off --verbose
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
UNIT

# The supervisor: enabled on boot + restarted on failure. Owns
# binding coordination and starts/stops the gateway unit above.
cat > "${SUPERVISOR_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw Computer supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-${RUNTIME_ENV_FILE}
ExecStart=/usr/bin/python3 ${SUPERVISOR_PATH}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now tinyhat-openclaw.service

echo "[tinyhat-runtime] bootstrap complete"
