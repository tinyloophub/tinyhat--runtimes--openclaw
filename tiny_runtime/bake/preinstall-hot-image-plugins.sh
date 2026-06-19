#!/usr/bin/env bash
# Preinstall binding-independent OpenClaw plugins while baking a hot image.
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
import supervisor

supervisor.ensure_codex_subscription_plugin_installed()
supervisor.ensure_tinyhat_plugin_installed()
PY

if [[ "$(id -u)" -eq 0 ]] && id -u "${runtime_user}" >/dev/null 2>&1; then
  chown -R "${runtime_user}:${runtime_group}" \
    "$(dirname -- "${config_path}")" \
    "${state_dir}"
fi

runuser_cmd=()
if [[ "$(id -u)" -eq 0 ]] && id -u "${runtime_user}" >/dev/null 2>&1; then
  runuser_cmd=(runuser -u "${runtime_user}" --)
fi

"${runuser_cmd[@]}" env \
  HOME="${state_dir}" \
  OPENCLAW_CONFIG_PATH="${config_path}" \
  OPENCLAW_STATE_DIR="${state_dir}" \
  openclaw plugins inspect codex --json \
  | python3 -c 'import json, sys; p=(json.load(sys.stdin).get("plugin") or {}); ids=p.get("providerIds") or p.get("providers") or []; sys.exit(0 if p.get("id") == "codex" and p.get("enabled") is not False and p.get("status") == "loaded" and "codex" in ids else 1)'
test -f "${state_dir}/tinyhat-plugin.version"

echo "[tinyhat-runtime] hot image plugin preinstall verified"
