from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "package.json"
ADAPTER = ROOT / "plugins/arc/dsh/index.js"
PATCH = ROOT / "plugins/arc/dsh/cordis.patch.yml"
SKILL = ROOT / "plugins/arc/skills/arc/SKILL.md"
NODE_AVAILABLE = shutil.which("node") is not None


def test_dsh_bundle_manifest_points_to_adapter_patch() -> None:
    manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
    assert manifest["name"] == "arc-dsh"
    assert manifest["private"] is True
    assert manifest["main"] == "./plugins/arc/dsh/index.js"
    assert manifest["dsh"]["bundle"]["patch"] == (
        "./plugins/arc/dsh/cordis.patch.yml"
    )
    assert "version" not in manifest
    assert "dependencies" not in manifest
    assert "publishConfig" not in manifest


def test_dsh_patch_loads_package_entry() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert "id: arc" in patch
    assert "name: arc-dsh" in patch


@pytest.mark.skipif(not NODE_AVAILABLE, reason="DSH adapter requires Node.js")
def test_dsh_adapter_registers_existing_arc_skill() -> None:
    script = """
      import { apply } from './plugins/arc/dsh/index.js'
      let captured
      apply({ skills: { register(value) { captured = value } } })
      if (captured?.name !== 'arc') process.exit(1)
      if (captured?.resourceBase?.kind !== 'directory') process.exit(2)
      if (!captured?.content?.includes('# Agent Research Copilot')) process.exit(3)
      if (!captured?.content?.includes('arc-runtime')) process.exit(4)
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
    )


@pytest.mark.skipif(not NODE_AVAILABLE, reason="DSH adapter requires Node.js")
def test_dsh_adapter_has_valid_node_syntax() -> None:
    subprocess.run(["node", "--check", str(ADAPTER)], cwd=ROOT, check=True)


def test_arc_skill_and_runtime_resources_are_present() -> None:
    assert SKILL.is_file()
    assert (SKILL.parent / "scripts/arc-runtime").is_file()
    assert (SKILL.parent / "manuals/arc-paper.md").is_file()
    assert (SKILL.parent / "rules/integrity.md").is_file()
