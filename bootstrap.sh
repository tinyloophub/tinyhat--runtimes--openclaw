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
#   TINYHAT_PUBLIC_RUNTIME_CACHE_STATUS_PATH — public cache hit/miss status file
#   TINYHAT_HARD_RESET_USER_STATE_MIGRATION — when 1, back up old OpenClaw
#       state/config, clean install-layout paths, and migrate old user data
#   TINYHAT_HARD_RESET_USER_STATE_MIGRATION_TOKEN — optional one-shot token;
#       change this value to intentionally run another hard-reset migration
#   TINYHAT_HARD_RESET_BACKUP_RETENTION — number of hard-reset backups to keep
#   TINYHAT_INSTALL_TINY_RUNTIME_FROM_SOURCE — when 1, this legacy bootstrap
#       stops/removes old tinyhat-openclaw units, assembles a tiny_runtime bundle
#       from the checked-out runtime source, installs it, starts only the
#       tinyhat-runtime-* services, and exits.
#
# Private-access enrollment is runtime-owned. The Computer authenticates to
# the platform with its service-account identity and pulls one-time enrollment
# material from /hapi/v1/computers/me/private-access/enrollment; the platform
# startup metadata must not carry provider auth keys.
#
# This file ships in the standalone public Tinyhat Computer runtime
# repository. It must not import from or assume the Tinyhat monorepo.

set -euo pipefail

RUNTIME_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR_PATH="${RUNTIME_DIR}/supervisor.py"
SUPERVISOR_UNIT_NAME="tinyhat-openclaw.service"
GATEWAY_UNIT_NAME="tinyhat-openclaw-gateway.service"
WORKLOAD_SLICE_UNIT_NAME="tinyhat-openclaw-workload.slice"
TINY_RUNTIME_PLATFORM_UNIT_NAME="tinyhat-runtime-platform.service"
TINY_RUNTIME_GATEWAY_UNIT_NAME="tinyhat-runtime-gateway.service"
TINY_RUNTIME_ATTESTATION_UNIT_NAME="tinyhat-runtime-attestation.service"
SUPERVISOR_UNIT="/etc/systemd/system/${SUPERVISOR_UNIT_NAME}"
GATEWAY_UNIT="/etc/systemd/system/${GATEWAY_UNIT_NAME}"
WORKLOAD_SLICE_UNIT="/etc/systemd/system/${WORKLOAD_SLICE_UNIT_NAME}"
RUNTIME_ENV_FILE="/etc/tinyhat/runtime.env"

OPENCLAW_CONFIG_PATH="/etc/openclaw/openclaw.json"
OPENCLAW_CONFIG_DIR="$(dirname "${OPENCLAW_CONFIG_PATH}")"
OPENCLAW_STATE_DIR="/var/lib/tinyhat-openclaw"
OPENCLAW_USER_STATE_BACKUP_ROOT="/var/lib/tinyhat-openclaw-backups"
TINYHAT_RUNTIME_LOG_ROOT="${TINYHAT_RUNTIME_LOG_ROOT:-/var/log/tinyhat}"
RUNTIME_BOOTSTRAP_STATUS_PATH="${OPENCLAW_STATE_DIR}/bootstrap-status.json"
OPENCLAW_GATEWAY_PORT="18789"
OPENCLAW_INSTALL_SPEC="${TINYHAT_FRAMEWORK_INSTALL_SPEC:-}"
CODEX_SUBSCRIPTION_PLUGIN_PACKAGE="@openclaw/codex"
TINYHAT_RUNTIME_USER="${TINYHAT_OPENCLAW_RUNTIME_USER:-tinyhat}"
TINYHAT_RUNTIME_GROUP="${TINYHAT_OPENCLAW_RUNTIME_GROUP:-tinyhat}"
HARD_RESET_USER_STATE_MIGRATION="${TINYHAT_HARD_RESET_USER_STATE_MIGRATION:-0}"
HARD_RESET_USER_STATE_MIGRATION_TOKEN="${TINYHAT_HARD_RESET_USER_STATE_MIGRATION_TOKEN:-default}"
HARD_RESET_BACKUP_RETENTION="${TINYHAT_HARD_RESET_BACKUP_RETENTION:-3}"
INSTALL_TINY_RUNTIME_FROM_SOURCE="${TINYHAT_INSTALL_TINY_RUNTIME_FROM_SOURCE:-0}"

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
  mkdir -p \
    "${OPENCLAW_CONFIG_DIR}" \
    "${OPENCLAW_STATE_DIR}" \
    "${TINYHAT_RUNTIME_LOG_ROOT}/commands" \
    "${TINYHAT_RUNTIME_LOG_ROOT}/diagnostics"
  chown -R \
    "${TINYHAT_RUNTIME_USER}:${TINYHAT_RUNTIME_GROUP}" \
    "${OPENCLAW_CONFIG_DIR}" \
    "${OPENCLAW_STATE_DIR}" \
    "${TINYHAT_RUNTIME_LOG_ROOT}"
  chmod 0700 \
    "${OPENCLAW_CONFIG_DIR}" \
    "${OPENCLAW_STATE_DIR}" \
    "${TINYHAT_RUNTIME_LOG_ROOT}"
}

legacy_process_pids() {
  local mode="$1"
  local pid
  local args
  ps -eo pid=,args= 2>/dev/null | while read -r pid args; do
    [[ -n "${pid}" ]] || continue
    [[ "${pid}" != "$$" ]] || continue
    case "${mode}" in
      supervisor)
        if [[ "${args}" == *"python3"* \
          && "${args}" == *"${RUNTIME_DIR}/supervisor.py"* ]]; then
          printf '%s\n' "${pid}"
        fi
        ;;
      gateway)
        if [[ "${args}" == *"openclaw gateway run"* \
          && "${args}" == *"--auth none"* \
          && "${args}" == *"--tailscale off"* ]]; then
          printf '%s\n' "${pid}"
        fi
        ;;
    esac
  done
}

terminate_legacy_processes() {
  local mode="$1"
  local label="$2"
  local pids=()
  local attempt
  mapfile -t pids < <(legacy_process_pids "${mode}")
  if [[ "${#pids[@]}" -eq 0 ]]; then
    return 0
  fi
  echo "[tinyhat-runtime] terminating ${label}: ${pids[*]}"
  kill -TERM "${pids[@]}" >/dev/null 2>&1 || true
  for attempt in 1 2 3; do
    sleep 1
    mapfile -t pids < <(legacy_process_pids "${mode}")
    if [[ "${#pids[@]}" -eq 0 ]]; then
      return 0
    fi
  done
  echo "[tinyhat-runtime] force-killing ${label}: ${pids[*]}"
  kill -KILL "${pids[@]}" >/dev/null 2>&1 || true
}

cleanup_legacy_openclaw_processes() {
  terminate_legacy_processes supervisor "legacy supervisor processes"
  terminate_legacy_processes gateway "legacy gateway processes"
}

remove_legacy_openclaw_units() {
  echo "[tinyhat-runtime] removing legacy tinyhat-openclaw supervisor units"
  systemctl stop \
    "${SUPERVISOR_UNIT_NAME}" \
    "${GATEWAY_UNIT_NAME}" \
    "${WORKLOAD_SLICE_UNIT_NAME}" >/dev/null 2>&1 || true
  cleanup_legacy_openclaw_processes
  systemctl disable \
    "${SUPERVISOR_UNIT_NAME}" \
    "${GATEWAY_UNIT_NAME}" >/dev/null 2>&1 || true
  rm -f -- \
    "${SUPERVISOR_UNIT}" \
    "${GATEWAY_UNIT}" \
    "${WORKLOAD_SLICE_UNIT}"
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl reset-failed \
    "${SUPERVISOR_UNIT_NAME}" \
    "${GATEWAY_UNIT_NAME}" >/dev/null 2>&1 || true
}

stop_existing_tiny_runtime_units() {
  echo "[tinyhat-runtime] stopping existing tiny_runtime services before source reinstall"
  systemctl stop \
    "${TINY_RUNTIME_PLATFORM_UNIT_NAME}" \
    "${TINY_RUNTIME_GATEWAY_UNIT_NAME}" \
    "${TINY_RUNTIME_ATTESTATION_UNIT_NAME}" >/dev/null 2>&1 || true
  systemctl reset-failed \
    "${TINY_RUNTIME_PLATFORM_UNIT_NAME}" \
    "${TINY_RUNTIME_GATEWAY_UNIT_NAME}" \
    "${TINY_RUNTIME_ATTESTATION_UNIT_NAME}" >/dev/null 2>&1 || true
}

quiesce_for_tiny_runtime_source_reinstall() {
  if [[ "${INSTALL_TINY_RUNTIME_FROM_SOURCE}" != "1" ]]; then
    return 0
  fi

  write_runtime_bootstrap_status "updating" "stopping existing runtime before source reinstall"
  stop_existing_tiny_runtime_units
  remove_legacy_openclaw_units
  write_runtime_bootstrap_status "updating" "existing runtime stopped for source reinstall"
}

fail_tiny_runtime_source_reinstall() {
  local diagnostic="$1"
  write_runtime_bootstrap_status "error" "${diagnostic}"
  echo "[tinyhat-runtime] ERROR: ${diagnostic}" >&2
  exit 1
}

write_tiny_runtime_source_env() {
  install -d -m 0755 "$(dirname "${RUNTIME_ENV_FILE}")"
  cat > "${RUNTIME_ENV_FILE}" <<TINY_RUNTIME_ENV
TINYHAT_BACKEND_AUDIENCE=${TINYHAT_BACKEND_AUDIENCE:-}
TINYHAT_PLATFORM_BASE_URL=${TINYHAT_PLATFORM_BASE_URL:-}
TINYHAT_FRAMEWORK_INSTALL_SPEC=${OPENCLAW_INSTALL_SPEC:-}
TINYHAT_PLATFORM_PLUGIN_REPO_URL=${TINYHAT_PLATFORM_PLUGIN_REPO_URL:-}
TINYHAT_PLATFORM_PLUGIN_REPO_REF=${TINYHAT_PLATFORM_PLUGIN_REPO_REF:-}
TINYHAT_PUBLIC_RUNTIME_CACHE_STATUS_PATH=${TINYHAT_PUBLIC_RUNTIME_CACHE_STATUS_PATH:-}
TINYHAT_HARD_RESET_USER_STATE_MIGRATION=${HARD_RESET_USER_STATE_MIGRATION}
TINYHAT_RUNTIME_EXPECTED_REPO_REF=$(git -C "${RUNTIME_DIR}" rev-parse HEAD 2>/dev/null || printf '%s' "${TINYHAT_RUNTIME_EXPECTED_REPO_REF:-unknown}")
TINYHAT_RUNTIME_INSTALL_ROOT=/opt/tinyhat
TINYHAT_RUNTIME_CURRENT_LINK=/opt/tinyhat/current
TINYHAT_OPENCLAW_RUNTIME_USER=${TINYHAT_RUNTIME_USER}
TINYHAT_OPENCLAW_RUNTIME_GROUP=${TINYHAT_RUNTIME_GROUP}
TINYHAT_RUNTIME_LOG_ROOT=${TINYHAT_RUNTIME_LOG_ROOT}
TINYHAT_RUNTIME_GENERATION=tiny_runtime
TINYHAT_RUNTIME_STARTUP_IMAGE_MODE=source_reinstall
TINY_RUNTIME_ENV
  chmod 0600 "${RUNTIME_ENV_FILE}"
}

install_tiny_runtime_from_source() {
  if [[ "${INSTALL_TINY_RUNTIME_FROM_SOURCE}" != "1" ]]; then
    return 1
  fi

  local assembler="${RUNTIME_DIR}/tiny_runtime/bake/assemble-bundle.sh"
  local bundle_out
  local runtime_ref
  local openclaw_ref
  local openclaw_bin
  local plugin_ref
  local installed
  if [[ ! -x "${assembler}" ]]; then
    write_runtime_bootstrap_status "error" "tiny_runtime source assembler missing"
    echo "[tinyhat-runtime] ERROR: tiny_runtime assembler missing at ${assembler}" >&2
    exit 1
  fi

  runtime_ref="$(git -C "${RUNTIME_DIR}" rev-parse HEAD 2>/dev/null || printf '%s' "${TINYHAT_RUNTIME_EXPECTED_REPO_REF:-unknown}")"
  openclaw_ref="${OPENCLAW_INSTALL_SPEC:-openclaw}"
  openclaw_bin="$(command -v openclaw 2>/dev/null || true)"
  if [[ -n "${openclaw_bin}" ]]; then
    openclaw_bin="$(readlink -f "${openclaw_bin}" 2>/dev/null || printf '%s' "${openclaw_bin}")"
  fi
  plugin_ref="${TINYHAT_PLATFORM_PLUGIN_REPO_REF:-unknown}"
  mkdir -p /opt/tinyhat/source-bundles
  bundle_out="$(mktemp -d /opt/tinyhat/source-bundles/tiny-runtime-bundle.XXXXXX)"

  write_tiny_runtime_source_env
  echo "[tinyhat-runtime] assembling tiny_runtime source bundle at ${bundle_out}"
  TINYHAT_RUNTIME_REF="${runtime_ref}" \
  TINYHAT_OPENCLAW_REF="${openclaw_ref}" \
  TINYHAT_OPENCLAW_BIN="${openclaw_bin}" \
  TINYHAT_PLUGIN_REF="${plugin_ref}" \
    "${assembler}" "${bundle_out}" \
    || {
      rm -rf -- "${bundle_out}"
      fail_tiny_runtime_source_reinstall "tiny_runtime source bundle assembly failed"
    }

  echo "[tinyhat-runtime] installing tiny_runtime source bundle"
  installed="$(TINYHAT_RUNTIME_BUNDLE_DIR="${bundle_out}" "${bundle_out}/install.sh")" \
    || {
      rm -rf -- "${bundle_out}"
      fail_tiny_runtime_source_reinstall "tiny_runtime source bundle install failed"
    }
  rm -rf -- "${bundle_out}"
  echo "[tinyhat-runtime] ${installed}"

  echo "[tinyhat-runtime] preinstalling tiny_runtime OpenClaw plugins"
  /opt/tinyhat/current/bin/tinyhat-runtime bake preinstall-plugins \
    || fail_tiny_runtime_source_reinstall "tiny_runtime plugin preinstall failed"

  echo "[tinyhat-runtime] warming tiny_runtime platform config"
  /opt/tinyhat/current/bin/tinyhat-runtime platform warm-config \
    --platform-base-url "${TINYHAT_PLATFORM_BASE_URL:-}" \
    --backend-audience "${TINYHAT_BACKEND_AUDIENCE:-}" \
    || fail_tiny_runtime_source_reinstall "tiny_runtime platform warm-config failed"
  echo "[tinyhat-runtime] enrolling private access via Computer identity"
  if ! /opt/tinyhat/current/bin/tinyhat-runtime private-access enroll-platform; then
    echo "[tinyhat-runtime] WARNING: private-access enrollment failed; platform loop will report diagnostics" >&2
  fi
  systemctl daemon-reload \
    || fail_tiny_runtime_source_reinstall "systemd daemon reload failed"
  remove_legacy_openclaw_units
  systemctl enable --now \
    tinyhat-runtime-gateway.service \
    tinyhat-runtime-attestation.service \
    tinyhat-runtime-platform.service \
    || fail_tiny_runtime_source_reinstall "tiny_runtime systemd services failed to start"
  write_runtime_bootstrap_status "ready" "tiny_runtime source reinstall complete"
  echo "[tinyhat-runtime] tiny_runtime source reinstall complete"
  return 0
}

verify_codex_subscription_plugin() {
  HOME="${OPENCLAW_STATE_DIR}" \
    OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" \
    OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" \
    openclaw plugins inspect codex --json \
    | python3 -c 'import json, sys; p=(json.load(sys.stdin).get("plugin") or {}); ids=p.get("providerIds") or p.get("providers") or []; sys.exit(0 if p.get("id") == "codex" and p.get("enabled") is not False and p.get("status") == "loaded" and "codex" in ids else 1)'
}

enroll_private_access_from_platform_source() {
  echo "[tinyhat-runtime] enrolling private access via Computer identity"
  if PYTHONPATH="${RUNTIME_DIR}/tiny_runtime" \
    python3 -m tinyhat_runtime.main private-access enroll-platform; then
    echo "[tinyhat-runtime] private access enrollment command completed"
  else
    echo "[tinyhat-runtime] WARNING: private-access enrollment failed; OpenClaw bootstrap will continue" >&2
  fi
}

remove_path_if_present() {
  local path="$1"
  if [[ -e "${path}" || -L "${path}" ]]; then
    rm -rf -- "${path}"
  fi
}

copy_path_if_present() {
  local source_path="$1"
  local target_path="$2"
  if [[ -e "${source_path}" || -L "${source_path}" ]]; then
    if ! mkdir -p "$(dirname "${target_path}")"; then
      echo "[tinyhat-runtime] ERROR: failed to create restore target parent for ${target_path}" >&2
      exit 1
    fi
    if ! cp -a -- "${source_path}" "${target_path}"; then
      echo "[tinyhat-runtime] ERROR: failed to restore ${source_path} to ${target_path}" >&2
      exit 1
    fi
    return 0
  fi
  return 1
}

hard_reset_safe_token() {
  printf '%s' "${HARD_RESET_USER_STATE_MIGRATION_TOKEN}" \
    | tr -c 'A-Za-z0-9_.-' '_'
}

hard_reset_user_state_marker_path() {
  printf '%s/.hard-reset-user-state-migration-%s.done\n' \
    "${OPENCLAW_USER_STATE_BACKUP_ROOT}" \
    "$(hard_reset_safe_token)"
}

hard_reset_backup_contains_expected_user_state() {
  local state_backup="$1"
  local config_backup="$2"
  local relative_path
  for relative_path in \
    agents \
    workspace \
    memory \
    memories \
    credentials \
    auth \
    auth-store \
    sessions \
    stores \
    data \
    auth-profiles.json \
    openclaw-agent.sqlite; do
    if [[ -e "${state_backup}/${relative_path}" || -L "${state_backup}/${relative_path}" ]]; then
      return 0
    fi
  done
  if [[ -e "${config_backup}/tinyhat-secrets.json" || -L "${config_backup}/tinyhat-secrets.json" ]]; then
    return 0
  fi
  return 1
}

hard_reset_count_preserved_expected_user_state() {
  local count=0
  local relative_path
  for relative_path in \
    agents \
    workspace \
    memory \
    memories \
    credentials \
    auth \
    auth-store \
    sessions \
    stores \
    data \
    auth-profiles.json \
    openclaw-agent.sqlite; do
    if [[ -e "${OPENCLAW_STATE_DIR}/${relative_path}" || -L "${OPENCLAW_STATE_DIR}/${relative_path}" ]]; then
      count=$((count + 1))
    fi
  done
  if [[ -e "${OPENCLAW_CONFIG_DIR}/tinyhat-secrets.json" || -L "${OPENCLAW_CONFIG_DIR}/tinyhat-secrets.json" ]]; then
    count=$((count + 1))
  fi
  printf '%s\n' "${count}"
}

hard_reset_remove_disposable_install_paths() {
  # Keep unknown OpenClaw user data in place. Only the runtime-owned install and
  # generated paths are cleaned so old broken package layouts cannot survive.
  remove_path_if_present "${OPENCLAW_STATE_DIR}/platform-plugins"
  remove_path_if_present "${OPENCLAW_STATE_DIR}/extensions"
  remove_path_if_present "${OPENCLAW_STATE_DIR}/bootstrap-status.json"
  remove_path_if_present "${OPENCLAW_STATE_DIR}/hard-reset-restore.env"
  remove_path_if_present "${OPENCLAW_CONFIG_PATH}"
}

hard_reset_prune_old_backups() {
  local retention="${HARD_RESET_BACKUP_RETENTION}"
  local backup
  local backups=()
  if ! [[ "${retention}" =~ ^[0-9]+$ ]]; then
    retention=3
  fi
  if [[ "${retention}" -lt 1 ]]; then
    retention=1
  fi
  shopt -s nullglob
  backups=("${OPENCLAW_USER_STATE_BACKUP_ROOT}"/hard-reset-*)
  shopt -u nullglob
  if [[ "${#backups[@]}" -le "${retention}" ]]; then
    return 0
  fi
  for backup in "${backups[@]:0:$((${#backups[@]} - retention))}"; do
    echo "[tinyhat-runtime] pruning old hard-reset backup: ${backup}"
    remove_path_if_present "${backup}"
  done
}

hard_reset_openclaw_user_state_layout() {
  if [[ "${HARD_RESET_USER_STATE_MIGRATION}" != "1" ]]; then
    return 0
  fi

  local reset_id
  local backup_root
  local state_backup
  local config_backup
  local migration_marker
  local layout_warning=""
  local migrated_count=0
  local preserved_count=0
  local restored_count=0

  mkdir -p "${OPENCLAW_USER_STATE_BACKUP_ROOT}"
  migration_marker="$(hard_reset_user_state_marker_path)"
  if [[ -f "${migration_marker}" ]]; then
    echo "[tinyhat-runtime] hard reset: user-state migration already completed for token ${HARD_RESET_USER_STATE_MIGRATION_TOKEN}; skipping"
    return 0
  fi

  reset_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  backup_root="${OPENCLAW_USER_STATE_BACKUP_ROOT}/hard-reset-${reset_id}"
  state_backup="${backup_root}/state"
  config_backup="${backup_root}/config"

  echo "[tinyhat-runtime] hard reset: backing up OpenClaw user state to ${backup_root}"
  systemctl stop "${SUPERVISOR_UNIT_NAME}" "${GATEWAY_UNIT_NAME}" >/dev/null 2>&1 || true
  mkdir -p "${backup_root}"
  if [[ -e "${OPENCLAW_STATE_DIR}" || -L "${OPENCLAW_STATE_DIR}" ]]; then
    cp -a -- "${OPENCLAW_STATE_DIR}" "${state_backup}"
  fi
  if [[ -e "${OPENCLAW_CONFIG_DIR}" || -L "${OPENCLAW_CONFIG_DIR}" ]]; then
    cp -a -- "${OPENCLAW_CONFIG_DIR}" "${config_backup}"
  fi

  mkdir -p "${OPENCLAW_STATE_DIR}" "${OPENCLAW_CONFIG_DIR}"

  if ! hard_reset_backup_contains_expected_user_state "${state_backup}" "${config_backup}"; then
    layout_warning="no_expected_openclaw_user_state_paths_found"
    echo "[tinyhat-runtime] WARNING: hard reset backup did not contain expected OpenClaw user-state paths; preserved unknown paths in place and kept full backup at ${backup_root}" >&2
  fi

  # Boundary exception: OpenClaw does not yet expose an official export/import
  # command for local auth, memory, and credential state. Keep this narrow and
  # tracked by the internal-state audit called out in AGENTS.md; prefer official
  # OpenClaw commands once they exist.
  hard_reset_remove_disposable_install_paths

  # Some pre-2026.6 layouts stored auth material at the state root. Move those
  # into the canonical default-agent location when that location is still empty.
  if [[ -f "${state_backup}/auth-profiles.json" \
    && ! -f "${OPENCLAW_STATE_DIR}/agents/main/agent/auth-profiles.json" ]]; then
    copy_path_if_present \
      "${state_backup}/auth-profiles.json" \
      "${OPENCLAW_STATE_DIR}/agents/main/agent/auth-profiles.json"
    migrated_count=$((migrated_count + 1))
  fi
  if [[ -f "${state_backup}/openclaw-agent.sqlite" \
    && ! -f "${OPENCLAW_STATE_DIR}/agents/main/agent/openclaw-agent.sqlite" ]]; then
    copy_path_if_present \
      "${state_backup}/openclaw-agent.sqlite" \
      "${OPENCLAW_STATE_DIR}/agents/main/agent/openclaw-agent.sqlite"
    migrated_count=$((migrated_count + 1))
  fi

  if [[ -f "${config_backup}/tinyhat-secrets.json" \
    && ! -f "${OPENCLAW_CONFIG_DIR}/tinyhat-secrets.json" ]]; then
    copy_path_if_present \
      "${config_backup}/tinyhat-secrets.json" \
      "${OPENCLAW_CONFIG_DIR}/tinyhat-secrets.json"
    chmod 0600 "${OPENCLAW_CONFIG_DIR}/tinyhat-secrets.json" || true
    migrated_count=$((migrated_count + 1))
  fi

  preserved_count="$(hard_reset_count_preserved_expected_user_state)"
  restored_count=$((preserved_count + migrated_count))
  {
    printf 'mode=hard_reset_user_state_migration\n'
    printf 'backup_root=%s\n' "${backup_root}"
    printf 'migration_marker=%s\n' "${migration_marker}"
    printf 'layout_warning=%s\n' "${layout_warning}"
    printf 'preserved_count=%s\n' "${preserved_count}"
    printf 'migrated_count=%s\n' "${migrated_count}"
    printf 'restored_count=%s\n' "${restored_count}"
  } > "${OPENCLAW_STATE_DIR}/hard-reset-restore.env"
  {
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'backup_root=%s\n' "${backup_root}"
    printf 'layout_warning=%s\n' "${layout_warning}"
  } > "${migration_marker}"
  hard_reset_prune_old_backups
  echo "[tinyhat-runtime] hard reset: preserved ${preserved_count} and migrated ${migrated_count} user-state paths"
}

cleanup_stale_openclaw_npm_temp_dirs() {
  local global_root="$1"
  local path
  shopt -s nullglob
  for path in "${global_root}"/.openclaw-*; do
    echo "[tinyhat-runtime] removing stale OpenClaw npm temp dir: ${path}"
    remove_path_if_present "${path}"
  done
  shopt -u nullglob
}

verify_openclaw_cli() {
  local verify_log
  verify_log="$(mktemp /tmp/tinyhat-openclaw-smoke.XXXXXX.log)"
  if openclaw --version >"${verify_log}" 2>&1; then
    rm -f "${verify_log}"
    return 0
  fi
  echo "[tinyhat-runtime] ERROR: openclaw CLI smoke failed" >&2
  tail -n 20 "${verify_log}" >&2 || true
  rm -f "${verify_log}"
  return 1
}

repair_or_cleanup_openclaw_backups() {
  local global_root="$1"
  local package_dir="${global_root}/openclaw"
  local latest=""
  local backup
  local backups=()
  shopt -s nullglob
  backups=("${global_root}"/.tinyhat-openclaw-backup-*)
  shopt -u nullglob
  if [[ "${#backups[@]}" -gt 0 ]]; then
    latest="${backups[$((${#backups[@]} - 1))]}"
    echo "[tinyhat-runtime] restoring interrupted OpenClaw framework backup: ${latest}"
    remove_path_if_present "${package_dir}"
    mv -- "${latest}" "${package_dir}"
  fi
  for backup in "${backups[@]}"; do
    if [[ -n "${latest}" && "${backup}" == "${latest}" ]]; then
      continue
    fi
    echo "[tinyhat-runtime] removing stale OpenClaw framework backup: ${backup}"
    remove_path_if_present "${backup}"
  done
}

install_openclaw_framework_package() {
  local install_spec="$1"
  local global_root
  global_root="$(npm root -g | awk 'NF { line = $0 } END { print line }')"
  if [[ -z "${global_root}" ]]; then
    echo "[tinyhat-runtime] ERROR: npm root -g returned an empty path" >&2
    return 1
  fi
  local package_dir="${global_root}/openclaw"
  local backup_dir=""
  local install_log
  repair_or_cleanup_openclaw_backups "${global_root}"
  cleanup_stale_openclaw_npm_temp_dirs "${global_root}"
  if [[ -e "${package_dir}" || -L "${package_dir}" ]]; then
    backup_dir="${global_root}/.tinyhat-openclaw-backup-$(date +%s)-$$"
    mv -- "${package_dir}" "${backup_dir}"
  fi
  for attempt in 1 2; do
    install_log="$(mktemp /tmp/tinyhat-openclaw-npm.XXXXXX.log)"
    if npm install -g --no-fund --no-audit "${install_spec}" >"${install_log}" 2>&1; then
      if verify_openclaw_cli; then
        remove_path_if_present "${backup_dir}"
        rm -f "${install_log}"
        echo "[tinyhat-runtime] installed framework package: ${install_spec}"
        return 0
      fi
      echo "[tinyhat-runtime] ERROR: ${install_spec} installed but OpenClaw CLI is not runnable (attempt ${attempt})" >&2
    else
      echo "[tinyhat-runtime] ERROR: npm install failed for ${install_spec} (attempt ${attempt})" >&2
      tail -n 20 "${install_log}" >&2 || true
    fi
    rm -f "${install_log}"
    remove_path_if_present "${package_dir}"
    cleanup_stale_openclaw_npm_temp_dirs "${global_root}" || true
    if [[ "${attempt}" == "1" ]]; then
      echo "[tinyhat-runtime] retrying clean OpenClaw framework install after failed attempt" >&2
      npm cache clean --force >/dev/null 2>&1 || true
    fi
  done
  if [[ -n "${backup_dir}" && ( -e "${backup_dir}" || -L "${backup_dir}" ) ]]; then
    if [[ "${HARD_RESET_USER_STATE_MIGRATION}" == "1" ]]; then
      remove_path_if_present "${backup_dir}"
      echo "[tinyhat-runtime] hard reset: discarded previous OpenClaw framework package after failed fresh install" >&2
    else
      mv -- "${backup_dir}" "${package_dir}"
      echo "[tinyhat-runtime] restored previous OpenClaw framework package after failed install" >&2
    fi
  fi
  return 1
}

if [[ ! -f "${SUPERVISOR_PATH}" ]]; then
  echo "[tinyhat-runtime] ERROR: supervisor.py not found at ${SUPERVISOR_PATH}" >&2
  exit 1
fi

quiesce_for_tiny_runtime_source_reinstall

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
hard_reset_openclaw_user_state_layout
mkdir -p /opt/tinyhat /etc/openclaw /etc/tinyhat /var/lib/tinyhat /var/lib/tinyhat-private-access "${TINYHAT_RUNTIME_LOG_ROOT}"
chown_runtime_paths
if [[ "${INSTALL_TINY_RUNTIME_FROM_SOURCE}" != "1" ]]; then
  enroll_private_access_from_platform_source
else
  echo "[tinyhat-runtime] source reinstall will enroll private access after installing tiny_runtime"
fi

if [[ -n "${OPENCLAW_INSTALL_SPEC}" ]]; then
  if install_openclaw_framework_package "${OPENCLAW_INSTALL_SPEC}"; then
    :
  else
    write_runtime_bootstrap_status "error" "openclaw framework npm install failed"
    echo "[tinyhat-runtime] ERROR: failed to install ${OPENCLAW_INSTALL_SPEC}" >&2
    exit 1
  fi
else
  echo "[tinyhat-runtime] WARNING: TINYHAT_FRAMEWORK_INSTALL_SPEC is unset; using existing openclaw binary from platform bootstrap"
fi
if verify_openclaw_cli; then
  write_runtime_bootstrap_status "ready" "openclaw CLI available"
else
  write_runtime_bootstrap_status "error" "openclaw CLI failed after bootstrap"
  echo "[tinyhat-runtime] ERROR: openclaw CLI failed after bootstrap" >&2
  exit 1
fi

if [[ "${INSTALL_TINY_RUNTIME_FROM_SOURCE}" == "1" ]]; then
  install_tiny_runtime_from_source
  echo "[tinyhat-runtime] bootstrap complete"
  exit 0
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

# Root-only on-box CLI (the `tinyhat` diagnose surface). A thin
# wrapper so a root support shell can run
# `tinyhat status|health|manifest show|manifest drift|whoami`.
# Privilege enforcement (euid==0) lives in the Python entrypoint;
# diagnose commands never mutate runtime state. The wrapper bakes the
# checkout path at bootstrap time — the runtime self-update re-checks
# the repo out at the same path, so the wrapper stays valid.
TINYHAT_CLI_WRAPPER="/usr/local/bin/tinyhat"
cat > "${TINYHAT_CLI_WRAPPER}" <<WRAPPER
#!/usr/bin/env bash
export PYTHONPATH="${RUNTIME_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
exec /usr/bin/python3 -m tinyhat_cli "\$@"
WRAPPER
chmod 0755 "${TINYHAT_CLI_WRAPPER}"
echo "[tinyhat-runtime] installed tinyhat CLI wrapper at ${TINYHAT_CLI_WRAPPER}"

# Fallback runtime config. The supervisor prefers GCE instance
# metadata (tinyhat-backend-audience / tinyhat-platform-base-url)
# and only reads this file if the metadata server is unreachable.
{
  echo "TINYHAT_BACKEND_AUDIENCE=${TINYHAT_BACKEND_AUDIENCE:-}"
  echo "TINYHAT_PLATFORM_BASE_URL=${TINYHAT_PLATFORM_BASE_URL:-}"
  echo "TINYHAT_PLATFORM_PLUGIN_REPO_URL=${TINYHAT_PLATFORM_PLUGIN_REPO_URL:-}"
  echo "TINYHAT_PLATFORM_PLUGIN_REPO_REF=${TINYHAT_PLATFORM_PLUGIN_REPO_REF:-}"
  echo "TINYHAT_PUBLIC_RUNTIME_CACHE_STATUS_PATH=${TINYHAT_PUBLIC_RUNTIME_CACHE_STATUS_PATH:-}"
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

# The supervisor: started by the GCE startup script after this bootstrap
# completes on every boot, then restarted by systemd on failure. Do not enable
# it directly under multi-user.target: combined with
# After=google-startup-scripts.service, that creates an ordering cycle and can
# make reboot recovery depend on which job systemd deletes.
cat > "${SUPERVISOR_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw Computer supervisor
After=network-online.target google-startup-scripts.service
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
UNIT

systemctl daemon-reload
systemctl start "${WORKLOAD_SLICE_UNIT_NAME}"
# Remove stale boot-target symlinks from older bootstrap versions. The GCE
# metadata startup script runs this bootstrap on each boot and queues the
# supervisor after the runtime package/plugin install has completed.
systemctl disable "${SUPERVISOR_UNIT_NAME}" >/dev/null 2>&1 || true
echo "[tinyhat-runtime] queueing supervisor start after bootstrap"
systemctl start --no-block "${SUPERVISOR_UNIT_NAME}"

echo "[tinyhat-runtime] bootstrap complete"
