from __future__ import annotations

import tomllib
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_arc_paper_metadata_publishes_only_the_supported_cli() -> None:
    value = tomllib.loads(
        (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = value["project"]

    assert project["version"] == "2.0.0"
    assert project["scripts"] == {"arc-paper": "arc_paper.cli:main"}
    assert "ac-jobs>=2,<3" in project["dependencies"]
    assert "ac-llm>=2,<3" in project["dependencies"]
    assert project["optional-dependencies"] == {"test": ["pytest>=8.0"]}
    assert project["license"] == "MIT"
    assert project["requires-python"] == ">=3.11"
