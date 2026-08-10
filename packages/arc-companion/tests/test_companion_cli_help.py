from __future__ import annotations

import json

import pytest

from arc_companion.cli import main


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["build", "--help"],
        ["status", "--help"],
        ["resume", "--help"],
        ["stop", "--help"],
        ["render", "--help"],
        ["revise", "--help"],
        ["validate", "--help"],
    ],
)
def test_root_and_subcommand_help_is_human_readable(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("usage: arc-companion")
    assert "arc.command_result.v2" not in captured.out


def test_help_has_no_obsolete_json_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["status", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--json" not in output


def test_only_paper_access_commands_accept_a_paper_cache_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("build", "resume"):
        assert main([command, "--help"]) == 0
        assert "--paper-cache-root" in capsys.readouterr().out
    for command in ("render", "revise", "validate"):
        assert main([command, "--help"]) == 0
        assert "--paper-cache-root" not in capsys.readouterr().out


def test_build_and_resume_expose_explicit_host_authority(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("build", "resume"):
        assert main([command, "--help"]) == 0
        assert "--host-authority" in capsys.readouterr().out


def test_usage_error_points_to_contextual_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["status"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["error"]["code"] == "invalid_request"
    assert result["error"]["details"] == {
        "help_command": "arc-companion status --help"
    }
