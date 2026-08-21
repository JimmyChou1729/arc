#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
foundation="${AC_FOUNDATION_REPO_ROOT:-$root/../ac-foundation}"
source_path="$(find "$foundation/packages" "$root/packages" -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)"

PYTHONPATH="$source_path" python -m pytest --import-mode=importlib \
  "$root"/packages/*/tests "$root/tests"
"$root/scripts/build-packages.sh"
