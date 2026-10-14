#!/usr/bin/env python3
"""Validate a version tag and, optionally, its installed source provenance."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def check_versions(root: Path, tag: str) -> None:
    version = (root / "VERSION").read_text().strip()
    if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag):
        raise ValueError("tag must be vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        raise ValueError(f"tag {tag} does not match VERSION {version}")
    for package in ("arc-paper", "arc-domain"):
        directory = root / "packages" / package
        project = tomllib.loads((directory / "pyproject.toml").read_text())
        if project["project"]["version"] != version:
            raise ValueError(f"{package} version does not match VERSION")
        init = directory / "src" / package.replace("-", "_") / "__init__.py"
        match = re.search(r'^__version__ = "([^"]+)"$', init.read_text(), re.M)
        if not match or match[1] != version:
            raise ValueError(f"{package} __version__ does not match VERSION")
    for relative in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        path = root / "plugins/arc" / relative
        if json.loads(path.read_text())["version"] != version:
            raise ValueError(f"{relative} version does not match VERSION")


def check_pins(root: Path, foundation_root: Path) -> None:
    path = root / "plugins/arc/skills/arc/scripts/runtime-sources.json"
    sources = json.loads(path.read_text())["sources"]
    for name, repository, checkout in (
        ("product", "arc", root),
        ("foundation", "ac-foundation", foundation_root),
    ):
        matches = [source for source in sources if source["id"] == name]
        if len(matches) != 1:
            raise ValueError(f"expected one {name} source")
        source = matches[0]
        if source["repository"] != f"https://github.com/tririver/{repository}.git":
            raise ValueError(f"{name} must use its official upstream repository")
        commit = source["commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"{name} pin must be a full commit SHA")
        target = "HEAD" if name == "product" else "origin/main"
        git(checkout, "merge-base", "--is-ancestor", commit, target)
        if name == "product":
            if git(root, "rev-parse", f"{commit}:packages") != git(root, "rev-parse", "HEAD:packages"):
                raise ValueError("product pin packages differ from tagged packages")
        elif git(checkout, "rev-parse", "HEAD") != commit:
            raise ValueError("Foundation checkout does not match its source pin")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--check-pins", action="store_true")
    parser.add_argument("--foundation-root", type=Path)
    args = parser.parse_args()
    try:
        check_versions(ROOT, args.tag)
        if args.check_pins:
            if args.foundation_root is None:
                raise ValueError("--check-pins requires --foundation-root")
            check_pins(ROOT, args.foundation_root)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release tag validation failed: {exc}\n")
    print(f"Validated {args.tag}" + (" and source pins" if args.check_pins else " versions"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
