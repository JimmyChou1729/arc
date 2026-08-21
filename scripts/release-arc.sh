#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  printf 'Usage: release-arc.sh VERSION\n' >&2
  exit 64
fi

version="$1"
root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
python_bin="${PYTHON:-python3}"

case "$version" in
  *[!0-9.]*|*.*.*.*|''|.*|*.)
    printf 'VERSION must be a numeric MAJOR.MINOR.PATCH value: %s\n' "$version" >&2
    exit 64
    ;;
esac
if [ "$(printf '%s' "$version" | awk -F. '{print NF}')" -ne 3 ]; then
  printf 'VERSION must be a numeric MAJOR.MINOR.PATCH value: %s\n' "$version" >&2
  exit 64
fi
if [ -n "$(git -C "$root" status --porcelain)" ]; then
  printf 'Release requires a clean worktree.\n' >&2
  exit 65
fi

"$python_bin" - "$root" "$version" <<'PY'
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]
expected = {
    "arc-paper",
    "arc-domain",
}
projects = sorted((root / "packages").glob("arc-*/pyproject.toml"))
observed = {path.parent.name for path in projects}
if observed != expected:
    raise SystemExit(f"package set mismatch: expected {sorted(expected)}, got {sorted(observed)}")
current = (root / "VERSION").read_text(encoding="utf-8").strip()
if tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
    raise SystemExit(f"release version must be newer than {current}: {version}")
major = int(version.split(".")[0])
internal_range = f">={major},<{major + 1}"

for path in projects:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data["project"]["version"] != current:
        raise SystemExit(f"{path} version differs from VERSION")
    for dependency in data["project"].get("dependencies", []):
        if dependency.startswith(("ac-", "arc-")) and internal_range not in dependency:
            raise SystemExit(
                f"{path} AC/ARC dependency must use {internal_range}: {dependency}"
            )
    text = path.read_text(encoding="utf-8")
    text, count = re.subn(
        rf'(?m)^version = "{re.escape(current)}"$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"could not update project version in {path}")
    path.write_text(text, encoding="utf-8")
    init_path = next((path.parent / "src").glob("*/__init__.py"))
    init_text = init_path.read_text(encoding="utf-8")
    init_text, count = re.subn(
        rf'(?m)^__version__ = "{re.escape(current)}"$',
        f'__version__ = "{version}"',
        init_text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"could not update __version__ in {init_path}")
    init_path.write_text(init_text, encoding="utf-8")

for path in (
    root / "plugins/arc/.codex-plugin/plugin.json",
    root / "plugins/arc/.claude-plugin/plugin.json",
):
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != current:
        raise SystemExit(f"{path} version differs from VERSION")
    document["version"] = version
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

constraints = root / "plugins/arc/skills/arc/scripts/runtime-constraints.txt"
text = constraints.read_text(encoding="utf-8")
text, count = re.subn(
    rf"(?m)^# Direct external dependencies tested for ARC v{re.escape(current)}\.$",
    f"# Direct external dependencies tested for ARC v{version}.",
    text,
    count=1,
)
if count != 1:
    raise SystemExit("runtime constraints release header is inconsistent")
constraints.write_text(text, encoding="utf-8")
(root / "VERSION").write_text(version + "\n", encoding="utf-8")
PY

source_path="$(find "$root/packages" -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)"
foundation_path="${AC_FOUNDATION_REPO_ROOT:-$root/../ac-foundation}"
foundation_source="$(find "$foundation_path/packages" -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)"
PYTHONPATH="$foundation_source:$source_path" "$python_bin" -m pytest \
  --import-mode=importlib "$root"/packages/*/tests "$root/tests"
PATH="$(dirname "$(command -v "$python_bin")"):$PATH" "$root/scripts/build-packages.sh"

git -C "$root" add VERSION packages plugins/arc
git -C "$root" commit -m "chore: release v${version}"
source_commit="$(git -C "$root" rev-parse HEAD)"

"$python_bin" - "$root" "$source_commit" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
source_commit = sys.argv[2]
path = root / "plugins/arc/skills/arc/scripts/runtime-sources.json"
document = json.loads(path.read_text(encoding="utf-8"))
products = [source for source in document["sources"] if source["id"] == "product"]
if len(products) != 1:
    raise SystemExit("runtime source lock must contain exactly one product source")
products[0]["commit"] = source_commit
path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
PY

git -C "$root" add plugins/arc/skills/arc/scripts/runtime-sources.json
git -C "$root" commit -m "chore(plugin): pin ARC ${version} source"
printf 'Prepared ARC %s at %s. Create tag v%s only after publication approval.\n' \
  "$version" "$(git -C "$root" rev-parse HEAD)" "$version"
