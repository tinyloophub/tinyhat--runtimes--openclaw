#!/usr/bin/env bash
#
# Tinyhat Computer in-place force-upgrade.
#
# Run this ON an existing Computer (as root) to rebuild it to the CURRENT
# runtime + plugins + OpenClaw — exactly like a Computer freshly built today —
# while PRESERVING the user's data (credentials, ChatGPT auth, identity, skills,
# agent state).
#
# The whole point: it works on a box running an OLD version and does NOT require
# updating the box first. It does not depend on the box's current runtime
# understanding any new command — it simply clones the public runtime repo and
# runs the standard install with the data-preserving migration forced on, the
# same way a fresh boot does. So a legacy / pre-tiny_runtime Computer is brought
# fully current in one shot.
#
# Usage (on the box, as root):
#   curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--openclaw/v0.16.8/force-upgrade.sh | sudo bash
#
# or, fetched and run with a pinned runtime ref:
#   sudo TINYHAT_FORCE_UPGRADE_REF=v0.16.8 bash force-upgrade.sh
#
# Run remotely without copying the file:
#   gcloud compute ssh <instance> --zone <zone> --tunnel-through-iap --command \
#     'curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--openclaw/v0.16.8/force-upgrade.sh | sudo bash'
#
# Platform / OpenClaw / plugin config (base URL, identity audience, framework
# version, plugin ref) is read from the box's own GCE metadata, just like the
# normal boot bootstrap — nothing platform-specific is hard-coded here.
#
# This file ships in the standalone public Tinyhat Computer runtime repository.
# It must not import from or assume the Tinyhat monorepo.

set -euo pipefail

REPO_URL="${TINYHAT_FORCE_UPGRADE_REPO_URL:-https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git}"
REF="${TINYHAT_FORCE_UPGRADE_REF:-v0.16.8}"

log() { echo "[force-upgrade] $*"; }
fail() { echo "[force-upgrade] ERROR: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || fail "must run as root (use sudo)"
command -v git >/dev/null 2>&1 || fail "git is required but not installed"
command -v bash >/dev/null 2>&1 || fail "bash is required"

WORKBASE="/opt/tinyhat/force-upgrade"
mkdir -p "${WORKBASE}"
WORKDIR="$(mktemp -d "${WORKBASE}/src.XXXXXX")"
cleanup() { rm -rf -- "${WORKDIR}" 2>/dev/null || true; }
trap cleanup EXIT

log "cloning ${REPO_URL} @ ${REF}"
# --branch works for both tags and branches; fall back to a full clone +
# checkout for an arbitrary commit SHA.
if ! git clone --depth 1 --branch "${REF}" "${REPO_URL}" "${WORKDIR}" 2>/dev/null; then
  rm -rf -- "${WORKDIR}"
  git clone "${REPO_URL}" "${WORKDIR}" || fail "git clone failed"
  git -C "${WORKDIR}" checkout "${REF}" || fail "could not checkout ref ${REF}"
fi

[[ -x "${WORKDIR}/bootstrap.sh" ]] || fail "bootstrap.sh missing in cloned runtime (${REF})"
RESOLVED_SHA="$(git -C "${WORKDIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
log "running data-preserving in-place reinstall from ${REF} (${RESOLVED_SHA})"

# Drive the standard runtime bootstrap install path, but force:
#   - source install of the current tiny_runtime from the cloned repo, and
#   - the hard-reset user-state migration (back up the old OpenClaw state, clean
#     the install-layout paths, reinstall fresh, then restore the user data),
# with a unique migration token so it always runs for this upgrade. The bootstrap
# reads the platform base URL / identity audience / framework + plugin specs from
# the box's GCE metadata, so an old box is rebuilt to today's stack in place.
TINYHAT_INSTALL_TINY_RUNTIME_FROM_SOURCE=1 \
TINYHAT_HARD_RESET_USER_STATE_MIGRATION=1 \
TINYHAT_HARD_RESET_USER_STATE_MIGRATION_TOKEN="force-upgrade-${RESOLVED_SHA}-$(date +%s)" \
  bash "${WORKDIR}/bootstrap.sh"

log "force-upgrade complete: runtime rebuilt to ${REF}, user data preserved, services restarted"
