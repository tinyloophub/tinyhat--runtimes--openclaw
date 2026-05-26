---
name: update-guidance
description: Edit AGENTS.md, CLAUDE adapters, or SKILL.md files in the public Tinyhat OpenClaw runtime repo.
---

# update-guidance - runtime repo adapter

Parent alignment: when this repo is nested under Tinyloop, skim `../../../.agents/skills/sharpen-skill/SKILL.md` and the parent guidance files before changing skill shape.

## Rules

- Keep always-loaded files (`AGENTS.md`) short; put procedures in skills.
- Canonical dev skills live in `.agents/skills`.
- Claude adapters under `.claude/skills` are symlinks only.
- If a parent workflow changes, update the adapter skill's overrides, not a copied parent body.
- Run `python3 scripts/check_dev_skills.py` after any skill or adapter change.
