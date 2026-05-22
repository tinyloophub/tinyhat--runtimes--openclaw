---
name: tinyhat-secrets
description: List or request runtime credentials for this Tinyhat OpenClaw Computer.
user-invocable: false
---

# Tinyhat Secrets

Use Tinyhat's platform tools when the user asks about credentials,
runtime secrets, API keys, tokens, or environment-style variables.

Rules:

- Never ask the user to paste a secret value into chat.
- Use `tinyhat_list_runtime_secrets` to see configured secret names and
  revision metadata plus the Mini App management link.
- Use `tinyhat_request_runtime_secret` to create a Telegram Mini App link
  for adding or replacing one secret value.
- Pass a short, readable `description` whenever you know why the user
  needs the secret. Derive it from the current conversation; do not make
  the user repeat context just to label the credential.
- Tell the user to open the returned Mini App link or button and save the
  value there.
- Secret values are never visible through these tools.

Native slash command:

- `/tinyhat_secrets list`
- `/tinyhat_secrets manage`
- `/tinyhat_secrets_manage`
- `/tinyhat_secrets add OPENAI_API_KEY used by the runtime to call OpenAI`
