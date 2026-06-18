"""Bundle activation and rollback mechanics."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import bundle, paths
from .redaction import redact_text


@dataclass(frozen=True)
class ActivationResult:
    activated: bool
    bundle_id: str
    target: str
    previous_target: str | None
    rolled_back: bool
    phase: str
    diagnostic: str


def _replace_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = link.with_name(f".{link.name}.tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(target, tmp_link)
    os.replace(tmp_link, link)


def _readlink_or_none(path: Path) -> str | None:
    if path.is_symlink():
        return os.readlink(path)
    return None


def _run_command(command: Sequence[str], *, timeout: int) -> tuple[bool, str]:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    detail = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode == 0:
        return True, detail or "ok"
    return False, redact_text(detail or f"command exited {completed.returncode}", limit=1000)


def _restore_link(link: Path, previous: str | None) -> None:
    if previous is not None:
        _replace_symlink(link, Path(previous))
    else:
        link.unlink(missing_ok=True)


def activate_bundle(
    bundle_dir: Path,
    *,
    current_link: Path = paths.CURRENT_LINK,
    stop_command: Sequence[str] | None = None,
    start_command: Sequence[str] | None = None,
    health_command: Sequence[str] | None = None,
    timeout: int = 30,
) -> ActivationResult:
    manifest = bundle.load_manifest(bundle_dir)
    bundle.verify_manifest(bundle_dir, manifest)
    previous = _readlink_or_none(current_link)
    if stop_command:
        ok, detail = _run_command(stop_command, timeout=timeout)
        if not ok:
            return ActivationResult(
                activated=False,
                bundle_id=str(manifest["bundle_id"]),
                target=str(bundle_dir.resolve()),
                previous_target=previous,
                rolled_back=False,
                phase="stop",
                diagnostic=detail,
            )

    _replace_symlink(current_link, bundle_dir.resolve())

    if start_command:
        ok, detail = _run_command(start_command, timeout=timeout)
        if not ok:
            _restore_link(current_link, previous)
            if start_command:
                # Best effort: after restoring the old target, try to bring the
                # gateway back even though the original start command failed.
                _run_command(start_command, timeout=timeout)
            return ActivationResult(
                activated=False,
                bundle_id=str(manifest["bundle_id"]),
                target=str(bundle_dir.resolve()),
                previous_target=previous,
                rolled_back=True,
                phase="start",
                diagnostic=detail,
            )

    if health_command:
        ok, detail = _run_command(health_command, timeout=timeout)
        if not ok:
            if stop_command:
                _run_command(stop_command, timeout=timeout)
            _restore_link(current_link, previous)
            if start_command:
                # Best effort: activation has failed, so prefer attempting to
                # restart the previous target over leaving the gateway stopped.
                _run_command(start_command, timeout=timeout)
            return ActivationResult(
                activated=False,
                bundle_id=str(manifest["bundle_id"]),
                target=str(bundle_dir.resolve()),
                previous_target=previous,
                rolled_back=True,
                phase="health",
                diagnostic=detail,
            )

    return ActivationResult(
        activated=True,
        bundle_id=str(manifest["bundle_id"]),
        target=str(bundle_dir.resolve()),
        previous_target=previous,
        rolled_back=False,
        phase="activated",
        diagnostic="activated",
    )
