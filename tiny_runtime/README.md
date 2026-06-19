# Tinyhat tiny_runtime

`tiny_runtime/` is the greenfield Tinyhat Computer runtime substrate.
It is installed as an immutable bundle under `/opt/tinyhat/bundles/*`
and activated through the stable `/opt/tinyhat/current` symlink.

This tree is intentionally separate from the legacy `supervisor.py`
runtime. The stable contract is:

- assemble a content-addressed runtime bundle from public, pinned refs,
  including bundle-local OpenClaw under `vendor/openclaw/`;
- install the bundle at bake time and expose stable bin shims;
- run systemd units through `/opt/tinyhat/current`; systemd/OpenClaw owns
  gateway liveness, while the tiny Tinyhat platform loop owns only
  assignment, heartbeat, ledger dispatch, and timing reports;
- reuse the platform `/me/*` identity surface;
- report a non-secret attestation document with `runtime_generation =
  tiny_runtime`;
- keep every OpenClaw command behind `tinyhat_runtime/openclaw_adapter.py`;
- mirror every platform-dispatched runtime command to
  `/var/log/tinyhat/commands/<command_id>/command.json` before execution,
  with a local `/var/log/tinyhat/commands/commands.sqlite` index for
  on-box listing/querying;
- execute only the closed runtime command set:
  `activate_bundle`, `rollback_bundle`, `export_diagnostics`,
  `apply_config`, and `link_chatgpt`.

Bundle verification proves the local files match the declared manifest and
bundle id. It is not a signature system; production promotion should still pin
the expected bundle id from a trusted build.

Non-goals:

- no platform default flip;
- no Computer migration path;
- no product-specific logic inside the runtime.

## Command ledger

`tinyhat-runtime command run --command-json <path>` executes a single
platform ledger command. The platform row remains the source of record, but
the Computer keeps a redacted local mirror so the user can verify what the
admin asked the machine to do:

```bash
sudo cat /var/log/tinyhat/commands/<command_id>/command.json
sudo python3 - <<'PY'
import sqlite3

db = "/var/log/tinyhat/commands/commands.sqlite"
with sqlite3.connect(db) as connection:
    for row in connection.execute(
        "select command_id, kind, status, phase from commands order by updated_at desc"
    ):
        print(row)
PY
```

`activate_bundle` verifies a staged content-addressed bundle, stops the
gateway, flips `/opt/tinyhat/current`, starts the gateway, then runs the health
and attestation gate. A failed gate rolls the symlink back to the previous
target and restarts the gateway on that target.

`export_diagnostics` calls the official OpenClaw command:

```bash
openclaw gateway diagnostics export --json --output <zip>
```

The runtime rewrites the resulting zip through Tinyhat redaction before
settling the command. The expected support bundle includes `summary.md`,
`diagnostics.json`, `manifest.json`, health snapshots, and
`stability/latest.json`.

`apply_config` pulls the latest `/me/runtime-secrets` map, writes the
Computer-local OpenClaw SecretRef source, and calls the official:

```bash
openclaw secrets reload --json
```

SecretRef-backed fields hot-refresh through `openclaw secrets reload`. Tinyhat
assignment and credential updates never request a gateway restart. Values that
cannot be refreshed through OpenClaw's official hot surfaces must move to
SecretRefs or a separate typed maintenance operation; `restart_requested`,
`gateway_rebind_requested`, and `systemd_restart_requested` stay `false`.

`link_chatgpt` starts the official OpenClaw device-code flow for the local
Computer. OAuth tokens stay on the Computer; the platform receives only the
public device-code state and later runtime verification that the active auth
path is `chatgpt_subscription`.

## Local proof

From the repository root:

```bash
tiny_runtime/bake/assemble-bundle.sh /tmp/tiny_runtime_bundle
tiny_runtime/bake/verify-bundle.sh /tmp/tiny_runtime_bundle
docker build -f tiny_runtime/dev/Dockerfile -t tinyhat-openclaw-runtime:tiny-runtime-m1 .
docker run --rm tinyhat-openclaw-runtime:tiny-runtime-m1
```
