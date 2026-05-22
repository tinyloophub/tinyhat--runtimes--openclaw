# Tinyhat

OpenClaw tool plugin for Tinyhat-hosted Computers.

The plugin exposes metadata-only credential helpers to the agent:

- `tinyhat_list_runtime_secrets`
- `tinyhat_request_runtime_secret`
- `tinyhat_open_manage_computer_link`
- `tinyhat_open_terminal_link`
- `tinyhat_secret_command`

It also registers the native Telegram commands `/tinyhat_secrets` and
`/tinyhat_secrets_manage`, which bypass the LLM and return metadata-only status,
a Mini App manager link, or a Mini App add link.

The native Telegram command `/tinyhat_computer` returns a Manage Computer Mini
App button for the assigned Computer. The page shows status, credential
entrypoints, private-access state, and the advanced terminal launch action.

The native Telegram command `/tinyhat_terminal` returns a Mini App button for
the dev mobile terminal spike when this Computer is a local OpenClaw runtime
container and the backend has enabled it. A command after `/tinyhat_terminal`
or in `tinyhat_open_terminal_link.command` is only a launch hint: the Mini App
shows it to the admin for approval before opening the terminal and running it.
Do not include secret values in terminal launch commands.

It never reads or returns runtime secret values. Value entry stays in
the authenticated Telegram Mini App.
