"""Manifest unit — running versions + the box's own desired-state record.

Moved readers from ``supervisor.py`` (see
``tinyhat_cli/extraction_map.json``) plus the new as-known-on-box
``manifest show|drift`` assembly.

The box holds **no platform-current desired manifest**: ``/me/binding``
carries identity/config only. What it does hold:

- creation-time specs in ``/etc/tinyhat/runtime.env`` (plugin repo
  url/ref, platform base URL, runtime user/group) — mtime is the spec's
  "as-of";
- a durable plugin-source override written by an in-place plugin
  component update (``_tinyhat_plugin_source`` resolves the precedence);
- the last-**acked** ``update_component`` record (revision, status,
  the component versions resolved right after that update applied) —
  absent until a first update is acked.

So ``manifest drift`` is honest by construction: its JSON always
carries ``desired_source: "on_box_last_known"``, ``desired_staleness``,
and ``admin_drift_authoritative: true``, and the human output prints
the same caveat. The platform/admin projection stays the authoritative
drift verdict.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any

from tinyhat_cli._facade import supervisor_module as _sup

log = logging.getLogger("tinyhat-supervisor")

# Keys bootstrap.sh writes into /etc/tinyhat/runtime.env. Unknown keys
# are reported by NAME only — their values are never echoed.
RUNTIME_ENV_KNOWN_KEYS = (
    "TINYHAT_BACKEND_AUDIENCE",
    "TINYHAT_PLATFORM_BASE_URL",
    "TINYHAT_PLATFORM_PLUGIN_REPO_URL",
    "TINYHAT_PLATFORM_PLUGIN_REPO_REF",
    "TINYHAT_OPENCLAW_RUNTIME_USER",
    "TINYHAT_OPENCLAW_RUNTIME_GROUP",
    "TINYHAT_FRAMEWORK_INSTALL_SPEC",
)


# ── moved readers ────────────────────────────────────────────────────


def _read_runtime_repo_version() -> str:
    """Version string for this runtime checkout (the repo-root ``VERSION``).

    Returns ``""`` when the file is missing so the caller omits the
    runtime version rather than reporting a placeholder.
    """
    sup = _sup()
    version_path = os.path.join(sup.runtime_dir(), "VERSION")
    try:
        with open(version_path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _read_runtime_git_sha() -> str:
    """Full git SHA of this runtime checkout, or ``""`` when not a git tree.

    The production Computer clones the runtime repo at a pinned ref, so a
    ``rev-parse HEAD`` resolves the exact commit. A non-git deployment
    (e.g. a tarball drop) returns ``""`` and the caller sends a null sha.
    """
    sup = _sup()
    try:
        result = subprocess.run(
            ["git", "-C", sup.runtime_dir(), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _read_installed_plugin_marker() -> dict:
    """Read the Tinyhat plugin install marker (written at install time).

    Shape mirrors ``ensure_tinyhat_plugin_installed``'s payload:
    ``{repo_url, repo_ref, resolved_commit_sha, version}``. Returns an
    empty dict when the marker is missing or unreadable.
    """
    sup = _sup()
    marker_path = sup._tinyhat_plugin_marker_path()
    try:
        with open(marker_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_component_versions() -> dict:
    """Best-effort snapshot of installed component versions for the heartbeat.

    Returns the shape the platform ingests on ``POST /me/heartbeat``::

        {"runtime":   {"version": <str|None>, "sha": <str|None>},
         "plugin":    {"version": <str|None>, "sha": <str|None>},
         "framework": {"version": <str|None>, "sha": None}}

    Each component is resolved under its own ``try``/``except`` so one
    failing source never suppresses the others, and the whole function
    degrades to ``{}`` on any unexpected error — the heartbeat must never
    throw because version collection failed. A component is omitted
    entirely when neither its version nor its sha can be resolved; the
    platform then falls back to its provisioning manifest for that
    component.
    """
    sup = _sup()
    components: dict[str, dict[str, Any]] = {}
    try:
        # runtime: this repo's VERSION file + the checkout's git SHA.
        try:
            runtime_version = sup._read_runtime_repo_version()
            runtime_sha = sup._read_runtime_git_sha()
            if runtime_version or runtime_sha:
                components["runtime"] = {
                    "version": runtime_version or None,
                    "sha": runtime_sha or None,
                }
        except Exception as exc:
            log.warning("could not collect runtime component version: %s", exc)

        # plugin: read from the install marker the plugin installer wrote.
        # A "unknown" version (no package.json/version.txt at install) is
        # treated as not-reported so the platform uses its manifest value,
        # while the real resolved sha is still surfaced.
        try:
            marker = sup._read_installed_plugin_marker()
            plugin_version = str(marker.get("version") or "").strip()
            if plugin_version.lower() == "unknown":
                plugin_version = ""
            plugin_sha = str(marker.get("resolved_commit_sha") or "").strip()
            if plugin_version or plugin_sha:
                components["plugin"] = {
                    "version": plugin_version or None,
                    "sha": plugin_sha or None,
                }
        except Exception as exc:
            log.warning("could not collect plugin component version: %s", exc)

        # framework: OpenClaw npm package version. No git sha for an npm pkg.
        try:
            framework_version = sup._read_openclaw_framework_version()
            if framework_version:
                components["framework"] = {
                    "version": framework_version,
                    "sha": None,
                }
        except Exception as exc:
            log.warning("could not collect framework component version: %s", exc)
    except Exception as exc:  # pragma: no cover - belt and suspenders
        log.warning("component version collection failed entirely: %s", exc)
        return {}
    return components


def _component_update_state_path() -> str:
    """Resolve the dedupe-state file path, stable across a supervisor restart.

    The whole point of the persisted ``reported`` record is that the process
    started AFTER a runtime self-update restart (systemd restart / ``os.execv``)
    reads back the same file the pre-restart process wrote. That guarantee only
    holds if this function returns the SAME absolute path before and after the
    restart, so the path must not depend on anything that changes between boots
    (the process cwd, or a location inside the runtime checkout that the
    in-place re-checkout can move/erase).

    Two hardening rules make that explicit rather than implicit:

    - The result is always absolutized, so the returned path never depends on
      the cwd the supervisor happens to be launched with.
    - The ``TINYHAT_COMPONENT_UPDATE_STATE_PATH`` override stays honoured for
      explicit operator control, but ONLY when it points outside the runtime
      checkout dir. An override that resolves INSIDE ``runtime_dir()`` is the
      exact footgun a reviewer flagged: the runtime self-update checks the repo
      out in place, so a state file under that dir is not guaranteed to survive
      the restart. In that case we warn and fall back to the fixed default
      (a per-box absolute path outside the checkout) so the dedupe guarantee is
      preserved instead of silently voided.

    See tinyloophub/tinyloop#562 for the platform side of this contract.
    """
    sup = _sup()
    default_path = os.path.abspath(
        os.path.join(sup.openclaw_state_dir(), "component-update-state.json")
        if sup._dev_mode()
        else sup._DEFAULT_COMPONENT_UPDATE_STATE_PATH.strip()
    )
    override = (os.environ.get("TINYHAT_COMPONENT_UPDATE_STATE_PATH") or "").strip()
    if not override:
        return default_path

    override_abs = os.path.abspath(override)
    checkout_dir = os.path.abspath(sup.runtime_dir())
    # ``os.path.commonpath`` raises on e.g. mixed drives; treat any failure as
    # "not inside the checkout" and trust the operator's explicit absolute path.
    try:
        inside_checkout = (
            os.path.commonpath([override_abs, checkout_dir]) == checkout_dir
        )
    except ValueError:
        inside_checkout = False
    if inside_checkout:
        log.warning(
            "TINYHAT_COMPONENT_UPDATE_STATE_PATH=%s resolves inside the runtime "
            "checkout dir (%s); a runtime self-update re-checks-out that dir in "
            "place, so the dedupe state would not survive the restart. Falling "
            "back to the restart-stable default %s.",
            override_abs,
            checkout_dir,
            default_path,
        )
        return default_path
    return override_abs


def _read_component_update_state() -> dict:
    """Read the per-revision component-update dedupe record.

    Returns an empty dict when the file is missing or unreadable so a first
    update (or a wiped box) is never blocked.
    """
    sup = _sup()
    try:
        with open(sup._component_update_state_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ── new as-known-on-box assembly ─────────────────────────────────────


def runtime_env_file_path() -> str:
    """The creation-time spec file (``/etc/tinyhat/runtime.env``).

    ``TINYHAT_RUNTIME_ENV_FILE`` overrides for tests/dev harnesses, in
    the same style as the other control-plane path helpers.
    """
    configured = (os.environ.get("TINYHAT_RUNTIME_ENV_FILE") or "").strip()
    if configured:
        return configured
    return _sup()._DEFAULT_RUNTIME_ENV_FILE


def _file_mtime_unix(path: str) -> int | None:
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return None


def read_creation_specs() -> dict[str, Any]:
    """Parse the creation-time spec file, values for known keys only.

    Unknown keys are listed by name (never value) so an operator sees
    that something extra is present without this unit echoing content
    it does not understand.
    """
    path = runtime_env_file_path()
    spec: dict[str, Any] = {
        "path": path,
        "present": False,
        "mtime_unix": None,
        "values": {},
        "unknown_keys": [],
    }
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return spec
    spec["present"] = True
    spec["mtime_unix"] = _file_mtime_unix(path)
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in RUNTIME_ENV_KNOWN_KEYS:
            spec["values"][key] = value.strip()
        else:
            spec["unknown_keys"].append(key)
    return spec


def _normalize_ref(value: Any) -> str:
    """Comparison form for refs/versions.

    Strips whitespace, a full-ref prefix (``refs/tags/`` /
    ``refs/heads/``), and a leading ``v``. Components report bare
    versions (``0.2.2``) while desired refs come in tag shapes the
    installer accepts — ``v0.2.2`` or the full ``refs/tags/v0.2.2`` —
    and comparing without normalization renders a correctly tag-pinned
    Computer as drifted.
    """
    text = str(value or "").strip()
    for prefix in ("refs/tags/", "refs/heads/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.lower().startswith("v") and any(ch.isdigit() for ch in text[1:2]):
        return text[1:]
    return text


def _refs_match(desired: str, *candidates: Any) -> bool:
    desired_norm = _normalize_ref(desired)
    if not desired_norm:
        return False
    for candidate in candidates:
        candidate_norm = _normalize_ref(candidate)
        if not candidate_norm:
            continue
        if desired_norm == candidate_norm:
            return True
        # Allow short-SHA pins to match the full resolved SHA.
        if (
            len(desired_norm) >= 7
            and candidate_norm.startswith(desired_norm)
            and all(ch in "0123456789abcdef" for ch in desired_norm.lower())
        ):
            return True
        if (
            len(candidate_norm) >= 7
            and desired_norm.startswith(candidate_norm)
            and all(ch in "0123456789abcdef" for ch in candidate_norm.lower())
        ):
            return True
    return False


def desired_staleness(creation_spec: dict, last_acked: dict, *, now: int) -> dict:
    """How stale the box's desired-state knowledge is — always reported."""
    creation_mtime = creation_spec.get("mtime_unix")
    acked_mtime = last_acked.get("mtime_unix")
    block: dict[str, Any] = {
        "creation_spec_mtime_unix": creation_mtime,
        "creation_spec_age_seconds": (
            max(0, now - creation_mtime) if isinstance(creation_mtime, int) else None
        ),
        "last_acked_update_mtime_unix": acked_mtime,
        "last_acked_update_age_seconds": (
            max(0, now - acked_mtime) if isinstance(acked_mtime, int) else None
        ),
        "last_acked_revision": last_acked.get("last_revision"),
    }
    if last_acked.get("present"):
        block["summary"] = (
            "desired refs are the box's last-acked platform update "
            f"(revision {last_acked.get('last_revision')}); newer platform "
            "intent may exist that this box has not seen"
        )
    elif creation_spec.get("present"):
        block["summary"] = (
            "desired refs are creation-time specs; no platform component "
            "update has been acked on this box"
        )
    else:
        block["summary"] = (
            "no on-box desired-state record found (no creation spec file, "
            "no acked update)"
        )
    return block


def _last_acked_update_block() -> dict[str, Any]:
    sup = _sup()
    path = sup._component_update_state_path()
    state = sup._read_component_update_state()
    block: dict[str, Any] = {
        "present": bool(state),
        "path": path,
        "mtime_unix": _file_mtime_unix(path) if state else None,
        "last_revision": state.get("last_revision"),
        "status": state.get("status"),
        "reported": state.get("reported"),
        "applied_versions": state.get("applied_versions") or None,
    }
    return block


def _active_plugin_source() -> dict[str, Any]:
    sup = _sup()
    override = sup._read_tinyhat_plugin_source_override()
    if override is not None:
        override_path = sup._tinyhat_plugin_source_override_path()
        return {
            "repo_url": override[0],
            "repo_ref": override[1],
            "origin": "component_update_override",
            "override_path": override_path,
            "override_mtime_unix": _file_mtime_unix(override_path),
        }
    repo_url, repo_ref = sup._tinyhat_plugin_source()
    if (os.environ.get(sup.TINYHAT_PLUGIN_REPO_REF_ENV) or "").strip():
        origin = "creation_spec"
    else:
        # The daemon sees the creation specs as process env (systemd
        # loads runtime.env via EnvironmentFile); a CLI shell does not.
        # Fall back to the spec FILE so both contexts report the same
        # desired plugin source.
        origin = "default"
        spec_values = read_creation_specs().get("values") or {}
        file_ref = str(spec_values.get("TINYHAT_PLATFORM_PLUGIN_REPO_REF") or "").strip()
        if file_ref:
            repo_ref = file_ref
            repo_url = (
                str(spec_values.get("TINYHAT_PLATFORM_PLUGIN_REPO_URL") or "").strip()
                or repo_url
            )
            origin = "creation_spec"
    return {
        "repo_url": repo_url,
        "repo_ref": repo_ref,
        "origin": origin,
        "override_path": None,
        "override_mtime_unix": None,
    }


def manifest_show(*, now: int | None = None) -> dict[str, Any]:
    """The full as-known-on-box manifest picture (no verdicts)."""
    sup = _sup()
    now = int(time.time()) if now is None else now
    creation_spec = read_creation_specs()
    last_acked = _last_acked_update_block()
    return {
        "running": sup.collect_component_versions(),
        "creation_spec": creation_spec,
        "active_plugin_source": _active_plugin_source(),
        "last_acked_update": last_acked,
        "desired_source": "on_box_last_known",
        "admin_drift_authoritative": True,
        "desired_staleness": desired_staleness(creation_spec, last_acked, now=now),
    }


def _component_drift_row(
    component: str,
    running: dict[str, Any],
    desired_ref: str | None,
    desired_origin: str | None,
    note: str | None = None,
    extra_candidates: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """One component verdict.

    ``extra_candidates`` carries additional running-side identities the
    desired ref may legitimately equal — for the plugin, the install
    marker's ``repo_ref`` (the exact ref the installer was given), so a
    box installed from the very ref its desired record names is
    ``in_sync`` even when the ref shape matches neither the bare
    version nor the SHA.
    """
    row: dict[str, Any] = {
        "running_version": (running.get(component) or {}).get("version"),
        "running_sha": (running.get(component) or {}).get("sha"),
        "desired_ref": desired_ref,
        "desired_origin": desired_origin,
    }
    if not desired_ref:
        row["verdict"] = "unknown"
        row["note"] = note or (
            "no on-box desired record for this component; the platform/admin "
            "drift verdict is the only authority"
        )
        return row
    if not row["running_version"] and not row["running_sha"]:
        row["verdict"] = "unknown"
        row["note"] = note or "running version could not be resolved on-box"
        return row
    if _refs_match(
        desired_ref, row["running_version"], row["running_sha"], *extra_candidates
    ):
        row["verdict"] = "in_sync"
    else:
        row["verdict"] = "divergent"
    if note:
        row["note"] = note
    return row


def manifest_drift(*, now: int | None = None) -> dict[str, Any]:
    """As-known-on-box drift verdicts (admin verdict stays authoritative)."""
    now = int(time.time()) if now is None else now
    show = manifest_show(now=now)
    running = show["running"]
    last_acked = show["last_acked_update"]
    applied = last_acked.get("applied_versions") or {}
    plugin_source = show["active_plugin_source"]

    components: dict[str, Any] = {}
    # The install marker records the exact ref the installer was given;
    # a desired ref equal to it (tag, branch, or sha shape) is in_sync
    # by construction even when it matches neither version nor SHA.
    marker = _sup()._read_installed_plugin_marker()
    components["plugin"] = _component_drift_row(
        "plugin",
        running,
        plugin_source.get("repo_ref"),
        plugin_source.get("origin"),
        note=(
            None
            if (running.get("plugin") or {}).get("version")
            or (running.get("plugin") or {}).get("sha")
            else "plugin not installed yet (installed on first agent bind)"
        ),
        extra_candidates=(marker.get("repo_ref"),),
    )
    for component in ("runtime", "framework"):
        acked = applied.get(component) if isinstance(applied, dict) else None
        acked_version = (acked or {}).get("version") if isinstance(acked, dict) else None
        acked_sha = (acked or {}).get("sha") if isinstance(acked, dict) else None
        desired_ref = acked_sha or acked_version
        components[component] = _component_drift_row(
            component,
            running,
            str(desired_ref) if desired_ref else None,
            "last_acked_update" if desired_ref else None,
            note=(
                None
                if desired_ref
                else (
                    f"the box holds no desired {component} record (creation "
                    "specs do not persist it; no acked update targeted it)"
                )
            ),
        )

    verdicts = [row["verdict"] for row in components.values()]
    if "divergent" in verdicts:
        drift_detected: bool | None = True
    elif all(verdict == "in_sync" for verdict in verdicts):
        drift_detected = False
    else:
        drift_detected = None

    return {
        "desired_source": "on_box_last_known",
        "admin_drift_authoritative": True,
        "desired_staleness": show["desired_staleness"],
        "components": components,
        "drift_detected": drift_detected,
    }


# ── human renderers ──────────────────────────────────────────────────


def _format_age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "unknown age"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def render_show(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    running = data.get("running") or {}
    lines.append("running components:")
    for component in ("runtime", "plugin", "framework"):
        entry = running.get(component) or {}
        version = entry.get("version") or "?"
        sha = entry.get("sha")
        suffix = f" @ {str(sha)[:12]}" if sha else ""
        lines.append(f"  {component:<10} {version}{suffix}")
    spec = data.get("creation_spec") or {}
    lines.append(f"creation spec: {spec.get('path')}")
    if spec.get("present"):
        for key, value in (spec.get("values") or {}).items():
            lines.append(f"  {key}={value}")
        if spec.get("unknown_keys"):
            lines.append(
                "  (unknown keys present, values not shown: "
                + ", ".join(spec["unknown_keys"])
                + ")"
            )
    else:
        lines.append("  (missing)")
    source = data.get("active_plugin_source") or {}
    lines.append(
        "active plugin source: "
        f"{source.get('repo_url')}@{source.get('repo_ref')} "
        f"(origin: {source.get('origin')})"
    )
    acked = data.get("last_acked_update") or {}
    if acked.get("present"):
        lines.append(
            "last acked platform update: revision "
            f"{acked.get('last_revision')} status={acked.get('status')} "
            f"reported={acked.get('reported')}"
        )
    else:
        lines.append(
            "last acked platform update: none "
            "(desired == creation specs as far as this box knows)"
        )
    staleness = data.get("desired_staleness") or {}
    lines.append(f"staleness: {staleness.get('summary')}")
    lines.append(
        "note: desired refs above are as-known-on-box; the platform/admin "
        "drift verdict is authoritative"
    )
    return lines


def render_drift(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    drift = data.get("drift_detected")
    headline = {
        True: "DRIFT DETECTED (as known on-box)",
        False: "in sync (as known on-box)",
        None: "drift unknown (incomplete on-box desired record)",
    }[drift if drift in (True, False) else None]
    lines.append(headline)
    for component, row in (data.get("components") or {}).items():
        running_version = row.get("running_version") or "?"
        running_sha = row.get("running_sha")
        running_text = running_version + (
            f" @ {str(running_sha)[:12]}" if running_sha else ""
        )
        desired = row.get("desired_ref") or "(no on-box record)"
        lines.append(
            f"  {component:<10} {row.get('verdict', 'unknown'):<9} "
            f"running={running_text} desired={desired}"
        )
        if row.get("note"):
            lines.append(f"             note: {row['note']}")
    staleness = data.get("desired_staleness") or {}
    lines.append(f"staleness: {staleness.get('summary')}")
    age = staleness.get("last_acked_update_age_seconds")
    if age is None:
        age = staleness.get("creation_spec_age_seconds")
    lines.append(f"desired record age: {_format_age(age)}")
    lines.append(
        "note: this verdict uses on-box last-known desired refs only; "
        "the platform/admin drift verdict is authoritative"
    )
    return lines
