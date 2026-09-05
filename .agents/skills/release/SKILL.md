---
name: release
description: Cut or verify a release of the public Tinyhat OpenClaw runtime repo.
---

# release - runtime repo adapter

Apply the [shared skill contract](../../../AGENTS.md#shared-skill-contract).
This repo releases the runtime package itself.

## Before Release

- Confirm `VERSION` and `CHANGELOG.md` match the intended runtime behavior.
- Confirm the release commit is on `main` and includes only reviewed changes.
- Run:

  ```bash
  git diff --check
  bash -n bootstrap.sh dev/entrypoint.sh
  python -m unittest tests.test_supervisor -v
  docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:<version> .
  ```

## Release Shape

- Follow [RELEASING.md](../../../RELEASING.md) for final `vX.Y.Z` and
  candidate `vX.Y.Z-rc.N` tags, immutable tags, matching release titles, and
  Pre-release/Latest markers. Verify the live release payload before calling it done.
- The GitHub release notes should be public-safe and should name any required companion monorepo or plugin repo PRs.
- Do not publish a runtime that pins or requires unavailable plugin behavior unless the release notes call out the dependency.
