# Changelog

All notable changes to the Tinyhat Computer runtime are recorded
here. The runtime is consumed by the Tinyhat platform's Computer
provisioning step, which records the resolved commit SHA + the
runtime's published `VERSION` on each new Computer row.

## 0.9.1

### Fixed

- Mirror user-managed runtime secrets into `openclaw.json`'s `env`
  block so OpenClaw's `applyConfigEnvVars` populates the gateway
  `process.env` (and therefore the bash tool's child shells) at boot.
  Previously the apply path only wired `OPENAI_API_KEY` into a
  `models.providers.openai.apiKey` SecretRef and registered the file
  provider, leaving non-OpenAI keys like `EXA_API_KEY` reachable to
  OpenClaw's SecretRef snapshot but absent from the agent shell's
  environment. `OPENAI_API_KEY` keeps the SecretRef path and
  `OPENROUTER_API_KEY` is preserved as binding-managed; everything
  else lands in `config["env"]` as plaintext, matching the existing
  OpenRouter pattern. `apply_runtime_secret_map` now signals a gateway
  rebind only when the env block actually changed; OpenAI-only edits
  still resolve through `openclaw secrets reload` without a restart.

## 0.9.0

### Changed

- Install the Tinyhat OpenClaw plugin from the public repo/ref passed
  by the platform provisioning manifest instead of vendoring the plugin
  implementation inside the runtime repo. The runtime now owns only
  plugin pinning/install and keeps tool/default-skill implementation in
  `tinyhat-ai/tinyhat`.

## 0.8.0

### Added

- Consume Tinyhat's OpenRouter model package when writing OpenClaw
  config. Paid bindings now expose the enabled model catalog, use the
  package default as the primary model, and include the cheap model as
  fallback for the default paid role.

### Fixed

- Treat `openrouter_model_package` changes as binding-signature changes
  so the supervisor rewrites `openclaw.json` after a platform-side
  package update or rebind.
- Keep no-credit OpenRouter bindings isolated on the free-demo model
  instead of inheriting the paid catalog.

## 0.7.2

### Added

- Let the local dev container optionally join Tailscale in userspace
  networking mode with Tailscale SSH enabled, so a dev Computer can be
  reached by SSH / the managed terminal like a managed cloud Computer.
  The entrypoint still starts the supervisor as the unprivileged
  `tinyhat` user when private access is disabled.

### Fixed

- Mirror production private-access bootstrap status reporting in the
  dev entrypoint. It now writes `ready`, `error`, or `config_missing`
  status JSON before the supervisor starts, allowing heartbeats to
  report Tailscale readiness or diagnostics back to Tinyhat.

## 0.7.1

### Fixed

- Treat OpenClaw's first-save "secrets runtime snapshot is not active"
  reload response as a fast synced runtime-secret apply after the
  supervisor has written the Tinyhat file provider and OpenClaw config.
  This lets a newly saved Computer runtime secret become available
  without forcing the admin to replace the value or blocking heartbeat
  processing through the gateway-settle retry window.
- Keep retrying `openclaw secrets reload` through the slow initial
  gateway settle window so a first save immediately after activation
  does not fail before OpenClaw finishes provider prewarm.

## 0.7.0

### Changed

- `bootstrap.sh` now owns generic Computer provisioning after the
  platform startup script clones this repo. It installs base OS
  packages, Node.js, the requested OpenClaw package, and optional
  Tailscale private access before starting the supervisor. Tinyloop's
  GCE startup script can stay focused on cloning the runtime repo and
  passing per-Computer config/auth material.
- The runtime bootstrap now fails loudly if OpenClaw cannot be
  installed or found, avoids an `openclaw@latest` fallback when the
  platform does not pass a framework spec, and keeps one-time
  Tailscale auth keys out of the process list by using an auth-key
  file.

## 0.6.0

### Added

- Native `/tinyhat_computer` Telegram command and matching
  `tinyhat_open_manage_computer_link` tool. Both return a Manage
  Computer Telegram Mini App button for the assigned Computer and
  never expose a management or terminal URL in message text.

## 0.5.0

### Added

- MIT license so operators can use, copy, modify, and redistribute the
  public runtime payload with clear permission.
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
