#!/usr/bin/env bash
# Assemble a tiny_runtime bundle directory and write its content manifest.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_root="${repo_root}/tiny_runtime"
out_dir="${1:-${repo_root}/dist/tiny_runtime_bundle}"
lock_file="${runtime_root}/bake/bundle.lock"

runtime_ref="${TINYHAT_RUNTIME_REF:-$(git -C "${repo_root}" rev-parse HEAD 2>/dev/null || printf 'unknown')}"
openclaw_ref="${TINYHAT_OPENCLAW_REF:-$(python3 - "${lock_file}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["dependencies"]["openclaw"]["resolved"])
PY
)}"
plugin_ref="${TINYHAT_PLUGIN_REF:-$(python3 - "${lock_file}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["dependencies"]["tinyhat_openclaw_plugin"]["ref"])
PY
)}"

rm -rf -- "${out_dir}"
mkdir -p "${out_dir}"
cp -a -- \
  "${runtime_root}/README.md" \
  "${runtime_root}/pyproject.toml" \
  "${runtime_root}/install.sh" \
  "${runtime_root}/bin" \
  "${runtime_root}/tinyhat_runtime" \
  "${runtime_root}/systemd" \
  "${runtime_root}/bake" \
  "${out_dir}/"

mkdir -p "${out_dir}/vendor/openclaw/bin"
if [[ -n "${TINYHAT_OPENCLAW_BIN:-}" ]]; then
  openclaw_bin="$(
    readlink -f "${TINYHAT_OPENCLAW_BIN}" 2>/dev/null \
      || realpath "${TINYHAT_OPENCLAW_BIN}" 2>/dev/null \
      || printf '%s' "${TINYHAT_OPENCLAW_BIN}"
  )"
  openclaw_package_dir=""
  if [[ -f "$(dirname "${openclaw_bin}")/../package.json" ]]; then
    openclaw_package_dir="$(cd -- "$(dirname "${openclaw_bin}")/.." && pwd)"
  elif [[ -f "$(dirname "${openclaw_bin}")/package.json" ]]; then
    openclaw_package_dir="$(cd -- "$(dirname "${openclaw_bin}")" && pwd)"
  fi
  if [[ -n "${openclaw_package_dir}" ]]; then
    rm -rf -- "${out_dir}/vendor/openclaw"
    mkdir -p "${out_dir}/vendor/openclaw"
    cp -a -- "${openclaw_package_dir}/." "${out_dir}/vendor/openclaw/"
  else
    cp -a -- "${openclaw_bin}" "${out_dir}/vendor/openclaw/bin/openclaw"
  fi
fi

chmod +x "${out_dir}/install.sh" "${out_dir}"/bin/tinyhat-* "${out_dir}"/bake/*.sh
if [[ -f "${out_dir}/vendor/openclaw/bin/openclaw" ]]; then
  chmod +x "${out_dir}/vendor/openclaw/bin/openclaw"
fi
PYTHONPATH="${out_dir}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m tinyhat_runtime.main bundle write \
    --bundle-dir "${out_dir}" \
    --runtime-ref "${runtime_ref}" \
    --openclaw-ref "${openclaw_ref}" \
    --plugin-ref "${plugin_ref}" >/dev/null
PYTHONPATH="${out_dir}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${out_dir}"
