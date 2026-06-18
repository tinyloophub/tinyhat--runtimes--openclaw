#!/usr/bin/env bash
set -euo pipefail

bundle_dir="${TINYHAT_RUNTIME_BUNDLE_DIR:-/opt/tinyhat/current}"
export PYTHONPATH="${bundle_dir}${PYTHONPATH:+:${PYTHONPATH}}"

python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${bundle_dir}" >/dev/null
python3 -m tinyhat_runtime.main attest --bundle-dir "${bundle_dir}" \
  --identity-file "${bundle_dir}/dev/identity.json" \
  --output /tmp/tinyhat-attestation.json >/dev/null
python3 -m tinyhat_runtime.main gateway health >/dev/null

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

cat /tmp/tinyhat-attestation.json
