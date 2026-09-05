---
name: update-guidance
description: Edit AGENTS.md, CLAUDE adapters, or SKILL.md files in the public Tinyhat OpenClaw runtime repo.
---

# update-guidance - runtime repo adapter

Apply the [shared skill contract](../../../AGENTS.md#shared-skill-contract).

## Rules

- Keep repository-wide constraints and task routing in `AGENTS.md`; put procedures
  in the relevant skill. Keep `CLAUDE.md` as an `@AGENTS.md` import.
- Before removing a rule, identify whether it is duplicated, obsolete, or still
  needed. Preserve safety boundaries, identity, tests, review, and release gates;
  verify every moved rule remains reachable from its task trigger.
- Canonical dev skills live in `.agents/skills`.
- Claude adapters under `.claude/skills` are symlinks only.
- If a parent workflow changes, update the adapter skill's overrides, not a copied parent body.
- Run the guidance checks in [define-tests](../define-tests/SKILL.md), and check
  changed local links plus the Claude import and skill symlinks.
