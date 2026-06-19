# Tinyhat Computer runtime — local dev container.
#
# A Docker image that runs the supervisor + the real OpenClaw npm
# package against a dev backend, without systemd, without the GCE
# metadata server, and without root-owned /etc writes. The image is
# self-contained: the only inputs at run time are environment
# variables (TINYHAT_PLATFORM_BASE_URL, optional
# TINYHAT_BACKEND_AUDIENCE).
#
# Trust boundary: this image must only be pointed at a dev backend.
# The supervisor's dev-mode auth path sends a constant marker bearer;
# the platform's verifier honours the matching short-circuit
# (DEV_AUTO_COMPUTER_ID) only when ENV=development.
#
# Build (from this directory's parent — the repo root):
#   docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:dev .
#
# Run (typical):
#   docker run --rm -it \
#     -e TINYHAT_DEV_RUNTIME=1 \
#     -e TINYHAT_PLATFORM_BASE_URL=https://<worktree>.ngrok.app \
#     -e TINYHAT_BACKEND_AUDIENCE=https://<worktree>.ngrok.app \
#     tinyhat-openclaw-runtime:dev

# Node 22 (LTS) is required: OpenClaw declares
# ``engines.node >=22.19.0`` as of ``openclaw@2026.5.19``; building
# on Node 20 produced an image whose CLI exits immediately with
# ``Node.js v22.19+ is required``. The ``22-slim`` tag tracks the
# latest 22.x patch so we always satisfy the >=22.19 floor without
# pinning a specific patch that goes stale.
FROM node:22-slim

# Base OS deps. The prod bootstrap.sh installs these on the VM; we
# bake them into the image so the container boots clean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
        curl \
        git \
        gnupg \
        gosu \
        tmux \
    && curl -fsSL https://tailscale.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# Install OpenClaw at the same npm package the baked runtime bundle records.
# Reading the ref from bundle.lock keeps dev-image proof aligned with the
# preinstalled image contract and prevents a cached ``latest`` layer from
# silently testing an older framework than the bundle will ship.
#
# ``openclaw --version`` immediately after install is the build-time
# smoke check: if the runtime's Node base falls behind OpenClaw's
# engines floor again (the failure mode that triggered the Node 22
# bump in this Dockerfile), the build fails here instead of at the
# first ``docker run``.
COPY tiny_runtime/bake/bundle.lock /tmp/tinyhat-bundle.lock
RUN OPENCLAW_INSTALL_SPEC="$(python3 -c 'import json; print(json.load(open("/tmp/tinyhat-bundle.lock", encoding="utf-8"))["dependencies"]["openclaw"]["resolved"])')" \
    && npm install -g --no-fund --no-audit "${OPENCLAW_INSTALL_SPEC}" \
    && openclaw --version

# Non-root runtime user (``tinyhat:tinyhat``). The state dir lives
# under this user's home so the supervisor can write
# ``openclaw.json``, ``workspace/``, and ``openclaw-gateway.log``
# without root. The Tinyhat SecretRef file intentionally uses the
# prod path under /etc/openclaw, so dev mode owns that one directory
# without making the whole container run as root.
RUN groupadd --system tinyhat \
    && useradd --system --gid tinyhat --create-home \
        --home-dir /home/tinyhat --shell /bin/bash tinyhat \
    && mkdir -p \
        /home/tinyhat/runtime/openclaw \
        /etc/openclaw \
        /var/log/tinyhat/commands \
        /var/log/tinyhat/diagnostics \
    && chown -R tinyhat:tinyhat \
        /home/tinyhat \
        /etc/openclaw \
        /var/log/tinyhat \
        /usr/local/bin \
        /usr/local/lib/node_modules

# Ship the same official Codex provider plugin that production bootstrap
# installs. The supervisor also self-heals this at boot, but baking it into
# the dev image makes local "new Computer" testing match a fresh VM.
RUN HOME=/home/tinyhat/runtime \
    OPENCLAW_CONFIG_PATH=/home/tinyhat/runtime/openclaw/openclaw.json \
    OPENCLAW_STATE_DIR=/home/tinyhat/runtime \
    openclaw plugins install @openclaw/codex --force \
    && HOME=/home/tinyhat/runtime \
       OPENCLAW_CONFIG_PATH=/home/tinyhat/runtime/openclaw/openclaw.json \
       OPENCLAW_STATE_DIR=/home/tinyhat/runtime \
       openclaw plugins inspect codex --json \
       | python3 -c 'import json, sys; p=(json.load(sys.stdin).get("plugin") or {}); ids=p.get("providerIds") or p.get("providers") or []; sys.exit(0 if p.get("id") == "codex" and p.get("enabled") is not False and p.get("status") == "loaded" and "codex" in ids else 1)' \
    && chown -R tinyhat:tinyhat /home/tinyhat /etc/openclaw

WORKDIR /opt/tinyhat-runtime

# Copy the supervisor, the tiny_runtime/tinyhat_cli packages it delegates the
# extracted units to, and bootstrap.sh (copied for parity with the
# production runtime layout).
COPY supervisor.py bootstrap.sh VERSION README.md ./
COPY tiny_runtime ./tiny_runtime
COPY tinyhat_cli ./tinyhat_cli
COPY dev/entrypoint.sh ./dev-entrypoint.sh
RUN chmod +x supervisor.py bootstrap.sh dev-entrypoint.sh \
    && chown -R tinyhat:tinyhat /opt/tinyhat-runtime

# No ``USER tinyhat``: the dev entrypoint starts as root so it can bring
# up tailscaled + Tailscale SSH (which execs login shells and needs
# root), then drops to the unprivileged tinyhat user via gosu for the
# supervisor + OpenClaw. With no Tailscale env it drops to tinyhat
# immediately, so the runtime stays unprivileged either way.

# Dev-mode env that the supervisor reads. ``TINYHAT_DEV_RUNTIME=1``
# is what flips off the systemd / metadata-server paths. The two
# Tinyhat env vars are documented as required-at-run-time; we do
# NOT bake a default platform URL into the image.
ENV TINYHAT_DEV_RUNTIME=1 \
    TINYHAT_RUNTIME_HOME=/home/tinyhat/runtime \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/opt/tinyhat-runtime/dev-entrypoint.sh"]
