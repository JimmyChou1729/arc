from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
PLUGIN = ROOT / "plugins/arc"
SCRIPTS = PLUGIN / "skills/arc/scripts"
EXPECTED = {
    "arc-paper": {"ac-document", "ac-jobs", "ac-llm"},
    "arc-domain": {"ac-jobs", "ac-llm", "arc-paper"},
}
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
ARC_MAJOR = int(VERSION.split(".")[0])


def _project(package: str) -> dict[str, object]:
    return tomllib.loads(
        (PACKAGES / package / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]


def test_package_set_metadata_and_dependency_graph() -> None:
    assert {path.name for path in PACKAGES.iterdir() if path.is_dir()} == set(EXPECTED)
    for package, internal in EXPECTED.items():
        project = _project(package)
        assert project["name"] == package
        assert project["version"] == VERSION
        assert project["authors"] == [{"name": "ARC"}]
        assert project["urls"]["Repository"] == "https://github.com/tririver/arc"
        dependencies = {
            dependency.split(">=", 1)[0]
            for dependency in project.get("dependencies", [])
            if dependency.startswith(("ac-", "arc-"))
        }
        assert dependencies == internal
        for dependency in project.get("dependencies", []):
            if dependency.startswith("arc-"):
                assert dependency.endswith(f">={ARC_MAJOR},<{ARC_MAJOR + 1}")
            elif dependency.startswith("ac-"):
                assert re.search(r">=\d+,<\d+$", dependency)


def test_research_packages_have_no_learning_or_legacy_core_dependency() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for package in PACKAGES.iterdir()
        for path in (*((package / "src").rglob("*.py")), package / "pyproject.toml")
        if path.is_file()
    )
    for stale in (
        "arc_render",
        "arc_translate",
        "arc_companion",
        "arc_ocr_proofread",
        "arc_document",
        "arc_jobs",
        "arc_llm",
        "arc_proposer_reviewer",
    ):
        assert stale not in source


def test_arc_paper_has_no_private_foundation_forwarders() -> None:
    package = PACKAGES / "arc-paper/src/arc_paper"
    retired = {
        "_durable_io.py",
        "_file_lock.py",
        "_parsed_document_cache.py",
        "_ripgrep.py",
    }
    assert not retired.intersection(path.name for path in package.iterdir())
    assert not list((package / "_parsing").glob("*.py"))
    assert not (package / "workflows/_llm.py").exists()
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in package.rglob("*.py")
    )
    assert "from ac_document._" not in source


def test_plugin_exposes_only_research_wrappers_and_workflows() -> None:
    wrappers = {path.name for path in (PLUGIN / "bin").iterdir() if path.is_file()}
    assert wrappers == {"arc-runtime", "arc-paper", "arc-domain"}
    workflows = {
        path.name for path in (PLUGIN / "skills/arc/workflows").iterdir()
        if path.is_file() and path.suffix == ".md"
    }
    assert workflows == {"domain.md", "ideas.md", "plan.md", "calculate.md", "check.md"}
    skill = (PLUGIN / "skills/arc/SKILL.md").read_text(encoding="utf-8")
    for learning in ("arc-render", "arc-translate", "arc-companion", "arc-ocr-proofread"):
        assert learning not in skill


def test_runtime_source_lock_uses_full_shas() -> None:
    lock = json.loads((SCRIPTS / "runtime-sources.json").read_text(encoding="utf-8"))
    assert lock["schema_version"] == "ac.runtime_sources.v2"
    assert lock["profile"] == "arc"
    assert {source["id"] for source in lock["sources"]} == {"foundation", "product"}
    for source in lock["sources"]:
        assert re.fullmatch(r"[0-9a-f]{40}", source["commit"])
        assert "version" not in source


def test_generated_foundation_copies_match_manifest() -> None:
    manifest = json.loads((SCRIPTS / "generated-sources.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "ac.generated_sources.v1"
    for relative, metadata in manifest["files"].items():
        path = (SCRIPTS / relative).resolve()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]


def test_release_script_covers_all_packages_without_publishing() -> None:
    release = (ROOT / "scripts/release-arc.sh").read_text(encoding="utf-8")
    for package in EXPECTED:
        assert f'"{package}"' in release
    assert "plugins/arc/.codex-plugin/plugin.json" in release
    assert "plugins/arc/.claude-plugin/plugin.json" in release
    assert "runtime-sources.json" in release
    assert "git push" not in release
    assert "git tag" not in release
    assert "check-generated-foundation.py" in release
    assert "check-runtime-constraints.py" in release


def test_build_outputs_are_checkout_local() -> None:
    build = (ROOT / "scripts/build-packages.sh").read_text(encoding="utf-8")
    assert "$root/local/dist" in build
    assert "packages/arc-*" in build
