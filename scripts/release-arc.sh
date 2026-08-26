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

foundation_path="${AC_FOUNDATION_REPO_ROOT:-$root/../ac-foundation}"
AC_FOUNDATION_REPO_ROOT="$foundation_path" \
  "$python_bin" "$root/scripts/check-generated-foundation.py"
constraint_args=()
for project in "$foundation_path"/packages/ac-*/pyproject.toml \
  "$root"/packages/arc-*/pyproject.toml; do
  constraint_args+=(--package "$project")
done
"$python_bin" "$foundation_path/scripts/check-runtime-constraints.py" \
  --constraints "$root/plugins/arc/skills/arc/scripts/runtime-constraints.txt" \
  "${constraint_args[@]}"

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
current_major = int(current.split(".")[0])
target_major = int(version.split(".")[0])
current_range = f">={current_major},<{current_major + 1}"
target_range = f">={target_major},<{target_major + 1}"

updates: list[tuple[Path, str, Path, str]] = []

for path in projects:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data["project"]["version"] != current:
        raise SystemExit(f"{path} version differs from VERSION")
    for dependency in data["project"].get("dependencies", []):
        if dependency.startswith("arc-") and current_range not in dependency:
            raise SystemExit(
                f"{path} ARC dependency must use {current_range}: {dependency}"
            )
        if dependency.startswith("ac-"):
            ac_range = re.search(
                r">=(\d+)(?:\.\d+\.\d+)?,<(\d+)$", dependency
            )
            if ac_range is None or int(ac_range[2]) != int(ac_range[1]) + 1:
                raise SystemExit(
                    f"{path} AC dependency must use a bounded major range: {dependency}"
                )
    text = path.read_text(encoding="utf-8")
    updated_text, count = re.subn(
        rf'(?m)^version = "{re.escape(current)}"$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"could not update project version in {path}")
    updated_text = re.sub(
        rf'(?m)^(\s*"arc-[^"]*?){re.escape(current_range)}',
        rf'\g<1>{target_range}',
        updated_text,
    )
    init_path = next((path.parent / "src").glob("*/__init__.py"))
    init_text = init_path.read_text(encoding="utf-8")
    updated_init, count = re.subn(
        rf'(?m)^__version__ = "{re.escape(current)}"$',
        f'__version__ = "{version}"',
        init_text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"could not update __version__ in {init_path}")
    updates.append((path, updated_text, init_path, updated_init))

for path in (
    root / "plugins/arc/.codex-plugin/plugin.json",
    root / "plugins/arc/.claude-plugin/plugin.json",
):
    manifest_text = path.read_text(encoding="utf-8")
    document = json.loads(manifest_text)
    if document.get("version") != current:
        raise SystemExit(f"{path} version differs from VERSION")
    updated_manifest, count = re.subn(
        rf'("version"\s*:\s*"){re.escape(current)}(")',
        rf'\g<1>{version}\2',
        manifest_text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"could not update plugin version in {path}")
    updates.append((path, updated_manifest, path, ""))

constraints = root / "plugins/arc/skills/arc/scripts/runtime-constraints.txt"
constraints_text = constraints.read_text(encoding="utf-8")
constraints_text, count = re.subn(
    rf"(?m)^# Direct external dependencies tested for ARC v{re.escape(current)}\.$",
    f"# Direct external dependencies tested for ARC v{version}.",
    constraints_text,
    count=1,
)
if count != 1:
    raise SystemExit("runtime constraints release header is inconsistent")
(root / "VERSION").write_text(version + "\n", encoding="utf-8")
for path, file_text, init_path, init_text in updates:
    path.write_text(file_text, encoding="utf-8")
    if init_path != path:
        init_path.write_text(init_text, encoding="utf-8")
constraints.write_text(constraints_text, encoding="utf-8")
PY

source_path="$(find "$root/packages" -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)"
foundation_source="$(find "$foundation_path/packages" -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)"
PYTHONPATH="$foundation_source:$source_path" "$python_bin" -m pytest \
  --import-mode=importlib "$root"/packages/*/tests "$root/tests"
PYTHON="$python_bin" "$root/scripts/build-packages.sh"

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
