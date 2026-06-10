# Local development for the Tinyhat Computer runtime

This directory holds the local development surface for the
Tinyhat Computer runtime: a Docker image that runs the same
`supervisor.py` and the real OpenClaw npm package, pointed at a
dev backend instead of a production GCE VM.

The goal is to let runtime contributors (and the agents who help
them) iterate on `supervisor.py` without provisioning a fresh GCE
Computer for every test.

## What "dev mode" means here

The supervisor is the same file the GCE bootstrap clones at boot.
When `TINYHAT_DEV_RUNTIME=1` is set in its environment, four
production-only paths are swapped for local equivalents:

| Production | Dev mode |
| --- | --- |
| Reads `tinyhat-platform-base-url` from GCE instance metadata. | Reads `TINYHAT_PLATFORM_BASE_URL` from the environment. |
| Reads `tinyhat-backend-audience` from GCE instance metadata. | Reads `TINYHAT_BACKEND_AUDIENCE` from the environment. |
| Fetches a Google-signed VM identity JWT from the metadata server and sends it as `Authorization: Bearer …`. | Sends a constant marker bearer. The platform's `computer_identity_verifier` ignores the bearer body entirely when `DEV_AUTO_COMPUTER_ID` is set under `ENV=development`. |
| Runs the OpenClaw gateway as a `systemd` unit (`tinyhat-openclaw-gateway.service`). Health probe reads `journalctl`. | Runs `openclaw gateway run …` as a subprocess of the supervisor. Health probe tails a flat log file under `$TINYHAT_RUNTIME_HOME`. |

Everything else — the `/me/state` → `/me/binding` → `write_openclaw_config` → `start_openclaw_gateway` → `/me/heartbeat` state machine, the rebind watchdog, the OpenClaw config shape — is identical to production. That's the point: any change to `supervisor.py` that breaks the dev loop will break the prod loop too.

Runtime secrets follow the production path too. When the supervisor
receives an `apply_config` heartbeat command, it writes the Computer's
latest secret map to `/etc/openclaw/tinyhat-secrets.json` with mode
`0600`, syncs OpenClaw file SecretRefs in `openclaw.json`, and runs
`openclaw secrets reload --json`. The dev image keeps the supervisor
non-root by chowning only `/etc/openclaw` to the `tinyhat` user.

## Trust boundary

The dev image is **only** safe against a dev backend. The bearer
the supervisor sends in dev mode carries no secret; the platform
short-circuits its verifier only when `ENV=development` AND a
matching `DEV_AUTO_COMPUTER_ID` row is configured. Running the dev
image against a production backend therefore authenticates as
nothing and is rejected.

Do **not** point the dev image at a production-ish deployment to
"see what happens." Build a separate dev backend.

## Build

From the repository root:

```bash
docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:dev .
```

The image is `node:22-slim` (OpenClaw declares `engines.node
>=22.19`) + git + the latest `openclaw` npm package + `supervisor.py`
and a non-root `tinyhat` user. The Tinyhat OpenClaw plugin is cloned
from the public repo/ref in `TINYHAT_PLATFORM_PLUGIN_REPO_URL` /
`TINYHAT_PLATFORM_PLUGIN_REPO_REF` when the supervisor installs the
gateway config; it is not bundled in this image. `openclaw --version`
runs immediately after the `npm install -g openclaw@latest` step as a
build-time smoke check, so a future engine-floor bump fails the build
instead of the first `docker run`. First build is slow (~600MB,
~2min); subsequent builds cache the npm layer.

## Smoke the ChatGPT link retry

Before merging runtime changes that touch subscription linking, run:

```bash
python3 scripts/smoke_start_chatgpt_link_retry.py
```

The smoke starts a localhost backend, invokes the real
`start_chatgpt_link` heartbeat handler in dev mode, and shadows only
the `openclaw` executable with a fake CLI. It forces two pre-code
failure shapes that are hard to trigger against a live account:

- first CLI spawn exits before printing the device-code URL/code;
- first CLI spawn hangs before printing the device-code URL/code.

Both scenarios must recover on the retry and POST
`pending` then `linked` to `/hapi/v1/computers/me/subscription-link-result`.
This validates the Computer-side retry path; it does not approve a
real OpenAI device code or mutate a ChatGPT account.

## Run

The minimum env the supervisor needs:

```bash
docker run --rm -it \
  -e TINYHAT_DEV_RUNTIME=1 \
  -e TINYHAT_PLATFORM_BASE_URL=https://<your-dev-base-url> \
  -e TINYHAT_BACKEND_AUDIENCE=https://<your-dev-base-url> \
  tinyhat-openclaw-runtime:dev
```

- `TINYHAT_PLATFORM_BASE_URL` — the public origin of the dev
  backend (an ngrok / Cloudflare tunnel into a developer's
  worktree, typically). The supervisor POSTs `/hapi/v1/computers/me/…`
  here.
- `TINYHAT_BACKEND_AUDIENCE` — usually the same string. Some
  backends configure a separate JWT audience claim; in dev they
  match.
- `TINYHAT_PLATFORM_PLUGIN_REPO_URL` / `TINYHAT_PLATFORM_PLUGIN_REPO_REF`
  — optional. Defaults to the public `tinyhat-ai/tinyhat` plugin repo
  at `main`.
- The platform must have a `tinyhat_computers` row in `state=ready`
  with `DEV_AUTO_COMPUTER_ID=<that row id>` in its environment.

Live edits to `supervisor.py`: mount the host file into the image
at the same path:

```bash
docker run --rm -it \
  -v "$(pwd)/supervisor.py:/opt/tinyhat-runtime/supervisor.py:ro" \
  -e TINYHAT_DEV_RUNTIME=1 \
  -e TINYHAT_PLATFORM_BASE_URL=https://<your-dev-base-url> \
  -e TINYHAT_BACKEND_AUDIENCE=https://<your-dev-base-url> \
  tinyhat-openclaw-runtime:dev
```

Restart the container to pick up changes. (The supervisor reloads
no code at runtime; that matches prod.)

## What runs inside

1. Supervisor POSTs `/hapi/v1/computers/me/state` with
   `state=ready` (or skips on a 400 if the row is already past
   `provisioning`).
2. Supervisor polls `/hapi/v1/computers/me/binding` until the
   platform attaches a Telegram bot to the Computer.
3. Supervisor writes `openclaw.json` under `$TINYHAT_RUNTIME_HOME`
   (default `/home/tinyhat/runtime/openclaw/openclaw.json`).
4. Supervisor reattaches if the gateway is already active, ready,
   and running the same persisted config fingerprint. Otherwise it
   clears the platform's fallback webhook for that bot (Telegram only
   allows one webhook OR one long-poller) and starts/restarts
   `openclaw gateway run …`.
5. When starting, the supervisor sets `OPENCLAW_CONFIG_PATH` in the
   subprocess environment so OpenClaw finds the `openclaw.json`
   written in step 3. (The `gateway run` CLI does not accept a
   config-path argv; the env variable is the supported entry point and
   is the same one the prod systemd unit uses.) The subprocess's stdout
   + stderr stream to `$TINYHAT_RUNTIME_HOME/openclaw-gateway.log`
   (NOT to the container's stdout); the supervisor's own log lines —
   `dev: starting OpenClaw gateway subprocess`, the readiness probe,
   the heartbeat ticks, the rebind watchdog — are what flow to
   `docker logs`.
6. Supervisor waits for `[gateway] ready` +
   `[telegram] connected to gateway` log lines before writing local runtime health
   `healthy` and POSTing lifecycle `state=active`.
7. Supervisor heartbeats every 30s. A watchdog thread re-polls
   `/me/binding` and triggers rebind if the platform unassigns
   this Computer.

## Manual recovery markers

The supervisor writes local runtime health to
`$TINYHAT_RUNTIME_HOME/tinyhat-control/runtime-state.json` in dev
mode, mirroring the production control-plane path
`/var/lib/tinyhat-control/runtime-state.json`.

The same file also holds the gateway recovery policy state. In
production the supervisor samples the gateway cgroup v2 files
`memory.current`, `memory.max`, and `memory.events.local` from the
gateway service cgroup, falling back to the workload slice when the
service is inactive. A local `oom_kill` delta or restart failure is
counted inside the ten-minute failure window. Three failures enter a
ten-minute hold-down, and recovery waits for three stable memory
samples spaced ten seconds apart before another restart. A thirty-minute
healthy window resets counters; after two failed hold-down cycles, the
runtime enters `unrecoverable_manual`.

An operator can intentionally hold recovery by creating
`$TINYHAT_RUNTIME_HOME/tinyhat-control/unrecoverable-manual`
(`TINYHAT_RUNTIME_STATE_MANUAL_MARKER_PATH` overrides the path). On the
next binding cycle, the supervisor records runtime state
`unrecoverable_manual`, reports lifecycle `broken`, and exits non-zero
so the service manager owns backoff instead of hot-spinning.

After completing manual repair, create
`$TINYHAT_RUNTIME_HOME/tinyhat-control/clear-unrecoverable-manual`
(`TINYHAT_RUNTIME_STATE_CLEAR_MANUAL_PATH` overrides the path). The
supervisor consumes the clear marker, removes the manual marker if
present, and resumes normal gateway recovery.

When the container receives `SIGTERM` (`docker stop`), the
supervisor stops the gateway subprocess cleanly and exits.

## Looking inside a running container

```bash
docker exec -it <container> sh
ls /home/tinyhat/runtime/        # openclaw.json, workspace/, openclaw-gateway.log
ls -l /etc/openclaw/             # tinyhat-secrets.json after first apply_config
tail -f /home/tinyhat/runtime/openclaw-gateway.log
cat /home/tinyhat/runtime/openclaw/openclaw.json
```

The state dir is intentionally under the unprivileged `tinyhat`
user's home. The only `/etc` path the dev image writes is the
production-compatible Tinyhat SecretRef file under `/etc/openclaw`.
