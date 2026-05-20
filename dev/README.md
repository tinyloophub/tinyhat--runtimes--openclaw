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

The image is `node:20-slim` + the latest `openclaw` npm package +
`supervisor.py` + a non-root `tinyhat` user. First build is slow
(~600MB, ~2min); subsequent builds cache the npm layer.

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
4. Supervisor clears the platform's fallback webhook for that bot
   (Telegram only allows one webhook OR one long-poller).
5. Supervisor launches `openclaw gateway run --config …` as a
   subprocess. Logs flow to `$TINYHAT_RUNTIME_HOME/openclaw-gateway.log`
   AND to `docker logs`.
6. Supervisor waits for `[gateway] ready` + `[telegram] connected
   to gateway` log lines, then POSTs `state=active`.
7. Supervisor heartbeats every 30s. A watchdog thread re-polls
   `/me/binding` and triggers rebind if the platform unassigns
   this Computer.

When the container receives `SIGTERM` (`docker stop`), the
supervisor stops the gateway subprocess cleanly and exits.

## Looking inside a running container

```bash
docker exec -it <container> sh
ls /home/tinyhat/runtime/        # openclaw.json, workspace/, openclaw-gateway.log
tail -f /home/tinyhat/runtime/openclaw-gateway.log
cat /home/tinyhat/runtime/openclaw/openclaw.json
```

The state dir is intentionally under the unprivileged `tinyhat`
user's home so nothing in the image needs root.
