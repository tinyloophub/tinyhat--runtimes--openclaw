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

FROM node:20-slim

# Base OS deps. The prod bootstrap.sh installs these on the VM; we
# bake them into the image so the container boots clean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install OpenClaw at the same npm package the prod bootstrap does.
# The dev image pins ``latest`` deliberately — local dev wants the
# version a fresh prod Computer would install today. The
# ``platform_repo_seeds/runtimes/openclaw/VERSION`` file in the
# monorepo records the resolved version for any given prod boot.
RUN npm install -g openclaw@latest

# Non-root runtime user (``tinyhat:tinyhat``). The state dir lives
# under this user's home so the supervisor can write
# ``openclaw.json``, ``workspace/``, and ``openclaw-gateway.log``
# without root.
RUN groupadd --system tinyhat \
    && useradd --system --gid tinyhat --create-home \
        --home-dir /home/tinyhat --shell /usr/sbin/nologin tinyhat \
    && mkdir -p /home/tinyhat/runtime \
    && chown -R tinyhat:tinyhat /home/tinyhat

WORKDIR /opt/tinyhat-runtime

# Copy the supervisor + bootstrap.sh. The supervisor is the only
# Python file we actually run; bootstrap.sh is copied for parity
# with the prod layout (and so the seed comparison ``diff
# platform_repo_seeds/runtimes/openclaw/`` stays clean).
COPY supervisor.py bootstrap.sh VERSION README.md ./
RUN chmod +x supervisor.py bootstrap.sh \
    && chown -R tinyhat:tinyhat /opt/tinyhat-runtime

USER tinyhat

# Dev-mode env that the supervisor reads. ``TINYHAT_DEV_RUNTIME=1``
# is what flips off the systemd / metadata-server paths. The two
# Tinyhat env vars are documented as required-at-run-time; we do
# NOT bake a default platform URL into the image.
ENV TINYHAT_DEV_RUNTIME=1 \
    TINYHAT_RUNTIME_HOME=/home/tinyhat/runtime \
    PYTHONUNBUFFERED=1

CMD ["python3", "/opt/tinyhat-runtime/supervisor.py"]
