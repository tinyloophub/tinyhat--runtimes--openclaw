# Tinyhat Computer runtime

Platform-owned code that runs on every **Tinyhat Computer** (a
private VM). This is the runtime the VM clones at boot; it owns all
communication between the Computer and the Tinyhat platform:

- lifecycle **state reports** (`ready` / `active` / `broken`);
- **binding** polling (which Telegram bot + owner + optional
  provider credentials this Computer should run);
- writing the **framework config** (OpenClaw) for the binding;
- starting and monitoring the framework **gateway** under systemd;
- bounded gateway recovery from restart storms and cgroup v2 OOM signals;
- **heartbeat** while active, plus rebind / unassign detection;
- runtime config apply for Computer-scoped secrets;
- installing the public Tinyhat OpenClaw plugin from the repo/ref
  pinned by the platform provisioning manifest.

## Why this is its own repository

Earlier Computers embedded this supervisor as a large inline
startup-script heredoc in the platform backend. That made the
Computer-side behaviour opaque and unversioned. Moving it to a
standalone public repository means a Computer boots from an explicit
`ref` / `tag` / `SHA`, and the platform records exactly what runtime
revision and framework version were installed on each VM.

## Standalone contract

This repository is **standalone and public**. It must not:

- import from or assume the Tinyhat monorepo layout;
- reference internal Drive paths, admin URLs, or dev hostnames;
- contain any secret values.

All deployment configuration (platform base URL, JWT audience) is
read at runtime from GCE instance metadata, with optional env-file
fallbacks written by `bootstrap.sh`. Nothing in this repo is
environment-specific.

## Layout

| File | Purpose |
| --- | --- |
| `supervisor.py` | The platform-communication supervisor (state, binding, heartbeat, gateway monitor, OpenClaw config writer). Reads `tinyhat-backend-audience` and `tinyhat-platform-base-url` from instance metadata. |
| `bootstrap.sh` | The runtime's install command. Installs generic Computer dependencies, optional private access, the requested framework package, and the supervisor + gateway systemd units after the VM's thin startup script clones this repo. |
| `VERSION` | The runtime version published by this repo; recorded per Computer alongside the resolved commit SHA. |
| `tiny_runtime/` | Greenfield M1 runtime substrate: content-addressed bundle assembly, install/activation shims, systemd units rooted at `/opt/tinyhat/current`, identity/attestation, and the single OpenClaw adapter boundary. |
| `dev/` | Local-development container that runs the supervisor + real OpenClaw against a dev backend without GCE provisioning. See [`dev/README.md`](dev/README.md). |
| `CHANGELOG.md` | What changed between published versions. |

## Local development

A `Dockerfile` ships under [`dev/`](dev/) so the supervisor +
real `openclaw` npm package can run against a dev backend
without booting a fresh GCE Computer. The dev paths are gated on
`TINYHAT_DEV_RUNTIME=1` — production behaviour is unchanged when
that env var is unset. See [`dev/README.md`](dev/README.md) for
the trust boundary and the build/run recipe.

## How a Computer uses this repo

The VM's GCE startup script is a thin bootstrap:

1. install only the minimal packages needed to clone this repo;
2. `git clone` this repository and `git checkout` the configured
   ref/tag/SHA;
3. export per-Computer config/auth material for the runtime
   bootstrap;
4. run this repo's `bootstrap.sh`.

This repo's `bootstrap.sh` owns the generic Computer provisioning
after clone: base OS packages, Node.js, the configured framework
(OpenClaw) version (`npm install -g openclaw@<version>`), optional
private access enrollment, and the supervisor/gateway systemd units.
If the platform does not pass `TINYHAT_FRAMEWORK_INSTALL_SPEC`, the
bootstrap does not install `openclaw@latest`; it expects an existing
OpenClaw binary from a legacy platform bootstrap and fails loudly when
none is available.

The framework (OpenClaw) is installed from npm and is **not** vendored
here — only a package/version pin is recorded. A separate
framework-reference repository would only be introduced if a
framework needed a lifecycle that npm cannot express.

## Gateway recovery

The supervisor is the only authority for intentional gateway hold-down.
It reads the gateway service cgroup, falling back to the workload slice
when the service is inactive, and records non-secret recovery evidence in
`/var/lib/tinyhat-control/runtime-state.json`.

Recovery policy:

- `memory.events.local` `oom_kill` deltas are the primary local OOM signal;
- `oom` is recorded only as supporting evidence;
- three gateway OOM/restart failures inside ten minutes enter hold-down;
- hold-down starts at ten minutes;
- recovery waits for three samples, spaced ten seconds apart, where
  `memory.current <= 70% memory.max` and no new `oom_kill` delta appears;
- a thirty-minute stable healthy window resets counters;
- after two failed hold-down cycles without a stable window, the runtime
  enters `unrecoverable_manual`.

When `unrecoverable_manual` is set, automatic gateway restart attempts stop.
The operator clear path is typed and file-based:
`/var/lib/tinyhat-control/clear-unrecoverable-manual` clears the marker after
manual repair. This repo does not add arbitrary shell, arbitrary `systemctl`,
or broad repair commands.

## Tinyhat plugin support

The Tinyhat OpenClaw plugin is not vendored in this runtime repo. The
platform passes `TINYHAT_PLATFORM_PLUGIN_REPO_URL` and
`TINYHAT_PLATFORM_PLUGIN_REPO_REF` from the provisioning manifest, and
the supervisor clones that public repo into local runtime state before
installing it with `openclaw plugins install`.

By default the plugin source is
[`tinyhat-ai/tinyhat`](https://github.com/tinyhat-ai/tinyhat). That repo
owns default skills, router/tool implementation, and Telegram
presentation. This runtime owns only boot, supervision, config apply,
diagnostics, and pinning/installing the plugin source.

## Framework support

OpenClaw is the only framework wired today. The supervisor's config
writer is OpenClaw-specific; the state/binding/heartbeat protocol is
framework-agnostic, so a future framework (e.g. Hermes) can reuse the
protocol and add its own config writer without changing the platform
inventory shape.
