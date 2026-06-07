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
| Provider-plugin install, ChatGPT subscription linking, device-code flow | Above plus the supervisor retry/unit coverage, device-code startup smoke, and the [local-e2e](../local-e2e/SKILL.md) ChatGPT/Codex device-auth walk |
| Software update, component restart, or `/restart` behavior | Above plus an upgrade-from-previous-release smoke and the [local-e2e](../local-e2e/SKILL.md) update/restart recovery walk |
| Release/version files | Relevant checks above plus review `CHANGELOG.md` and `VERSION` together |

Report exactly what ran.
If Docker is unavailable, say that explicitly and name the runtime surface left unverified.
For provider auth, media, subscription, update, and restart rows, unit tests
alone are not enough. Use the [local-e2e](../local-e2e/SKILL.md) report format
to include runtime logs, runtime/plugin SHAs or versions, the local Computer
label, Telegram pass conditions, and explicit redactions. Never paste device
codes, chat ids, private URLs, or secrets into PRs or logs.
