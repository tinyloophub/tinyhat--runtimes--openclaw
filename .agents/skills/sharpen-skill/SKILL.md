---
name: sharpen-skill
description: Edit or add development skills in the public Tinyhat OpenClaw runtime repo while keeping them aligned with Tinyloop parent skill patterns.
---

# sharpen-skill - runtime repo adapter

Parent alignment: when this repo is nested under Tinyloop, first read `../../../.agents/skills/sharpen-skill/SKILL.md` for the current skill-editing workflow.
Then keep runtime repo skills small and adapter-shaped.

## Rules

- Canonical skills live in `.agents/skills/<name>/SKILL.md`.
- `.claude/skills/<name>` must be a symlink to `../../.agents/skills/<name>`.
- Prefer adapter skills that cite the parent Tinyloop skill and list only runtime-specific overrides.
- Do not paste large parent skill bodies into this public repo.
- Keep private Tinyloop docs, local paths, and secrets out of skill text.

## Validate

```bash
python3 scripts/check_dev_skills.py
git diff --check
```
