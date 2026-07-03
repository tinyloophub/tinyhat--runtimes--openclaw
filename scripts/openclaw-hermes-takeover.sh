#!/usr/bin/env bash
#
# Tinyhat OpenClaw -> Hermes host takeover.
#
# This script is fetched from the public OpenClaw runtime channel and run ON an
# existing legacy Computer by the Tinyhat admin terminal/Tailscale path. It does
# not require the existing OpenClaw runtime daemon to understand any new command.

set -euo pipefail

log() { echo "[openclaw-hermes-takeover] $*"; }
fail() { echo "[openclaw-hermes-takeover] ERROR: $*" >&2; exit 1; }

HERMES_INSTALLER_URL=""
HERMES_REF=""
PLATFORM_URL=""
COMPUTER_ID=""

require_value() {
  local flag="$1"
  local value="${2:-}"
  [[ -n "${value}" && "${value}" != --* ]] || fail "${flag} requires a value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-installer-url)
      require_value "$1" "${2:-}"
      HERMES_INSTALLER_URL="$2"
      shift 2
      ;;
    --hermes-ref)
      require_value "$1" "${2:-}"
      HERMES_REF="$2"
      shift 2
      ;;
    --platform-url)
      require_value "$1" "${2:-}"
      PLATFORM_URL="$2"
      shift 2
      ;;
    --computer-id)
      require_value "$1" "${2:-}"
      COMPUTER_ID="$2"
      shift 2
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || fail "must run as root (use sudo)"
[[ -n "${HERMES_INSTALLER_URL}" ]] || fail "--hermes-installer-url is required"
[[ -n "${HERMES_REF}" ]] || fail "--hermes-ref is required"
[[ -n "${PLATFORM_URL}" ]] || fail "--platform-url is required"
[[ "${COMPUTER_ID}" =~ ^[0-9]+$ ]] || fail "--computer-id must be numeric"
[[ "${HERMES_INSTALLER_URL}" == https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/*/install.sh ]] \
  || fail "unsupported Hermes installer URL"
[[ "${HERMES_REF}" != *..* ]] || fail "--hermes-ref must not contain '..'"
[[ "${HERMES_INSTALLER_URL}" != *"/../"* && "${HERMES_INSTALLER_URL}" != *"/.." ]] \
  || fail "--hermes-installer-url must not contain path traversal"

command -v bash >/dev/null 2>&1 || fail "bash is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v openclaw >/dev/null 2>&1 || fail "openclaw CLI is required for verified backup"
command -v python3 >/dev/null 2>&1 || fail "python3 is required for migration manifest"
command -v pgrep >/dev/null 2>&1 || fail "pgrep is required to verify legacy process shutdown"
command -v ps >/dev/null 2>&1 || fail "ps is required to verify legacy process shutdown"

MIGRATION_ROOT="${TINYHAT_OPENCLAW_HERMES_MIGRATION_ROOT:-/var/lib/tinyhat-migrations/openclaw-to-hermes}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
WORKDIR="${MIGRATION_ROOT}/${RUN_ID}"
BACKUP_ARCHIVE="${WORKDIR}/openclaw-backup.tar.zst"
BACKUP_OUTPUT="${WORKDIR}/openclaw-backup-result.json"
INSTALLER_SCRIPT="${WORKDIR}/hermes-install.sh"
MANIFEST="${WORKDIR}/manifest.json"

mkdir -p "${WORKDIR}"
chmod 0700 "${MIGRATION_ROOT}" "${WORKDIR}" 2>/dev/null || true

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

legacy_openclaw_pids() {
  pgrep -f '(/var/lib/tinyhat-openclaw|/opt/tinyhat/|tinyhat-openclaw|tinyhat-runtime-(gateway|platform|attestation))' \
    2>/dev/null \
    | while read -r pid; do
        [[ -n "${pid}" ]] || continue
        [[ "${pid}" != "$$" ]] || continue
        ps -p "${pid}" -o args= 2>/dev/null \
          | grep -Ev 'openclaw-hermes-takeover|hermes-install.sh|tinyhat--runtimes--hermes' >/dev/null \
          && printf '%s\n' "${pid}"
      done
}

stop_legacy_openclaw() {
  log "stopping legacy OpenClaw services if present"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop tinyhat-openclaw-gateway.service tinyhat-runtime-gateway.service >/dev/null 2>&1 || true
    systemctl stop tinyhat-openclaw.service tinyhat-runtime-platform.service tinyhat-runtime-attestation.service >/dev/null 2>&1 || true
  fi

  local pids=""
  pids="$(legacy_openclaw_pids | sort -u || true)"
  if [[ -n "${pids}" ]]; then
    log "terminating legacy OpenClaw process(es): ${pids//$'\n'/ }"
    # shellcheck disable=SC2086
    kill ${pids} >/dev/null 2>&1 || true
    sleep 2
  fi

  pids="$(legacy_openclaw_pids | sort -u || true)"
  if [[ -n "${pids}" ]]; then
    log "force killing legacy OpenClaw process(es): ${pids//$'\n'/ }"
    # shellcheck disable=SC2086
    kill -9 ${pids} >/dev/null 2>&1 || true
    sleep 1
  fi

  pids="$(legacy_openclaw_pids | sort -u || true)"
  if [[ -n "${pids}" ]]; then
    echo "[openclaw-hermes-takeover] ERROR: legacy OpenClaw processes are still running: ${pids//$'\n'/ }" >&2
    return 1
  fi
}

write_manifest() {
  local status="$1"
  local backup_sha="${2:-}"
  local detail="${3:-}"
  python3 - "$MANIFEST" "$status" "$backup_sha" "$detail" "$WORKDIR" "$BACKUP_ARCHIVE" "$HERMES_REF" "$HERMES_INSTALLER_URL" "$PLATFORM_URL" "$COMPUTER_ID" <<'PY'
import json
import sys
from datetime import datetime, timezone

manifest_path = sys.argv[1]
status = sys.argv[2]
backup_sha = sys.argv[3]
detail = sys.argv[4]
workdir = sys.argv[5]
backup_archive = sys.argv[6]
hermes_ref = sys.argv[7]
hermes_installer_url = sys.argv[8]
platform_url = sys.argv[9]
computer_id = int(sys.argv[10])

payload = {
    "schema": "tinyhat_openclaw_hermes_takeover_manifest_v1",
    "status": status,
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "workdir": workdir,
    "backup_archive": backup_archive,
    "backup_sha256": backup_sha or None,
    "hermes_ref": hermes_ref,
    "hermes_installer_url": hermes_installer_url,
    "platform_url": platform_url,
    "computer_id": computer_id,
    "detail": detail or None,
}
with open(manifest_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  chmod 0600 "${MANIFEST}" 2>/dev/null || true
}

write_manifest "starting" "" "migration workspace created"

log "creating verified OpenClaw backup"
if ! openclaw backup create --output "${BACKUP_ARCHIVE}" --verify --json >"${BACKUP_OUTPUT}" 2>&1; then
  write_manifest "failed" "" "openclaw backup create failed"
  fail "openclaw backup create failed; see ${BACKUP_OUTPUT}"
fi
[[ -s "${BACKUP_ARCHIVE}" ]] || {
  write_manifest "failed" "" "backup archive was not created"
  fail "backup archive was not created"
}
BACKUP_SHA="$(sha256_file "${BACKUP_ARCHIVE}")"
write_manifest "backup_ready" "${BACKUP_SHA}" "verified OpenClaw backup created"
log "backup ready: ${BACKUP_ARCHIVE} sha256=${BACKUP_SHA}"

if ! stop_legacy_openclaw; then
  write_manifest "failed" "${BACKUP_SHA}" "legacy OpenClaw stop failed"
  fail "legacy OpenClaw stop failed"
fi

log "fetching Hermes installer ${HERMES_REF}"
if ! curl --proto '=https' --tlsv1.2 -fsSL \
  -o "${INSTALLER_SCRIPT}" \
  "${HERMES_INSTALLER_URL}"; then
  write_manifest "failed" "${BACKUP_SHA}" "Hermes installer download failed"
  fail "Hermes installer download failed"
fi
[[ -s "${INSTALLER_SCRIPT}" ]] || {
  write_manifest "failed" "${BACKUP_SHA}" "Hermes installer was empty"
  fail "Hermes installer was empty"
}
chmod 0700 "${INSTALLER_SCRIPT}" 2>/dev/null || true
INSTALLER_SHA="$(sha256_file "${INSTALLER_SCRIPT}")"

log "running Hermes installer ${HERMES_REF} sha256=${INSTALLER_SHA}"
if ! bash "${INSTALLER_SCRIPT}" \
  --ref "${HERMES_REF}" \
  --platform-url "${PLATFORM_URL}" \
  --computer-id "${COMPUTER_ID}"; then
  write_manifest "failed" "${BACKUP_SHA}" "Hermes installer failed"
  fail "Hermes installer failed"
fi

log "retiring legacy OpenClaw runtime units"
if command -v systemctl >/dev/null 2>&1; then
  for unit in \
    tinyhat-openclaw-gateway.service \
    tinyhat-openclaw.service \
    tinyhat-runtime-gateway.service \
    tinyhat-runtime-platform.service \
    tinyhat-runtime-attestation.service; do
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
  done
  systemctl daemon-reload >/dev/null 2>&1 || true
fi

write_manifest "hermes_installer_started" "${BACKUP_SHA}" "Hermes installer completed"
log "takeover complete; waiting for Hermes heartbeat on Computer ${COMPUTER_ID}"
