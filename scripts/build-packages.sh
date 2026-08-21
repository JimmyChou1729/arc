#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
output="${ARC_BUILD_DIR:-$root/local/dist}"
mkdir -p "$output"

for project in "$root"/packages/arc-*/pyproject.toml; do
  python -m build --outdir "$output" "${project%/pyproject.toml}"
done
