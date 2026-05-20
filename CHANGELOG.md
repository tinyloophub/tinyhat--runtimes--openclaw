# Changelog

All notable changes to the Tinyhat Computer runtime are recorded
here. The runtime is consumed by the Tinyhat platform's Computer
provisioning step, which records the resolved commit SHA + the
runtime's published `VERSION` on each new Computer row.

## 0.2.0

### Added

- **Dev mode** for running the supervisor against a dev backend
  without GCE metadata, without `systemd`, and without root-owned
  `/etc` writes. Enabled with `TINYHAT_DEV_RUNTIME=1` (off by
  default; production behaviour is unchanged).
  - The supervisor reads `TINYHAT_PLATFORM_BASE_URL` /
    `TINYHAT_BACKEND_AUDIENCE` from the environment in dev mode
    and never contacts the GCE metadata server.
  - The supervisor sends a constant marker bearer in dev mode. The
    platform's `computer_identity_verifier` ignores the bearer
    body when its dev short-circuit is configured (only honoured
    under `ENV=development`).
  - The OpenClaw gateway runs as a subprocess of the supervisor
    instead of as a `systemd` unit; the health probe tails a flat
    log file under `$TINYHAT_RUNTIME_HOME` instead of querying
    `journalctl`.
  - `openclaw.json`, the workspace, and the gateway log file live
    under `$TINYHAT_RUNTIME_HOME` (default still
    `/var/lib/tinyhat-openclaw`); the dev Dockerfile points it at
    an unprivileged user's home dir.
- `dev/Dockerfile` — local-dev container image: `node:20-slim` +
  `openclaw@latest` + the supervisor, running as a non-root user.
- `dev/README.md` — what dev mode does, the trust boundary, and
  how to build/run the container.

### Unchanged

- The state-machine, OpenClaw config shape, rebind watchdog, and
  every production code path are identical to 0.1.0. Dev mode
  swaps four well-bounded subprocess / metadata calls; everything
  the platform sees on the wire is the same shape.

## 0.1.0

- Initial public release. Supervisor + bootstrap installer +
  framework config writer for OpenClaw, hosted standalone so each
  Tinyhat Computer boots from an explicit ref/tag/SHA.
