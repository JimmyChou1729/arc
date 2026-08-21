#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
output="${ARC_BUILD_DIR:-$root/local/dist}"
python_bin="${PYTHON:-python3}"
rm -rf "$output"
mkdir -p "$output"

for project in "$root"/packages/arc-*/pyproject.toml; do
  "$python_bin" -m build --outdir "$output" "${project%/pyproject.toml}"
done
