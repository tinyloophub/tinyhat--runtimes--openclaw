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

This harness is a dev asset; it is excluded from the packaged runtime.
