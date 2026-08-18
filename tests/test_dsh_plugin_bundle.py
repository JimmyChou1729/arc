from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "package.json"
ADAPTER = ROOT / "plugins/arc/dsh/index.js"
BRIDGE = ROOT / "plugins/arc/dsh/llm-bridge.js"
BRIDGE_TEST = ROOT / "tests/test_dsh_llm_bridge.mjs"
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
      import { mkdtempSync, rmSync } from 'node:fs'
      import { tmpdir } from 'node:os'
      import { join } from 'node:path'
      import { apply, inject } from './plugins/arc/dsh/index.js'
      const root = mkdtempSync(join(tmpdir(), 'arc-dsh-plugin-'))
      process.env.DSH_ARC_LLM_SOCKET = join(root, 'bridge.sock')
      process.env.DSH_ARC_LLM_TOKEN_FILE = join(root, 'bridge.token')
      let captured
      let contributor
      let cleanup
      try {
        await apply({
          skills: { register(value) { captured = value } },
          llm: { prepareCall() { throw new Error('not called by registration test') } },
          shellEnv: { register(value) { contributor = value } },
          effect(callback) { cleanup = callback() },
        })
        if (!inject.includes('llm') || !inject.includes('shellEnv')) process.exit(1)
        if (captured?.name !== 'arc') process.exit(2)
        if (captured?.resourceBase?.kind !== 'directory') process.exit(3)
        if (!captured?.content?.includes('# Agent Research Copilot')) process.exit(4)
        if (!captured?.content?.includes('arc-runtime')) process.exit(5)
        const variables = contributor?.resolve()
        if (variables?.DSH_ARC_LLM_SOCKET !== process.env.DSH_ARC_LLM_SOCKET) process.exit(6)
        if (!variables?.DSH_ARC_RUNTIME?.endsWith('/scripts/arc-runtime')) process.exit(7)
      } finally {
        await cleanup?.()
        rmSync(root, { recursive: true, force: true })
      }
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
    )


@pytest.mark.skipif(not NODE_AVAILABLE, reason="DSH adapter requires Node.js")
def test_dsh_adapter_has_valid_node_syntax() -> None:
    subprocess.run(["node", "--check", str(ADAPTER)], cwd=ROOT, check=True)
    subprocess.run(["node", "--check", str(BRIDGE)], cwd=ROOT, check=True)


@pytest.mark.skipif(not NODE_AVAILABLE, reason="DSH adapter requires Node.js")
def test_dsh_native_bridge_protocol_smoke() -> None:
    subprocess.run(["node", str(BRIDGE_TEST)], cwd=ROOT, check=True)


def test_arc_skill_and_runtime_resources_are_present() -> None:
    assert SKILL.is_file()
    assert (SKILL.parent / "scripts/arc-runtime").is_file()
    assert (SKILL.parent / "manuals/arc-paper.md").is_file()
    assert (SKILL.parent / "rules/integrity.md").is_file()
