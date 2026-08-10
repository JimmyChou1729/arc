from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc_jobs import CommandResult, CommandStatus, RunStatus
from arc_render import BrowserValidation, HTMLRenderError

from arc_companion import cli
from arc_companion.project import CompanionProjectPaths


class _Service:
    def __init__(self, _root: Path, publication: object) -> None:
        self._publication = publication
        self._snapshot = SimpleNamespace(status=RunStatus.SUCCEEDED)

    def inspect(self, _run_id: str) -> object:
        return SimpleNamespace(snapshot=self._snapshot)

    def build_diagnostics(self, _run_id: str) -> None:
        return None

    def publication(self, _run_id: str) -> object:
        return self._publication

    def materialize_publication(
        self, _run_id: str, workspace: Path, **_kwargs: object
    ) -> Path:
        return workspace / "publication.json"


def _state(publication: object) -> object:
    return SimpleNamespace(
        publication=publication,
        publication_digest=publication.publication_digest,
        edition_digest="b" * 64,
        revisions=(),
        selected_revisions=(),
        selected_revision_digests=(),
    )


def test_status_does_not_advertise_stale_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run")
    publication_path = paths.publication_workspace("run") / "publication.json"
    publication_path.parent.mkdir(parents=True)
    publication_path.write_text("{}", encoding="utf-8")
    paths.delivery_html.write_text("stale", encoding="utf-8")
    publication = SimpleNamespace(publication_digest="a" * 64)
    service = _Service(paths.jobs_root, publication)

    monkeypatch.setattr(
        cli.CompanionProjectPaths, "load", lambda _value: paths
    )
    monkeypatch.setattr(cli, "CompanionService", lambda _root: service)
    monkeypatch.setattr(
        cli,
        "command_result_from_snapshot",
        lambda *_args, **_kwargs: CommandResult(CommandStatus.COMPLETED),
    )
    monkeypatch.setattr(cli, "snapshot_data", lambda _snapshot: {})
    monkeypatch.setattr(
        cli, "validate_publication_workspace", lambda _path: ()
    )
    monkeypatch.setattr(
        cli, "read_publication_workspace_state", lambda _path: _state(publication)
    )
    monkeypatch.setattr(
        cli,
        "validate_standalone_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTMLRenderError("wrong publication")
        ),
    )

    result = cli._status(SimpleNamespace(project_dir=str(paths.root)))

    assert [item.role for item in result.artifacts] == ["publication"]
    assert any(
        item.code == "standalone_html_stale"
        for item in result.warnings
    )


def test_status_never_advertises_html_when_workspace_state_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run")
    publication_path = paths.publication_workspace("run") / "publication.json"
    publication_path.parent.mkdir(parents=True)
    publication_path.write_text("{}", encoding="utf-8")
    paths.delivery_html.write_text("reader", encoding="utf-8")
    publication = SimpleNamespace(publication_digest="a" * 64)
    service = _Service(paths.jobs_root, publication)
    monkeypatch.setattr(cli.CompanionProjectPaths, "load", lambda _value: paths)
    monkeypatch.setattr(cli, "CompanionService", lambda _root: service)
    monkeypatch.setattr(
        cli,
        "command_result_from_snapshot",
        lambda *_args, **_kwargs: CommandResult(CommandStatus.COMPLETED),
    )
    monkeypatch.setattr(cli, "snapshot_data", lambda _snapshot: {})
    monkeypatch.setattr(cli, "validate_publication_workspace", lambda _path: ())
    monkeypatch.setattr(
        cli,
        "read_publication_workspace_state",
        lambda _path: (_ for _ in ()).throw(HTMLRenderError("bad workspace")),
    )
    monkeypatch.setattr(
        cli,
        "validate_standalone_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTML must not validate without workspace state")
        ),
    )

    result = cli._status(SimpleNamespace(project_dir=str(paths.root)))

    assert result.artifacts == ()
    assert result.data["workspace_html_consistent"] is False


def test_validate_requires_standalone_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run")
    publication = SimpleNamespace(publication_digest="a" * 64)
    service = _Service(paths.jobs_root, publication)
    publication_path = paths.publication_workspace("run") / "publication.json"
    publication_path.parent.mkdir(parents=True)
    publication_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        cli.CompanionProjectPaths, "load", lambda _value: paths
    )
    monkeypatch.setattr(cli, "CompanionService", lambda _root: service)
    monkeypatch.setattr(
        cli, "validate_publication_workspace", lambda _path: ()
    )
    monkeypatch.setattr(
        cli, "read_publication_workspace_state", lambda _path: _state(publication)
    )

    with pytest.raises(
        HTMLRenderError, match="no standalone HTML release"
    ):
        cli._validate(SimpleNamespace(project_dir=str(paths.root)))


def test_validate_forwards_optional_browser_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run")
    publication = SimpleNamespace(publication_digest="a" * 64)
    service = _Service(paths.jobs_root, publication)
    publication_path = paths.publication_workspace("run") / "publication.json"
    publication_path.parent.mkdir(parents=True)
    publication_path.write_text("{}", encoding="utf-8")
    paths.delivery_html.write_text("reader", encoding="utf-8")
    calls: list[tuple[Path, str | None, int]] = []

    monkeypatch.setattr(cli.CompanionProjectPaths, "load", lambda _value: paths)
    monkeypatch.setattr(cli, "CompanionService", lambda _root: service)
    monkeypatch.setattr(cli, "validate_publication_workspace", lambda _path: ())
    monkeypatch.setattr(
        cli, "read_publication_workspace_state", lambda _path: _state(publication)
    )
    monkeypatch.setattr(
        cli, "validate_standalone_html", lambda *_args, **_kwargs: None
    )

    def validate_browser(
        path: Path, *, browser_executable: str | None, timeout_seconds: int
    ) -> BrowserValidation:
        calls.append((path, browser_executable, timeout_seconds))
        return BrowserValidation("/usr/bin/chromium", timeout_seconds)

    monkeypatch.setattr(cli, "validate_reader_in_browser", validate_browser)

    result = cli._validate(SimpleNamespace(
        project_dir=str(paths.root),
        browser=True,
        browser_executable="custom-chromium",
        browser_timeout=11,
    ))

    assert calls == [(paths.delivery_html, "custom-chromium", 11)]
    assert result.data["browser"] == {
        "executable": "/usr/bin/chromium",
        "timeout_seconds": 11,
    }


@pytest.mark.parametrize(
    ("promoted", "artifact_roles"),
    ((True, ["publication", "web"]), (False, ["publication"])),
)
def test_render_advertises_only_the_promoted_root_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    promoted: bool,
    artifact_roles: list[str],
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run")
    publication = SimpleNamespace(publication_digest="a" * 64)
    service = _Service(paths.jobs_root, publication)
    run_html = paths.publication_html("run")

    monkeypatch.setattr(
        cli.CompanionProjectPaths, "load", lambda _value: paths
    )
    monkeypatch.setattr(cli, "CompanionService", lambda _root: service)
    monkeypatch.setattr(
        cli,
        "render_publication_html",
        lambda _publication, _output: SimpleNamespace(
            html_path=run_html,
            warnings=(),
        ),
    )
    monkeypatch.setattr(
        cli, "read_publication_workspace_state", lambda _path: _state(publication)
    )
    monkeypatch.setattr(
        cli, "validate_standalone_html", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        CompanionProjectPaths,
        "_promote_publication_html_locked",
        lambda _self, _run_id: promoted,
    )

    result = cli._render(SimpleNamespace(project_dir=str(paths.root)))

    assert [item.role for item in result.artifacts] == artifact_roles
    if promoted:
        assert result.artifacts[-1].path == str(paths.delivery_html)
        assert result.data["delivery"] == {
            "html": str(paths.delivery_html)
        }
    else:
        assert result.data["delivery"] == {}
        assert any(
            item.code == "publication_not_selected"
            for item in result.warnings
        )


def test_main_reports_html_validation_errors_as_protocol_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_dispatch",
        lambda _args: (_ for _ in ()).throw(
            HTMLRenderError("standalone HTML is invalid")
        ),
    )

    assert cli.main(["status", "--project-dir", "unused"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "html_render_failed"
