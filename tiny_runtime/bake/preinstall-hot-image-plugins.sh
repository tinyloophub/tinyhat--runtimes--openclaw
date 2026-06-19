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
export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"

install -d -m 0750 "$(dirname -- "${config_path}")"
install -d -m 0700 "${state_dir}" "${state_dir}/workspace"
if [[ "$(id -u)" -eq 0 ]] && id -u "${runtime_user}" >/dev/null 2>&1; then
  chown -R "${runtime_user}:${runtime_group}" \
    "$(dirname -- "${config_path}")" \
    "${state_dir}"
fi

python3 - <<'PY'
import sys
import supervisor

supervisor.ensure_codex_subscription_plugin_installed()
supervisor.ensure_tinyhat_plugin_installed()
if not supervisor._is_codex_subscription_plugin_available():
    sys.exit("codex subscription plugin not available after preinstall")
PY

if [[ "$(id -u)" -eq 0 ]] && id -u "${runtime_user}" >/dev/null 2>&1; then
  chown -R "${runtime_user}:${runtime_group}" \
    "$(dirname -- "${config_path}")" \
    "${state_dir}"
fi

test -f "${state_dir}/tinyhat-plugin.version"

echo "[tinyhat-runtime] hot image plugin preinstall verified"
