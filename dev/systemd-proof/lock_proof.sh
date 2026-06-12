#!/usr/bin/env bash
# Global command lock — the seven live proof cases (`command-lock-concurrency`).
#
# Exercises the REAL tinyhat_cli.units.command_lock (+ the real locked
# gateway-restart transaction for case 7) against the real control-plane
# tree and real systemd, via lock_proof_helper.py. Root required;
# disposable VM only. Case 7 uses the stub gateway unit from
# run_proof.sh (install + a present/absent ready-file drive readiness).
#
#   sudo bash dev/systemd-proof/lock_proof.sh all
#
# Subcommands: case1 … case7, all, clean.
set -euo pipefail

ROOT="${TINYHAT_RUNTIME_DIR:-/opt/tinyhat-runtime}"
PROOF_DIR="${ROOT}/dev/systemd-proof"
HELPER=(/usr/bin/python3 "${PROOF_DIR}/lock_proof_helper.py")
STATE_DIR="/var/lib/tinyhat-control"
LOCK_DIR="${STATE_DIR}/command-lock"
SPOOL_DIR="${STATE_DIR}/command-results/spool"
READY_FILE="/run/tinyhat-proof-gateway-ready"
GW_UNIT="tinyhat-openclaw-gateway.service"
HOLD_UNIT="tinyhat-proof-lockholder.service"
export TINYHAT_RUNTIME_DIR="${ROOT}"
export TINYHAT_PLATFORM_BASE_URL=""

say() { echo "[lock-proof] $*"; }
fail() { say "$1"; exit 1; }

helper() { "${HELPER[@]}" "$@"; }

lock_json() { cat "${LOCK_DIR}/lock.json" 2>/dev/null || echo "{}"; }
lock_field() { python3 -c "import json,sys;print(json.load(open('${LOCK_DIR}/lock.json')).get('$1'))" 2>/dev/null || echo ""; }

clean_slate() {
  rm -rf "${LOCK_DIR}" "${STATE_DIR}/command-results"
  rm -f /tmp/lock-proof-marker
}

# C1 — daemon-vs-human deferral: while a human (cli) holds the lock, a
# daemon contender must answer typed-busy on probe and DEFER (blocking
# acquire) until release; no interleaving.
cmd_case1() {
  clean_slate
  say "C1 daemon-vs-human deferral"
  "${HELPER[@]}" hold --holder cli --seconds 6 --command "gateway restart" >/tmp/c1-holder.log 2>&1 &
  local holder_pid=$!
  sleep 1
  local probe
  probe=$(helper probe || true)
  echo "${probe}" | grep -q "BUSY busy: cli pid" || fail "C1 FAIL: expected typed busy, got: ${probe}"
  say "C1: typed busy while human holds: ${probe}"
  local out
  out=$(helper acquire-blocking --holder daemon --wait 30)
  wait "${holder_pid}"
  echo "${out}" | grep -q "RESULT outcome=succeeded" || fail "C1 FAIL: daemon never acquired"
  local waited
  waited=$(echo "${out}" | grep -o "WAITED seconds=[0-9.]*" | grep -o "[0-9.]*")
  python3 -c "import sys; sys.exit(0 if float('${waited}') >= 3 else 1)" || \
    fail "C1 FAIL: daemon did not actually defer (waited ${waited}s)"
  say "C1 PASS: daemon deferred ${waited}s, acquired only after release"
}

# C2 — watchdog/systemd restart mid-command: a daemon-shaped holder
# (transient unit, Restart=on-failure) is SIGKILLed mid-hold; systemd
# respawns it; the respawn stale-takes-over its predecessor and
# converges to a clean terminal state.
cmd_case2() {
  clean_slate
  say "C2 watchdog restart mid-command"
  systemctl reset-failed "${HOLD_UNIT}" 2>/dev/null || true
  systemd-run --unit "${HOLD_UNIT%.service}" --property Restart=on-failure \
    --property RestartSec=1 --setenv TINYHAT_RUNTIME_DIR="${ROOT}" \
    --setenv TINYHAT_PLATFORM_BASE_URL= \
    /usr/bin/python3 "${PROOF_DIR}/lock_proof_helper.py" hold --holder daemon \
    --seconds 4 --command "gateway restart" >/dev/null
  sleep 1.5
  local pid1
  pid1=$(systemctl show -p MainPID --value "${HOLD_UNIT}")
  [ -n "${pid1}" ] && [ "${pid1}" != "0" ] || fail "C2 FAIL: holder unit not running"
  kill -9 "${pid1}"
  say "C2: SIGKILLed holder pid ${pid1}; awaiting Restart= respawn + takeover"
  local converged=""
  for _ in $(seq 1 30); do
    if journalctl -u "${HOLD_UNIT}" --no-pager 2>/dev/null | grep -q "STALE-TAKEOVER"; then
      converged=yes; break
    fi
    sleep 1
  done
  [ "${converged}" = "yes" ] || fail "C2 FAIL: respawned holder never reported stale takeover"
  for _ in $(seq 1 20); do
    [ "$(lock_field operation_phase)" = "terminal" ] && break
    sleep 1
  done
  [ "$(lock_field operation_phase)" = "terminal" ] || fail "C2 FAIL: did not converge to terminal"
  [ "$(lock_field generation)" = "2" ] || fail "C2 FAIL: expected generation 2, got $(lock_field generation)"
  systemctl stop "${HOLD_UNIT}" 2>/dev/null || true
  say "C2 PASS: respawned holder took over (generation 2) and converged to terminal"
}

# C3 — deadline kills the mutation child's process group, marks
# timed_out, releases; a contender never took over concurrently.
cmd_case3() {
  clean_slate
  say "C3 deadline kill + timed_out + clean release"
  "${HELPER[@]}" run-op --child-sleep 300 --timeout 3 --command "gateway restart" >/tmp/c3-op.log 2>&1 &
  local op_pid=$!
  sleep 1
  local probe child_pgid
  probe=$(helper probe || true)
  echo "${probe}" | grep -q "BUSY" || fail "C3 FAIL: lock not held during op"
  child_pgid=$(lock_field child_pgid)
  [ -n "${child_pgid}" ] && [ "${child_pgid}" != "None" ] || fail "C3 FAIL: child_pgid not recorded"
  wait "${op_pid}" && fail "C3 FAIL: op exited 0 despite timeout" || true
  grep -q "RESULT outcome=timed_out" /tmp/c3-op.log || fail "C3 FAIL: no timed_out verdict"
  if pgrep -g "${child_pgid}" >/dev/null 2>&1; then
    fail "C3 FAIL: child process group ${child_pgid} survived the deadline"
  fi
  [ "$(lock_field operation_phase)" = "terminal" ] || fail "C3 FAIL: lock.json not terminal"
  helper probe | grep -q "FREE" || fail "C3 FAIL: lock not released after timeout"
  say "C3 PASS: child pgid ${child_pgid} killed, timed_out recorded, lock released"
}

# C4 — idempotency replay returns the stored result without re-execution.
cmd_case4() {
  clean_slate
  say "C4 idempotency replay"
  local marker=/tmp/lock-proof-marker
  helper run-op --key proof-replay-key --exec-marker "${marker}" \
    --command "gateway restart" >/dev/null
  local out
  out=$(helper run-op --key proof-replay-key --exec-marker "${marker}" \
    --command "gateway restart")
  echo "${out}" | grep -q "REPLAYED key=proof-replay-key outcome=succeeded" || \
    fail "C4 FAIL: replay did not return the stored result: ${out}"
  [ "$(wc -l < "${marker}")" -eq 1 ] || \
    fail "C4 FAIL: operation executed $(wc -l < "${marker}") times (expected 1)"
  say "C4 PASS: replayed key returned stored result; exactly one execution"
}

# C5 — hard-crash stale recovery: holder dies with no children → fd
# free → next acquire reconciles the non-terminal record.
cmd_case5() {
  clean_slate
  say "C5 hard-crash stale recovery"
  "${HELPER[@]}" hold --holder cli --seconds 300 --command "gateway restart" >/tmp/c5-holder.log 2>&1 &
  local holder_pid=$!
  sleep 1.5
  kill -9 "${holder_pid}"; wait "${holder_pid}" 2>/dev/null || true
  sleep 0.5
  local out
  out=$(helper acquire-blocking --holder daemon --wait 5)
  echo "${out}" | grep -q "STALE-TAKEOVER" || fail "C5 FAIL: no stale takeover reported"
  echo "${out}" | grep -q "RESULT outcome=succeeded" || fail "C5 FAIL: takeover did not converge"
  say "C5 PASS: crash left non-terminal record; next holder took over with stale event"
}

# C6 — runner SIGKILLed while its mutation child sleeps: the contender
# must stay BLOCKED (flock held by the inherited fd) until the child
# dies, then reconcile with the stale event.
cmd_case6() {
  clean_slate
  say "C6 contender blocked while the mutation child outlives its runner"
  "${HELPER[@]}" hold --holder cli --seconds 300 --child-sleep 8 \
    --command "gateway restart" >/tmp/c6-holder.log 2>&1 &
  local holder_pid=$!
  sleep 1.5
  local child_pgid
  child_pgid=$(lock_field child_pgid)
  [ -n "${child_pgid}" ] && [ "${child_pgid}" != "None" ] || fail "C6 FAIL: no child recorded"
  kill -9 "${holder_pid}"; wait "${holder_pid}" 2>/dev/null || true
  sleep 0.5
  pgrep -g "${child_pgid}" >/dev/null || fail "C6 FAIL: child died with the runner"
  local probe
  probe=$(helper probe || true)
  echo "${probe}" | grep -q "mutation child still completing (pgid ${child_pgid})" || \
    fail "C6 FAIL: expected child-completing busy answer, got: ${probe}"
  say "C6: runner dead, child alive — typed answer: ${probe}"
  local t0 out waited
  t0=$(date +%s)
  out=$(helper acquire-blocking --holder daemon --wait 30)
  waited=$(( $(date +%s) - t0 ))
  echo "${out}" | grep -q "STALE-TAKEOVER" || fail "C6 FAIL: no stale takeover after child death"
  [ "${waited}" -ge 4 ] || fail "C6 FAIL: contender acquired in ${waited}s while the child slept 8s"
  say "C6 PASS: contender blocked ~${waited}s on the inherited fd, then reconciled"
}

# C7 — runner SIGKILLed after `systemctl restart` returned but before
# readiness is terminal: the next holder must reconcile the prior
# restart to terminal BEFORE starting its own restart.
cmd_case7() {
  clean_slate
  say "C7 runner-lost readiness transaction reconciles before a second restart"
  [ -f "/etc/systemd/system/${GW_UNIT}" ] || bash "${PROOF_DIR}/run_proof.sh" install
  rm -f "${READY_FILE}"
  systemctl reset-failed "${GW_UNIT}" 2>/dev/null || true
  systemctl restart "${GW_UNIT}"
  "${HELPER[@]}" gateway-restart --holder cli >/tmp/c7-victim.log 2>&1 &
  local victim_pid=$!
  local phase=""
  for _ in $(seq 1 30); do
    phase=$(lock_field operation_phase)
    [ "${phase}" = "readiness_wait" ] && break
    sleep 0.5
  done
  [ "${phase}" = "readiness_wait" ] || fail "C7 FAIL: victim never reached readiness_wait (${phase})"
  kill -9 "${victim_pid}"; wait "${victim_pid}" 2>/dev/null || true
  say "C7: victim SIGKILLed in readiness_wait; ready-file appears in 3s"
  ( sleep 3; touch "${READY_FILE}" ) &
  local out
  out=$(helper gateway-restart --holder cli --wait 10)
  echo "${out}" | grep -q "RESULT outcome=succeeded" || fail "C7 FAIL: second restart did not succeed: ${out}"
  local dump reconciled_finished second_started
  dump=$(helper dump)
  echo "${dump}" | grep -q '"runner_lost": *true\|"runner_lost":true' || \
    fail "C7 FAIL: no runner_lost terminal record for the lost transaction"
  reconciled_finished=$(echo "${dump}" | grep -o '"runner_lost":true[^}]*"started_at_unix":[0-9]*\|"started_at_unix":[0-9]*[^}]*"runner_lost":true' | grep -o '"started_at_unix":[0-9]*' | head -1 | grep -o '[0-9]*$' || true)
  second_started=$(echo "${out}" | grep -o "marker=[0-9]*" | grep -o "[0-9]*" || true)
  if [ -n "${reconciled_finished}" ] && [ -n "${second_started}" ]; then
    [ "${second_started}" -ge "${reconciled_finished}" ] || \
      fail "C7 FAIL: second restart started before the lost one was terminal"
  fi
  say "C7 PASS: lost transaction normalized (runner_lost) before the second restart ran"
}

cmd_all() {
  cmd_case1; cmd_case2; cmd_case3; cmd_case4; cmd_case5; cmd_case6; cmd_case7
  say "ALL SEVEN LOCK CASES PASSED"
}

cmd_clean() {
  systemctl stop "${HOLD_UNIT}" 2>/dev/null || true
  clean_slate
  say "cleaned lock + spool state"
}

case "${1:-all}" in
  case1) cmd_case1 ;;
  case2) cmd_case2 ;;
  case3) cmd_case3 ;;
  case4) cmd_case4 ;;
  case5) cmd_case5 ;;
  case6) cmd_case6 ;;
  case7) cmd_case7 ;;
  clean) cmd_clean ;;
  all) cmd_all ;;
  *) echo "usage: $0 {case1..case7|all|clean}"; exit 2 ;;
esac
