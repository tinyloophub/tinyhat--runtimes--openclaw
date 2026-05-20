# Tinyhat Computer runtime

Platform-owned code that runs on every **Tinyhat Computer** (a
private VM). This is the runtime the VM clones at boot; it owns all
communication between the Computer and the Tinyhat platform:

- lifecycle **state reports** (`ready` / `active` / `broken`);
- **binding** polling (which Telegram bot + owner + optional
  provider credentials this Computer should run);
- writing the **framework config** (OpenClaw) for the binding;
- starting and monitoring the framework **gateway** under systemd;
- **heartbeat** while active, plus rebind / unassign detection;
- future management hooks (config apply, terminal, diagnostics).

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
| `bootstrap.sh` | The runtime's install command. Writes the supervisor + gateway systemd units and starts the supervisor. Invoked by the VM's thin startup script after this repo is cloned and the framework is installed. |
| `VERSION` | The runtime version published by this repo; recorded per Computer alongside the resolved commit SHA. |

## How a Computer uses this repo

The VM's GCE startup script is a thin bootstrap:

1. install base OS dependencies (`git`, `python3`, Node.js, …);
2. `git clone` this repository and `git checkout` the configured
   ref/tag/SHA;
3. install the configured framework (OpenClaw) at the configured
   version (`npm install -g openclaw@<version>`);
4. run this repo's `bootstrap.sh`.

The framework (OpenClaw) is installed from npm and is **not** vendored
here — only a package/version pin is recorded. A separate
framework-reference repository would only be introduced if a
framework needed a lifecycle that npm cannot express.

## Framework support

OpenClaw is the only framework wired today. The supervisor's config
writer is OpenClaw-specific; the state/binding/heartbeat protocol is
framework-agnostic, so a future framework (e.g. Hermes) can reuse the
protocol and add its own config writer without changing the platform
inventory shape.
