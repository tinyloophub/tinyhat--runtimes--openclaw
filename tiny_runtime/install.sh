#!/usr/bin/env bash
# Install and activate a tiny_runtime content-addressed bundle.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bundle_source="${TINYHAT_RUNTIME_BUNDLE_DIR:-${script_dir}}"
install_root="${TINYHAT_RUNTIME_INSTALL_ROOT:-/opt/tinyhat}"
bundles_dir="${TINYHAT_RUNTIME_BUNDLES_DIR:-${install_root}/bundles}"
current_link="${TINYHAT_RUNTIME_CURRENT_LINK:-${install_root}/current}"
systemd_dir="${TINYHAT_SYSTEMD_DIR:-/etc/systemd/system}"
skip_systemd="${TINYHAT_RUNTIME_SKIP_SYSTEMD:-0}"
# Stage-only: materialize + verify bundles/<digest> but DO NOT flip
# /opt/tinyhat/current and DO NOT enable systemd units. The caller (the
# stage_and_activate_bundle ledger verb) defers the flip to
# launcher.activate_bundle, which owns the stop->flip->start->health->
# auto-rollback sequence. Defaults off, so existing bootstrap callers keep
# flipping inline at install time.
stage_only="${TINYHAT_BUNDLE_STAGE_ONLY:-0}"

export PYTHONPATH="${bundle_source}${PYTHONPATH:+:${PYTHONPATH}}"

python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${bundle_source}" >/dev/null
bundle_id="$(python3 -m tinyhat_runtime.main bundle id --bundle-dir "${bundle_source}")"
bundle_name="${bundle_id#sha256:}"
if [[ "${bundle_id}" != sha256:* || "${#bundle_name}" -ne 64 || ! "${bundle_name}" =~ ^[0-9a-f]+$ ]]; then
  echo "tiny_runtime install: malformed bundle id: ${bundle_id}" >&2
  exit 1
fi
target="${bundles_dir}/${bundle_name}"
tmp_target="${target}.tmp.$$"

mkdir -p "${bundles_dir}"

# Stage-only must never endanger the live rollback target. bundles/<digest> is
# content-addressed, so an already-materialized target holds exactly this
# content: reuse it (verify only) instead of rm -rf + recreate. This is the safe
# path when the platform replays the same resolved refs, and it makes it
# impossible to delete the directory ${current_link} points at -- an
# interruption between `rm -rf` and `mv` would otherwise leave a live Computer's
# `current` dangling at a missing bundle.
if [[ "${stage_only}" == "1" && -d "${target}" ]]; then
  PYTHONPATH="${target}${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${target}" >/dev/null
  printf '{"staged":true,"activated":false,"reused":true,"bundle_id":"%s","bundle_dir":"%s"}\n' \
    "${bundle_id}" "${target}"
  exit 0
fi
rm -rf -- "${tmp_target}"
cp -a -- "${bundle_source}" "${tmp_target}"
chmod +x "${tmp_target}"/bin/tinyhat-*
if [[ "$(id -u)" -eq 0 ]]; then
  # OpenClaw refuses root-managed plugin paths owned by the unpacking user.
  chown -R 0:0 "${tmp_target}"
fi
rm -rf -- "${target}"
mv -- "${tmp_target}" "${target}"
PYTHONPATH="${target}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${target}" >/dev/null
if [[ "${stage_only}" != "1" ]]; then
  ln -sfn -- "${target}" "${current_link}"
fi

if [[ "${stage_only}" != "1" && "${skip_systemd}" != "1" ]]; then
  install -d -m 0755 "${systemd_dir}"
  install -m 0644 "${target}/systemd/tinyhat-runtime-gateway.service" \
    "${systemd_dir}/tinyhat-runtime-gateway.service"
  install -m 0644 "${target}/systemd/tinyhat-runtime-attestation.service" \
    "${systemd_dir}/tinyhat-runtime-attestation.service"
  install -m 0644 "${target}/systemd/tinyhat-runtime-platform.service" \
    "${systemd_dir}/tinyhat-runtime-platform.service"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable \
      tinyhat-runtime-gateway.service \
      tinyhat-runtime-attestation.service \
      tinyhat-runtime-platform.service
  fi
fi

if [[ "${stage_only}" == "1" ]]; then
  printf '{"staged":true,"activated":false,"bundle_id":"%s","bundle_dir":"%s"}\n' \
    "${bundle_id}" "${target}"
else
  printf '{"installed":true,"bundle_id":"%s","current":"%s"}\n' \
    "${bundle_id}" "${current_link}"
fi
