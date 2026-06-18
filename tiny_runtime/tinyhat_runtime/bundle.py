"""Content-addressed tiny_runtime bundle manifests."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any, Iterable

from . import RUNTIME_GENERATION

MANIFEST_FILENAME = "bundle-manifest.json"
MANIFEST_SCHEMA = "tiny_runtime_bundle_v1"
_IGNORED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})
_IGNORED_FILES = frozenset({MANIFEST_FILENAME, ".DS_Store"})


class BundleVerificationError(ValueError):
    """Raised when a bundle manifest does not match bundle contents."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in _IGNORED_DIRS for part in rel_parts):
            continue
        if path.name in _IGNORED_FILES:
            continue
        if path.is_file():
            yield path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_file_records(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    records: list[dict[str, Any]] = []
    for path in _iter_files(root):
        mode = stat.S_IMODE(path.stat().st_mode)
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256_file(path),
                "mode": f"{mode:04o}",
                "size": path.stat().st_size,
            }
        )
    return records


def compute_bundle_id(root: Path, *, components: dict[str, Any] | None = None) -> str:
    payload = {
        "runtime_generation": RUNTIME_GENERATION,
        "components": components or {},
        "files": collect_file_records(root),
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def build_manifest(root: Path, *, components: dict[str, Any] | None = None) -> dict[str, Any]:
    root = root.resolve()
    component_doc = components or {}
    files = collect_file_records(root)
    identity_payload = {
        "runtime_generation": RUNTIME_GENERATION,
        "components": component_doc,
        "files": files,
    }
    bundle_id = "sha256:" + hashlib.sha256(_canonical_json(identity_payload)).hexdigest()
    return {
        "schema": MANIFEST_SCHEMA,
        "runtime_generation": RUNTIME_GENERATION,
        "bundle_id": bundle_id,
        "components": component_doc,
        "files": files,
    }


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_FILENAME


def write_manifest(
    root: Path,
    *,
    components: dict[str, Any] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    manifest = build_manifest(root, components=components)
    target = output_path or manifest_path(root)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def load_manifest(root_or_file: Path) -> dict[str, Any]:
    path = root_or_file if root_or_file.name == MANIFEST_FILENAME else manifest_path(root_or_file)
    return json.loads(path.read_text(encoding="utf-8"))


def verify_manifest(root: Path, manifest: dict[str, Any] | None = None) -> bool:
    manifest = manifest or load_manifest(root)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise BundleVerificationError("bundle manifest schema mismatch")
    if manifest.get("runtime_generation") != RUNTIME_GENERATION:
        raise BundleVerificationError("bundle manifest runtime_generation mismatch")

    expected = build_manifest(root, components=manifest.get("components") or {})
    for key in ("bundle_id", "files"):
        if manifest.get(key) != expected.get(key):
            raise BundleVerificationError(f"bundle manifest {key} does not match content")
    return True
