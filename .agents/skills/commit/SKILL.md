---
name: commit
description: Commit changes in the public Tinyhat OpenClaw runtime repo. Use parent Tinyloop atomicity guidance, then run runtime-specific checks before committing.
---

# commit - runtime repo adapter

Parent alignment: when this repo is nested under Tinyloop, first read `../../../.agents/skills/commit/SKILL.md` for the current atomicity and commit-message rules.
Apply the runtime-specific checks below instead of the monorepo `./scripts/pre-commit.sh` gate.

## Steps

1. Run `git status --short` and group the diff into one logical change.
   Split unrelated docs, runtime behavior, CI, and release changes into separate commits.
2. Run baseline checks:

   ```bash
   git diff --check
   python3 scripts/check_dev_skills.py
   ```

3. For runtime code, bootstrap, or dev image changes, also run:

   ```bash
   bash -n bootstrap.sh dev/entrypoint.sh
   python -m unittest tests.test_supervisor -v
   docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:<topic> .
   ```

4. Commit with a Conventional Commit subject such as:

   ```bash
   git commit -m "feat(runtime): install Tinyhat plugin from public repo"
   ```

## Notes

- Keep generated/runtime repo behavior public-safe; never commit tenant secrets, private URLs, or local env values.
- Use the Codex or Claude bot identity when the maintainer machine has one configured.
