# Changelog

All notable changes to the Tinyhat Computer runtime are recorded
here. The runtime is consumed by the Tinyhat platform's Computer
provisioning step, which records the resolved commit SHA + the
runtime's published `VERSION` on each new Computer row.

## 0.5.0

### Added

- Heartbeats now include non-secret private-access diagnostics when
  the platform bootstrap enrolled Tailscale on the Computer. The
  report contains provider/state/node/IP diagnostics only, never auth
  keys or terminal data.

## 0.4.0

### Added

- Bundled `tinyhat` OpenClaw tool plugin. The supervisor
  installs it before gateway startup and enables it in
  `openclaw.json`.
- Agent-callable credential helpers:
  `tinyhat_list_runtime_secrets`,
  `tinyhat_request_runtime_secret`, and the `/tinyhat_secrets`
  skill-command dispatcher. These tools return secret metadata and
  Mini App add-secret links only; they never return secret values.

## 0.3.0

### Added

- Heartbeat-delivered `apply_config` command handling. The
  supervisor now pulls the latest Computer-scoped runtime secret
  map, writes `/etc/openclaw/tinyhat-secrets.json` with mode
  `0600`, syncs OpenClaw file SecretRefs, runs
  `openclaw secrets reload --json`, and posts the apply result back
  to Tinyhat.
- Rapid saves coalesce because the supervisor pulls the latest
  revision at apply time, not the revision that happened to be in
  the heartbeat payload.
- Failed applies record diagnostics and are not retried locally on
  every heartbeat until Tinyhat issues a newer desired revision.

### Changed

- OpenClaw config now includes a Tinyhat file SecretRef provider.
  When `OPENAI_API_KEY` exists in the runtime secret map, the
  supervisor wires `models.providers.openai.apiKey` to the file
  pointer `/OPENAI_API_KEY`.
- The dev Docker image keeps running as the non-root `tinyhat` user
  while chowning `/etc/openclaw` so the local harness exercises the
  same Tinyhat SecretRef file path as production.

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
- `dev/Dockerfile` — local-dev container image based on
  `node:22-slim` (OpenClaw `>=22.19` engine floor) + `openclaw@latest`
  + the supervisor, running as a non-root user. Runs
  `openclaw --version` immediately after install so a future Node /
  OpenClaw engine mismatch fails the build instead of the first
  `docker run`.
- `dev/README.md` — what dev mode does, the trust boundary, and
  how to build/run the container.

### Notes on the gateway subprocess argv

The dev gateway subprocess invokes `openclaw gateway run` with the
same loopback / no-auth / no-tailscale flags the prod systemd unit
uses. It does **not** pass `--config <path>` because the OpenClaw
CLI does not accept that flag on `gateway run`; the config path is
read from the `OPENCLAW_CONFIG_PATH` environment variable (set on
the subprocess and on the prod systemd unit alike).

### Unchanged

- The state-machine, OpenClaw config shape, rebind watchdog, and
  every production code path are identical to 0.1.0. Dev mode
  swaps four well-bounded subprocess / metadata calls; everything
  the platform sees on the wire is the same shape.

## 0.1.0

- Initial public release. Supervisor + bootstrap installer +
  framework config writer for OpenClaw, hosted standalone so each
  Tinyhat Computer boots from an explicit ref/tag/SHA.
