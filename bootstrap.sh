#!/usr/bin/env bash
# Tinyhat Computer runtime bootstrap.
#
# Invoked by the VM's thin GCE startup script after the runtime repo
# has been cloned. This is the runtime's own install command: it
# installs generic Computer dependencies, optional private access,
# the requested framework package, and the supervisor + framework
# gateway systemd units.
#
# Idempotent: re-running re-writes the units and reloads systemd
# without breaking a running supervisor.
#
# Optional fallback config (used only when the GCE metadata server
# is unreachable) is read from the environment and written to
# /etc/tinyhat/runtime.env:
#   TINYHAT_BACKEND_AUDIENCE   — JWT audience for the identity token
#   TINYHAT_PLATFORM_BASE_URL  — platform origin for /me/* calls
#   TINYHAT_FRAMEWORK_INSTALL_SPEC — npm package spec, e.g. openclaw@2026.5.19
#   TINYHAT_PLATFORM_PLUGIN_REPO_URL — public Tinyhat OpenClaw plugin repo
#   TINYHAT_PLATFORM_PLUGIN_REPO_REF — public Tinyhat plugin ref/SHA to install
#
# Optional private-access material is passed by the platform startup
# script through env and consumed here:
#   TINYHAT_PRIVATE_ACCESS_PROVIDER
#   TINYHAT_TAILSCALE_AUTH_KEY
#   TINYHAT_TAILSCALE_NODE_NAME
#   TINYHAT_TAILSCALE_TAGS
#
# This file ships in the standalone public Tinyhat Computer runtime
# repository. It must not import from or assume the Tinyhat monorepo.

set -euo pipefail

RUNTIME_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR_PATH="${RUNTIME_DIR}/supervisor.py"
SUPERVISOR_UNIT_NAME="tinyhat-openclaw.service"
GATEWAY_UNIT_NAME="tinyhat-openclaw-gateway.service"
WORKLOAD_SLICE_UNIT_NAME="tinyhat-openclaw-workload.slice"
SUPERVISOR_UNIT="/etc/systemd/system/${SUPERVISOR_UNIT_NAME}"
GATEWAY_UNIT="/etc/systemd/system/${GATEWAY_UNIT_NAME}"
WORKLOAD_SLICE_UNIT="/etc/systemd/system/${WORKLOAD_SLICE_UNIT_NAME}"
RUNTIME_ENV_FILE="/etc/tinyhat/runtime.env"

OPENCLAW_CONFIG_PATH="/etc/openclaw/openclaw.json"
OPENCLAW_CONFIG_DIR="$(dirname "${OPENCLAW_CONFIG_PATH}")"
OPENCLAW_STATE_DIR="/var/lib/tinyhat-openclaw"
RUNTIME_BOOTSTRAP_STATUS_PATH="${OPENCLAW_STATE_DIR}/bootstrap-status.json"
OPENCLAW_GATEWAY_PORT="18789"
OPENCLAW_INSTALL_SPEC="${TINYHAT_FRAMEWORK_INSTALL_SPEC:-}"
CODEX_SUBSCRIPTION_PLUGIN_PACKAGE="@openclaw/codex"
PRIVATE_ACCESS_PROVIDER="${TINYHAT_PRIVATE_ACCESS_PROVIDER:-disabled}"
TINYHAT_RUNTIME_USER="${TINYHAT_OPENCLAW_RUNTIME_USER:-tinyhat}"
TINYHAT_RUNTIME_GROUP="${TINYHAT_OPENCLAW_RUNTIME_GROUP:-tinyhat}"

echo "[tinyhat-runtime] bootstrap starting from ${RUNTIME_DIR}"

write_runtime_bootstrap_status() {
  local state="$1"
  local diagnostic="$2"
  mkdir -p "${OPENCLAW_STATE_DIR}"
  printf '{"provider":"openclaw","state":"%s","diagnostic":"%s"}\n' \
    "${state}" "${diagnostic}" > "${RUNTIME_BOOTSTRAP_STATUS_PATH}"
}

ensure_runtime_user() {
  if ! getent group "${TINYHAT_RUNTIME_GROUP}" >/dev/null 2>&1; then
    groupadd --system "${TINYHAT_RUNTIME_GROUP}"
  fi
  if ! id -u "${TINYHAT_RUNTIME_USER}" >/dev/null 2>&1; then
    useradd \
      --system \
      --gid "${TINYHAT_RUNTIME_GROUP}" \
      --home-dir "${OPENCLAW_STATE_DIR}" \
      --shell /usr/sbin/nologin \
      "${TINYHAT_RUNTIME_USER}"
  fi
}

chown_runtime_paths() {
  mkdir -p "${OPENCLAW_CONFIG_DIR}" "${OPENCLAW_STATE_DIR}"
  chown -R \
    "${TINYHAT_RUNTIME_USER}:${TINYHAT_RUNTIME_GROUP}" \
    "${OPENCLAW_CONFIG_DIR}" \
    "${OPENCLAW_STATE_DIR}"
  chmod 0700 "${OPENCLAW_CONFIG_DIR}" "${OPENCLAW_STATE_DIR}"
}

verify_codex_subscription_plugin() {
  HOME="${OPENCLAW_STATE_DIR}" \
    OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" \
    OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" \
    openclaw plugins inspect codex --json \
    | python3 -c 'import json, sys; p=(json.load(sys.stdin).get("plugin") or {}); ids=p.get("providerIds") or p.get("providers") or []; sys.exit(0 if p.get("id") == "codex" and p.get("enabled") is not False and p.get("status") == "loaded" and "codex" in ids else 1)'
}

if [[ ! -f "${SUPERVISOR_PATH}" ]]; then
  echo "[tinyhat-runtime] ERROR: supervisor.py not found at ${SUPERVISOR_PATH}" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates gnupg jq git tmux python3 python3-pip
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
node --version
npm --version
git --version

ensure_runtime_user
mkdir -p /opt/tinyhat /etc/openclaw /etc/tinyhat /var/lib/tinyhat /var/lib/tinyhat-private-access
chown_runtime_paths

if [[ "${PRIVATE_ACCESS_PROVIDER}" == "tailscale" ]]; then
  TAILSCALE_AUTH_KEY="${TINYHAT_TAILSCALE_AUTH_KEY:-}"
  TAILSCALE_NODE_NAME="${TINYHAT_TAILSCALE_NODE_NAME:-}"
  TAILSCALE_TAGS="${TINYHAT_TAILSCALE_TAGS:-}"
  if [[ -n "${TAILSCALE_AUTH_KEY}" && -n "${TAILSCALE_NODE_NAME}" ]]; then
    if ! command -v tailscale >/dev/null 2>&1; then
      curl -fsSL https://tailscale.com/install.sh | sh
    fi
    systemctl enable --now tailscaled
    tailscale_auth_file="$(mktemp /tmp/tinyhat-tailscale-auth.XXXXXX)"
    chmod 0600 "${tailscale_auth_file}"
    printf '%s' "${TAILSCALE_AUTH_KEY}" > "${tailscale_auth_file}"
    tailscale_up_args=(
      up
      "--auth-key=file:${tailscale_auth_file}"
      "--hostname=${TAILSCALE_NODE_NAME}"
      --ssh
    )
    if [[ -n "${TAILSCALE_TAGS}" ]]; then
      tailscale_up_args+=("--advertise-tags=${TAILSCALE_TAGS}")
    fi

    set +e
    tailscale "${tailscale_up_args[@]}"
    ts_status="$?"
    rm -f "${tailscale_auth_file}"
    set -e
    if [[ "${ts_status}" == "0" ]]; then
      echo '{"provider":"tailscale","state":"ready"}' \
        > /var/lib/tinyhat-private-access/bootstrap-status.json
      echo "[tinyhat-runtime] private access enrolled with Tailscale"
    else
      echo '{"provider":"tailscale","state":"error","diagnostic":"tailscale up failed"}' \
        > /var/lib/tinyhat-private-access/bootstrap-status.json
      echo "[tinyhat-runtime] WARNING: Tailscale enrollment failed; OpenClaw bootstrap will continue"
    fi
  else
    echo '{"provider":"tailscale","state":"config_missing","diagnostic":"missing auth key or node name"}' \
      > /var/lib/tinyhat-private-access/bootstrap-status.json
    echo "[tinyhat-runtime] WARNING: private access configured but enrollment material is missing"
  fi
fi

if [[ -n "${OPENCLAW_INSTALL_SPEC}" ]]; then
  if npm install -g "${OPENCLAW_INSTALL_SPEC}"; then
    echo "[tinyhat-runtime] installed framework package: ${OPENCLAW_INSTALL_SPEC}"
  else
    write_runtime_bootstrap_status "error" "openclaw npm install failed"
    echo "[tinyhat-runtime] ERROR: failed to install ${OPENCLAW_INSTALL_SPEC}" >&2
    exit 1
  fi
else
  echo "[tinyhat-runtime] WARNING: TINYHAT_FRAMEWORK_INSTALL_SPEC is unset; using existing openclaw binary from platform bootstrap"
fi
if command -v openclaw >/dev/null 2>&1; then
  write_runtime_bootstrap_status "ready" "openclaw binary available"
else
  write_runtime_bootstrap_status "error" "openclaw binary missing"
  echo "[tinyhat-runtime] ERROR: openclaw binary is missing after bootstrap" >&2
  exit 1
fi

echo "[tinyhat-runtime] installing subscription provider plugin: ${CODEX_SUBSCRIPTION_PLUGIN_PACKAGE}"
if HOME="${OPENCLAW_STATE_DIR}" \
    OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" \
    OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" \
    openclaw plugins install "${CODEX_SUBSCRIPTION_PLUGIN_PACKAGE}" --force; then
  echo "[tinyhat-runtime] installed subscription provider plugin"
else
  write_runtime_bootstrap_status "ready" "codex subscription plugin install pending supervisor self-heal"
  echo "[tinyhat-runtime] WARNING: failed to install ${CODEX_SUBSCRIPTION_PLUGIN_PACKAGE}; supervisor will retry during gateway boot" >&2
fi
if verify_codex_subscription_plugin; then
  echo "[tinyhat-runtime] verified subscription provider plugin: codex"
else
  write_runtime_bootstrap_status "ready" "codex subscription plugin verify pending supervisor self-heal"
  echo "[tinyhat-runtime] WARNING: codex plugin is not registered yet; supervisor will retry during gateway boot" >&2
fi
chown_runtime_paths

# Fallback runtime config. The supervisor prefers GCE instance
# metadata (tinyhat-backend-audience / tinyhat-platform-base-url)
# and only reads this file if the metadata server is unreachable.
{
  echo "TINYHAT_BACKEND_AUDIENCE=${TINYHAT_BACKEND_AUDIENCE:-}"
  echo "TINYHAT_PLATFORM_BASE_URL=${TINYHAT_PLATFORM_BASE_URL:-}"
  echo "TINYHAT_PLATFORM_PLUGIN_REPO_URL=${TINYHAT_PLATFORM_PLUGIN_REPO_URL:-}"
  echo "TINYHAT_PLATFORM_PLUGIN_REPO_REF=${TINYHAT_PLATFORM_PLUGIN_REPO_REF:-}"
  echo "TINYHAT_OPENCLAW_RUNTIME_USER=${TINYHAT_RUNTIME_USER}"
  echo "TINYHAT_OPENCLAW_RUNTIME_GROUP=${TINYHAT_RUNTIME_GROUP}"
} > "${RUNTIME_ENV_FILE}"
chmod 0644 "${RUNTIME_ENV_FILE}"

# The workload slice is explicit so the supervisor can still sample
# bounded cgroup memory state while the gateway service itself is
# stopped for hold-down.
cat > "${WORKLOAD_SLICE_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw workload slice

[Slice]
MemoryAccounting=true
MemoryHigh=2400M
MemoryMax=3072M
CPUAccounting=true
CPUQuota=175%
TasksAccounting=true
TasksMax=512
UNIT

# The OpenClaw gateway: a separate systemd unit so it has
# first-class lifecycle, logs, and crash-restart semantics. Started
# and stopped only by the supervisor; restarted by systemd if
# OpenClaw crashes.
#
# Deliberately NOT PartOf= the supervisor (#685). Under systemd a
# PartOf child is restarted whenever the parent restarts — INCLUDING
# the supervisor's own Restart=on-failure / watchdog respawns — which
# bounced this gateway on every supervisor restart and defeated the
# reattach-continuity goal (a healthy gateway must survive a supervisor
# restart so the respawned supervisor can reattach without disrupting
# OpenClaw). Live GCE proof confirmed the gateway PID changed on each
# supervisor watchdog/crash restart with PartOf present. Teardown is
# instead owned by the supervisor itself: main() ends with
# `finally: stop_openclaw_gateway()`, so a clean stop of the supervisor
# (SIGTERM) still stops the gateway, while a crash/watchdog respawn
# leaves the running gateway untouched for the new supervisor to
# reattach. This matches the "supervisor is the lifecycle authority"
# fallback documented in the v0.11.0 scope (§4.1).
cat > "${GATEWAY_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw gateway
After=network-online.target ${SUPERVISOR_UNIT_NAME}
Wants=network-online.target
StartLimitIntervalSec=10min
StartLimitBurst=3

[Service]
Type=simple
User=${TINYHAT_RUNTIME_USER}
Group=${TINYHAT_RUNTIME_GROUP}
UMask=0077
Environment=OPENCLAW_CONFIG_PATH=${OPENCLAW_CONFIG_PATH}
Environment=OPENCLAW_STATE_DIR=${OPENCLAW_STATE_DIR}
Environment=HOME=${OPENCLAW_STATE_DIR}
WorkingDirectory=${OPENCLAW_STATE_DIR}
ExecStart=/usr/bin/env openclaw gateway run --force --allow-unconfigured --port ${OPENCLAW_GATEWAY_PORT} --bind loopback --auth none --tailscale off --verbose
Slice=${WORKLOAD_SLICE_UNIT_NAME}
MemoryAccounting=true
MemoryHigh=2400M
MemoryMax=3072M
CPUAccounting=true
CPUQuota=175%
TasksAccounting=true
TasksMax=512
OOMPolicy=stop
OOMScoreAdjust=500
Nice=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${OPENCLAW_CONFIG_DIR} ${OPENCLAW_STATE_DIR}
CapabilityBoundingSet=
AmbientCapabilities=
Restart=on-failure
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
StartLimitIntervalSec=10min
StartLimitBurst=6

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=180s
EnvironmentFile=-${RUNTIME_ENV_FILE}
Environment=TINYHAT_OPENCLAW_RUNTIME_USER=${TINYHAT_RUNTIME_USER}
Environment=TINYHAT_OPENCLAW_RUNTIME_GROUP=${TINYHAT_RUNTIME_GROUP}
ExecStart=/usr/bin/python3 ${SUPERVISOR_PATH}
Slice=tinyhat-openclaw-control.slice
MemoryAccounting=true
MemoryHigh=512M
MemoryMax=1536M
CPUAccounting=true
CPUQuota=100%
TasksAccounting=true
TasksMax=512
OOMPolicy=continue
OOMScoreAdjust=-800
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl start "${WORKLOAD_SLICE_UNIT_NAME}"
systemctl enable --now "${SUPERVISOR_UNIT_NAME}"

echo "[tinyhat-runtime] bootstrap complete"
