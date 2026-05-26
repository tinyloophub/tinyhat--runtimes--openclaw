---
name: release
description: Cut or verify a release of the public Tinyhat OpenClaw runtime repo.
---

# release - runtime repo adapter

Parent alignment: when this repo is nested under Tinyloop, skim `../../../.agents/skills/release/SKILL.md` for the current Tinyloop release discipline.
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

- Tags use `vX.Y.Z`.
- The GitHub release notes should be public-safe and should name any required companion monorepo or plugin repo PRs.
- Do not publish a runtime that pins or requires unavailable plugin behavior unless the release notes call out the dependency.
