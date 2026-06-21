"""The only tiny_runtime module allowed to invoke OpenClaw commands."""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import paths
from .redaction import redact_json, redact_text


@dataclass(frozen=True)
class AdapterResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json_payload(self) -> dict[str, Any]:
        if not self.stdout.strip():
            return {}
        payload = json.loads(self.stdout)
        if not isinstance(payload, dict):
            raise ValueError("OpenClaw JSON response must be an object")
        return payload

    def public_summary(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": redact_text(self.stdout, limit=1000),
            "stderr": redact_text(self.stderr, limit=1000),
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]

TINYHAT_PLUGIN_ID = "tinyhat"
TINYHAT_PLUGIN_REPO_URL = "https://github.com/tinyhat-ai/tinyhat.git"
TINYHAT_PLUGIN_REPO_REF = "main"
TINYHAT_PLUGIN_MARKER = "tinyhat-plugin.version"


def openclaw_env(
    *,
    state_dir: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, str]:
    resolved_state = state_dir or paths.OPENCLAW_STATE_DIR
    resolved_config = config_path or paths.OPENCLAW_CONFIG_PATH
    base_env = dict(os.environ)
    base_env["PATH"] = f"{paths.BUNDLE_OPENCLAW_BIN}{os.pathsep}{base_env.get('PATH', '')}"
    return {
        **base_env,
        "HOME": str(resolved_state),
        "OPENCLAW_BUNDLE_DIR": str(paths.BUNDLE_OPENCLAW_DIR),
        "OPENCLAW_STATE_DIR": str(resolved_state),
        "OPENCLAW_CONFIG_PATH": str(resolved_config),
    }


def run_openclaw(
    args: Sequence[str],
    *,
    timeout: int | None = 30,
    runner: Runner = subprocess.run,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> AdapterResult:
    argv = ("openclaw", *tuple(args))
    try:
        completed = runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env or openclaw_env(),
            input=input_text,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AdapterResult(command=argv, returncode=127, stdout="", stderr=str(exc))
    return AdapterResult(
        command=argv,
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def spawn_models_auth_login_device_code(
    *,
    stdin: int,
    stdout: int,
    stderr: int,
) -> subprocess.Popen[bytes]:
    """Spawn OpenClaw's official ChatGPT/Codex device-code auth CLI."""
    return subprocess.Popen(
        [
            "openclaw",
            "models",
            "auth",
            "login",
            "--provider",
            "openai",
            "--device-code",
        ],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=openclaw_env(),
        start_new_session=True,
    )


def inspect_plugin(plugin_id: str, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("plugins", "inspect", plugin_id, "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "plugin": redact_json(result.json_payload())}


def install_plugin(source: str, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(
        ("plugins", "install", source, "--force"),
        timeout=120,
        runner=runner,
    )
    if not result.ok:
        return {"state": "failed", "detail": result.public_summary()}
    return {"state": "ready", "detail": result.public_summary()}


def _run_host_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    runner: Runner = subprocess.run,
) -> AdapterResult:
    argv = tuple(args)
    try:
        completed = runner(
            list(argv),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AdapterResult(command=argv, returncode=127, stdout="", stderr=str(exc))
    return AdapterResult(
        command=argv,
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _plugin_checkout_dir() -> Path:
    configured = (os.environ.get("TINYHAT_PLUGIN_CHECKOUT_DIR") or "").strip()
    if configured:
        return Path(configured)
    return paths.OPENCLAW_STATE_DIR / "platform-plugins" / TINYHAT_PLUGIN_ID


def _plugin_marker_path() -> Path:
    return paths.OPENCLAW_STATE_DIR / TINYHAT_PLUGIN_MARKER


def _plugin_version(checkout: Path) -> str:
    package_json = checkout / "package.json"
    if not package_json.exists():
        return "unknown"
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("version") or "unknown")


def _tinyhat_plugin_contract(binding: dict[str, Any]) -> dict[str, Any]:
    platform = binding.get("tinyhat_platform")
    if not isinstance(platform, dict):
        return {}
    plugin = platform.get("plugin")
    return plugin if isinstance(plugin, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _tinyhat_plugin_source(binding: dict[str, Any]) -> dict[str, str | None]:
    plugin = _tinyhat_plugin_contract(binding)
    repo_url = (
        _text(os.environ.get("TINYHAT_PLATFORM_PLUGIN_REPO_URL"))
        or _text(plugin.get("repo_url"))
        or TINYHAT_PLUGIN_REPO_URL
    )
    requested_ref = (
        _text(os.environ.get("TINYHAT_PLATFORM_PLUGIN_REPO_REF"))
        or _text(plugin.get("repo_ref"))
        or _text(plugin.get("requested_ref"))
        or _text(plugin.get("version"))
        or TINYHAT_PLUGIN_REPO_REF
    )
    resolved_sha = _text(plugin.get("resolved_commit_sha")) or None
    checkout_ref = resolved_sha or requested_ref or TINYHAT_PLUGIN_REPO_REF
    return {
        "plugin_id": _text(plugin.get("id")) or TINYHAT_PLUGIN_ID,
        "repo_url": repo_url,
        "requested_ref": requested_ref,
        "resolved_commit_sha": resolved_sha,
        "checkout_ref": checkout_ref,
    }


def _read_tinyhat_plugin_marker() -> dict[str, Any]:
    try:
        payload = json.loads(_plugin_marker_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _marker_matches_source(
    marker: dict[str, Any],
    source: dict[str, str | None],
) -> bool:
    if marker.get("plugin_id") != TINYHAT_PLUGIN_ID:
        return False
    if marker.get("repo_url") != source.get("repo_url"):
        return False
    source_sha = source.get("resolved_commit_sha")
    if source_sha:
        return marker.get("resolved_commit_sha") == source_sha
    return marker.get("requested_ref") == source.get("requested_ref") or marker.get(
        "repo_ref"
    ) == source.get("requested_ref")


def _write_tinyhat_plugin_marker(
    *,
    source: dict[str, str | None],
    checkout: Path,
    resolved_sha: str,
) -> None:
    marker = _plugin_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": "tinyhat_plugin_install_marker_v1",
        "plugin_id": TINYHAT_PLUGIN_ID,
        "repo_url": source.get("repo_url"),
        "repo_ref": source.get("checkout_ref"),
        "requested_ref": source.get("requested_ref"),
        "resolved_commit_sha": resolved_sha,
        "version": _plugin_version(checkout),
        "checkout_dir": str(checkout),
    }
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git_stdout(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    runner: Runner = subprocess.run,
) -> tuple[str | None, dict[str, Any] | None]:
    result = _run_host_command(args, cwd=cwd, timeout=timeout, runner=runner)
    if not result.ok:
        return None, result.public_summary()
    return result.stdout.strip(), None


def _latest_git_tag(
    checkout: Path,
    *,
    runner: Runner = subprocess.run,
) -> str | None:
    stdout, detail = _git_stdout(
        ("git", "tag", "--sort=-v:refname"),
        cwd=checkout,
        runner=runner,
    )
    if detail is not None or not stdout:
        return None
    for line in stdout.splitlines():
        tag = line.strip()
        if tag:
            return tag
    return None


def _default_remote_ref(
    checkout: Path,
    *,
    runner: Runner = subprocess.run,
) -> str:
    stdout, detail = _git_stdout(
        ("git", "rev-parse", "--abbrev-ref", "origin/HEAD"),
        cwd=checkout,
        runner=runner,
    )
    if detail is None and stdout and stdout.startswith("origin/"):
        return stdout
    return "origin/main"


def _checkout_tinyhat_plugin(
    source: dict[str, str | None],
    *,
    runner: Runner = subprocess.run,
) -> tuple[Path | None, str | None, dict[str, Any] | None]:
    checkout = _plugin_checkout_dir()
    checkout.parent.mkdir(parents=True, exist_ok=True)
    repo_url = str(source.get("repo_url") or TINYHAT_PLUGIN_REPO_URL)
    if (checkout / ".git").exists():
        set_url = _run_host_command(
            ("git", "remote", "set-url", "origin", repo_url),
            cwd=checkout,
            runner=runner,
        )
        if not set_url.ok:
            return None, None, set_url.public_summary()
        fetch = _run_host_command(
            ("git", "fetch", "--tags", "--prune", "origin"),
            cwd=checkout,
            runner=runner,
        )
        if not fetch.ok:
            return None, None, fetch.public_summary()
    else:
        if checkout.exists():
            return None, None, {"error": f"plugin checkout exists but is not a git repo: {checkout}"}
        clone = _run_host_command(("git", "clone", repo_url, str(checkout)), runner=runner)
        if not clone.ok:
            return None, None, clone.public_summary()

    checkout_ref = str(source.get("checkout_ref") or TINYHAT_PLUGIN_REPO_REF)
    if checkout_ref == "latest":
        checkout_ref = _latest_git_tag(checkout, runner=runner) or _default_remote_ref(
            checkout,
            runner=runner,
        )
    checkout_result = _run_host_command(
        ("git", "checkout", checkout_ref),
        cwd=checkout,
        runner=runner,
    )
    if not checkout_result.ok:
        return None, None, checkout_result.public_summary()
    remote_ref = f"origin/{checkout_ref}"
    remote_sha, _detail = _git_stdout(
        ("git", "rev-parse", "--verify", f"{remote_ref}^{{commit}}"),
        cwd=checkout,
        runner=runner,
    )
    if remote_sha:
        reset = _run_host_command(
            ("git", "reset", "--hard", remote_ref),
            cwd=checkout,
            runner=runner,
        )
        if not reset.ok:
            return None, None, reset.public_summary()
    resolved_sha, detail = _git_stdout(
        ("git", "rev-parse", "HEAD"),
        cwd=checkout,
        runner=runner,
    )
    if detail is not None:
        return None, None, detail
    return checkout, resolved_sha, None


def materialize_tinyhat_plugin(
    binding: dict[str, Any],
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Make the public Tinyhat OpenClaw plugin available before activation."""
    source = _tinyhat_plugin_source(binding)
    if source.get("plugin_id") != TINYHAT_PLUGIN_ID:
        return {
            "state": "failed",
            "detail": f"unsupported Tinyhat plugin id: {source.get('plugin_id')}",
        }

    existing = inspect_plugin(TINYHAT_PLUGIN_ID, runner=runner)
    marker = _read_tinyhat_plugin_marker()
    if existing.get("state") == "ready" and _marker_matches_source(marker, source):
        return {
            "state": "ready",
            "action": "skipped",
            "source": redact_json(source),
            "plugin": existing,
            "marker": redact_json(marker),
        }

    checkout, resolved_sha, checkout_error = _checkout_tinyhat_plugin(
        source,
        runner=runner,
    )
    if checkout_error is not None or checkout is None or not resolved_sha:
        return {
            "state": "failed",
            "stage": "checkout",
            "source": redact_json(source),
            "detail": checkout_error or "checkout did not resolve a commit",
        }
    install = install_plugin(str(checkout), runner=runner)
    if install.get("state") != "ready":
        return {
            "state": "failed",
            "stage": "install",
            "source": redact_json(source),
            "resolved_commit_sha": resolved_sha,
            "detail": install,
        }
    inspected = inspect_plugin(TINYHAT_PLUGIN_ID, runner=runner)
    if inspected.get("state") != "ready":
        return {
            "state": "failed",
            "stage": "inspect",
            "source": redact_json(source),
            "resolved_commit_sha": resolved_sha,
            "detail": inspected,
        }
    _write_tinyhat_plugin_marker(
        source=source,
        checkout=checkout,
        resolved_sha=resolved_sha,
    )
    return {
        "state": "ready",
        "action": "installed",
        "source": redact_json(source),
        "resolved_commit_sha": resolved_sha,
        "checkout_dir": str(checkout),
        "install": install,
        "plugin": inspected,
    }


def config_patch(
    patch: dict[str, Any],
    *,
    dry_run: bool = False,
    replace_paths: Sequence[str] = (),
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    args: list[str] = ["config", "patch", "--stdin"]
    if dry_run:
        args.extend(["--dry-run", "--json"])
    for path in replace_paths:
        args.extend(["--replace-path", path])
    result = run_openclaw(
        tuple(args),
        runner=runner,
        input_text=json.dumps(patch, sort_keys=True),
    )
    if not result.ok:
        return {"state": "failed", "detail": result.public_summary()}
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            payload = redact_json(result.json_payload())
        except json.JSONDecodeError:
            payload = {"stdout": redact_text(result.stdout)}
    return {"state": "ready", "patch": payload, "detail": result.public_summary()}


def gateway_health(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("gateway", "health", "--json"), runner=runner)
    if not result.ok:
        try:
            payload = result.json_payload()
        except (json.JSONDecodeError, ValueError):
            payload = {}
        if (
            payload.get("gateway", {}).get("reachable") is True
            and payload.get("error", {}).get("type") == "gateway_credentials_required"
        ):
            return {
                "state": "healthy",
                "gateway": redact_json(payload),
                "readiness": "reachable_auth_required",
            }
        return {"state": "unhealthy", "detail": result.public_summary()}
    return {"state": "healthy", "gateway": redact_json(result.json_payload())}


def gateway_status(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("gateway", "status", "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "gateway": redact_json(result.json_payload())}


def gateway_run() -> int:
    try:
        return subprocess.call(
            [
                "openclaw",
                "gateway",
                "run",
                "--allow-unconfigured",
                "--bind",
                "loopback",
                "--auth",
                "none",
                "--tailscale",
                "off",
            ],
            env=openclaw_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(redact_text(str(exc)), flush=True)
        return 127


def models_status(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("models", "status", "--json"), runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    return {"state": "ready", "models": redact_json(result.json_payload())}


def secrets_reload(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("secrets", "reload", "--json"), runner=runner)
    if not result.ok:
        return {"state": "failed", "detail": result.public_summary()}
    return {"state": "ready", "secrets": redact_json(result.json_payload())}


def backup_create(
    *,
    output_path: Path,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Create and verify an OpenClaw backup archive on this Computer."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_openclaw(
        (
            "backup",
            "create",
            "--output",
            str(output_path),
            "--verify",
            "--json",
        ),
        timeout=300,
        runner=runner,
    )
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            payload = redact_json(result.json_payload())
        except (json.JSONDecodeError, ValueError):
            payload = {"stdout": redact_text(result.stdout)}
    archive_exists = output_path.exists()
    return {
        "state": "ready"
        if result.ok and archive_exists and payload.get("verified") is True
        else "failed",
        "archive_path": str(output_path) if archive_exists else None,
        "archive_bytes": output_path.stat().st_size if archive_exists else None,
        "backup": payload,
        "detail": result.public_summary(),
    }


def doctor_repair(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Run OpenClaw's non-interactive safe repair path."""
    result = run_openclaw(
        ("doctor", "--fix", "--non-interactive", "--yes"),
        timeout=300,
        runner=runner,
    )
    return {
        "state": "ready" if result.ok else "failed",
        "detail": result.public_summary(),
    }


def status_json(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    result = run_openclaw(("status", "--json"), timeout=30, runner=runner)
    if not result.ok:
        return {"state": "unavailable", "detail": result.public_summary()}
    try:
        status_payload = redact_json(result.json_payload())
    except (json.JSONDecodeError, ValueError):
        status_payload = {"stdout": redact_text(result.stdout)}
    return {
        "state": "ready",
        "status": status_payload,
        "detail": result.public_summary(),
    }


def _tinyhat_file_secret_ref(pointer: str) -> dict[str, str]:
    return {"source": "file", "provider": "tinyhat", "id": pointer}


def _tinyhat_plugin_config(
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if platform_base_url:
        config["platformBaseUrl"] = platform_base_url
    if backend_audience:
        config["backendAudience"] = backend_audience
    return config


def warm_image_config_patch(
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
) -> dict[str, Any]:
    tinyhat_plugin_config = _tinyhat_plugin_config(
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )
    return {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": int(os.environ.get("TINYHAT_OPENCLAW_GATEWAY_PORT", "18789")),
            "auth": {"mode": "none"},
            "tailscale": {"mode": "off"},
        },
        "agents": {
            "defaults": {
                "workspace": str(paths.OPENCLAW_STATE_DIR / "workspace"),
                "model": {"primary": "openai/gpt-5.5"},
                "compaction": {"reserveTokensFloor": 20000},
            }
        },
        "channels": {
            "telegram": {
                "enabled": False,
                "dmPolicy": "disabled",
                "groupPolicy": "disabled",
            }
        },
        "plugins": {
            "entries": {
                "telegram": {"enabled": True},
                "openai": {"enabled": True},
                "codex": {"enabled": True},
                "codex-supervisor": {"enabled": True},
                "tinyhat": {
                    "enabled": True,
                    "config": tinyhat_plugin_config,
                },
            }
        },
        "secrets": {
            "providers": {
                "tinyhat": {
                    "source": "file",
                    "path": str(paths.OPENCLAW_SECRETS_PATH),
                    "mode": "json",
                }
            },
            "defaults": {"file": "tinyhat"},
        },
        "session": {"dmScope": "per-channel-peer"},
    }


def apply_warm_image_config(
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    return config_patch(
        warm_image_config_patch(
            platform_base_url=platform_base_url,
            backend_audience=backend_audience,
        ),
        replace_paths=("channels.telegram",),
        runner=runner,
    )


def _deep_merge_json_objects(
    existing: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in overlay.items():
        previous = merged.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_json_objects(previous, value)
        else:
            merged[key] = value
    return merged


def _read_existing_secrets(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _count_secret_leaves(payload: Any) -> int:
    if isinstance(payload, dict):
        return sum(_count_secret_leaves(value) for value in payload.values())
    return 1


def write_openclaw_secrets(
    secrets: dict[str, Any],
    *,
    merge: bool = True,
) -> dict[str, Any]:
    paths.OPENCLAW_SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_existing_secrets(paths.OPENCLAW_SECRETS_PATH) if merge else {}
    payload = _deep_merge_json_objects(existing, secrets)
    tmp_path = paths.OPENCLAW_SECRETS_PATH.with_name(
        f".{paths.OPENCLAW_SECRETS_PATH.name}.tmp"
    )
    tmp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(paths.OPENCLAW_SECRETS_PATH)
    return {
        "state": "ready",
        "secret_count": _count_secret_leaves(secrets),
        "merged": merge,
    }


def binding_secrets_payload(binding: dict[str, Any]) -> dict[str, Any]:
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")
    openrouter_key = str(binding.get("openrouter_api_key") or "").strip()
    return {
        "channels": {"telegram": {"botToken": bot_token}},
        "providers": {"openrouter": {"apiKey": openrouter_key}},
    }


def binding_config_patch(
    binding: dict[str, Any],
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
) -> dict[str, Any]:
    owner_id = str(binding.get("telegram_owner_user_id") or "").strip()
    bot_token = str(binding.get("telegram_bot_token") or "").strip()
    if not owner_id:
        raise ValueError("binding is missing telegram_owner_user_id")
    if not bot_token:
        raise ValueError("binding is missing telegram_bot_token")

    openrouter_key = str(binding.get("openrouter_api_key") or "").strip()
    openrouter_model = str(binding.get("openrouter_default_model") or "").strip()
    llm_model_ref = str(binding.get("llm_model_ref") or "").strip()
    llm_auth_mode = str(binding.get("llm_auth_mode") or "platform_credits").strip()

    primary_model = (
        llm_model_ref
        if llm_auth_mode == "chatgpt_subscription" and llm_model_ref
        else f"openrouter/{openrouter_model.lstrip('/')}"
        if openrouter_model
        else "openai/gpt-5.5"
    )
    tinyhat_plugin_config = _tinyhat_plugin_config(
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )

    patch: dict[str, Any] = {
        "agents": {"defaults": {"model": {"primary": primary_model}}},
        "channels": {
            "telegram": {
                "enabled": True,
                "dmPolicy": "allowlist",
                "groupPolicy": "disabled",
                "allowFrom": [owner_id],
                "botToken": _tinyhat_file_secret_ref("/channels/telegram/botToken"),
                "execApprovals": {"approvers": [owner_id]},
            }
        },
        "plugins": {
            "entries": {
                "tinyhat": {"config": tinyhat_plugin_config},
            }
        },
        "secrets": {
            "providers": {
                "tinyhat": {
                    "source": "file",
                    "path": str(paths.OPENCLAW_SECRETS_PATH),
                    "mode": "json",
                }
            },
            "defaults": {"file": "tinyhat"},
        },
        "session": {"dmScope": "per-channel-peer"},
    }
    if openrouter_key:
        patch["models"] = {
            "providers": {
                "openrouter": {
                    "apiKey": _tinyhat_file_secret_ref("/providers/openrouter/apiKey")
                }
            }
        }
    return patch


def apply_binding_config(
    binding: dict[str, Any],
    *,
    platform_base_url: str | None = None,
    backend_audience: str | None = None,
    preserve_existing_secrets: bool = True,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    patch = binding_config_patch(
        binding,
        platform_base_url=platform_base_url,
        backend_audience=backend_audience,
    )
    secrets_result = write_openclaw_secrets(
        binding_secrets_payload(binding),
        merge=preserve_existing_secrets,
    )
    plugin_result = materialize_tinyhat_plugin(binding, runner=runner)
    if plugin_result.get("state") != "ready":
        return {
            "state": "failed",
            "stage": "tinyhat_plugin",
            "secrets": secrets_result,
            "tinyhat_plugin": plugin_result,
        }
    patch_result = config_patch(
        patch,
        replace_paths=("channels.telegram",),
        runner=runner,
    )
    patch_result["secrets"] = secrets_result
    patch_result["tinyhat_plugin"] = plugin_result
    return patch_result


def _redact_zip_member(name: str, data: bytes) -> bytes:
    lowered = name.lower()
    if lowered.endswith(".json"):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return redact_text(data.decode("utf-8", errors="replace")).encode("utf-8")
        return (
            json.dumps(redact_json(payload), indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return b"[binary omitted by Tinyhat diagnostics redaction]\n"
    if "\x00" in text:
        return b"[binary omitted by Tinyhat diagnostics redaction]\n"
    return redact_text(text).encode("utf-8")


def redact_diagnostics_zip(path: Path) -> list[str]:
    tmp_path = path.with_name(f".{path.name}.redacted.tmp")
    entries: list[str] = []
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(
        tmp_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as dst:
        for info in src.infolist():
            if info.is_dir():
                continue
            entries.append(info.filename)
            dst.writestr(info.filename, _redact_zip_member(info.filename, src.read(info)))
    os.replace(tmp_path, path)
    return sorted(entries)


def export_diagnostics(
    *,
    output_path: Path,
    runner: Runner = subprocess.run,
    log_bytes: int = 1_000_000,
    log_lines: int = 5_000,
    timeout_ms: int = 3_000,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_openclaw(
        (
            "gateway",
            "diagnostics",
            "export",
            "--json",
            "--output",
            str(output_path),
            "--log-bytes",
            str(log_bytes),
            "--log-lines",
            str(log_lines),
            "--timeout",
            str(timeout_ms),
        ),
        timeout=max(5, int(timeout_ms / 1000) + 5),
        runner=runner,
    )
    if not output_path.exists():
        return {
            "state": "failed",
            "output_path": str(output_path),
            "detail": result.public_summary(),
        }
    entries = redact_diagnostics_zip(output_path)
    export_payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                export_payload = redact_json(parsed)
        except json.JSONDecodeError:
            export_payload = {"stdout": redact_text(result.stdout)}
    return {
        "state": "ready" if result.ok else "ready_with_warnings",
        "output_path": str(output_path),
        "zip_bytes": output_path.stat().st_size,
        "entries": entries,
        "export": export_payload,
        "detail": result.public_summary(),
    }


def adapter_attestation(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    return {
        "schema": "openclaw_adapter_attestation_v1",
        "plugin": inspect_plugin("tinyhat", runner=runner),
        "gateway": gateway_health(runner=runner),
        "models": models_status(runner=runner),
    }
