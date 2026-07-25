from __future__ import annotations

import json

from arc_translate import cli


def _result(capsys):
    return json.loads(capsys.readouterr().out)


def test_cli_rejects_invalid_language_before_project_creation(tmp_path, capsys):
    project = tmp_path / "project"
    code = cli.main(
        [
            "detect-language",
            "paper.md",
            "--target-language",
            " ",
            "--project-dir",
            str(project),
        ]
    )
    assert code == 1
    assert _result(capsys)["error"]["code"] == "invalid_request"
    assert not project.exists()


def test_cli_never_implicitly_runs_missing_language_step(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    code = cli.main(
        [
            "build-glossary",
            "paper.md",
            "--project-dir",
            str(project),
        ]
    )
    assert code == 1
    assert _result(capsys)["error"]["code"] in {
        "project_not_found",
        "project_state_conflict",
    }
    assert tuple(project.iterdir()) == ()


def test_cli_validates_approximate_count_without_writes(tmp_path, capsys):
    project = tmp_path / "project"
    code = cli.main(
        [
            "build-glossary",
            "paper.md",
            "--approx-term-count",
            "0",
            "--project-dir",
            str(project),
        ]
    )
    assert code == 1
    assert _result(capsys)["error"]["code"] == "invalid_request"
    assert not project.exists()
