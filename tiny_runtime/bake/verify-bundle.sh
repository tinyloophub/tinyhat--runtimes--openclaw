#!/usr/bin/env bash
# Verify that a tiny_runtime bundle matches its manifest.
set -euo pipefail

bundle_dir="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHONPATH="${bundle_dir}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m tinyhat_runtime.main bundle verify --bundle-dir "${bundle_dir}"
