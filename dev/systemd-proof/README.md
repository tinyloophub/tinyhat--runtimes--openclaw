# systemd watchdog / reattach / no-early-healthy proof (#685)

Live proof harness for the v0.11.0 supervisor systemd-behaviour gates
that unit tests cannot cover (they don't run real systemd):

- `watchdog-wedge-gce` — a wedged supervisor stops feeding the systemd
  watchdog and gets killed + respawned.
- `supervisor-reattach-gce` — a crashed/watchdog-restarted supervisor
  reattaches to the still-running gateway without bouncing it.
- `gateway-lifecycle-no-race-gce` — an active-but-not-ready gateway
  never produces runtime `healthy`.

It installs the real-shaped supervisor + gateway systemd units (matching
`bootstrap.sh`, `WatchdogSec` reduced for proof speed) running the real
supervisor watchdog/notify/reattach/health code via `steady_supervisor.py`
over a `stub_gateway.py`, then drives the three demos with assertions.

The platform binding loop is substituted by the steady driver (it is
environment-independent Python already covered by `tests/test_supervisor.py`);
everything systemd-specific — the `Type=notify`/`WatchdogSec` mechanism,
`Restart=` respawn, gateway PID continuity, and the readiness-gated health
computation — runs the real code under real systemd.

## Run (disposable GCE VM only — installs systemd units, needs root)

```bash
# on a throwaway Ubuntu 22.04 VM with the runtime at /opt/tinyhat-runtime
sudo bash dev/systemd-proof/run_proof.sh all
```

Subcommands: `install`, `show-units`, `up`, `reattach`, `watchdog`,
`no-early-healthy`, `all`. `WATCHDOG_SEC` / `PERIOD_SECONDS` env override
the proof-speed timings (production `WatchdogSec` is 180s).

## Global command lock proof (`command-lock-concurrency`)

`lock_proof.sh` drives the seven live lock cases against the REAL
`tinyhat_cli.units.command_lock` (+ the real locked gateway-restart
transaction) — daemon-vs-human deferral, watchdog-restart convergence,
deadline pgid kill, idempotency replay, hard-crash stale recovery,
contender-blocked-while-child-survives, and runner-lost readiness
reconcile-before-second-restart:

```bash
sudo bash dev/systemd-proof/lock_proof.sh all     # or case1 .. case7
```

Case 7 reuses this harness's stub gateway unit (`run_proof.sh install`)
with the ready-file driving readiness. `lock_proof_helper.py` is the
per-action driver; its output lines (`ACQUIRED`, `BUSY`, `STALE-TAKEOVER`,
`RESULT`, `REPLAYED`) are what the shell asserts on.

This harness is a dev asset; it is excluded from the packaged runtime.
