from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/arc/skills/arc"


def test_domain_workflow_script_commands_resolve_to_packaged_files() -> None:
    workflow = (SKILL / "workflows/domain.md").read_text(encoding="utf-8")
    references = set(re.findall(r"<skill-dir>/scripts/([A-Za-z0-9_.-]+)", workflow))
    assert references
    missing = sorted(name for name in references if not (SKILL / "scripts" / name).is_file())
    assert not missing, f"Domain workflow references missing packaged scripts: {missing}"
