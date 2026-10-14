from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("arc_release_tag", ROOT / "scripts/check-release-tag.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root):
    git(root, "add", "packages", "VERSION")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q")
    (root / "VERSION").write_text("1.2.3\n")
    for name in ("arc-paper", "arc-domain"):
        package = root / "packages" / name
        init = package / "src" / name.replace("-", "_") / "__init__.py"
        init.parent.mkdir(parents=True)
        init.write_text('__version__ = "1.2.3"\n')
        (package / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    for path in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        target = root / "plugins/arc" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"version":"1.2.3"}')
    commit(root)
    return root


def test_matching_tag(repo):
    release.check_versions(repo, "v1.2.3")


@pytest.mark.parametrize("tag", ["1.2.3", "v01.2.3", "v1.2.3-rc1", "v1.2.4"])
def test_invalid_tag(repo, tag):
    with pytest.raises(ValueError):
        release.check_versions(repo, tag)


@pytest.mark.parametrize("path", [
    "packages/arc-domain/pyproject.toml",
    "packages/arc-paper/src/arc_paper/__init__.py",
    "plugins/arc/.claude-plugin/plugin.json",
])
def test_version_drift(repo, path):
    target = repo / path
    target.write_text(target.read_text().replace("1.2.3", "1.2.4"))
    with pytest.raises(ValueError, match="version"):
        release.check_versions(repo, "v1.2.3")


def test_pins_require_official_reachable_matching_packages(repo, tmp_path):
    foundation = tmp_path / "foundation"
    foundation.mkdir()
    git(foundation, "init", "-q")
    (foundation / "packages").mkdir()
    (foundation / "VERSION").write_text("1.0.0")
    foundation_sha = commit(foundation)
    git(foundation, "update-ref", "refs/remotes/origin/main", foundation_sha)
    product_sha = git(repo, "rev-parse", "HEAD")
    lock = repo / "plugins/arc/skills/arc/scripts/runtime-sources.json"
    lock.parent.mkdir(parents=True)
    sources = [
        {"id": "product", "repository": "https://github.com/tririver/arc.git", "commit": product_sha},
        {"id": "foundation", "repository": "https://github.com/tririver/ac-foundation.git", "commit": foundation_sha},
    ]
    lock.write_text(json.dumps({"sources": sources}))
    release.check_pins(repo, foundation)
    sources[0]["repository"] = "https://github.com/some-fork/arc.git"
    lock.write_text(json.dumps({"sources": sources}))
    with pytest.raises(ValueError, match="official"):
        release.check_pins(repo, foundation)
    sources[0]["repository"] = "https://github.com/tririver/arc.git"
    lock.write_text(json.dumps({"sources": sources}))
    (repo / "packages/arc-paper/src/arc_paper/__init__.py").write_text('# changed code\n__version__ = "1.2.3"\n')
    commit(repo)
    with pytest.raises(ValueError, match="packages differ"):
        release.check_pins(repo, foundation)
