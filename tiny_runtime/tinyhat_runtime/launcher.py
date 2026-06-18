"""Bundle activation and rollback mechanics."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import bundle, paths


@dataclass(frozen=True)
class ActivationResult:
    activated: bool
    bundle_id: str
    target: str
    previous_target: str | None
    rolled_back: bool
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


def activate_bundle(
    bundle_dir: Path,
    *,
    current_link: Path = paths.CURRENT_LINK,
    health_command: Sequence[str] | None = None,
    timeout: int = 30,
) -> ActivationResult:
    manifest = bundle.load_manifest(bundle_dir)
    bundle.verify_manifest(bundle_dir, manifest)
    previous = _readlink_or_none(current_link)
    _replace_symlink(current_link, bundle_dir.resolve())

    if health_command:
        completed = subprocess.run(
            list(health_command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            if previous is not None:
                _replace_symlink(current_link, Path(previous))
            else:
                current_link.unlink(missing_ok=True)
            return ActivationResult(
                activated=False,
                bundle_id=str(manifest["bundle_id"]),
                target=str(bundle_dir.resolve()),
                previous_target=previous,
                rolled_back=True,
                diagnostic=(completed.stderr or completed.stdout or "health command failed").strip(),
            )

    return ActivationResult(
        activated=True,
        bundle_id=str(manifest["bundle_id"]),
        target=str(bundle_dir.resolve()),
        previous_target=previous,
        rolled_back=False,
        diagnostic="activated",
    )
