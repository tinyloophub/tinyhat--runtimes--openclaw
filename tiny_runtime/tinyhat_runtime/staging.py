"""Stage a new content-addressed tiny_runtime bundle from resolved git refs.

This is the STAGE half of the ``stage_and_activate_bundle`` ledger verb: it
clones the runtime ref, installs the OpenClaw framework package, assembles a
bundle tree, and runs the bundle's own ``install.sh`` in *stage-only* mode so
``bundles/<digest>`` is materialized and verified WITHOUT flipping
``/opt/tinyhat/current``. The flip is the launcher's job
(``launcher.activate_bundle``), which the command handler calls next.

Everything here stays inside tiny_runtime and integrates with OpenClaw only
through the official ``npm install openclaw@<version>`` + the bundle's
``bake preinstall-plugins`` / ``platform warm-config`` subcommands — never the
legacy supervisor or a hand-edited OpenClaw config.

The module is deliberately effect-injectable: the host-command runner, the
clone, the npm install, the assemble, the install.sh invocation, and the
plugin preinstall are all hooks with real defaults so tests can stub the whole
chain (no real clone / npm / git) and assert the resulting ``bundles/<digest>``
+ ``bundle_id`` plumbing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import paths

# A host-command runner with the same signature subprocess.run exposes, so
# tests can pass a fake. Returns an object with .returncode / .stdout / .stderr.
Runner = Callable[..., Any]

DEFAULT_RUNTIME_REPO_URL = os.environ.get(
    "TINYHAT_RUNTIME_REPO_URL",
    "https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git",
)


class StagingError(RuntimeError):
    """A staging step (clone / npm / assemble / install) failed."""


@dataclass(frozen=True)
class StagedBundle:
    bundle_id: str
    bundle_dir: str
    runtime_sha: str
    plugin_sha: str
    framework_version: str


def _run(
    args: Sequence[str],
    *,
    runner: Runner,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 600,
) -> str:
    completed = runner(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if getattr(completed, "returncode", 1) != 0:
        detail = (
            getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or ""
        ).strip()
        raise StagingError(f"{args[0]} failed: {detail}")
    return (getattr(completed, "stdout", "") or "").strip()


def _clone_runtime(
    *,
    runtime_ref: str,
    runtime_commit_sha: str,
    dest: Path,
    repo_url: str,
    runner: Runner,
) -> str:
    """Clone the runtime repo at ``runtime_ref`` and hard-pin ``runtime_commit_sha``.

    Mirrors force-upgrade.sh's clone-then-checkout fallback so an arbitrary
    commit SHA (not a branch/tag) still resolves. Returns the resolved HEAD SHA.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(
            ["git", "clone", "--depth", "1", "--branch", runtime_ref, repo_url, str(dest)],
            runner=runner,
        )
    except StagingError:
        # Arbitrary commit SHA: full clone, then checkout.
        _run(["git", "clone", repo_url, str(dest)], runner=runner)
        _run(["git", "checkout", runtime_ref], cwd=dest, runner=runner)
    if runtime_commit_sha:
        # Pin exactly what the platform resolved; never trust a moving ref.
        _run(["git", "checkout", runtime_commit_sha], cwd=dest, runner=runner)
    return _run(["git", "rev-parse", "HEAD"], cwd=dest, runner=runner)


def _install_openclaw_framework(
    *, framework_version: str, runner: Runner
) -> str:
    """``npm install -g openclaw@<version>`` and return the resolved binary path.

    The same official install bootstrap.sh::install_openclaw_framework_package
    performs. ``--no-fund --no-audit`` keep it quiet; a failed install raises so
    the verb fails closed before any flip.
    """
    spec = framework_version if "@" in framework_version else f"openclaw@{framework_version}"
    _run(["npm", "install", "-g", "--no-fund", "--no-audit", spec], runner=runner, timeout=600)
    openclaw_bin = _run(["bash", "-lc", "command -v openclaw"], runner=runner)
    return openclaw_bin


def _run_assembler(
    *,
    runtime_dir: Path,
    out_dir: Path,
    runtime_ref: str,
    framework_version: str,
    openclaw_bin: str,
    plugin_ref: str,
    runner: Runner,
) -> None:
    assembler = runtime_dir / "tiny_runtime" / "bake" / "assemble-bundle.sh"
    if not assembler.exists():
        raise StagingError(f"assembler not found in clone: {assembler}")
    env = dict(os.environ)
    env.update(
        {
            "TINYHAT_RUNTIME_REF": runtime_ref,
            "TINYHAT_OPENCLAW_REF": framework_version,
            "TINYHAT_OPENCLAW_BIN": openclaw_bin,
            "TINYHAT_PLUGIN_REF": plugin_ref,
        }
    )
    _run(["bash", str(assembler), str(out_dir)], runner=runner, env=env, timeout=600)


def _run_install_stage_only(
    *,
    bundle_out: Path,
    bundles_dir: Path,
    current_link: Path,
    runner: Runner,
) -> StagedBundle:
    """Run the assembled bundle's install.sh with TINYHAT_BUNDLE_STAGE_ONLY=1.

    Materializes + verifies ``bundles/<digest>`` and prints
    ``{"staged":true,...,"bundle_dir":...}`` without flipping ``current``.
    """
    installer = bundle_out / "install.sh"
    if not installer.exists():
        raise StagingError(f"bundle install.sh missing: {installer}")
    env = dict(os.environ)
    env.update(
        {
            "TINYHAT_RUNTIME_BUNDLE_DIR": str(bundle_out),
            "TINYHAT_RUNTIME_BUNDLES_DIR": str(bundles_dir),
            "TINYHAT_RUNTIME_CURRENT_LINK": str(current_link),
            "TINYHAT_BUNDLE_STAGE_ONLY": "1",
            # Stage-only already skips the enable block, but keep this explicit
            # so a future install.sh change can't accidentally touch systemd.
            "TINYHAT_RUNTIME_SKIP_SYSTEMD": "1",
        }
    )
    raw = _run(["bash", str(installer)], runner=runner, env=env, timeout=600)
    try:
        payload = json.loads(raw.splitlines()[-1]) if raw else {}
    except (json.JSONDecodeError, IndexError) as exc:
        raise StagingError(f"install.sh stage output not JSON: {raw!r}") from exc
    bundle_id = str(payload.get("bundle_id") or "")
    bundle_dir = str(payload.get("bundle_dir") or "")
    if not bundle_id or not bundle_dir:
        raise StagingError(f"install.sh stage output incomplete: {payload!r}")
    return StagedBundle(
        bundle_id=bundle_id,
        bundle_dir=bundle_dir,
        runtime_sha="",
        plugin_sha="",
        framework_version="",
    )


def stage_bundle(
    *,
    runtime_ref: str,
    runtime_commit_sha: str,
    plugin_ref: str,
    plugin_commit_sha: str,
    framework_version: str,
    bundles_dir: Path = paths.BUNDLES_DIR,
    current_link: Path = paths.CURRENT_LINK,
    repo_url: str = DEFAULT_RUNTIME_REPO_URL,
    runner: Runner = subprocess.run,
    clone_hook: Callable[..., str] | None = None,
    install_framework_hook: Callable[..., str] | None = None,
    assemble_hook: Callable[..., None] | None = None,
    install_stage_hook: Callable[..., StagedBundle] | None = None,
    preinstall_plugins_hook: Callable[[Path], dict[str, Any]] | None = None,
    warm_config_hook: Callable[[Path], dict[str, Any]] | None = None,
    workdir: Path | None = None,
) -> StagedBundle:
    """Stage a new content-addressed bundle from resolved git refs WITHOUT flipping.

    Steps (all hookable; defaults are the real effects):
      1. clone runtime_ref + pin runtime_commit_sha
      2. npm install openclaw@framework_version (official; backup/verify in npm)
      3. assemble-bundle.sh -> verified bundle tree (vendors the installed openclaw)
      4. install.sh with TINYHAT_BUNDLE_STAGE_ONLY=1 -> bundles/<digest>, no flip
      5. preinstall plugin_commit_sha into the staged bundle layout
      6. warm-config so the (later) restart comes up with channels

    Returns the StagedBundle (bundle_id + bundle_dir) for the caller to hand to
    launcher.activate_bundle. Never touches ``current``.
    """
    clone_hook = clone_hook or _clone_runtime
    install_framework_hook = install_framework_hook or _install_openclaw_framework
    assemble_hook = assemble_hook or _run_assembler
    install_stage_hook = install_stage_hook or _run_install_stage_only

    bundles_dir.mkdir(parents=True, exist_ok=True)
    owns_workdir = workdir is None
    workdir = workdir or Path(tempfile.mkdtemp(prefix="tiny-runtime-stage."))
    try:
        runtime_dir = workdir / "runtime"
        resolved_runtime_sha = clone_hook(
            runtime_ref=runtime_ref,
            runtime_commit_sha=runtime_commit_sha,
            dest=runtime_dir,
            repo_url=repo_url,
            runner=runner,
        )
        openclaw_bin = install_framework_hook(
            framework_version=framework_version, runner=runner
        )
        out_dir = workdir / "bundle"
        assemble_hook(
            runtime_dir=runtime_dir,
            out_dir=out_dir,
            runtime_ref=runtime_ref,
            framework_version=framework_version,
            openclaw_bin=openclaw_bin,
            plugin_ref=plugin_commit_sha or plugin_ref,
            runner=runner,
        )
        staged = install_stage_hook(
            bundle_out=out_dir,
            bundles_dir=bundles_dir,
            current_link=current_link,
            runner=runner,
        )
        bundle_dir = Path(staged.bundle_dir)

        # Plugin preinstall + warm-config are content prep, not a flip. Default
        # to the bundle's own bake/platform subcommands; injectable for tests.
        if preinstall_plugins_hook is not None:
            preinstall_plugins_hook(bundle_dir)
        if warm_config_hook is not None:
            warm_config_hook(bundle_dir)

        return StagedBundle(
            bundle_id=staged.bundle_id,
            bundle_dir=str(bundle_dir),
            runtime_sha=resolved_runtime_sha or runtime_commit_sha,
            plugin_sha=plugin_commit_sha,
            framework_version=framework_version,
        )
    finally:
        if owns_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
