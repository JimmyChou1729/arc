from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"
old_dont_write_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
try:
    from _arc_workflows.source_checkout import validate_strict_checkout_path
finally:
    sys.path.remove(str(SCRIPTS))
    sys.dont_write_bytecode = old_dont_write_bytecode


def test_checkout_path_policy_is_inactive_without_required_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARC_REQUIRE_REPO_ROOT", raising=False)

    validate_strict_checkout_path(
        tmp_path / "missing",
        expected_relative_path="expected",
        field_name="workflow_dir",
    )


def test_checkout_path_policy_requires_the_exact_existing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "plugins" / "arc" / "workflows"
    expected.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ARC_REQUIRE_REPO_ROOT", str(tmp_path))

    validate_strict_checkout_path(
        expected,
        expected_relative_path="plugins/arc/workflows",
        field_name="workflow_dir",
    )

    with pytest.raises(
        ValueError,
        match="strict ARC source mode requires workflow_dir",
    ):
        validate_strict_checkout_path(
            other,
            expected_relative_path="plugins/arc/workflows",
            field_name="workflow_dir",
        )
    with pytest.raises(
        ValueError,
        match="strict ARC source mode cannot resolve workflow_dir",
    ):
        validate_strict_checkout_path(
            tmp_path / "missing",
            expected_relative_path="plugins/arc/workflows",
            field_name="workflow_dir",
        )
