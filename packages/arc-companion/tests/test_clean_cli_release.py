from __future__ import annotations

import errno
import json
import shutil
from pathlib import Path

import arc_companion.project as project_module
import arc_companion.release as release_module
import pytest
from arc_jobs import RunRepository, RunSpec
from arc_companion.cli import _parser, main
from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    SourceAnchor,
)
from arc_companion.project import CompanionProjectPaths
from arc_companion.release import (
    DELIVERY_RECIPE,
    RELEASE_MANIFEST_SCHEMA,
    CompanionReleaseError,
    CompanionReleasePublisher,
    release_id_for,
)


class _FakeRenderer:
    def render_all(self, book, *, web_dir: Path, pdf_path: Path):
        web_dir.mkdir(parents=True)
        (web_dir / "assets").mkdir()
        (web_dir / "assets" / "reader.css").write_text(
            ".source{display:block}", encoding="utf-8"
        )
        (web_dir / "index.html").write_text(
            f'<html data-book-digest="{book.content_digest}"></html>',
            encoding="utf-8",
        )
        pdf_path.write_bytes(b"%PDF-1.4\n% deterministic fixture\n")

    def validate_pdf(self, book, path: Path) -> None:
        assert path.read_bytes().startswith(b"%PDF")

    def validate_web(self, book, path: Path) -> None:
        assert book.content_digest in path.read_text(encoding="utf-8")


class _FailingRenderer:
    def render_all(self, book, *, web_dir: Path, pdf_path: Path):
        raise RuntimeError("renderer failed")


def _book() -> AcceptedBook:
    return AcceptedBook(
        document_digest="a" * 64,
        title="Fixture",
        source_language="en",
        target_language="en",
        translation_mode="skipped",
        chapters=(
            AcceptedChapter(
                chapter_id="chapter",
                title="Chapter",
                guide="Guide",
                source_anchors=(
                    SourceAnchor(
                        block_id="block",
                        ordinal=0,
                        kind="paragraph",
                        section_path=(),
                        payload={
                            "text": "Source",
                            "links": (),
                            "inline_math": (),
                            "inline_spans": (
                                {
                                    "kind": "text",
                                    "start": 0,
                                    "end": 6,
                                    "text": "Source",
                                },
                            ),
                        },
                    ),
                ),
            ),
        ),
    )


def test_cli_exposes_exactly_six_protocol_commands() -> None:
    parser = _parser()
    subparsers = next(
        action
        for action in parser._actions
        if hasattr(action, "choices") and action.choices
    )
    assert set(subparsers.choices) == {
        "build",
        "status",
        "resume",
        "cancel",
        "render",
        "validate",
    }


def test_legacy_project_is_rejected_without_modification(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nText.", encoding="utf-8")
    project = tmp_path / "legacy"
    project.mkdir()
    legacy = project / "state.json"
    legacy.write_text('{"legacy":true}\n', encoding="utf-8")
    before = legacy.read_bytes()

    assert main(
        ["build", str(source), "--project-dir", str(project), "--json"]
    ) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == "arc.command_result.v1"
    assert result["status"] == "failed"
    assert result["error"]["code"] == "legacy_project_state"
    assert legacy.read_bytes() == before
    assert tuple(project.iterdir()) == (legacy,)


def test_status_persists_source_diagnostics_and_cancel_uses_protocol(
    tmp_path: Path, capsys
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    run_id = "companion-diagnostics"
    project.select_run(run_id)
    project.write_source_diagnostics(
        run_id,
        ("no PDF validator was supplied",),
    )
    repository = RunRepository(project.jobs_root)
    repository.create(
        RunSpec(run_id, "arc.companion.build.v1", {})
    )
    repository.request_cancel(run_id, reason="prepared fixture")

    assert main(
        ["status", "--project-dir", str(project.root), "--json"]
    ) == 3
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == "arc.command_result.v1"
    assert status["status"] == "cancelled"
    assert status["warnings"] == [
        {
            "code": "source_diagnostic",
            "details": {},
            "message": "no PDF validator was supplied",
        }
    ]

    assert main(
        [
            "cancel",
            "--project-dir",
            str(project.root),
            "--reason",
            "user requested",
            "--json",
        ]
    ) == 3
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["status"] == "cancelled"


def test_release_is_immutable_reused_and_current_updates_last(
    tmp_path: Path, monkeypatch
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    events: list[tuple[str, Path, bool]] = []
    original_release_fsync = release_module._fsync_directory
    original_project_fsync = project_module._fsync_directory

    def record_release_fsync(path: Path) -> None:
        release_path = path / release_module.release_id_for(book)
        events.append(("release", path, release_path.is_dir()))
        original_release_fsync(path)

    def record_project_fsync(path: Path) -> None:
        events.append(("project", path, project.current.is_file()))
        original_project_fsync(path)

    monkeypatch.setattr(release_module, "_fsync_directory", record_release_fsync)
    monkeypatch.setattr(project_module, "_fsync_directory", record_project_fsync)

    first = publisher.publish(book, run_id="run-one")
    second = publisher.publish(book, run_id="run-two")

    assert not first.reused
    assert second.reused
    assert first.directory == second.directory
    assert first.pdf.read_bytes() == second.pdf.read_bytes()
    manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
    assert manifest["identity"]["accepted_book_digest"] == book.content_digest
    assert {item["path"] for item in manifest["files"]} >= {
        "companion.pdf",
        "reader/index.html",
    }
    current = project.current_release()
    assert current is not None
    assert current["release_id"] == first.release_id
    assert current["run_id"] == "run-two"
    release_parent_syncs = [
        event for event in events
        if event[0] == "release" and event[1] == project.releases_root
    ]
    assert release_parent_syncs[-1][2]
    current_parent_syncs = [
        event for event in events
        if event[0] == "project" and event[1] == project.root
    ]
    assert current_parent_syncs
    assert all(event[2] for event in current_parent_syncs)
    assert events.index(release_parent_syncs[-1]) < events.index(
        current_parent_syncs[0]
    )


@pytest.mark.parametrize("race_errno", [errno.EEXIST, errno.ENOTEMPTY])
def test_release_publish_reuses_concurrent_winner_and_cleans_staging(
    tmp_path: Path, monkeypatch, race_errno: int
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()

    def concurrent_winner(staging: Path, target: Path) -> None:
        shutil.copytree(staging, target)
        raise OSError(race_errno, "cooperating publisher won")

    monkeypatch.setattr(release_module.os, "rename", concurrent_winner)

    release = publisher.publish(book, run_id="run-race-loser")

    assert release.reused
    assert release.directory == project.releases_root / release_id_for(book)
    assert not tuple(project.releases_root.glob(f".{release.release_id}.*"))
    current = project.current_release()
    assert current is not None
    assert current["release_id"] == release.release_id
    assert current["run_id"] == "run-race-loser"


def test_release_publish_reraises_unrelated_rename_error(
    tmp_path: Path, monkeypatch
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]

    def fail_rename(_staging: Path, _target: Path) -> None:
        raise OSError(errno.EACCES, "rename denied")

    monkeypatch.setattr(release_module.os, "rename", fail_rename)

    with pytest.raises(OSError, match="rename denied"):
        publisher.publish(_book(), run_id="run-error")

    assert not project.current.exists()
    assert not tuple(project.releases_root.iterdir())


def test_release_renderer_failure_does_not_publish_current(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FailingRenderer())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="renderer failed"):
        publisher.publish(_book(), run_id="run-render-error")

    assert not project.current.exists()
    assert not tuple(project.releases_root.iterdir())


def test_release_identity_covers_delivery_contract_and_rejects_extra_files(
    tmp_path: Path, monkeypatch
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()

    release = publisher.publish(book, run_id="run-one")
    manifest = json.loads(release.manifest.read_text(encoding="utf-8"))
    assert manifest["release_id"] == release_id_for(book)
    assert manifest["identity"]["delivery_recipe"] == DELIVERY_RECIPE
    assert manifest["identity"]["manifest_schema"] == RELEASE_MANIFEST_SCHEMA
    original_release_id = release_id_for(book)
    monkeypatch.setattr(
        release_module,
        "WEB_RENDER_RECIPE",
        "arc.companion.web-render.test-next",
    )
    assert book.content_digest == _book().content_digest
    assert release_id_for(book) != original_release_id
    monkeypatch.undo()

    extra = release.directory / "reader" / "unexpected.txt"
    extra.write_text("not declared by the immutable manifest", encoding="utf-8")
    with pytest.raises(CompanionReleaseError, match="file set"):
        publisher.publish(book, run_id="run-two")


@pytest.mark.parametrize(
    "argv",
    [
        ["build", "unused-source", "--project-dir", "unused-project", "--workers", "0"],
        [
            "build",
            "unused-source",
            "--project-dir",
            "unused-project",
            "--workers",
            "25",
        ],
        ["resume", "--project-dir", "unused-project", "--workers", "0"],
        ["resume", "--project-dir", "unused-project", "--workers", "25"],
    ],
)
def test_cli_rejects_build_and_resume_worker_bounds(
    argv: list[str], capsys
) -> None:
    assert main(argv) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == "arc.command_result.v1"
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_request"
    assert result["error"]["message"] == "--workers must be between 1 and 24"


def test_cli_rejects_missing_pdf_before_source_or_project_import(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nText.", encoding="utf-8")
    project = tmp_path / "project"
    missing_pdf = tmp_path / "missing.pdf"

    def unexpected_import(*_args, **_kwargs):
        raise AssertionError("source import must not run")

    monkeypatch.setattr(
        "arc_companion.cli.ArcPaperService.import_source",
        unexpected_import,
    )

    assert main(
        [
            "build",
            str(source),
            "--project-dir",
            str(project),
            "--pdf",
            str(missing_pdf),
            "--json",
        ]
    ) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == "arc.command_result.v1"
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_request"
    assert result["error"]["message"] == (
        "--pdf must be an existing path or 'fetch'"
    )
    assert not project.exists()
