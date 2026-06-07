---
name: local-e2e
description: Run live local Docker Computer plus Telegram verification for public Tinyhat OpenClaw runtime changes.
---

# local-e2e - runtime repo adapter

Use this skill when a runtime change affects a user-visible Telegram path, a
provider/auth path, software update behavior, or restart/recovery behavior.
Unit tests and Docker builds are still required by `define-tests`; this skill
defines the live end-to-end walk that proves the runtime works on a real local
Computer before the PR is called ready.

## Safety And Redaction

- Keep this public repo public-safe. Do not commit or paste bot tokens, API
  keys, OAuth tokens, device codes, user emails, chat ids, private admin URLs,
  ngrok URLs, or local-only paths from the user's machine.
- In issues and PRs, use generic labels such as "local Docker Computer",
  "reviewer-designated Telegram bot", and "OpenAI OAuth profile present".
- Screenshots are useful local evidence, but do not embed them in public PRs if
  they show private messages, account identifiers, codes, or URLs.
- Never paste a ChatGPT/Codex device code into GitHub, logs, or chat summaries.
  It is enough to say that a separate bare-code Telegram message was delivered.

## Preflight

1. Confirm the worktree and branch.

   ```bash
   git status --short
   git branch --show-current
   ```

2. Confirm the runtime build under test.

   ```bash
   git rev-parse --short HEAD
   docker images | grep tinyhat-openclaw-runtime || true
   docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
   ```

3. Check that only one local runtime is polling the target bot. If logs show
   `Conflict: terminated by other getUpdates request`, stop the stale local
   runtime or duplicate poller before diagnosing runtime behavior.

4. Confirm Telegram Desktop or another user-approved Telegram surface is open
   to the reviewer-designated bot chat before sending messages. If the user has
   not explicitly offered their Telegram app for live testing, ask before
   controlling it.

5. Capture a redacted runtime configuration summary. Record provider order,
   selected model names, runtime/plugin/OpenClaw versions, and container name.
   Do not copy full environment dumps or config files into the PR.

## Start Or Reuse The Local Computer

- Prefer reusing an already connected local Docker Computer when it is running
  the branch under test and is the only poller for the target bot.
- If a fresh local Computer is needed, build the runtime image from this repo
  and start it with the repo's current dev entrypoint or task-specific dev
  harness. Use a named Docker volume so restart tests do not destroy user data.

  ```bash
  docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:<topic> .
  ```

- Wait for the gateway and Telegram bridge to report ready before sending test
  messages. Record the container name, image tag, runtime commit, plugin
  version, and OpenClaw version.
- When testing a cross-repo fix, also record the plugin and Tinyloop platform
  branch, commit, or release version that the local Computer is using.

## Telegram Driver

Drive the same Telegram path the user will use:

1. Send `/status` and confirm the bot responds from the local Computer.
2. For ChatGPT/Codex subscription linking, trigger the subscription link tool.
   The pass condition before user sign-in is:
   - a native Telegram sign-in button is delivered
   - a separate bare device-code message is delivered
   - the actual code is redacted from notes and PR text
3. After the user completes sign-in, verify that Telegram receives a clear
   success message and, when the runtime restarts, a final recovery message that
   says the restart completed and the OpenAI/ChatGPT model is active.
4. For media behavior, send the actual payload shape being tested through
   Telegram: voice note versus audio file, image with caption versus bare image,
   or document attachment. Prefer small disposable fixtures under a temporary
   directory such as `/tmp/<topic>-media-e2e`.
5. For update/restart behavior, perform the version change or local equivalent,
   send `/restart` from Telegram, and then send `/status` after recovery. The
   pass condition is that OpenClaw responds and reports the selected versions or
   selected model/auth state.

## Scenario Matrix

Use the rows that match the change:

| Scenario | Required live proof |
| --- | --- |
| ChatGPT/Codex device auth | Telegram sign-in button, separate bare-code message, post-sign-in success message, post-restart recovery/status message |
| Provider plugin install | New local Computer can start the auth command without "No provider plugins found" and no manual plugin install |
| OpenAI media behavior | `/status` shows OpenAI auth/model active; audio/image path tries OpenAI first when OpenAI auth exists; any fallback is logged and explained |
| Software update | Upgrade from a previous released runtime/plugin version; Telegram progress finishes; `/status` reports the requested versions |
| `/restart` recovery | `/restart` from Telegram restarts OpenClaw; a later Telegram message receives a normal response; no stale poller conflict remains |
| Data-preserving full restart | Restart the local Computer/gateway process with the same Docker volume; existing config/auth state survives; `/status` works afterward |

## Evidence Report

Add a concise local E2E block to the PR body or a PR comment:

```markdown
Local E2E:
- Computer: local Docker Computer `<container-or-label>` using image `<tag>`
- Versions: runtime `<sha-or-version>`, plugin `<version>`, OpenClaw `<version>`
- Cross-repo inputs: plugin `<branch/version>`, platform `<branch/version>` when relevant
- Telegram driver: user-approved desktop Telegram, reviewer-designated bot chat
- Scenarios:
  - `/status`: pass/fail, observed model/auth and version summary
  - ChatGPT/Codex device auth: pass/fail, button delivered, bare-code message delivered, post-sign-in/restart messages delivered
  - Media/update/restart path: pass/fail, payload or command used, observed result
- Provider/config evidence: redacted model/provider order and fallback behavior
- Logs: ordered, redacted gateway/runtime lines with timestamps
- Redactions: no tokens, device codes, chat ids, private URLs, or local-only admin details included
- Not run / residual risk: `<none or explicit gap>`
```

If a scenario cannot run, say why and identify the exact user-visible path left
unverified. Do not replace a required local E2E scenario with unit-test-only
evidence for provider auth, media, update, or restart changes.
