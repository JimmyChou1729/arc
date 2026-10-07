from __future__ import annotations

import json
import os
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


@pytest.fixture(scope="module")
def unix_socket_capability():
    probe = """
    import { createServer } from 'node:net';
    import { mkdtempSync, rmSync } from 'node:fs';
    import { tmpdir } from 'node:os';
    import { join } from 'node:path';
    const root=mkdtempSync(join(tmpdir(),'arc-socket-probe-'));
    const server=createServer();
    server.on('error',error=>{rmSync(root,{recursive:true,force:true}); console.error(error.code);
      process.exit(['EPERM','EACCES'].includes(error.code)?77:1)});
    server.listen(join(root,'probe.sock'),()=>server.close(()=>rmSync(root,{recursive:true,force:true})));
    """
    result = subprocess.run(["node", "--input-type=module", "--eval", probe], capture_output=True, text=True)
    if result.returncode == 77 and os.environ.get("ARC_REQUIRE_DSH_TESTS") != "1":
        pytest.skip("Host denies Unix socket listen: " + result.stderr.strip())
    assert result.returncode == 0, "Required DSH socket capability failed: " + result.stderr


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


@pytest.mark.skipif(
    not NODE_AVAILABLE and os.environ.get("ARC_REQUIRE_DSH_TESTS") != "1",
    reason="DSH adapter requires Node.js",
)
def test_dsh_adapter_registers_existing_arc_skill(unix_socket_capability) -> None:
    script = """
      import { mkdtempSync, rmSync } from 'node:fs'
      import { tmpdir } from 'node:os'
      import { join } from 'node:path'
      import { apply, inject } from './plugins/arc/dsh/index.js'
      const root = mkdtempSync(join(tmpdir(), 'arc-dsh-plugin-'))
      process.env.DSH_AC_LLM_SOCKET = join(root, 'bridge.sock')
      process.env.DSH_AC_LLM_TOKEN_FILE = join(root, 'bridge.token')
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
        if (variables?.DSH_AC_LLM_SOCKET !== process.env.DSH_AC_LLM_SOCKET) process.exit(6)
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


@pytest.mark.skipif(
    not NODE_AVAILABLE and os.environ.get("ARC_REQUIRE_DSH_TESTS") != "1",
    reason="DSH adapter requires Node.js",
)
def test_dsh_adapter_has_valid_node_syntax() -> None:
    subprocess.run(["node", "--check", str(ADAPTER)], cwd=ROOT, check=True)
    subprocess.run(["node", "--check", str(BRIDGE)], cwd=ROOT, check=True)


@pytest.mark.skipif(
    not NODE_AVAILABLE and os.environ.get("ARC_REQUIRE_DSH_TESTS") != "1",
    reason="DSH adapter requires Node.js",
)
def test_dsh_native_bridge_protocol_smoke(unix_socket_capability) -> None:
    subprocess.run(["node", str(BRIDGE_TEST)], cwd=ROOT, check=True)


def test_arc_skill_and_runtime_resources_are_present() -> None:
    assert SKILL.is_file()
    assert (SKILL.parent / "scripts/arc-runtime").is_file()
    assert (SKILL.parent / "manuals/arc-paper.md").is_file()
    assert (SKILL.parent / "rules/integrity.md").is_file()
