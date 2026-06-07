---
name: open-pr
description: Open a PR for the public Tinyhat OpenClaw runtime repo. Use parent Tinyloop PR discipline, then apply runtime repo scope and test-report requirements.
---

# open-pr - runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, first read the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Apply this repo's target, checks, and release boundary below.

## Scope Check

- One related thread per PR.
- Keep runtime behavior separate from monorepo provisioning changes and separate from `tinyhat-ai/tinyhat` plugin payload changes.
- If a PR depends on a monorepo or plugin PR, link it and mark the PR draft until the dependency is ready.

## Commands

```bash
git status --short
git log --oneline origin/main..HEAD
git diff --check
python3 scripts/check_dev_skills.py
```

Add runtime checks from `define-tests` for any touched runtime surface.
For provider plugins, ChatGPT subscription linking, software update,
component restart, or `/restart`, the PR is not ready with unit-test-only
evidence. Follow the repo-local `local-e2e` skill and include its evidence
report when the user-visible path is Telegram-delivered. For ChatGPT linking,
the minimum pass condition is a native sign-in button plus a separate bare
device-code message; redact the actual code.

## PR Creation

Create PRs against:

```text
tinyloophub/tinyhat--runtimes--openclaw
```

Use the configured Codex bot identity for Codex-authored PRs when available, then restore `gh` to the maintainer account.

The PR body should include:

- What changed and why.
- Runtime-vs-plugin boundary notes when plugin install behavior changes.
- Exact verification commands and results.
- A `Local E2E:` report for provider auth, media, subscription, update, or
  restart changes, following `.agents/skills/local-e2e/SKILL.md`.
- Dependency links to Tinyloop monorepo or `tinyhat-ai/tinyhat` PRs when relevant.
