from __future__ import annotations

import errno
import json
import shutil
import threading
from pathlib import Path

import arc_companion.project as project_module
import arc_companion.release as release_module
import pytest
from arc_jobs import (
    Awaiting,
    Paused,
    ResumeReason,
    RunEngine,
    RunRepository,
    RunSpec,
    RunStatus,
)
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
        "stop",
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
    assert result["schema_version"] == "arc.command_result.v2"
    assert result["status"] == "failed"
    assert result["error"]["code"] == "legacy_project_state"
    assert legacy.read_bytes() == before
    assert tuple(project.iterdir()) == (legacy,)


def test_status_persists_source_diagnostics_and_stop_uses_same_run(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    run_id = "companion-diagnostics"
    project.select_run(run_id)
    project.write_source_diagnostics(
        run_id,
        ("no PDF validator was supplied",),
    )
    repository = RunRepository(project.jobs_root)
    class PausingHandler:
        name = "arc.companion.build.v1"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    RunEngine(repository).execute(
        RunSpec(run_id, PausingHandler.name, {}), PausingHandler()
    )

    assert main(
        ["status", "--project-dir", str(project.root), "--json"]
    ) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == "arc.command_result.v2"
    assert status["status"] == "paused"
    assert status["data"]["selected_run"] == status["data"]["run"]
    assert status["data"]["selected_run"]["id"] == run_id
    assert status["data"]["active_release"] is None
    assert status["data"]["release_matches_selected_run"] is False
    assert status["warnings"] == [
        {
            "code": "source_diagnostic",
            "details": {},
            "message": "no PDF validator was supplied",
        }
    ]

    stopped: list[tuple[str, str | None]] = []

    def fake_stop(self, received_run_id, *, reason=None):
        stopped.append((received_run_id, reason))
        return self.inspect(received_run_id)

    monkeypatch.setattr("arc_companion.cli.CompanionService.stop", fake_stop)
    assert main(
        [
            "stop",
            "--project-dir",
            str(project.root),
            "--reason",
            "user requested",
            "--json",
        ]
    ) == 0
    stopped_result = json.loads(capsys.readouterr().out)
    assert stopped_result["status"] == "completed"
    assert stopped_result["data"]["run"] == {
        "status": "paused",
        "attempt": 1,
        "stop_requested": False,
    }
    assert stopped == [(run_id, "user requested")]


def test_status_does_not_attribute_an_old_release_to_the_selected_run(
    tmp_path: Path, capsys
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    selected_run_id = "companion-selected"
    project.select_run(selected_run_id)
    project.publish_current(
        release_id="release-old",
        manifest=project.root / "releases/release-old/manifest.json",
        run_id="companion-old",
    )
    repository = RunRepository(project.jobs_root)

    class PausingHandler:
        name = "arc.companion.build.v1"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    RunEngine(repository).execute(
        RunSpec(selected_run_id, PausingHandler.name, {}), PausingHandler()
    )

    assert main(
        ["status", "--project-dir", str(project.root), "--json"]
    ) == 2
    status = json.loads(capsys.readouterr().out)
    data = status["data"]
    assert data["selected_run"] == data["run"]
    assert data["selected_run"]["id"] == selected_run_id
    assert data["active_release"] == data["release"]
    assert data["active_release"]["run_id"] == "companion-old"
    assert data["release_matches_selected_run"] is False
    assert status["warnings"] == [
        {
            "code": "release_pointer_stale",
            "details": {},
            "message": (
                "active release is missing; rerun render for the selected run"
            ),
        }
    ]

    project.publish_current(
        release_id="release-selected",
        manifest=(
            project.root / "releases/release-selected/manifest.json"
        ),
        run_id=selected_run_id,
    )
    assert main(
        ["status", "--project-dir", str(project.root), "--json"]
    ) == 2
    matched = json.loads(capsys.readouterr().out)["data"]
    assert matched["active_release"]["run_id"] == selected_run_id
    assert matched["release_matches_selected_run"] is True


def test_stop_acknowledges_a_running_attempt_before_it_pauses(
    tmp_path: Path, capsys
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    run_id = "companion-running-stop"
    project.select_run(run_id)
    repository = RunRepository(project.jobs_root)
    started = threading.Event()
    release = threading.Event()
    snapshots = []

    class BlockingHandler:
        name = "arc.companion.build.v1"

        def execute(self, context):
            started.set()
            assert release.wait(timeout=5)
            context.checkpoint()
            raise AssertionError("stopped attempt must not complete")

    thread = threading.Thread(
        target=lambda: snapshots.append(
            RunEngine(repository).execute(
                RunSpec(run_id, BlockingHandler.name, {}), BlockingHandler()
            )
        )
    )
    thread.start()
    assert started.wait(timeout=5)

    assert main(["stop", "--project-dir", str(project.root), "--reason", "pause"]) == 0
    acknowledgement = json.loads(capsys.readouterr().out)
    assert acknowledgement["status"] == "completed"
    assert acknowledgement["run"]["id"] == run_id
    assert acknowledgement["data"]["run"] == {
        "status": "running",
        "attempt": 1,
        "stop_requested": True,
    }

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert snapshots[0].status is RunStatus.PAUSED


def test_release_is_immutable_reused_and_current_updates_last(
    tmp_path: Path, monkeypatch
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    events: list[tuple[str, Path, bool]] = []
    original_release_fsync = release_module._fsync_directory
    original_project_write = project_module.atomic_write_json

    def record_release_fsync(path: Path) -> None:
        release_path = path / release_module.release_id_for(book)
        events.append(("release", path, release_path.is_dir()))
        original_release_fsync(path)

    def record_project_write(path: Path, value) -> None:
        original_project_write(path, value)
        events.append(("project", path.parent, project.current.is_file()))

    monkeypatch.setattr(release_module, "_fsync_directory", record_release_fsync)
    monkeypatch.setattr(project_module, "atomic_write_json", record_project_write)

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


def test_release_directory_fsync_is_skipped_on_windows(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(release_module, "_WINDOWS", True)
    monkeypatch.setattr(
        release_module.os,
        "open",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("Windows must not open directories for fsync")
        ),
    )

    release_module._fsync_directory(tmp_path)


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


def test_current_release_manifest_must_match_its_release_id(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    release = publisher.publish(book, run_id="run-one")
    current = project.current_release()
    assert current is not None

    validated = publisher.validate_current(current, book)
    assert validated.release_id == release.release_id

    project.publish_current(
        release_id=release.release_id,
        manifest=project.root / "releases/wrong/manifest.json",
        run_id="run-one",
    )
    malformed = project.current_release()
    assert malformed is not None
    with pytest.raises(
        CompanionReleaseError,
        match="manifest does not match",
    ):
        publisher.validate_current(malformed, book)


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
    assert result["schema_version"] == "arc.command_result.v2"
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
    assert result["schema_version"] == "arc.command_result.v2"
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_request"
    assert result["error"]["message"] == (
        "--pdf must be an existing path or 'fetch'"
    )
    assert not project.exists()
