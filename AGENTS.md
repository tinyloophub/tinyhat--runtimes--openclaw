# AGENTS.md - Tinyhat OpenClaw runtime

This public repo is the standalone runtime a Tinyhat-managed Computer clones at boot.
It is intentionally smaller than the Tinyloop monorepo: boot, supervision, config apply, diagnostics, and external plugin pin/install only.

## Dev Skills

Canonical repo-local development skills live under [`.agents/skills`](.agents/skills).
Claude-facing adapters under [`.claude/skills`](.claude/skills) are symlinks back to that canonical directory.

When this repo is checked out under the Tinyloop monorepo at `platform_repos/runtimes/openclaw`, skills that name a parent Tinyloop skill should read the parent file first, then apply this repo's override.
From the repo root, the default parent path is `../../../.agents/skills`; from inside an adapter `SKILL.md`, use the parent skill root described here or set `TINYLOOP_PARENT_REPO` when working from a standalone clone.

## Contribution Rules

- Keep this repo public-safe: no private Drive paths, tenant secrets, local-only URLs, or internal admin endpoints.
- Use one logical change per commit and Conventional Commit subjects.
- Never push directly to `main`; open a PR from a branch such as `codex/<topic>` or `claude/<topic>`.
- Runtime behavior changes should usually run:
  - `git diff --check`
  - `bash -n bootstrap.sh dev/entrypoint.sh`
  - `python -m unittest tests.test_supervisor -v`
  - `docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:<topic> .` when bootstrap, supervisor, or dev image behavior changes.
- Dev-skill changes should run `python3 scripts/check_dev_skills.py`.

## Skill Index

| Operation | Skill |
| --- | --- |
| Codex GitHub identity/writeback | [codex](.agents/skills/codex/SKILL.md) |
| Commit | [commit](.agents/skills/commit/SKILL.md) |
| Pick tests | [define-tests](.agents/skills/define-tests/SKILL.md) |
| Open a PR | [open-pr](.agents/skills/open-pr/SKILL.md) |
| Review a PR | [review](.agents/skills/review/SKILL.md) |
| Cut/check a release | [release](.agents/skills/release/SKILL.md) |
| Edit skills | [sharpen-skill](.agents/skills/sharpen-skill/SKILL.md) |
| Edit guidance | [update-guidance](.agents/skills/update-guidance/SKILL.md) |
