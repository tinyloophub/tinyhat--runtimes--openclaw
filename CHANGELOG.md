# Changelog

All notable changes to the Tinyhat Computer runtime are recorded
here. The runtime is consumed by the Tinyhat platform's Computer
provisioning step, which records the resolved commit SHA + the
runtime's published `VERSION` on each new Computer row.

## Unreleased

### Added

- Report the active tiny-runtime bundle's runtime, plugin, and OpenClaw
  component evidence in heartbeat and runtime-state payloads so the platform
  can prove data-preserving upgrade convergence.

### Fixed

- Quiesce existing tiny-runtime and legacy OpenClaw services before a
  source-reinstall upgrade installs or starts the promoted bundle.

## 0.16.5

Patch release for the Tinyloop v0.16 `tiny_runtime` train. This hardens
data-preserving upgrades of existing customer Computers so the legacy
supervisor path cannot survive beside the tiny runtime.

### Fixed

- Terminate orphaned legacy supervisor and unauthenticated legacy gateway
  processes during source reinstall before enabling the tiny-runtime systemd
  services.
- Materialize the pinned Tinyhat OpenClaw plugin during `rebuild_app_layer`
  before bundle activation, failing closed if the plugin cannot be made ready.
- Make the local tiny-runtime Docker canary image boot through the
  `tiny_runtime` platform loop without carrying or invoking `supervisor.py`.

## 0.16.4

Patch release for the Tinyloop v0.16 `tiny_runtime` train. This lets
ready-pool Computers accept platform runtime commands before assignment,
so private-access repair can complete while the Computer is still hot and
unassigned.

### Fixed

- Dispatch runtime commands from unassigned ready-pool Computers, including
  `enroll_private_access`, without waiting for an agent binding cycle.
- Treat `409` ready acknowledgements as a safe deferral so the tiny runtime
  keeps polling rather than exiting while platform prerequisites are still
  converging.
- Re-authenticate Tailscale enrollment with a logout-before-up sequence to
  avoid stale local node bindings during private-access repair.
- Resolve bundled OpenClaw package paths robustly when the CLI binary is nested
  inside the package root, preserving hot-image bundle assembly.

## 0.16.3

Patch release for the Tinyloop v0.16 `tiny_runtime` train. This keeps
fresh Computers on the runtime-owned setup path even when they boot from
a non-bundled base image, and it preserves the no-legacy-supervisor,
no-destructive-OpenClaw-restart-loop boundary.

### Added

- Add the public `tinyhat-gce-startup` source-install entrypoint used by
  non-bundled GCE Computers to assemble and start the tiny runtime from
  the public repository at boot.
- Add Computer-authenticated private-access enrollment at source startup
  so ready Computers can obtain their own private access setup material
  through `/hapi/v1/computers/me/*` APIs.
- Add guard coverage proving the runtime platform client is confined to
  the Computer-owned `/computers/me/*` API surface.

### Fixed

- Use the local runtime gateway auth path for secret reloads so config
  apply remains hot-reload only and does not require platform-managed
  OpenClaw restarts.
- Harden source startup readiness in the dev loop and make private-access
  enrollment idempotent so repeated activation does not create duplicate
  enrollment work.

## 0.16.2

Patch release for the Tinyloop v0.16 `tiny_runtime` train. This lets
Computers that booted from the tiny-runtime image repair missing Tailscale
private access without falling back to legacy supervisor behavior.

### Added

- Add the `enroll_private_access` tiny-runtime command, allowing the Computer
  to fetch a fresh one-time Tailscale enrollment payload through its
  Computer-authenticated platform channel and report the sanitized result back
  to the command ledger.

### Fixed

- Ensure private-access repair never requests an OpenClaw or runtime gateway
  restart, keeping the no-destructive-restart-loop invariant for the v0.16
  tiny-runtime model.
- Keep Tailscale auth material out of local command mirrors and runtime command
  results while still reporting `ready`/`tailnet_ip` state for admin
  assignment gates.

## 0.16.1

Patch release for the Tinyloop v0.16 `tiny_runtime` train. This keeps
production upgrades and hot-pool Computers on the isolated tiny runtime while
restoring the access/auth prerequisites expected by manual assignment,
terminal, and subscription flows.

### Added

- Add the source-reinstall bootstrap mode for data-preserving upgrades of
  legacy Ubuntu-baseline Computers into the tiny-runtime model, removing legacy
  supervisor units and starting only `tinyhat-runtime-*` services.
- Add tiny-runtime Tailscale enrollment/reporting for `tiny_runtime_bundle`
  Computers so hot-pool rows become private-access-ready before assignment.
- Add the tiny-runtime ChatGPT/Codex device-code worker and runtime model-status
  verification using OpenClaw's official CLI surfaces.

### Fixed

- Harden tiny-runtime subscription verification against unexpected
  `openclaw models status --json` shapes and keep Tailscale auth-key temp files
  under the private-access status directory.

## 0.16.0

Minor release for the Tinyloop v0.16 `tiny_runtime` release train.

### Added

- Add the explicit `rebuild_app_layer` command for `tiny_runtime` Computers,
  including a verified local OpenClaw backup, one-shot bundle reactivation,
  doctor/status checks, command-ledger reporting, and no automatic restart
  loop.
- Add the hot-assignment platform loop used by warm Computers to bind with the
  minimum post-assignment work.

### Fixed

- Harden the hot image assignment lifecycle so assignment stays on the fast path
  while preserving the no-restart tiny runtime model.
- Align dev OpenClaw state/config path resolution with baked
  `TINYHAT_RUNTIME_HOME` environments so runtime commands use the same
  OpenClaw state as the gateway.

## 0.15.6

Patch release for Tinyloop `tiny_runtime_bundle` image bakes that preinstall
the OpenClaw plugins needed by hot Computers before they enter the ready pool.

### Added

- Add a repo-owned hot-image preinstall helper that bakes the Codex subscription
  provider plugin and Tinyhat platform plugin into `tiny_runtime_bundle` images.

### Changed

- Avoid installing the optional Codex subscription plugin on the platform-credit
  assignment path; platform-credit bindings now inspect any preinstalled plugin
  and continue without npm work when it is absent.

## 0.15.5

Patch release for Tinyloop `tiny_runtime_bundle` image bakes that need the
bundle manifest, attestation, and image catalog to resolve from the same
platform authority refs.

### Fixed

- Let the `tiny_runtime` bundle assembler take explicit OpenClaw and Tinyhat
  plugin refs from the platform bake authority so the bundle manifest,
  attestation, and Tinyloop image catalog agree on the promoted components.

## 0.15.4

Patch release for the Tinyloop v0.16 warm-pool image train. This is the first
final runtime release that carries the content-addressed `tiny_runtime` bundle
tooling needed by `tiny_runtime_bundle` Computer images.

### Added

- Add the `tiny_runtime` bundle, installer, attestation, gateway launcher, and
  content manifest tooling used by baked Computer images.
- Add the command ledger runner used by hot component and credential commands.

### Fixed

- Normalize activated bundle ownership after install so root-baked bundles keep
  stable file ownership when moved into `/opt/tinyhat/current`.
- Let typed `apply_config` runtime commands use the existing runtime-secret
  env-block path for non-OpenAI credentials, post the applied command result,
  then request the legacy gateway rebind so child shells receive the new
  environment.
- Report env-block refreshes as `gateway_rebind_requested=true` while keeping
  `restart_requested=false` and `systemd_restart_requested=false`.
- Create and own the runtime command log root during bootstrap/dev image setup,
  and fall back to the runtime state directory if that root is unavailable so
  local command recording cannot block hot credential application.

## 0.15.3

Patch release for activating repaired legacy ChatGPT/Codex subscription auth
after one-Computer admin repair.

### Fixed

- Re-enter the OpenClaw binding cycle after a successful ChatGPT/Codex auth
  store repair even if the best-effort platform result POST fails, so repaired
  subscription auth can become active instead of leaving the gateway on fallback
  auth until command redelivery.

## 0.15.2

Patch release for migrating legacy ChatGPT/Codex subscription auth stores during
Computer upgrades and one-Computer admin repair.

### Fixed

- Migrate legacy ChatGPT/Codex OpenClaw auth stores with the official
  `openclaw doctor --fix --non-interactive --yes` path, fail closed when
  modern OpenClaw cannot read a subscription profile, and report the result
  back to Tinyloop for one-Computer admin repairs.
- Refresh the OpenClaw auth version probe after framework updates so upgraded
  Computers decide whether doctor migration is needed from the current
  framework install instead of stale pre-update data.

## 0.15.1

Patch release for upgraded Computers that hit corrupt OpenClaw framework
installs or old on-disk layout drift during in-place software recovery.

### Fixed

- Retry failed OpenClaw framework installs from a clean package path after npm
  tar/ENOENT extraction failures, while preserving rollback to the previous
  working framework tree until the new CLI verifies successfully.
- Smoke-test `openclaw --version` after bootstrap framework installs so a
  partially extracted global package fails fast instead of surfacing later as a
  missing module during plugin setup.
- Harden the hard-reset migration path: back up first, preserve unknown user
  state in place, clean only runtime-owned install/generated paths, write a
  one-shot marker, warn on layout drift, prune old backups, and fail loudly if
  a restore copy cannot complete.

## 0.15.0

This release aligns the public OpenClaw runtime with the Tinyloop `v0.15.0`
in-place Computer operations train. It includes safer component updates, reboot
bootstrap resilience, and startup-path measurements and prewarming used by the
platform to operate existing Computers without replacing them.

### Added

- Startup metrics: the binding cycle now emits `config_apply_to_runtime_ack_ms`
  and `bot_attach_to_first_ack_ms` as their own runtime samples, derived from
  the `binding/config apply` and `bot-ready` phase spans it already records.
  Previously only the coarse `assignment_to_serving_ms` sample was sent, so the
  consuming platform's hard-gate rows for those two metrics had no data.
  Telemetry-only; no change to assignment behavior or latency.
- Startup metrics: on a successful gateway readiness probe the runtime now logs
  the bot-ready split — prewarmable gateway/plugin boot (time to `[gateway]
  ready`) vs the largely irreducible Telegram-connect floor (`[gateway] ready`
  → `[telegram] connected to gateway`), read from the gateway's own journal via
  `journalctl`. Measure-first data for sizing a future gateway prewarm;
  log-only, best-effort, and never blocks or fails readiness.

### Changed

- Startup latency: the binding-independent Tinyhat plugin is now pre-installed in
  a background daemon thread at supervisor start, while the Computer waits for a
  binding, instead of being installed on the binding (assignment) critical path.
  When a binding arrives the bind-time setup hits the install marker fast-path,
  so `binding/config apply` no longer pays the git-clone + `openclaw plugins
  install` cost on un-baked images. The plugin install is serialized by a lock
  shared between the prewarm thread and the bind-time setup (no concurrent
  checkout/install race), and the prewarm is best-effort — any failure falls
  back to the existing bind-time install, so readiness is never blocked.
- Component update backup discovery now uses the framework's recorded backup
  paths directly, keeping the rollback lookup small and tied to the update
  transaction instead of scanning broader OpenClaw internals.

### Fixed

- Gate supervisor boot on successful platform setup so reboot recovery does not
  claim the runtime is ready before the platform plugin and binding path are in
  place.
- Verify OpenClaw framework updates atomically before advancing the recorded
  component set, preserving rollback state when a framework restart or readiness
  probe fails.

## 0.14.0

This release aligns the public OpenClaw runtime with the Tinyloop `v0.14.0`
startup-speed train. It includes the runtime companions needed for hot-pool
assignment measurement, public runtime cache images, and the final hot-ready
freshness gate.

### Added

- Report binding long-poll timing and phase evidence so Tinyloop can separate
  admin assignment response, runtime config apply, bot process readiness, and
  active-state acknowledgement.
- Trust verified public runtime cache checkouts when the producer records the
  expected repository and commit SHA, while preserving mismatch fallback.

### Fixed

- Continue refreshing runtime state while a Computer is hot-ready but unbound,
  so Tinyloop can reject stale ready rows before enabling the hot-ready claim
  gate.

## 0.12.6

### Changed

- Publish a version-alignment release for the Tinyloop `v0.12.6` train. The
  runtime code and behavior are unchanged from `v0.12.5`; this release keeps
  the public runtime version in lockstep with the monorepo release that pins
  the latest Tinyhat plugin.

## 0.12.5

### Changed

- Change the supervisor's defensive OpenRouter fallback from Kimi K2.6 to
  DeepSeek V4 Pro. New Computers normally receive an explicit platform model
  package from the backend, but older or malformed bindings should fall back to
  the same concrete agentic default instead of continuing on Kimi.
- After a ChatGPT/Codex subscription config restart, run
  `openclaw models status --json` and report the observed default model back to
  Tinyhat. The platform now sends the final "ChatGPT/Codex is ready" message
  only after that OpenClaw command proves the model resolved to
  `openai/gpt-5.5`.

## 0.12.4

### Fixed

- Run Tinyhat platform-plugin Git/update/install subprocesses as the
  OpenClaw runtime user after repairing checkout ownership. This fixes
  upgraded cloud Computers where the post-subscription restart still
  tripped Git's `safe.directory` guard because root was running `git -C`
  against a checkout already handed to the gateway user.

## 0.12.3

### Fixed

- Repair the Tinyhat platform-plugin checkout ownership before the first
  `git -C` update command. This prevents upgraded cloud Computers from
  tripping Git's `safe.directory` guard on a root-owned checkout during
  the post-subscription restart path.

## 0.12.2

### Fixed

- Run the ChatGPT/Codex device-code auth CLI as the same unprivileged
  runtime user as the OpenClaw gateway, and repair the per-agent auth
  store ownership before auth/config reads. This fixes cloud Computers
  where OAuth completed but the restarted gateway could not open
  `openclaw-agent.sqlite` because the supervisor-created SQLite store
  was root-owned.

## 0.12.1

### Fixed

- Read and wipe ChatGPT/Codex subscription auth profiles from OpenClaw
  2026.6.6's SQLite auth store in addition to the legacy
  `auth-profiles.json` store. This fixes completed subscription links
  that reported success but kept the Computer on the OpenRouter/Kimi
  platform-credit route because the supervisor could not see the new
  SQLite profile.
- Use Kimi K2.6 as the supervisor's defensive OpenRouter default when a
  keyed binding omits the explicit model field.

## 0.12.0

This release is the first public stable-runtime artifact for Tinyhat's
verified v0.11.0 runtime work and the v0.12.0 on-box `tinyhat` CLI +
capability-contract release. It pairs with Tinyhat plugin `v0.5.0` and the
Tinyloop monorepo `v0.12.0` release cut.

### Added

- Declared-vs-registered capability verification: after gateway start the
  runtime compares the installed plugin's declared manifest
  (`contracts.tools` / `contracts.skills` / `contracts.framework`) against
  the framework registry (`openclaw plugins inspect` — used only when it
  exposes positive tool-level data; proven live on a bound canary, the
  2026.6.6 CLI-side derived registry can omit a config-enabled,
  gateway-loaded install-dir plugin entirely) or the plugin's load beacon
  (`self_check`, never inventing missing names; plugin generations that
  predate the beacon report `unverifiable` instead of false shortfalls). The verdict ships
  as the additive `capabilities` block of `runtime_state_v1`
  (`{declared_tools, registered_tools, declared_skills, mounted_skills,
  missing: [<=10 names], missing_truncated, checked_at_unix, mechanism,
  status: ok|shortfall|unverifiable}`), is re-checked after every gateway
  start (TTL-cached on the daemon write path), and renders in
  `tinyhat status` / `tinyhat health` (live re-check).
- Framework supported-range check: a plugin-declared
  `contracts.framework` range outside the installed OpenClaw version
  demotes `healthy` to `unsupported_openclaw_version` — the value now
  means what it says.
- Unit-category allowlist guard: every module under `tinyhat_cli/units/`
  declares `UNIT_CATEGORY` from the closed seven-category mechanism set
  (identity / apply / supervision / recovery / framework-compatibility /
  diagnostics / release-update-lifecycle); CI rejects uncategorized or
  product-categorized units (with a deliberate red fixture proving the
  guard can fail).

- Global command lock for mutating commands: `flock(LOCK_EX)` on a stable
  root-owned mutex fd, deliberately inherited by mutation subprocess trees
  (own process group, recorded `child_pgid`), with a `command_lock_v1`
  status record, typed busy answers, stale takeover + runner-lost
  reconciliation, deadline enforcement (process-group SIGTERM→SIGKILL),
  and a bounded idempotency results store (50 records / 24 h).
- `tinyhat gateway restart` — the first operate-class CLI command: the
  lock-held operation transaction (webhook delete when a bot token is
  configured → `systemctl reset-failed` + `restart` → bounded readiness
  wait) driven to a terminal `succeeded`/`failed`/`timed_out` verdict;
  `--idempotency-key` replays a stored result without re-execution.
- Command-result spool (`command_result_spool_v1`): pre-redacted,
  atomically written result records (≤ 2 KiB each, ≤ 64 KiB / 50 records,
  bounded quarantine) that the daemon folds into the new `commands` ring
  (last 5) of `runtime_state_v1` on its next post; `tinyhat status` reads
  the same spool so a support shell sees results while the daemon is down.
- Daemon gateway restarts (recovery leg + component-update restart) now run
  the same lock-held transaction (`holder: "daemon"`), deferring while a
  human holds the lock instead of racing it.
- `dev/systemd-proof/lock_proof.sh` — the seven live lock-concurrency
  proof cases against real systemd.

### Changed

- Plugin-not-loaded health mapping: an enabled plugin with no fresh load
  beacon (and a plugin whose declared capabilities register as zero) now
  demotes `healthy` to `degraded_workload` with
  `last_error_category=plugin_not_loaded`; a partial shortfall reports
  `capability_shortfall`. The previous demotion target
  `unsupported_openclaw_version` was wrong copy and is reserved for true
  framework-range violations.

### Fixed

- Reattach the supervisor to an already healthy OpenClaw gateway when the
  persisted runtime state and current config fingerprint match, so supervisor
  restarts do not disrupt Telegram long polling.
- Persist root-owned local runtime health state for gateway startup/reattach,
  including `openclaw_not_ready` before bounded recovery and operator
  marker/clear-marker handling for `unrecoverable_manual` state.
- Add cgroup v2 memory/OOM recovery policy for the OpenClaw gateway: `oom_kill`
  deltas and restart failures enter supervisor-owned hold-down, recovery waits
  for bounded memory stability, stable healthy windows reset counters, and
  repeated failed hold-down cycles escalate to `unrecoverable_manual`.

## 0.11.14

### Changed

- Publish a no-op patch release to exercise Tinyhat's runtime Software update
  flow against a fresh runtime version. No runtime behavior changes are
  included in this release.

## 0.11.13

### Fixed

- Run the OpenClaw gateway as an unprivileged, bounded systemd workload while
  keeping the Tinyhat supervisor protected enough to report, update, and
  recover Computers when the gateway is stopped or OOM-killed.
- Publish runtime-owned config, auth profile, and secret files with the correct
  gateway-readable ownership atomically, so supervisor-written files are usable
  by the non-root OpenClaw process immediately after restart.
- This release pairs with the Tinyloop admin Computer recovery path in
  tinyloophub/tinyloop#639. No Tinyhat plugin release is required.

## 0.11.12

### Fixed

- Prefer user-owned OpenAI auth for Telegram media transcription and image
  understanding when ChatGPT/Codex auth is connected, while keeping managed
  OpenRouter media transcription as the fallback rail.

## 0.11.11

### Changed

- Sharpen the runtime repo's release and test guidance for critical Tinyhat
  paths: ChatGPT subscription linking, provider-plugin installation, software
  updates, component restarts, and `/restart` now explicitly require Docker
  Computer and live Telegram evidence when user-visible behavior changes.
- Align the runtime repo's `open-pr` and `define-tests` skills with the
  Tinyloop monorepo's critical runtime/plugin QA gates from
  tinyloophub/tinyloop#626.

## 0.11.10

### Fixed

- Gate plugin/framework Software update success on a fresh OpenClaw gateway
  restart and readiness probe, so the platform does not mark an update
  complete when the updated gateway process fails to load.
- Restart OpenClaw only once for multi-component plugin/framework updates,
  preserve bot/owner context in restart logs, suppress the Phase D inactivity
  monitor during the intentional restart, and report restart failures back to
  the platform instead of leaving the update command to retry forever.

## 0.11.9

### Fixed

- Retry ChatGPT subscription device-code startup when the OpenClaw CLI exits or
  hangs before emitting the verification URL and user code, while preserving
  fail-fast behavior for local provider-plugin and PTY allocation errors.
- Add a dev-mode smoke harness for the `start_chatgpt_link` retry path that
  forces first-attempt CLI exit/hang failures and verifies the runtime posts
  `pending` then `linked` after recovery.

## 0.11.8

### Fixed

- Ship and verify OpenClaw's official Codex provider plugin during Computer
  bootstrap, keep the generated gateway config enabling `codex` and
  `codex-supervisor`, and self-heal missing installs before gateway startup so
  ChatGPT subscription device-code linking works on new Computers without
  manual plugin installation.
- Apply Tinyhat plugin/default-skill package updates from platform commands,
  persist and repost package-apply outcomes across restarts, restart the
  gateway after successful package updates, and verify default skill files
  before persisting the updated plugin source override.

## 0.11.7

### Fixed

- Restore ChatGPT/Codex subscription linking on OpenClaw 2026.6.1 by using
  the current `openai` device-code provider, normalizing legacy
  `openai-codex:*` OAuth profile metadata to `openai:*`, and writing the
  non-secret auth profile selection that OpenClaw's native model route needs.
- Preserve legacy subscription profiles during migration, including
  provider/profile keyed metadata such as `order`, `usageStats`, and
  `lastGood`, while keeping token fields local to the Computer.
- This release pairs with Tinyhat plugin `v0.4.4`, which updates the packaged
  subscription guidance to match the current OpenAI provider route.

## 0.11.6

### Fixed

- Reserve 20,000 tokens of reply headroom in generated OpenClaw config so
  auto-compaction can recover turns instead of failing immediately after
  context compaction.
- This release contains the runtime-side fix from
  tinyloophub/tinyhat--runtimes--openclaw#42. No Tinyhat plugin release is
  required.

## 0.11.5

### Fixed

- Stop writing provider-level `agentRuntime` pins in generated OpenClaw config
  so OpenClaw 2026.5.22 can select its embedded runtime harness while Tinyhat
  continues to provide model package settings, provider secrets, and the
  8192-token OpenRouter completion cap.
- This release pairs with the Tinyloop payment-onboarding support in
  tinyloophub/tinyloop#618. No Tinyhat plugin release is required.

## 0.11.4

### Changed

- Publish another patch runtime release so production Tinyhat Software update
  flows can exercise upgrades after the plugin-update persistence fix, without
  changing runtime behavior.

## 0.11.3

### Fixed

- Persist the Tinyhat plugin source selected by an in-place component update so
  later gateway rebinds and supervisor restarts keep the upgraded plugin
  instead of reinstalling the VM's original boot-pinned plugin ref.

## 0.11.2

### Changed

- Publish another patch runtime release so production Tinyhat Software update
  flows can exercise upgrades from v0.11.1 without changing runtime behavior.

## 0.11.1

### Changed

- Publish a patch runtime release so production Tinyhat Software update flows
  can exercise runtime upgrades from the deployed catalog without changing
  runtime behavior.

## 0.11.0

### Added

- Publish a sample minor runtime release so Tinyhat Software update flows can
  exercise upgrading the runtime to a newer release without changing runtime
  behavior.

## 0.10.7

### Fixed

- Emit the OpenClaw runtime policy on each model provider entry instead of
  `agents.defaults.agentRuntime`, which OpenClaw 2026.5.28 rejects. Newly
  provisioned Computers on OpenClaw 2026.5.28 now pass config validation and
  complete gateway startup.
- Fast-fail when the OpenClaw gateway log shows a terminal startup error
  such as config validation failure, invalid input, or doctor-fix prompts,
  replacing the generic 90-second startup timeout with a useful diagnostic.

## 0.10.6

### Fixed

- Cap OpenRouter completion tokens at 8192 on every model-catalog entry
  so Kimi-style high-context routes do not request the provider's full
  context window as output tokens (tinyloophub/tinyloop#567).

## 0.10.5

### Fixed

- On redelivery, repost the component-update result using the cached applied_versions from the persisted state instead of recomputing them live, so the reported result stays faithful to what was applied (especially across a restart or for a failed component).

## 0.10.4

### Fixed

- Make the component-update dedupe-state path stable across a supervisor restart (independent of dev-only env), so the post-restart process finds the persisted unreported result and reposts it instead of re-running the update.

## 0.10.3

### Fixed

- Dedupe the component-update command after ack so a redelivered command does not re-run the update a second time.

## 0.10.2

### Added

- Handle the `update_component` heartbeat command — update the Tinyhat runtime, Tinyhat plugin, and OpenClaw framework in place to a target release and report the applied versions.

## 0.10.1

### Added

- Report installed component versions (Tinyhat runtime, Tinyhat plugin, and OpenClaw framework) on each heartbeat so the platform can show what a Computer is actually running.

## 0.10.0

### Added

- Recognize a new `chatgpt_subscription` value on the `llm_auth_mode`
  field of `/me/binding`. When the binding is in subscription mode AND
  an `openai-codex:*` OAuth profile is present on disk under the
  per-agent auth store, the supervisor writes a subscription-mode
  `openclaw.json` for that agent: `models.<agent>` is set to
  `openai/gpt-5.5` (configurable via the binding's `llm_model_ref`),
  the `pi` `agentRuntime` pin is dropped so OpenClaw auto-selects the
  native Codex app-server harness, the OpenAI-API-key SecretRef is
  omitted (the OAuth profile owns auth), and a cross-provider
  fallback to OpenRouter is configured when the binding still carries
  one — covers per-account Codex rate-window hits without taking the
  Computer offline. Non-subscription Computers keep today's `pi` +
  OpenRouter path unchanged.
- New `start_chatgpt_link` heartbeat command handler. When the
  platform emits this command, the supervisor spawns
  `openclaw models auth login --provider openai-codex --device-code`
  inside a PTY (the CLI rejects non-TTY stdin even with
  `--device-code`), reads stdout, regex-matches the `URL:` + `Code:`
  lines from OpenClaw's device-code panel, and POSTs the URL +
  9-character user code back via the existing
  `/me/subscription-link-result` endpoint so the Mini App / chat tool
  can render them. The subprocess keeps polling auth.openai.com in
  the background; on success or failure the supervisor POSTs exactly
  one terminal status (`linked` or `failed`) and falls back to the
  on-disk auth-profile check as a belt-and-braces success signal.
- New `read_chatgpt_subscription_profile()` and
  `wipe_chatgpt_subscription_profile()` helpers that read / atomically
  edit `${OPENCLAW_STATE_DIR}/agents/<agentId>/agent/auth-profiles.json`.
  The wipe preserves any non-`openai-codex` profiles in the file
  (`.tmp` + rename, mode `0600` on the replacement).

### Changed

- The watchdog's `_binding_signature()` now includes `llm_auth_mode`
  + `llm_model_ref` so a `platform_credits` → subscription flip on
  the same binding triggers a rebind. Owner-identity changes are
  tracked separately on `_owner_identity_signature()` so a mode flip
  for the same owner triggers a rebind without wiping the OAuth
  credential they just linked.
- The watchdog's owner-release path is now a single
  `_wipe_on_owner_release(reason)` helper that performs three
  operations in order: bump the binding generation, SIGTERM all
  in-flight subscription-link CLI subprocesses, then wipe the
  auth-profiles file. Wired into `assigned=false`, owner-identity-
  change, and Phase B cold-start branches.

### Fixed

- Cross-owner credential-leak guard: a monotonically-increasing
  `_binding_generation` counter + `_subscription_link_active_workers`
  registry let the watchdog cancel in-flight device-code workers
  before a late OAuth-profile write can land in the next owner's
  auth store. The dispatcher captures `starting_generation`
  synchronously and pre-registers the worker with `pid=None` so a
  release that fires in the dispatch → thread-start gap is still
  observable; the worker's pre-fork + main-loop supersession checks
  exit silently (without posting `linked`/`failed`) when the
  supervisor has moved past their captured generation. Defensive
  late re-wipe inside the worker covers the SIGTERM-vs-CLI-write
  race in the brief unregistered-pid window.
- Quick-exit handling: when the device-code CLI exits before printing
  URL + code (broken `openclaw_bin`, device-code disabled on the
  user's ChatGPT account, immediate provider error), the supervisor
  now POSTs exactly one terminal `failed` result with a non-secret
  diagnostic (security-settings hint + child exit code) instead of
  silently leaving the platform row stuck in `pending` forever.
- Phase B's binding-poll loop runs `_wipe_on_owner_release` on the
  first `assigned=false` observation per Phase B entry —
  unconditionally, not gated on profile presence — so a device-code
  worker that's still polling but hasn't yet written a profile gets
  cancelled before the user approves.
- The dispatcher now captures the binding generation synchronously
  (before `Thread.start()`) and passes it into the worker as an
  explicit kwarg. The worker no longer re-captures inside the
  thread, closing the post-fork race where owner-release could fire
  in the dispatch → in-worker-capture gap and the worker would stamp
  the already-bumped generation as its own.

### Coupled platform / plugin work

This release wires the runtime side of a slice that spans two
companion repos. Both must ship together for end-to-end behavior:

- Tinyloop platform PR — adds the Computer-callable
  `/me/subscription-link/{start,status,revert}` routes the chat-tool
  body calls, plus the model-auth state on the Computer row.
- `tinyhat-ai/tinyhat` plugin PR — adds the
  `tinyhat_open_chatgpt_subscription_link` and
  `tinyhat_revert_to_platform_credits` chat tools that drive the
  three routes above. The plugin itself never spawns a subprocess
  (OpenClaw's plugin install rejects `child_process`); the
  subprocess work lives here in the runtime.

The 0.10.0 supervisor is backward-compatible with bindings that omit
`llm_auth_mode`: those continue on the existing `pi` + OpenRouter
path unchanged.

## 0.9.1

### Fixed

- Mirror user-managed runtime secrets into `openclaw.json`'s `env`
  block so OpenClaw's `applyConfigEnvVars` populates the gateway
  `process.env` (and therefore the bash tool's child shells) at boot.
  Previously the apply path only wired `OPENAI_API_KEY` into a
  `models.providers.openai.apiKey` SecretRef and registered the file
  provider, leaving non-OpenAI keys like `EXA_API_KEY` reachable to
  OpenClaw's SecretRef snapshot but absent from the agent shell's
  environment. `OPENAI_API_KEY` keeps the SecretRef path and
  `OPENROUTER_API_KEY` is preserved as binding-managed; everything
  else lands in `config["env"]` as plaintext, matching the existing
  OpenRouter pattern. `apply_runtime_secret_map` now signals a gateway
  rebind only when the env block actually changed; OpenAI-only edits
  still resolve through `openclaw secrets reload` without a restart.

## 0.9.0

### Changed

- Install the Tinyhat OpenClaw plugin from the public repo/ref passed
  by the platform provisioning manifest instead of vendoring the plugin
  implementation inside the runtime repo. The runtime now owns only
  plugin pinning/install and keeps tool/default-skill implementation in
  `tinyhat-ai/tinyhat`.

## 0.8.0

### Added

- Consume Tinyhat's OpenRouter model package when writing OpenClaw
  config. Paid bindings now expose the enabled model catalog, use the
  package default as the primary model, and include the cheap model as
  fallback for the default paid role.

### Fixed

- Treat `openrouter_model_package` changes as binding-signature changes
  so the supervisor rewrites `openclaw.json` after a platform-side
  package update or rebind.
- Keep no-credit OpenRouter bindings isolated on the free-demo model
  instead of inheriting the paid catalog.

## 0.7.2

### Added

- Let the local dev container optionally join Tailscale in userspace
  networking mode with Tailscale SSH enabled, so a dev Computer can be
  reached by SSH / the managed terminal like a managed cloud Computer.
  The entrypoint still starts the supervisor as the unprivileged
  `tinyhat` user when private access is disabled.

### Fixed

- Mirror production private-access bootstrap status reporting in the
  dev entrypoint. It now writes `ready`, `error`, or `config_missing`
  status JSON before the supervisor starts, allowing heartbeats to
  report Tailscale readiness or diagnostics back to Tinyhat.

## 0.7.1

### Fixed

- Treat OpenClaw's first-save "secrets runtime snapshot is not active"
  reload response as a fast synced runtime-secret apply after the
  supervisor has written the Tinyhat file provider and OpenClaw config.
  This lets a newly saved Computer runtime secret become available
  without forcing the admin to replace the value or blocking heartbeat
  processing through the gateway-settle retry window.
- Keep retrying `openclaw secrets reload` through the slow initial
  gateway settle window so a first save immediately after activation
  does not fail before OpenClaw finishes provider prewarm.

## 0.7.0

### Changed

- `bootstrap.sh` now owns generic Computer provisioning after the
  platform startup script clones this repo. It installs base OS
  packages, Node.js, the requested OpenClaw package, and optional
  Tailscale private access before starting the supervisor. Tinyloop's
  GCE startup script can stay focused on cloning the runtime repo and
  passing per-Computer config/auth material.
- The runtime bootstrap now fails loudly if OpenClaw cannot be
  installed or found, avoids an `openclaw@latest` fallback when the
  platform does not pass a framework spec, and keeps one-time
  Tailscale auth keys out of the process list by using an auth-key
  file.

## 0.6.0

### Added

- Native `/tinyhat_computer` Telegram command and matching
  `tinyhat_open_manage_computer_link` tool. Both return a Manage
  Computer Telegram Mini App button for the assigned Computer and
  never expose a management or terminal URL in message text.

## 0.5.0

### Added

- MIT license so operators can use, copy, modify, and redistribute the
  public runtime payload with clear permission.
- Heartbeats now include non-secret private-access diagnostics when
  the platform bootstrap enrolled Tailscale on the Computer. The
  report contains provider/state/node/IP diagnostics only, never auth
  keys or terminal data.

## 0.4.0

### Added

- Bundled `tinyhat` OpenClaw tool plugin. The supervisor
  installs it before gateway startup and enables it in
  `openclaw.json`.
- Agent-callable credential helpers:
  `tinyhat_list_runtime_secrets`,
  `tinyhat_request_runtime_secret`, and the `/tinyhat_secrets`
  skill-command dispatcher. These tools return secret metadata and
  Mini App add-secret links only; they never return secret values.

## 0.3.0

### Added

- Heartbeat-delivered `apply_config` command handling. The
  supervisor now pulls the latest Computer-scoped runtime secret
  map, writes `/etc/openclaw/tinyhat-secrets.json` with mode
  `0600`, syncs OpenClaw file SecretRefs, runs
  `openclaw secrets reload --json`, and posts the apply result back
  to Tinyhat.
- Rapid saves coalesce because the supervisor pulls the latest
  revision at apply time, not the revision that happened to be in
  the heartbeat payload.
- Failed applies record diagnostics and are not retried locally on
  every heartbeat until Tinyhat issues a newer desired revision.

### Changed

- OpenClaw config now includes a Tinyhat file SecretRef provider.
  When `OPENAI_API_KEY` exists in the runtime secret map, the
  supervisor wires `models.providers.openai.apiKey` to the file
  pointer `/OPENAI_API_KEY`.
- The dev Docker image keeps running as the non-root `tinyhat` user
  while chowning `/etc/openclaw` so the local harness exercises the
  same Tinyhat SecretRef file path as production.

## 0.2.0

### Added

- **Dev mode** for running the supervisor against a dev backend
  without GCE metadata, without `systemd`, and without root-owned
  `/etc` writes. Enabled with `TINYHAT_DEV_RUNTIME=1` (off by
  default; production behaviour is unchanged).
  - The supervisor reads `TINYHAT_PLATFORM_BASE_URL` /
    `TINYHAT_BACKEND_AUDIENCE` from the environment in dev mode
    and never contacts the GCE metadata server.
  - The supervisor sends a constant marker bearer in dev mode. The
    platform's `computer_identity_verifier` ignores the bearer
    body when its dev short-circuit is configured (only honoured
    under `ENV=development`).
  - The OpenClaw gateway runs as a subprocess of the supervisor
    instead of as a `systemd` unit; the health probe tails a flat
    log file under `$TINYHAT_RUNTIME_HOME` instead of querying
    `journalctl`.
  - `openclaw.json`, the workspace, and the gateway log file live
    under `$TINYHAT_RUNTIME_HOME` (default still
    `/var/lib/tinyhat-openclaw`); the dev Dockerfile points it at
    an unprivileged user's home dir.
- `dev/Dockerfile` — local-dev container image based on
  `node:22-slim` (OpenClaw `>=22.19` engine floor) + `openclaw@latest`
  + the supervisor, running as a non-root user. Runs
  `openclaw --version` immediately after install so a future Node /
  OpenClaw engine mismatch fails the build instead of the first
  `docker run`.
- `dev/README.md` — what dev mode does, the trust boundary, and
  how to build/run the container.

### Notes on the gateway subprocess argv

The dev gateway subprocess invokes `openclaw gateway run` with the
same loopback / no-auth / no-tailscale flags the prod systemd unit
uses. It does **not** pass `--config <path>` because the OpenClaw
CLI does not accept that flag on `gateway run`; the config path is
read from the `OPENCLAW_CONFIG_PATH` environment variable (set on
the subprocess and on the prod systemd unit alike).

### Unchanged

- The state-machine, OpenClaw config shape, rebind watchdog, and
  every production code path are identical to 0.1.0. Dev mode
  swaps four well-bounded subprocess / metadata calls; everything
  the platform sees on the wire is the same shape.

## 0.1.0

- Initial public release. Supervisor + bootstrap installer +
  framework config writer for OpenClaw, hosted standalone so each
  Tinyhat Computer boots from an explicit ref/tag/SHA.
