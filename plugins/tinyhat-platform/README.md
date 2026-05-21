# Tinyhat Platform

OpenClaw tool plugin for Tinyhat-hosted Computers.

The plugin exposes metadata-only credential helpers to the agent:

- `tinyhat_list_runtime_secrets`
- `tinyhat_request_runtime_secret`
- `tinyhat_open_terminal_link`
- `tinyhat_secret_command`

It also registers the native Telegram commands `/tinyhat_secrets` and
`/tinyhat_secrets_manage`, which bypass the LLM and return metadata-only status,
a Mini App manager link, or a Mini App add link.

The native Telegram command `/tinyhat_terminal` returns a Mini App button for
the dev mobile terminal spike when this Computer is a local OpenClaw runtime
container and the backend has enabled it.

It never reads or returns runtime secret values. Value entry stays in
the authenticated Telegram Mini App.
