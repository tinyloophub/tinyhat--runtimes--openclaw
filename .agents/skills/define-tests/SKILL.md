---
name: define-tests
description: Pick the right verification set for changes in the public Tinyhat OpenClaw runtime repo.
---

# define-tests - runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, skim the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Use this repo-specific matrix for actual commands.

## Matrix

| Change | Minimum checks |
| --- | --- |
| Markdown/guidance/dev skills only | `git diff --check`; `python3 scripts/check_dev_skills.py` |
| `bootstrap.sh` or `dev/entrypoint.sh` | Above plus `bash -n bootstrap.sh dev/entrypoint.sh` |
| `supervisor.py` or runtime config behavior | Above plus `python -m unittest tests.test_supervisor -v` |
| `dev/Dockerfile` or install/runtime package changes | Above plus `docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:<topic> .` |
| Release/version files | Relevant checks above plus review `CHANGELOG.md` and `VERSION` together |

Report exactly what ran.
If Docker is unavailable, say that explicitly and name the runtime surface left unverified.
