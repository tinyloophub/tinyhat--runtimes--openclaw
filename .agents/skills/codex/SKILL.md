---
name: codex
description: Codex conventions for the public Tinyhat OpenClaw runtime repo. Use for GitHub writeback, PR comments/reviews, issue comments, and identity restoration.
---

# codex - runtime repo adapter

Parent alignment: when this repo is nested under Tinyloop, first read `../../../.agents/skills/codex/SKILL.md` for the current Codex writeback contract.
Apply the overrides below for `tinyloophub/tinyhat--runtimes--openclaw`.

## Rules

- Codex-authored GitHub comments and reviews use `tinyloop-farid-codex`.
- Restore `gh` to `farid-tinyloop` after the write and verify with `gh auth status`.
- End every Codex-authored GitHub comment/review body with:

```text
— posted by Codex
```

- Use the target repo explicitly in commands:

```bash
gh pr view <n> --repo tinyloophub/tinyhat--runtimes--openclaw
gh issue view <n> --repo tinyloophub/tinyhat--runtimes--openclaw
```

## Public-Repo Boundary

Do not copy private Tinyloop monorepo details, Drive paths, secrets, local env values, or internal URLs into this public repo, PR bodies, issues, or comments.
