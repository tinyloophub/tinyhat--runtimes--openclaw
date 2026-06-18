# Tinyhat tiny_runtime

`tiny_runtime/` is the greenfield Tinyhat Computer runtime substrate.
It is installed as an immutable bundle under `/opt/tinyhat/bundles/*`
and activated through the stable `/opt/tinyhat/current` symlink.

This tree is intentionally separate from the legacy `supervisor.py`
runtime. The M1 contract is:

- assemble a content-addressed runtime bundle from public, pinned refs,
  including bundle-local OpenClaw under `vendor/openclaw/`;
- install the bundle at bake time and expose stable bin shims;
- run systemd units through `/opt/tinyhat/current`;
- reuse the platform `/me/*` identity surface;
- report a non-secret attestation document with `runtime_generation =
  tiny_runtime`;
- keep every OpenClaw command behind `tinyhat_runtime/openclaw_adapter.py`.

Non-goals:

- no platform default flip;
- no Computer migration path;
- no product-specific logic inside the runtime.

## Local proof

From the repository root:

```bash
tiny_runtime/bake/assemble-bundle.sh /tmp/tiny_runtime_bundle
tiny_runtime/bake/verify-bundle.sh /tmp/tiny_runtime_bundle
docker build -f tiny_runtime/dev/Dockerfile -t tinyhat-openclaw-runtime:tiny-runtime-m1 .
docker run --rm tinyhat-openclaw-runtime:tiny-runtime-m1
```
