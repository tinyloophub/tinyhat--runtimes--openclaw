#!/usr/bin/env bash
# #685 systemd watchdog / reattach / no-early-healthy proof runner.
#
# Installs the real-shaped supervisor + gateway systemd units (matching
# bootstrap.sh, WatchdogSec reduced for proof speed) running the real
# supervisor watchdog/reattach/health code via steady_supervisor.py over
# a stub gateway, then drives the three #685 demonstrations with
# assertions. Root required (installs systemd units). Disposable VM only.
set -euo pipefail

ROOT="/opt/tinyhat-runtime"
PROOF_DIR="${ROOT}/dev/systemd-proof"
STATE_DIR="/var/lib/tinyhat-control"
RUNTIME_STATE="${STATE_DIR}/runtime-state.json"
READY_FILE="/run/tinyhat-proof-gateway-ready"
GATEWAY_PORT=18789
WATCHDOG_SEC="${WATCHDOG_SEC:-20}"        # production is 180s
PERIOD_SECONDS="${PERIOD_SECONDS:-5}"
SUP_UNIT="tinyhat-openclaw.service"
GW_UNIT="tinyhat-openclaw-gateway.service"

say() { echo "[proof] $*"; }
mainpid() { systemctl show -p MainPID --value "$1"; }
health() { python3 -c "import json;print(json.load(open('${RUNTIME_STATE}')).get('runtime_health'))" 2>/dev/null || echo "NONE"; }

render_units() {
  # Gateway: independent unit the supervisor manages. Deliberately NOT
  # PartOf the supervisor (#685): a PartOf child is bounced on the
  # parent's Restart=/watchdog respawn too, which breaks reattach
  # continuity. Teardown is owned by the supervisor's explicit
  # stop_openclaw_gateway() on clean exit; a crash/watchdog respawn
  # leaves the gateway running for the new supervisor to reattach.
  cat > "/etc/systemd/system/${GW_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw gateway (proof stub)
After=${SUP_UNIT}

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${PROOF_DIR}/stub_gateway.py --port ${GATEWAY_PORT} --ready-file ${READY_FILE}
Restart=on-failure
RestartSec=2
StandardOutput=journal
StandardError=journal
UNIT

  # Supervisor: the real notify/watchdog model from bootstrap.sh.
  cat > "/etc/systemd/system/${SUP_UNIT}" <<UNIT
[Unit]
Description=Tinyhat OpenClaw Computer supervisor (proof steady driver)
After=network-online.target

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=${WATCHDOG_SEC}s
Environment=PROOF_SUPERVISOR_PATH=${ROOT}/supervisor.py
Environment=PROOF_PERIOD_SECONDS=${PERIOD_SECONDS}
Environment=TINYHAT_RUNTIME_STATE_PATH=${RUNTIME_STATE}
Environment=TINYHAT_PLATFORM_BASE_URL=
ExecStartPre=/usr/bin/install -d -m 0700 ${STATE_DIR}
ExecStart=/usr/bin/python3 ${PROOF_DIR}/steady_supervisor.py
Restart=on-failure
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
}

cmd_install() {
  render_units
  rm -f "${READY_FILE}"
  say "units installed (WatchdogSec=${WATCHDOG_SEC}s, period=${PERIOD_SECONDS}s)"
}

cmd_show_units() {
  say "supervisor unit properties (the M1-deferred systemctl-show proof):"
  systemctl show "${SUP_UNIT}" -p Type -p NotifyAccess -p WatchdogUSec -p Restart
  say "gateway unit properties:"
  systemctl show "${GW_UNIT}" -p Type -p PartOf -p Restart
}

# Bring the stack to steady healthy: gateway ready, supervisor active,
# runtime-state healthy.
cmd_up() {
  systemctl start "${SUP_UNIT}" "${GW_UNIT}"
  touch "${READY_FILE}"
  for _ in $(seq 1 30); do [ "$(health)" = "healthy" ] && break; sleep 2; done
  say "steady: supervisor=$(systemctl is-active ${SUP_UNIT}) gateway=$(systemctl is-active ${GW_UNIT}) runtime_health=$(health)"
  say "supervisor MainPID=$(mainpid ${SUP_UNIT}) gateway MainPID=$(mainpid ${GW_UNIT})"
}

# D1 reattach: crash the supervisor PROCESS; Restart= respawns it;
# the gateway (independent unit) must keep its MainPID and the new
# supervisor must report healthy again without restarting the gateway.
cmd_demo_reattach() {
  local gw_before sup_before sup_after
  gw_before="$(mainpid ${GW_UNIT})"; sup_before="$(mainpid ${SUP_UNIT})"
  say "D1 reattach: before — supervisor PID=${sup_before} gateway PID=${gw_before}"
  kill -9 "${sup_before}"
  say "D1: sent SIGKILL to supervisor ${sup_before}; awaiting Restart= respawn"
  for _ in $(seq 1 20); do
    sup_after="$(mainpid ${SUP_UNIT})"
    [ -n "${sup_after}" ] && [ "${sup_after}" != "${sup_before}" ] && [ "${sup_after}" != "0" ] && break
    sleep 2
  done
  for _ in $(seq 1 20); do [ "$(health)" = "healthy" ] && break; sleep 2; done
  local gw_after; gw_after="$(mainpid ${GW_UNIT})"
  say "D1: after — supervisor PID=${sup_after} (respawned) gateway PID=${gw_after} runtime_health=$(health)"
  [ "${gw_before}" = "${gw_after}" ] || { say "D1 FAIL: gateway PID changed"; return 1; }
  [ "$(health)" = "healthy" ] || { say "D1 FAIL: not healthy after reattach"; return 1; }
  say "D1 PASS: supervisor respawned and reattached; gateway never restarted"
}

# D2 watchdog-wedge: SIGSTOP the supervisor so WATCHDOG=1 stops; systemd
# WatchdogSec must kill + Restart= respawn it; gateway must keep its PID.
cmd_demo_watchdog() {
  local gw_before sup_before sup_after
  gw_before="$(mainpid ${GW_UNIT})"; sup_before="$(mainpid ${SUP_UNIT})"
  say "D2 watchdog-wedge: before — supervisor PID=${sup_before} gateway PID=${gw_before}"
  kill -STOP "${sup_before}"
  say "D2: SIGSTOP supervisor ${sup_before}; forward progress frozen, awaiting WatchdogSec=${WATCHDOG_SEC}s"
  for _ in $(seq 1 40); do
    sup_after="$(mainpid ${SUP_UNIT})"
    [ -n "${sup_after}" ] && [ "${sup_after}" != "${sup_before}" ] && [ "${sup_after}" != "0" ] && break
    sleep 2
  done
  for _ in $(seq 1 20); do [ "$(health)" = "healthy" ] && break; sleep 2; done
  local gw_after; gw_after="$(mainpid ${GW_UNIT})"
  say "D2: watchdog journal:"
  journalctl -u "${SUP_UNIT}" --no-pager | grep -iE "watchdog|timeout" | tail -4 || true
  say "D2: after — supervisor PID=${sup_after} (respawned) gateway PID=${gw_after} runtime_health=$(health)"
  [ "${gw_before}" = "${gw_after}" ] || { say "D2 FAIL: gateway PID changed"; return 1; }
  [ "$(health)" = "healthy" ] || { say "D2 FAIL: not healthy after watchdog restart"; return 1; }
  say "D2 PASS: systemd watchdog killed+respawned the wedged supervisor; gateway untouched"
}

# D3 no-early-healthy: gateway active but not ready -> runtime never
# reports healthy; once ready, it flips to healthy.
cmd_demo_no_early_healthy() {
  systemctl stop "${SUP_UNIT}" "${GW_UNIT}" || true
  rm -f "${READY_FILE}" "${RUNTIME_STATE}"
  systemctl start "${GW_UNIT}" "${SUP_UNIT}"
  say "D3: gateway started active-but-not-ready (no ready marker)"
  sleep $(( PERIOD_SECONDS * 3 ))
  local during; during="$(health)"
  say "D3: while not-ready — gateway active=$(systemctl is-active ${GW_UNIT}) runtime_health=${during}"
  [ "${during}" != "healthy" ] || { say "D3 FAIL: reported healthy before gateway ready"; return 1; }
  touch "${READY_FILE}"
  for _ in $(seq 1 20); do [ "$(health)" = "healthy" ] && break; sleep 2; done
  say "D3: after ready marker — runtime_health=$(health)"
  [ "$(health)" = "healthy" ] || { say "D3 FAIL: did not reach healthy after ready"; return 1; }
  say "D3 PASS: active-but-not-ready never healthy; healthy only after readiness"
}

# D4 clean-shutdown teardown: with PartOf gone, an explicit
# `systemctl stop` of the supervisor must still stop the gateway, via the
# supervisor's own clean-shutdown guard (NOT systemd auto-propagation).
cmd_demo_clean_stop() {
  systemctl start "${SUP_UNIT}" "${GW_UNIT}" >/dev/null 2>&1 || true
  touch "${READY_FILE}"
  for _ in $(seq 1 20); do [ "$(health)" = "healthy" ] && break; sleep 2; done
  say "D4 clean-stop: before — gateway=$(systemctl is-active ${GW_UNIT}) supervisor=$(systemctl is-active ${SUP_UNIT})"
  systemctl stop "${SUP_UNIT}"
  sleep 3
  local gw_state; gw_state="$(systemctl is-active ${GW_UNIT} || true)"
  say "D4: after 'systemctl stop ${SUP_UNIT}' — gateway=${gw_state}"
  [ "${gw_state}" != "active" ] || { say "D4 FAIL: gateway still active after clean supervisor stop (orphaned)"; return 1; }
  say "D4 PASS: clean supervisor stop tore down the gateway (supervisor-owned, no PartOf)"
}

cmd_all() {
  cmd_install; cmd_show_units; cmd_up
  cmd_demo_reattach; cmd_demo_watchdog; cmd_demo_no_early_healthy; cmd_demo_clean_stop
  say "ALL DEMOS PASSED"
}

case "${1:-all}" in
  install) cmd_install ;;
  show-units) cmd_show_units ;;
  up) cmd_up ;;
  reattach) cmd_demo_reattach ;;
  watchdog) cmd_demo_watchdog ;;
  no-early-healthy) cmd_demo_no_early_healthy ;;
  clean-stop) cmd_demo_clean_stop ;;
  all) cmd_all ;;
  *) echo "usage: $0 {install|show-units|up|reattach|watchdog|no-early-healthy|clean-stop|all}"; exit 2 ;;
esac
