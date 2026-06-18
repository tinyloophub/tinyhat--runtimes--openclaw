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

export PYTHONPATH="${bundle_source}${PYTHONPATH:+:${PYTHONPATH}}"

python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${bundle_source}" >/dev/null
bundle_id="$(python3 -m tinyhat_runtime.main bundle id --bundle-dir "${bundle_source}")"
bundle_name="${bundle_id#sha256:}"
target="${bundles_dir}/${bundle_name}"
tmp_target="${target}.tmp.$$"

mkdir -p "${bundles_dir}"
rm -rf -- "${tmp_target}"
cp -a -- "${bundle_source}" "${tmp_target}"
chmod +x "${tmp_target}"/bin/tinyhat-*
rm -rf -- "${target}"
mv -- "${tmp_target}" "${target}"
ln -sfn -- "${target}" "${current_link}"

if [[ "${skip_systemd}" != "1" ]]; then
  install -d -m 0755 "${systemd_dir}"
  install -m 0644 "${target}/systemd/tinyhat-runtime-gateway.service" \
    "${systemd_dir}/tinyhat-runtime-gateway.service"
  install -m 0644 "${target}/systemd/tinyhat-runtime-attestation.service" \
    "${systemd_dir}/tinyhat-runtime-attestation.service"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable tinyhat-runtime-gateway.service tinyhat-runtime-attestation.service
  fi
fi

printf '{"installed":true,"bundle_id":"%s","current":"%s"}\n' "${bundle_id}" "${current_link}"
