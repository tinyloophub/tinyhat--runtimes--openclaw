#!/usr/bin/env bash
# Preinstall binding-independent OpenClaw plugins while baking a hot image.
# Invoked from the runtime source checkout by the monorepo image bake script.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
state_dir="${OPENCLAW_STATE_DIR:-/var/lib/tinyhat-openclaw}"
config_path="${OPENCLAW_CONFIG_PATH:-/etc/openclaw/openclaw.json}"
runtime_user="${TINYHAT_OPENCLAW_RUNTIME_USER:-tinyhat}"
runtime_group="${TINYHAT_OPENCLAW_RUNTIME_GROUP:-tinyhat}"

export HOME="${HOME:-${state_dir}}"
export OPENCLAW_CONFIG_PATH="${config_path}"
export OPENCLAW_STATE_DIR="${state_dir}"
export TINYHAT_RUNTIME_HOME="${TINYHAT_RUNTIME_HOME:-${state_dir}}"
export TINYHAT_OPENCLAW_RUNTIME_USER="${runtime_user}"
export TINYHAT_OPENCLAW_RUNTIME_GROUP="${runtime_group}"
export PYTHONPATH="${repo_root}/tiny_runtime${PYTHONPATH:+:${PYTHONPATH}}"

install -d -m 0750 "$(dirname -- "${config_path}")"
install -d -m 0700 "${state_dir}" "${state_dir}/workspace"
if [[ "$(id -u)" -eq 0 ]]; then
  # OpenClaw's plugin loader rejects non-root-owned plugin candidates when the
  # gateway runs as root. The baked image runs the gateway through systemd
  # without a User= override, so initialize plugin/config/state candidates as
  # root-owned rather than preserving temporary bake user ownership.
  chown -R 0:0 \
    "$(dirname -- "${config_path}")" \
    "${state_dir}"
fi

python3 -m tinyhat_runtime.main bake preinstall-plugins >/tmp/tinyhat-hot-image-plugins.json

if [[ "$(id -u)" -eq 0 ]]; then
  chown -R 0:0 \
    "$(dirname -- "${config_path}")" \
    "${state_dir}"
fi

test -f "${state_dir}/tinyhat-plugin.version"

echo "[tinyhat-runtime] hot image plugin preinstall verified"
