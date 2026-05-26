---
name: review
description: Review PRs in the public Tinyhat OpenClaw runtime repo, using parent Tinyloop review quality rules with runtime-specific risk checks.
---

# review - runtime repo adapter

Parent alignment: when this repo is nested under Tinyloop, first read `../../../.agents/skills/review/SKILL.md` for review depth, posted-review expectations, and finding format.
Apply the runtime-specific risk checklist below.

## Runtime Checklist

- Boot/install scripts remain public-safe and do not embed secrets, private URLs, or local-only paths.
- Supervisor changes preserve Computer auth boundaries and do not expose raw tokens or secret values.
- Plugin changes preserve the repo split: runtime installs/pins; plugin repo owns implementation/router/default skills.
- Dev Docker changes still build and run the supervisor under the intended user.
- Version/CHANGELOG changes match the behavior actually shipped.

## Evidence

Prefer concrete commands:

```bash
git diff --check
bash -n bootstrap.sh dev/entrypoint.sh
python -m unittest tests.test_supervisor -v
docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:<topic> .
```

Post GitHub reviews under the Codex bot when acting as Codex, and end with `— posted by Codex`.
