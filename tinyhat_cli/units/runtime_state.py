"""Runtime-state unit — the shared local-state projection + platform post.

Moved from ``supervisor.py`` (see ``tinyhat_cli/extraction_map.json``).
The daemon, the platform mirror, and the ``tinyhat`` CLI all consume
this ONE projection path: the daemon writes/post through
``_write_runtime_state``/``_post_runtime_state_to_platform``; the CLI
reads the same local state through ``read_runtime_state`` and renders
the same fields. The CLI never posts (single-writer rule: the daemon
is the only runtime-state poster).

New in the extraction (v0.12.0 M1):

- **Total-payload budgeter** (:func:`budget_runtime_state_payload`) in
  the posting unit. The platform rejects oversized runtime-state posts
  (HTTP 422 at its ingest limit) and the runtime previously had no
  total-size guard — three log sources at 15 × 1,024-char lines can
  exceed the limit and silently cost state freshness. Before POST the
  payload is deterministically trimmed to
  ``RUNTIME_STATE_PLATFORM_POST_MAX_BYTES`` (12,288 = 4 KiB headroom
  under the 16 KiB ingest limit) in a fixed order: oldest log-excerpt
  lines first, then the ``commands`` ring tail, then
  ``capabilities.missing`` (setting ``missing_truncated``). The local
  state file keeps full fidelity; only the POST is budgeted.
- :func:`capability_demotion` — the healthy-demotion rule factored out
  so the daemon's write path and ``tinyhat health``'s live re-check
  apply the identical projection. v0.12.0 M3 extends it beyond the
  load beacon: a declared-vs-registered capability shortfall or a
  framework outside the plugin's declared supported range also demotes
  ``healthy`` (``degraded_workload`` + ``plugin_not_loaded`` /
  ``capability_shortfall``; framework violations report
  ``unsupported_openclaw_version`` — no new primary enum value).
- :func:`lifecycle_block` — coarse boot lifecycle spans (rider).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import subprocess
import time
from collections import deque
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup
from tinyhat_cli.units import command_spool

UNIT_CATEGORY = "diagnostics"

log = logging.getLogger("tinyhat-supervisor")

# 4 KiB headroom under the platform's 16,384-byte runtime-state ingest
# limit, so additive fields and encoding drift never reach the cliff.
RUNTIME_STATE_PLATFORM_POST_MAX_BYTES = 12288

# The `commands` ring keeps the last N command-result summaries in the
# mirrored payload. Small on purpose: the ring answers "what was the
# last mutation and how did it end", not "give me an audit log".
COMMANDS_RING_MAX = 5

_runtime_state_platform_post_cache: dict[str, Any] = {"signature": None, "ts": 0.0}
_runtime_state_identity_cache: dict[str, str] = {}


# ── moved: local state read + small projections ──────────────────────


def read_runtime_state() -> dict[str, Any]:
    sup = _sup()
    path = sup.runtime_state_path()
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - corrupt state must not crash boot
        log.warning("failed to read runtime state from %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _runtime_state_name(state: dict[str, Any]) -> str:
    for key in ("state", "runtime_health", "runtime_state", "health", "primary"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _runtime_state_is_unrecoverable_manual(state: dict[str, Any]) -> bool:
    sup = _sup()
    return (
        sup._runtime_state_name(state) == "unrecoverable_manual"
        or state.get("manual_recovery_required") is True
    )


def _runtime_state_gateway_recovery(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("gateway_recovery")
    policy: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    failures = []
    for item in policy.get("failures") or []:
        if not isinstance(item, dict):
            continue
        try:
            at_unix = int(item.get("at_unix"))
        except (TypeError, ValueError):
            continue
        reason = str(item.get("reason") or "unknown").strip() or "unknown"
        compact = {"at_unix": at_unix, "reason": reason}
        for key in (
            "oom_kill",
            "oom",
            "memory_current_bytes",
            "memory_max_bytes",
            "control_group",
        ):
            if key in item:
                compact[key] = item[key]
        failures.append(compact)
    policy["failures"] = failures
    try:
        policy["hold_down_cycles"] = int(policy.get("hold_down_cycles") or 0)
    except (TypeError, ValueError):
        policy["hold_down_cycles"] = 0
    return policy


def _runtime_state_observed_at(now: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _runtime_health_value(state: str) -> str:
    sup = _sup()
    normalized = str(state or "").strip()
    if normalized in sup.RUNTIME_HEALTH_VALUES:
        return normalized
    return "degraded_workload"


def _reset_runtime_state_platform_post_cache() -> None:
    _runtime_state_platform_post_cache.clear()
    _runtime_state_platform_post_cache.update({"signature": None, "ts": 0.0})


def _runtime_state_platform_post_signature(payload: dict[str, Any]) -> str:
    stable_payload = dict(payload)
    stable_payload.pop("observed_at", None)
    stable_payload.pop("updated_at_unix", None)
    raw = json.dumps(
        stable_payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _runtime_state_event(
    event_type: str,
    detail: str | None,
    *,
    now: int,
) -> dict[str, str]:
    sup = _sup()
    event: dict[str, str] = {
        "type": sup._sanitize_runtime_state_text(event_type, limit=96),
        "at": sup._runtime_state_observed_at(now),
    }
    if detail:
        event["detail"] = sup._sanitize_runtime_state_text(
            detail,
            limit=sup.RUNTIME_STATE_EVENT_DETAIL_MAX_CHARS,
        )
    return event


def _append_runtime_state_event(
    events: list[dict[str, str]],
    event: dict[str, str],
) -> None:
    if events and events[-1].get("type") == event.get("type"):
        merged = dict(events[-1])
        merged.update(event)
        events[-1] = merged
        return
    events.append(event)


def _runtime_state_event_history(
    state: dict[str, Any],
    *,
    event_type: str | None = None,
    detail: str | None = None,
    now: int,
) -> list[dict[str, str]]:
    sup = _sup()
    source = state.get("runtime_events")
    if not isinstance(source, list):
        source = state.get("events")
    raw_events = source if isinstance(source, list) else []
    events: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        raw_type = item.get("type")
        if not isinstance(raw_type, str) or not raw_type.strip():
            continue
        event: dict[str, str] = {
            "type": sup._sanitize_runtime_state_text(raw_type, limit=96)
        }
        raw_at = item.get("at") or item.get("observed_at")
        if isinstance(raw_at, str) and raw_at.strip():
            event["at"] = sup._sanitize_runtime_state_text(raw_at, limit=64)
        raw_detail = item.get("detail") or item.get("message")
        if isinstance(raw_detail, str) and raw_detail.strip():
            event["detail"] = sup._sanitize_runtime_state_text(
                raw_detail,
                limit=sup.RUNTIME_STATE_EVENT_DETAIL_MAX_CHARS,
            )
        key = (
            event["type"],
            event.get("at", ""),
            event.get("detail", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        sup._append_runtime_state_event(events, event)
    if event_type:
        event = sup._runtime_state_event(event_type, detail, now=now)
        key = (
            event["type"],
            event.get("at", ""),
            event.get("detail", ""),
        )
        if key not in seen:
            sup._append_runtime_state_event(events, event)
    return events[-sup.RUNTIME_STATE_MAX_EVENTS :]


# ── moved: platform mirror ───────────────────────────────────────────


def _mark_runtime_state_platform_unreachable(
    payload: dict[str, Any],
    exc: Exception,
) -> None:
    sup = _sup()
    path = sup.runtime_state_path()
    observed_at = str(payload.get("observed_at") or "")
    try:
        now = int(payload.get("updated_at_unix") or time.time())
    except (TypeError, ValueError):
        now = int(time.time())
    if not observed_at:
        observed_at = sup._runtime_state_observed_at(now)
    detail = sup._sanitize_runtime_state_text(
        f"{type(exc).__name__}: {exc}",
        limit=sup.RUNTIME_STATE_EVENT_DETAIL_MAX_CHARS,
    )
    updated = dict(payload)
    platform = updated.get("platform")
    if not isinstance(platform, dict):
        platform = {}
    else:
        platform = dict(platform)
    platform.update(
        {
            "status": "unreachable",
            "last_error_category": "platform_unreachable",
            "last_error": detail,
            "last_error_at": observed_at,
        }
    )
    updated["platform"] = platform
    updated["platform_unreachable"] = True
    updated["runtime_events"] = sup._runtime_state_event_history(
        updated,
        event_type="platform_unreachable",
        detail=detail,
        now=now,
    )
    sup._atomic_write_json(path, updated, mode=0o600)


def _runtime_state_payload_bytes(payload: dict[str, Any]) -> int:
    """Size of the payload exactly as ``post_json`` will encode it."""
    return len(json.dumps(payload).encode("utf-8"))


def budget_runtime_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministically trim a runtime-state payload to the POST budget.

    Trim order (fixed, observable):

    1. oldest log-excerpt lines first — repeatedly drop the oldest line
       from whichever source currently holds the most lines (ties break
       in ``bootstrap`` → ``supervisor`` → ``gateway`` order);
    2. the ``commands`` ring tail — oldest result summaries first;
    3. ``capabilities.missing`` names (from the end), setting
       ``missing_truncated: true``.

    Two defensive levers beyond the contract guarantee the bound even
    on a pathological payload: oldest ``runtime_events`` entries, then
    a hard ``detail`` truncation. A trimmed payload carries
    ``payload_trimmed: true`` so the platform/admin can see budgeting
    happened. The input payload is never mutated — the local state file
    keeps full fidelity.
    """
    max_bytes = RUNTIME_STATE_PLATFORM_POST_MAX_BYTES
    size = _runtime_state_payload_bytes(payload)
    if size <= max_bytes:
        return payload
    trimmed = copy.deepcopy(payload)

    def _log_sources() -> list[list]:
        sources = []
        bootstrap = trimmed.get("bootstrap")
        if isinstance(bootstrap, dict) and isinstance(
            bootstrap.get("log_excerpt_lines"), list
        ):
            sources.append(bootstrap["log_excerpt_lines"])
        for key in ("supervisor", "gateway"):
            block = trimmed.get(key)
            if isinstance(block, dict) and isinstance(block.get("journal"), list):
                sources.append(block["journal"])
        return sources

    # Lever 1: oldest log-excerpt lines first.
    while size > max_bytes:
        sources = [entries for entries in _log_sources() if entries]
        if not sources:
            break
        sources.sort(key=len, reverse=True)
        sources[0].pop(0)
        size = _runtime_state_payload_bytes(trimmed)
    for key in ("supervisor", "gateway"):
        block = trimmed.get(key)
        if isinstance(block, dict) and block.get("journal") == []:
            del block["journal"]
    bootstrap = trimmed.get("bootstrap")
    if isinstance(bootstrap, dict) and bootstrap.get("log_excerpt_lines") == []:
        del trimmed["bootstrap"]

    # Lever 2: the commands ring tail (oldest first).
    commands = trimmed.get("commands")
    while size > max_bytes and isinstance(commands, list) and commands:
        commands.pop(0)
        size = _runtime_state_payload_bytes(trimmed)
    if trimmed.get("commands") == []:
        del trimmed["commands"]

    # Lever 3: capabilities.missing names (mark the truncation).
    capabilities = trimmed.get("capabilities")
    if size > max_bytes and isinstance(capabilities, dict):
        missing = capabilities.get("missing")
        while size > max_bytes and isinstance(missing, list) and missing:
            missing.pop()
            capabilities["missing_truncated"] = True
            size = _runtime_state_payload_bytes(trimmed)

    # Defensive levers beyond the contract: keep the bound even when a
    # payload is pathological in fields the contract order never named.
    events = trimmed.get("runtime_events")
    while size > max_bytes and isinstance(events, list) and events:
        events.pop(0)
        size = _runtime_state_payload_bytes(trimmed)
    if size > max_bytes and isinstance(trimmed.get("detail"), str):
        trimmed["detail"] = trimmed["detail"][:256]
        size = _runtime_state_payload_bytes(trimmed)
    if size > max_bytes:
        log.warning(
            "runtime_state payload still %d bytes after deterministic trim "
            "(budget %d); posting anyway",
            size,
            max_bytes,
        )
    trimmed["payload_trimmed"] = True
    return trimmed


def _post_runtime_state_to_platform(payload: dict[str, Any]) -> bool:
    """Best-effort platform mirror for the local runtime_state_v1 payload."""
    sup = _sup()
    post_payload = payload
    try:
        env_base_url = (os.environ.get("TINYHAT_PLATFORM_BASE_URL") or "").strip()
        if not env_base_url and not sup._gce_metadata_available():
            return False
        if not sup.get_backend_base_url():
            return False
        post_payload = sup.budget_runtime_state_payload(payload)
        now = time.time()
        signature = sup._runtime_state_platform_post_signature(post_payload)
        previous_signature = _runtime_state_platform_post_cache.get("signature")
        previous_ts = float(_runtime_state_platform_post_cache.get("ts") or 0.0)
        if (
            signature == previous_signature
            and now - previous_ts
            < sup.RUNTIME_STATE_PLATFORM_POST_MIN_INTERVAL_SECONDS
        ):
            log.debug("runtime_state platform POST skipped: unchanged payload")
            return False
        _runtime_state_platform_post_cache["signature"] = signature
        _runtime_state_platform_post_cache["ts"] = now
        sup.post_json("/hapi/v1/computers/me/runtime-state", post_payload)
    except Exception as exc:
        _runtime_state_platform_post_cache["signature"] = None
        _runtime_state_platform_post_cache["ts"] = 0.0
        try:
            # The unreachable marker keeps the FULL payload — budgeting
            # only applies to the platform POST, never the local file.
            sup._mark_runtime_state_platform_unreachable(payload, exc)
        except Exception as marker_exc:
            log.warning(
                "runtime_state platform unreachable marker failed: %s",
                marker_exc,
            )
        log.warning("runtime_state platform POST failed: %s", exc)
        return False
    log.info(
        "runtime_state platform POST succeeded: health=%s observed_at=%s",
        post_payload.get("runtime_health"),
        post_payload.get("observed_at"),
    )
    return True


# ── moved: identity ──────────────────────────────────────────────────


def _runtime_computer_id() -> str | None:
    sup = _sup()
    for env_name in (sup.TINYHAT_COMPUTER_ID_ENV, "DEV_AUTO_COMPUTER_ID"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    if sup._dev_mode():
        return None
    if not sup._gce_metadata_available():
        return None
    try:
        return sup._read_metadata_value(sup.METADATA_COMPUTER_ID_KEY, timeout=2) or None
    except Exception as exc:
        log.debug("runtime state computer id metadata unavailable: %s", exc)
        return None


def _gce_instance_id() -> str | None:
    sup = _sup()
    value = (os.environ.get(sup.TINYHAT_GCE_INSTANCE_ID_ENV) or "").strip()
    if value:
        return value
    if sup._dev_mode():
        return None
    if not sup._gce_metadata_available():
        return None
    try:
        return sup._read_metadata_path("instance/id", timeout=2) or None
    except Exception as exc:
        log.debug("runtime state GCE instance id metadata unavailable: %s", exc)
        return None


def _runtime_ref() -> str | None:
    sup = _sup()
    try:
        version = sup._read_runtime_repo_version()
    except Exception:
        version = ""
    try:
        sha = sup._read_runtime_git_sha()
    except Exception:
        sha = ""
    if version and sha:
        return f"{version}@{sha[:12]}"
    if sha:
        return sha
    return version or None


def _reset_runtime_state_identity_cache() -> None:
    _runtime_state_identity_cache.clear()


def _runtime_state_identity() -> dict[str, str | None]:
    sup = _sup()
    identity: dict[str, str | None] = {}
    for key, resolver in (
        ("computer_id", sup._runtime_computer_id),
        ("instance_id", sup._gce_instance_id),
        ("runtime_ref", sup._runtime_ref),
    ):
        value = _runtime_state_identity_cache.get(key)
        if not value:
            resolved = resolver()
            if resolved:
                _runtime_state_identity_cache[key] = resolved
                value = resolved
        identity[key] = value or None
    return identity


# ── moved: health/status projections ─────────────────────────────────


def _runtime_supervisor_status(runtime_health: str) -> str:
    sup = _sup()
    if runtime_health in sup.RUNTIME_HEALTH_VALUES:
        return runtime_health
    return "degraded_workload"


def _gateway_status(
    runtime_health: str,
    *,
    gateway_active: bool | None,
    openclaw_ready: bool | None,
) -> str:
    if runtime_health == "unrecoverable_manual":
        return "unrecoverable_manual"
    if runtime_health == "openclaw_not_ready":
        return "openclaw_not_ready"
    if gateway_active is False:
        return "inactive"
    if openclaw_ready is False:
        return "not_ready"
    return runtime_health


def _gateway_restart_count_window(
    gateway_recovery: dict[str, Any],
    *,
    now: int,
) -> int:
    sup = _sup()
    count = 0
    for item in gateway_recovery.get("failures") or []:
        if not isinstance(item, dict):
            continue
        try:
            at_unix = int(item.get("at_unix"))
        except (TypeError, ValueError):
            continue
        if now - at_unix <= sup.GATEWAY_RECOVERY_FAILURE_WINDOW_SECONDS:
            count += 1
    return count


def _runtime_state_last_error(
    runtime_health: str,
    detail: str,
    *,
    category: str | None,
) -> dict[str, str] | None:
    sup = _sup()
    if runtime_health == "healthy" and not category:
        return None
    safe_category = sup._sanitize_runtime_state_text(
        category or runtime_health,
        limit=sup.RUNTIME_STATE_ERROR_CATEGORY_MAX_LENGTH,
    )
    return {
        "category": safe_category or runtime_health,
        "detail": sup._sanitize_runtime_state_text(detail),
    }


def capability_demotion(
    runtime_health: str,
    plugin_check: dict[str, Any] | None,
    capabilities: dict[str, Any] | None = None,
    framework: dict[str, Any] | None = None,
) -> tuple[str, str | None, str] | None:
    """The shared healthy-demotion rule (daemon write path + ``tinyhat health``).

    An enabled plugin that never loaded, loaded short of its declared
    manifest, or runs under a framework outside its declared supported
    range must not let the runtime report ``healthy``. Only ever
    demotes ``healthy`` — every other state already carries a stronger
    signal. Returns ``(runtime_health, last_error_category, detail)``
    or ``None``; no new primary enum value (v0.12.0 §7):

    - framework out of declared range → ``unsupported_openclaw_version``
      (the value finally means what it says; category matches);
    - plugin not loaded (no fresh beacon) or declared capabilities not
      registered at all → ``degraded_workload`` + ``plugin_not_loaded``;
    - partial capability shortfall → ``degraded_workload`` +
      ``capability_shortfall``.
    """
    if runtime_health != "healthy":
        return None
    if isinstance(framework, dict) and framework.get("framework_in_range") is False:
        installed = framework.get("framework_installed") or "unknown"
        minimum = framework.get("framework_minimum") or "?"
        maximum = framework.get("framework_maximum")
        bound = f">= {minimum}" + (f" <= {maximum}" if maximum else "")
        return (
            "unsupported_openclaw_version",
            "unsupported_openclaw_version",
            f"installed OpenClaw {installed} is outside the tinyhat plugin's "
            f"declared supported range ({bound})",
        )
    if isinstance(plugin_check, dict) and plugin_check.get("load_check") == "not_loaded":
        return (
            "degraded_workload",
            "plugin_not_loaded",
            "tinyhat plugin enabled but not loaded (no fresh load beacon)",
        )
    if isinstance(capabilities, dict) and capabilities.get("status") == "shortfall":
        declared = capabilities.get("declared_tools") or 0
        registered = capabilities.get("registered_tools") or 0
        missing = capabilities.get("missing") or []
        skills_only = bool(missing) and all(
            str(name).startswith("skill:") for name in missing
        )
        if declared > 0 and registered == 0 and not skills_only:
            return (
                "degraded_workload",
                "plugin_not_loaded",
                "tinyhat plugin declared capabilities are not registered "
                f"(0 of {declared} tools)",
            )
        mounted = capabilities.get("mounted_skills") or 0
        declared_skills = capabilities.get("declared_skills") or 0
        return (
            "degraded_workload",
            "capability_shortfall",
            "tinyhat plugin capabilities fall short of the declared manifest "
            f"({registered}/{declared} tools, {mounted}/{declared_skills} skills)",
        )
    return None


def lifecycle_block(marks: dict[str, int] | None) -> dict[str, Any] | None:
    """Coarse boot lifecycle spans (v0.12.0 M1 rider).

    Returns ``None`` unless the daemon actually ran its boot phases —
    the ``ready_reported_at_unix`` mark (set only by the binding cycle's
    Phase A) plus at least one other mark. A process that merely
    imported the module or exercised isolated units (tests, the CLI)
    adds nothing to the payload.
    """
    if not isinstance(marks, dict):
        return None
    known = {key: value for key, value in marks.items() if isinstance(value, int)}
    if "ready_reported_at_unix" not in known or len(known) < 2:
        return None
    spans: dict[str, int] = {}
    for span_name, start_key, end_key in (
        ("boot_to_ready_seconds", "supervisor_started_at_unix", "ready_reported_at_unix"),
        ("ready_to_bind_seconds", "ready_reported_at_unix", "binding_acquired_at_unix"),
        ("bind_to_gateway_start_seconds", "binding_acquired_at_unix", "gateway_start_at_unix"),
        (
            "gateway_start_to_gateway_ready_seconds",
            "gateway_start_at_unix",
            "gateway_ready_at_unix",
        ),
    ):
        start = known.get(start_key)
        end = known.get(end_key)
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            spans[span_name] = end - start
    block: dict[str, Any] = {"marks": known}
    if spans:
        block["spans"] = spans
    return block


# ── moved: log excerpts ──────────────────────────────────────────────


def _runtime_state_log_entry(text: Any, *, unit: str | None = None) -> dict[str, str] | None:
    sup = _sup()
    safe_text = sup._sanitize_runtime_state_text(
        text,
        limit=sup.RUNTIME_STATE_LOG_LINE_MAX_CHARS,
    ).strip()
    if not safe_text:
        return None
    entry = {"text": safe_text}
    if unit:
        entry["unit"] = unit
    return entry


def _tail_runtime_log_file(path: str, *, limit: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\r\n") for line in deque(fh, maxlen=limit)]
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - diagnostics must stay best-effort
        log.debug("runtime log tail unavailable for %s: %s", path, exc)
        return []


def _journal_runtime_log_lines(unit: str, *, limit: int) -> list[str]:
    sup = _sup()
    if sup._dev_mode() or not unit:
        return []
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                unit,
                "-n",
                str(limit),
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=sup.RUNTIME_STATE_LOG_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("runtime journal tail unavailable for %s: %s", unit, exc)
        return []
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            log.debug("runtime journal tail failed for %s: %s", unit, detail[:200])
        return []
    return [line for line in (result.stdout or "").splitlines() if line.strip()][
        -limit:
    ]


def _runtime_state_log_entries(
    lines: list[str],
    *,
    unit: str | None = None,
) -> list[dict[str, str]]:
    sup = _sup()
    entries: list[dict[str, str]] = []
    for line in lines[-sup.RUNTIME_STATE_LOG_SOURCE_MAX_LINES :]:
        entry = sup._runtime_state_log_entry(line, unit=unit)
        if entry is not None:
            entries.append(entry)
    return entries


def _gateway_runtime_log_entries() -> list[dict[str, str]]:
    """Gateway tail for runtime-state mirroring.

    Production reads the gateway's systemd journal. The dev container
    has no journald — there the supervisor spawns the gateway directly
    and streams its output to ``openclaw-gateway.log`` (see
    ``_start_openclaw_gateway_dev``), so dev tails that file
    instead. Both paths get the same line/count caps and client-side
    redaction; without this fallback a dev Computer would always
    report an empty gateway excerpt. Dev entries carry no ``unit``
    because they do not come from a systemd unit.
    """
    sup = _sup()
    if sup._dev_mode():
        return sup._runtime_state_log_entries(
            sup._tail_runtime_log_file(
                sup._dev_gateway_log_path(),
                limit=sup.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
            )
        )
    return sup._runtime_state_log_entries(
        sup._journal_runtime_log_lines(
            sup.GATEWAY_SYSTEMD_UNIT,
            limit=sup.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
        ),
        unit=sup.GATEWAY_SYSTEMD_UNIT,
    )


def _runtime_state_recent_log_excerpts() -> dict[str, list[dict[str, str]]]:
    """Collect a small, redacted diagnostic tail for runtime-state mirroring."""
    sup = _sup()
    bootstrap_lines = sup._tail_runtime_log_file(
        sup.runtime_bootstrap_log_path(),
        limit=sup.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
    )
    return {
        "bootstrap": sup._runtime_state_log_entries(bootstrap_lines),
        "supervisor": sup._runtime_state_log_entries(
            sup._journal_runtime_log_lines(
                sup.SUPERVISOR_SYSTEMD_UNIT,
                limit=sup.RUNTIME_STATE_LOG_SOURCE_MAX_LINES,
            ),
            unit=sup.SUPERVISOR_SYSTEMD_UNIT,
        ),
        "gateway": sup._gateway_runtime_log_entries(),
    }


# ── the `commands` ring (spool fold) ─────────────────────────────────


def _ring_entry(record: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    name = record.get("name")
    outcome = record.get("outcome")
    if not isinstance(name, str) or outcome not in ("succeeded", "failed", "timed_out"):
        return None
    entry: dict[str, Any] = {"name": name, "outcome": outcome}
    command_class = record.get("class")
    entry["class"] = command_class if command_class in ("diagnose", "operate") else "operate"
    for key in ("started_at_unix", "finished_at_unix"):
        value = record.get(key)
        if isinstance(value, int):
            entry[key] = value
    for key in ("idempotency_key", "summary"):
        value = record.get(key)
        if isinstance(value, str) and value:
            entry[key] = value
    for key in ("runner_lost", "stale_takeover"):
        if record.get(key) is True:
            entry[key] = True
    return entry


def fold_command_results(
    existing_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Merge the command-result spool into the ``commands`` ring.

    Returns ``(ring, folded_paths, fresh_entries)``: the new
    last-:data:`COMMANDS_RING_MAX` ring (existing state entries +
    spooled records, oldest dropped), the spool paths now represented
    in it, and the entries that were newly folded this call (so the
    write path can mirror stale-takeover events). The daemon prunes the
    paths only after its state write lands; ``tinyhat status`` calls
    this read-only and prunes nothing (single-writer rule: the CLI
    never mutates the daemon's transport).
    """

    def _key(entry: dict[str, Any]) -> tuple[str, Any, Any]:
        return (
            entry.get("idempotency_key") or entry["name"],
            entry.get("finished_at_unix"),
            entry["outcome"],
        )

    ring: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, Any]] = set()
    existing = existing_state.get("commands")
    if isinstance(existing, list):
        for record in existing:
            entry = _ring_entry(record)
            if entry is None or _key(entry) in seen:
                continue
            seen.add(_key(entry))
            ring.append(entry)
    folded_paths: list[str] = []
    fresh_entries: list[dict[str, Any]] = []
    for path, record in command_spool.read_results():
        entry = _ring_entry(record)
        folded_paths.append(path)
        if entry is None or _key(entry) in seen:
            continue
        seen.add(_key(entry))
        ring.append(entry)
        fresh_entries.append(entry)
    ring.sort(key=lambda item: item.get("finished_at_unix") or 0)
    return ring[-COMMANDS_RING_MAX:], folded_paths, fresh_entries


# ── moved: the daemon write path ─────────────────────────────────────


def _write_runtime_state(
    state: str,
    detail: str,
    *,
    config_fingerprint: dict[str, str] | None = None,
    gateway_active: bool | None = None,
    gateway_action: str | None = None,
    openclaw_ready: bool | None = None,
    gateway_recovery: dict[str, Any] | None = None,
    gateway_cgroup: dict[str, Any] | None = None,
    last_error_category: str | None = None,
    event_type: str | None = None,
    event_detail: str | None = None,
) -> None:
    sup = _sup()
    path = sup.runtime_state_path()
    parent = os.path.dirname(path)
    sup._prepare_control_plane_state_dir(parent)
    existing_state = sup.read_runtime_state()
    now = int(time.time())
    runtime_health = sup._runtime_health_value(state)
    safe_detail = sup._sanitize_runtime_state_text(detail)
    safe_last_error_category = (
        sup._sanitize_runtime_state_text(
            last_error_category,
            limit=sup.RUNTIME_STATE_ERROR_CATEGORY_MAX_LENGTH,
        )
        if last_error_category
        else None
    )
    if gateway_recovery is None:
        gateway_recovery = sup._runtime_state_gateway_recovery(existing_state)
    if config_fingerprint is None:
        existing_fingerprint = existing_state.get("config_fingerprint")
        if isinstance(existing_fingerprint, dict):
            config_fingerprint = dict(existing_fingerprint)
    if openclaw_ready is None:
        existing_openclaw = existing_state.get("openclaw")
        if isinstance(existing_openclaw, dict) and isinstance(
            existing_openclaw.get("ready"),
            bool,
        ):
            openclaw_ready = existing_openclaw["ready"]
    if gateway_cgroup is not None:
        gateway_recovery = sup._gateway_recovery_policy_with_cgroup_baseline(
            gateway_recovery,
            gateway_cgroup,
        )
    # Capability contract: an enabled plugin that never loaded, loaded
    # short of its declared manifest, or runs under an unsupported
    # framework must not let the runtime report healthy. Only ever
    # demotes `healthy` — every other state already carries a stronger
    # signal.
    effective_gateway_active = gateway_active
    if effective_gateway_active is None:
        existing_gateway = existing_state.get("gateway")
        if isinstance(existing_gateway, dict) and isinstance(
            existing_gateway.get("active"),
            bool,
        ):
            effective_gateway_active = existing_gateway["active"]
    plugin_check = sup._plugin_load_check(
        existing_state,
        gateway_active=effective_gateway_active,
        now=now,
    )
    capabilities, framework_compat = sup.capability_verification_cached(now=now)
    demotion = sup.capability_demotion(
        runtime_health,
        plugin_check,
        capabilities,
        framework_compat,
    )
    if demotion is not None:
        demoted_health, demoted_category, demotion_detail = demotion
        runtime_health = demoted_health
        safe_detail = sup._sanitize_runtime_state_text(f"{detail}; {demotion_detail}")
        if not safe_last_error_category and demoted_category:
            safe_last_error_category = sup._sanitize_runtime_state_text(
                demoted_category,
                limit=sup.RUNTIME_STATE_ERROR_CATEGORY_MAX_LENGTH,
            )
    runtime_version = ""
    try:
        runtime_version = sup._read_runtime_repo_version()
    except Exception:
        runtime_version = ""
    gateway_payload: dict[str, Any] = {
        "unit": sup.GATEWAY_SYSTEMD_UNIT,
        "status": sup._gateway_status(
            runtime_health,
            gateway_active=gateway_active,
            openclaw_ready=openclaw_ready,
        ),
        "restart_count_window": sup._gateway_restart_count_window(
            gateway_recovery,
            now=now,
        ),
    }
    if gateway_active is not None:
        gateway_payload["active"] = bool(gateway_active)
    if gateway_action:
        gateway_payload["action"] = gateway_action
    identity = sup._runtime_state_identity()
    recent_logs = sup._runtime_state_recent_log_excerpts()
    runtime_events = sup._runtime_state_event_history(
        existing_state,
        event_type=event_type,
        detail=event_detail or safe_detail,
        now=now,
    )
    commands_ring, folded_spool_paths, fresh_command_entries = (
        sup.fold_command_results(existing_state)
    )
    for entry in fresh_command_entries:
        if entry.get("stale_takeover"):
            sup._append_runtime_state_event(
                runtime_events,
                sup._runtime_state_event(
                    "command_lock_stale_takeover",
                    f"{entry.get('name')} normalized to {entry.get('outcome')} "
                    "after its runner was lost",
                    now=now,
                ),
            )
    runtime_events = runtime_events[-sup.RUNTIME_STATE_MAX_EVENTS :]
    payload: dict[str, Any] = {
        "schema": sup.RUNTIME_STATE_SCHEMA,
        "schema_version": 1,
        "computer_id": identity["computer_id"],
        "instance_id": identity["instance_id"],
        "runtime_ref": identity["runtime_ref"],
        "observed_at": sup._runtime_state_observed_at(now),
        "runtime_health": runtime_health,
        "runtime_state": runtime_health,
        "state": runtime_health,
        "detail": safe_detail,
        "updated_at_unix": now,
        "supervisor": {
            "version": runtime_version or None,
            "status": sup._runtime_supervisor_status(runtime_health),
        },
        "manual_recovery_required": runtime_health == "unrecoverable_manual",
        "manual_recovery_marker_path": sup.runtime_state_manual_marker_path(),
        "manual_recovery_clear_marker_path": sup.runtime_state_clear_manual_path(),
        "gateway": gateway_payload,
        "openclaw": {},
        "last_error": sup._runtime_state_last_error(
            runtime_health,
            safe_detail,
            category=safe_last_error_category,
        ),
    }
    if recent_logs["bootstrap"]:
        payload["bootstrap"] = {"log_excerpt_lines": recent_logs["bootstrap"]}
    if recent_logs["supervisor"]:
        payload["supervisor"]["journal"] = recent_logs["supervisor"]
    if recent_logs["gateway"]:
        payload["gateway"]["journal"] = recent_logs["gateway"]
    if openclaw_ready is not None:
        payload["openclaw"]["ready"] = bool(openclaw_ready)
    if plugin_check:
        if framework_compat:
            plugin_check = {**plugin_check, **framework_compat}
        payload["plugin"] = plugin_check
    if capabilities:
        payload["capabilities"] = capabilities
    if config_fingerprint:
        payload["config_fingerprint"] = dict(config_fingerprint)
    if safe_last_error_category:
        payload["last_error_category"] = safe_last_error_category
    if runtime_events:
        payload["runtime_events"] = runtime_events
    if gateway_recovery:
        payload["gateway_recovery"] = gateway_recovery
    if commands_ring:
        payload["commands"] = commands_ring
    lifecycle = sup.lifecycle_block(getattr(sup, "_lifecycle_marks", None))
    if lifecycle is not None:
        payload["lifecycle"] = lifecycle
    sup._atomic_write_json(path, payload, mode=0o600)
    if folded_spool_paths:
        # Folded results are represented in the just-written state (and
        # every later write re-reads it), so the transport can drain.
        command_spool.prune_folded(folded_spool_paths)
    sup._post_runtime_state_to_platform(payload)
