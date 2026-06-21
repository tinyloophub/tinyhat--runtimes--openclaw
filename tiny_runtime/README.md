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
- keep attestation as a boot/update proof, not a dependency of every
  OpenClaw gateway restart;
- reuse the platform `/me/*` identity surface;
- keep platform-granted material on the Computer: private access enrollment
  and runtime secrets are fetched with Computer identity, while the local
  OpenClaw Gateway token is generated on-box and never posted to the platform;
- report a non-secret attestation document with `runtime_generation =
  tiny_runtime`;
- keep every OpenClaw command behind `tinyhat_runtime/openclaw_adapter.py`;
- mirror every platform-dispatched runtime command to
  `/var/log/tinyhat/commands/<command_id>/command.json` before execution,
  with a local `/var/log/tinyhat/commands/commands.sqlite` index for
  on-box listing/querying;
- execute only the closed runtime command set:
  `activate_bundle`, `rollback_bundle`, `export_diagnostics`,
  `apply_config`, `link_chatgpt`, and `rebuild_app_layer`.

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
Computer-local OpenClaw SecretRef source, and calls OpenClaw's public
`openclaw/plugin-sdk/gateway-runtime` gateway helper for `secrets.reload` as
the local backend client:

```text
method=secrets.reload client=gateway-client mode=backend scopes=operator.admin
```

The Gateway runs with OpenClaw's official token auth. The token lives in
`TINYHAT_OPENCLAW_GATEWAY_TOKEN_FILE` (default:
`/etc/tinyhat/openclaw-gateway-token`, or under `$TINYHAT_RUNTIME_HOME` for
dev containers) with mode `0600`; runtime-owned local gateway calls pass it
through `OPENCLAW_GATEWAY_TOKEN` so command ledgers do not expose secret argv
values. User or external clients still use OpenClaw's normal device-pairing
scope model.

SecretRef-backed fields hot-refresh through `openclaw secrets reload`. Tinyhat
assignment and credential updates never request a gateway restart. Values that
cannot be refreshed through OpenClaw's official hot surfaces must move to
SecretRefs or a separate typed maintenance operation; `restart_requested`,
`gateway_rebind_requested`, and `systemd_restart_requested` stay `false`.
Same-owner rebinds merge new secrets into the existing Tinyhat secrets file so
Mini App credential updates do not wipe user keys. If the platform ever sends a
different owner on an in-place rebind, the runtime replaces the file before
patching OpenClaw so stale owner secrets are not carried across.

`link_chatgpt` starts the official OpenClaw device-code flow for the local
Computer. OAuth tokens stay on the Computer; the platform receives only the
public device-code state and later runtime verification that the active auth
path is `chatgpt_subscription`.

`rebuild_app_layer` is an explicit admin maintenance operation for a
`tiny_runtime` Computer. It creates and verifies a local OpenClaw backup under
the runtime state directory, reactivates the current content-addressed bundle
once, runs the official non-interactive OpenClaw doctor repair/status checks,
and re-attests the active bundle. The backup archive stays on the Computer and
is not uploaded to the platform; support should prune old
`rebuild-backups/` archives after incident evidence is no longer needed. This
command may stop/start the Gateway once; it is not used by assignment or secret
updates and it does not implement an automatic restart loop. If the active
bundle files fail manifest verification, the command fails closed; restoring
bundle files remains the job of `activate_bundle` with a staged bundle id.

## Local proof

From the repository root:

```bash
tiny_runtime/bake/assemble-bundle.sh /tmp/tiny_runtime_bundle
tiny_runtime/bake/verify-bundle.sh /tmp/tiny_runtime_bundle
docker build -f tiny_runtime/dev/Dockerfile -t tinyhat-openclaw-runtime:tiny-runtime-m1 .
docker run --rm tinyhat-openclaw-runtime:tiny-runtime-m1
```
