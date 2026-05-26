---
name: codex
description: Codex conventions for the public Tinyhat OpenClaw runtime repo. Use for GitHub writeback, PR comments/reviews, issue comments, and identity restoration.
---

# codex - runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, first read the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Apply the overrides below for `tinyloophub/tinyhat--runtimes--openclaw`.

## Rules

- Codex-authored GitHub comments and reviews use the configured Codex bot identity when one is available.
- Restore `gh` to the maintainer account after the write and verify with `gh auth status`.
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
