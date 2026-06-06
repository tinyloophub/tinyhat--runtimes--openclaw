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
| Provider-plugin install, ChatGPT subscription linking, device-code flow | Above plus the supervisor retry/unit coverage, device-code startup smoke, a fresh local Docker Computer, and live Telegram proof up to the sign-in button plus bare code |
| Software update, component restart, or `/restart` behavior | Above plus an upgrade-from-previous-release smoke, `/restart` from Telegram, and proof OpenClaw responds with the selected versions afterward |
| Release/version files | Relevant checks above plus review `CHANGELOG.md` and `VERSION` together |

Report exactly what ran.
If Docker is unavailable, say that explicitly and name the runtime surface left unverified.
For the subscription/update/restart rows, unit tests alone are not enough:
include runtime logs, runtime/plugin SHAs or versions, and the dev Computer id.
Never paste device codes or secrets into PRs or logs.
