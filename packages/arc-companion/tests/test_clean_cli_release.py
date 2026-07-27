from __future__ import annotations

import errno
import hashlib
import json
import shutil
import threading
from dataclasses import replace
from pathlib import Path

import arc_companion.cli as cli_module
import arc_companion.project as project_module
import arc_companion.release as release_module
import pytest
from arc_jobs import (
    Awaiting,
    Failed,
    Paused,
    ResumeReason,
    RunEngine,
    RunError,
    RunRepository,
    RunSpec,
    RunStatus,
    file_lease,
)
from arc_companion.cli import _parser, main
from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    SourceAnchor,
)
from arc_companion.project import CompanionProjectError, CompanionProjectPaths
from arc_companion.release import (
    DELIVERY_RECIPE,
    RELEASE_MANIFEST_SCHEMA,
    CompanionReleaseError,
    CompanionReleasePublisher,
    release_id_for,
)
from arc_companion.renderer import RenderedCompanion


class _FakeRenderer:
    def render_all(self, book, *, web_dir: Path, pdf_path: Path):
        web_dir.mkdir(parents=True)
        (web_dir / "assets").mkdir()
        (web_dir / "assets" / "reader.css").write_text(
            ".source{display:block}", encoding="utf-8"
        )
        (web_dir / "index.html").write_text(
            (
                f'<html data-book-digest="{book.content_digest}">'
                '<head><link rel="stylesheet" href="assets/reader.css"></head>'
                '<body><a href="#chapter">Chapter</a></body></html>'
            ),
            encoding="utf-8",
        )
        pdf_path.write_bytes(b"%PDF-1.4\n% deterministic fixture\n")
        return RenderedCompanion(
            accepted_book_digest=book.content_digest,
            web_index=web_dir / "index.html",
            pdf_path=pdf_path,
        )

    def validate_pdf(self, book, path: Path) -> None:
        assert path.read_bytes().startswith(b"%PDF")

    def validate_web(self, book, path: Path) -> None:
        assert book.content_digest in path.read_text(encoding="utf-8")


class _FailingRenderer:
    def render_all(self, book, *, web_dir: Path, pdf_path: Path):
        raise RuntimeError("renderer failed")


class _WebOnlyRenderer(_FakeRenderer):
    def render_all(self, book, *, web_dir: Path, pdf_path: Path):
        rendered = super().render_all(
            book, web_dir=web_dir, pdf_path=pdf_path
        )
        pdf_path.unlink()
        return RenderedCompanion(
            accepted_book_digest=rendered.accepted_book_digest,
            web_index=rendered.web_index,
            warnings=("latexmk is unavailable",),
        )


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


def test_nonempty_explicit_project_preserves_unrelated_files_on_initialization(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "author"
    notes = project_root / "notes"
    notes.mkdir(parents=True)
    source = project_root / "source.md"
    source.write_text("# Source\n\nText.", encoding="utf-8")
    unknown = notes / "state.json"
    unknown.write_text('{"unknown":true}\n', encoding="utf-8")
    source_before = source.read_bytes()
    before = unknown.read_bytes()

    project = CompanionProjectPaths.open(project_root)

    assert project.root == project_root.resolve()
    assert source.read_bytes() == source_before
    assert unknown.read_bytes() == before
    assert project.marker.is_file()
    assert {path.relative_to(project_root) for path in project_root.iterdir()} == {
        Path(".arc"),
        Path("notes"),
        Path("source.md"),
    }


@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        (".arc/companion", "directory"),
        ("companion.pdf", "file"),
        ("companion.html", "file"),
        ("releases", "directory"),
        (".arc", "file"),
    ],
)
def test_initialization_rejects_unclaimed_managed_path_without_modification(
    tmp_path: Path,
    relative: str,
    kind: str,
) -> None:
    project_root = tmp_path / "author"
    project_root.mkdir()
    unrelated = project_root / "source.md"
    unrelated.write_bytes(b"source bytes")
    conflict = project_root / relative
    if kind == "directory":
        conflict.mkdir(parents=True)
        (conflict / "user-owned.txt").write_bytes(b"user bytes")
    else:
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_bytes(b"user bytes")
    before = {
        path.relative_to(project_root): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in project_root.rglob("*")
    }

    with pytest.raises(CompanionProjectError) as exc_info:
        CompanionProjectPaths.open(project_root)

    assert exc_info.value.code == "project_path_conflict"
    assert str(conflict.resolve()) in str(exc_info.value)
    after = {
        path.relative_to(project_root): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in project_root.rglob("*")
    }
    assert after == before
    assert unrelated.read_bytes() == b"source bytes"


def test_project_runtime_is_hidden_and_unrelated_arc_state_is_preserved(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    domain = project_root / ".arc" / "domain"
    domain.mkdir(parents=True)
    domain_state = domain / "state.json"
    domain_state.write_bytes(b'{"domain":true}\n')

    project = CompanionProjectPaths.open(project_root)

    assert project.runtime_root == project_root / ".arc" / "companion"
    assert project.marker == project.runtime_root / "project.json"
    assert domain_state.read_bytes() == b'{"domain":true}\n'
    assert not (project_root / "companion-project.json").exists()
    assert not (project.runtime_root / "paper-cache").exists()


def test_other_arc_state_does_not_bypass_companion_path_conflict(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".arc" / "domain").mkdir(parents=True)
    releases = project_root / "releases"
    releases.mkdir()
    owned = releases / "user-owned.txt"
    owned.write_bytes(b"user bytes")

    with pytest.raises(CompanionProjectError) as exc_info:
        CompanionProjectPaths.open(project_root)

    assert exc_info.value.code == "project_path_conflict"
    assert str(releases.resolve()) in str(exc_info.value)
    assert owned.read_bytes() == b"user bytes"
    assert not (project_root / ".arc" / "companion").exists()


def test_publisher_freezes_source_assets_for_later_project_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    payload = b"project-owned source image"
    digest = hashlib.sha256(payload).hexdigest()
    calls: list[Path | None] = []

    class _Repository:
        def get_asset(self, requested: str):
            assert requested == digest
            return object()

        def read_asset_bytes(self, _asset: object) -> bytes:
            return payload

    class _Paper:
        def __init__(self, *, cache_root=None) -> None:
            calls.append(cache_root)
            self.repository = _Repository()

    monkeypatch.setattr(cli_module, "ArcPaperService", _Paper)
    publisher = cli_module._publisher(project, paper_cache_root=tmp_path / "shared")
    loader = publisher.renderer._asset_loader
    assert loader is not None
    assert loader(digest) == payload
    assert project.frozen_asset_path(digest).read_bytes() == payload
    assert calls == [tmp_path / "shared"]

    project.frozen_asset_path(digest).write_bytes(b"accidental corruption")
    assert loader(digest) == payload
    assert project.frozen_asset_path(digest).read_bytes() == payload
    assert calls == [tmp_path / "shared"]

    monkeypatch.setattr(
        cli_module,
        "ArcPaperService",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen asset must not reopen the paper cache")
        ),
    )
    later = cli_module._publisher(project)
    later_loader = later.renderer._asset_loader
    assert later_loader is not None
    assert later_loader(digest) == payload


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
        name = "arc.companion.build.v2"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    RunEngine(repository).execute(
        RunSpec(run_id, PausingHandler.name, {}), PausingHandler()
    )

    assert main(
        ["status", "--project-dir", str(project.root)]
    ) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == "arc.command_result.v2"
    assert status["status"] == "paused"
    assert "run" not in status["data"]
    assert "release" not in status["data"]
    assert status["data"]["selected_run"]["id"] == run_id
    assert status["data"]["active_release"] is None
    assert status["data"]["release_matches_selected_run"] is False
    assert not project.delivery_lease.exists()
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
        name = "arc.companion.build.v2"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    RunEngine(repository).execute(
        RunSpec(selected_run_id, PausingHandler.name, {}), PausingHandler()
    )

    assert main(
        ["status", "--project-dir", str(project.root)]
    ) == 2
    status = json.loads(capsys.readouterr().out)
    data = status["data"]
    assert "run" not in data
    assert data["selected_run"]["id"] == selected_run_id
    assert "release" not in data
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
        ["status", "--project-dir", str(project.root)]
    ) == 2
    matched = json.loads(capsys.readouterr().out)["data"]
    assert matched["active_release"]["run_id"] == selected_run_id
    assert matched["release_matches_selected_run"] is True


@pytest.mark.parametrize("delivery_state", ["missing", "mixed", "corrupt"])
def test_status_warns_when_current_delivery_pair_is_invalid(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    delivery_state: str,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    run_id = "companion-delivery-status"
    project.select_run(run_id)
    repository = RunRepository(project.jobs_root)

    class PausingHandler:
        name = "arc.companion.build.v2"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    RunEngine(repository).execute(
        RunSpec(run_id, PausingHandler.name, {}),
        PausingHandler(),
    )
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    first = publisher.publish(_book(), run_id=run_id)
    if delivery_state == "missing":
        project.delivery_html.unlink()
    elif delivery_state == "corrupt":
        project.delivery_pdf.write_bytes(b"corrupt")
    else:
        publisher.publish(
            replace(_book(), title="Second fixture"),
            run_id="other-run",
        )
        project.delivery_pdf.write_bytes(first.pdf.read_bytes())
        project.publish_current(
            release_id=first.release_id,
            manifest=first.manifest,
            run_id=run_id,
        )
    before = {
        path.relative_to(project.root): path.read_bytes()
        for path in project.root.rglob("*")
        if path.is_file()
    }

    monkeypatch.setattr(
        "arc_companion.cli._publisher",
        lambda _paths: (_ for _ in ()).throw(
            AssertionError("status must not construct a publisher")
        ),
    )

    assert main(["status", "--project-dir", str(project.root)]) == 2

    result = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in result["warnings"]] == [
        "delivery_invalid"
    ]
    after = {
        path.relative_to(project.root): path.read_bytes()
        for path in project.root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_status_waits_for_delivery_publication_before_reading_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    run_id = "companion-delivery-sync"
    project.select_run(run_id)
    repository = RunRepository(project.jobs_root)

    class PausingHandler:
        name = "arc.companion.build.v2"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    class DistinctPdfRenderer(_FakeRenderer):
        def render_all(self, book, *, web_dir: Path, pdf_path: Path):
            rendered = super().render_all(
                book, web_dir=web_dir, pdf_path=pdf_path
            )
            pdf_path.write_bytes(
                b"%PDF-1.4\n% " + book.title.encode("utf-8") + b"\n"
            )
            return rendered

    RunEngine(repository).execute(
        RunSpec(run_id, PausingHandler.name, {}),
        PausingHandler(),
    )
    publisher = CompanionReleasePublisher(project, DistinctPdfRenderer())  # type: ignore[arg-type]
    first = publisher.publish(_book(), run_id=run_id)
    pdf_replaced = threading.Event()
    continue_render = threading.Event()
    status_lease_requested = threading.Event()
    status_validating = threading.Event()
    render_results = []
    render_errors: list[BaseException] = []
    status_results = []
    status_errors: list[BaseException] = []
    original_replace = publisher._replace_delivery_file
    original_validate = cli_module.validate_current_delivery

    def pause_after_pdf(staged: Path, target: Path) -> None:
        original_replace(staged, target)
        if target == project.delivery_pdf:
            pdf_replaced.set()
            assert continue_render.wait(timeout=5)

    def observed_file_lease(path: Path, *, blocking: bool):
        status_lease_requested.set()
        return file_lease(path, blocking=blocking)

    def observed_validate(paths, pointer):
        status_validating.set()
        return original_validate(paths, pointer)

    def render() -> None:
        try:
            render_results.append(
                publisher.publish(
                    replace(_book(), title="Second fixture"),
                    run_id=run_id,
                )
            )
        except BaseException as exc:
            render_errors.append(exc)

    def status() -> None:
        try:
            status_results.append(
                cli_module._status(
                    _parser().parse_args(
                        ["status", "--project-dir", str(project.root)]
                    )
                )
            )
        except BaseException as exc:
            status_errors.append(exc)

    monkeypatch.setattr(publisher, "_replace_delivery_file", pause_after_pdf)
    render_thread = threading.Thread(target=render)
    render_thread.start()
    assert pdf_replaced.wait(timeout=5)
    assert project.delivery_pdf.read_bytes() != first.pdf.read_bytes()
    assert _book().content_digest in project.delivery_html.read_text(encoding="utf-8")

    monkeypatch.setattr(cli_module, "file_lease", observed_file_lease)
    monkeypatch.setattr(
        cli_module,
        "validate_current_delivery",
        observed_validate,
    )
    status_thread = threading.Thread(target=status)
    status_thread.start()
    assert status_lease_requested.wait(timeout=5)
    assert not status_validating.is_set()

    continue_render.set()
    render_thread.join(timeout=5)
    status_thread.join(timeout=5)
    assert not render_thread.is_alive()
    assert not status_thread.is_alive()
    assert render_errors == []
    assert status_errors == []
    assert status_validating.is_set()

    result = status_results[0]
    assert result.data["active_release"]["release_id"] == (
        render_results[0].release_id
    )
    assert result.warnings == ()


def test_status_reports_web_only_current_release_without_pdf_artifact(
    tmp_path: Path,
    capsys,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    run_id = "companion-web-only-status"
    project.select_run(run_id)
    repository = RunRepository(project.jobs_root)

    class PausingHandler:
        name = "arc.companion.build.v2"

        def execute(self, _context):
            return Paused(
                Awaiting(ResumeReason.EXTERNAL_CONDITION, "resume", False)
            )

    RunEngine(repository).execute(
        RunSpec(run_id, PausingHandler.name, {}),
        PausingHandler(),
    )
    release = CompanionReleasePublisher(  # type: ignore[arg-type]
        project,
        _WebOnlyRenderer(),
    ).publish(_book(), run_id=run_id)

    assert main(["status", "--project-dir", str(project.root)]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["data"]["active_release"]["release_id"] == (
        release.release_id
    )
    assert result["data"]["active_release"]["available_formats"] == ["web"]
    assert {item["role"] for item in result["artifacts"]} == {
        "manifest",
        "web",
    }
    assert result["warnings"] == []
    assert not project.delivery_pdf.exists()


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
        name = "arc.companion.build.v2"

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
        delivery_ready = (
            project.delivery_pdf.is_file()
            and project.delivery_html.is_file()
        )
        original_project_write(path, value)
        events.append(("project", path.parent, delivery_ready))

    monkeypatch.setattr(release_module, "_fsync_directory", record_release_fsync)
    monkeypatch.setattr(project_module, "atomic_write_json", record_project_write)

    first = publisher.publish(book, run_id="run-one")
    second = publisher.publish(book, run_id="run-two")

    assert not first.reused
    assert second.reused
    assert first.directory == second.directory
    assert first.pdf.read_bytes() == second.pdf.read_bytes()
    assert project.delivery_pdf.read_bytes() == first.pdf.read_bytes()
    delivered_html = project.delivery_html.read_text(encoding="utf-8")
    assert "<base" not in delivered_html.casefold()
    assert "assets/reader.css" not in delivered_html
    assert "Content-Security-Policy" in delivered_html
    assert ".source{display:block}" in delivered_html
    assert project.delivery_html.read_bytes() != first.web_index.read_bytes()
    manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
    assert manifest["identity"]["accepted_book_digest"] == book.content_digest
    assert manifest["available_formats"] == ["pdf", "web"]
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
        if event[0] == "project" and event[1] == project.runtime_root
    ]
    assert current_parent_syncs
    assert all(event[2] for event in current_parent_syncs)
    assert events.index(release_parent_syncs[-1]) < events.index(
        current_parent_syncs[0]
    )


def test_pdf_failure_publishes_web_only_and_later_retry_publishes_full_release(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    initial = CompanionReleasePublisher(project, _FakeRenderer()).publish(  # type: ignore[arg-type]
        _book(),
        run_id="initial-run",
    )
    assert project.delivery_pdf.is_file()
    retry_book = replace(_book(), title="Retry fixture")

    web_only = CompanionReleasePublisher(  # type: ignore[arg-type]
        project,
        _WebOnlyRenderer(),
    ).publish(retry_book, run_id="web-only-run")

    assert web_only.available_formats == ("web",)
    assert web_only.pdf is None
    assert web_only.warnings == ("latexmk is unavailable",)
    assert web_only.release_id != initial.release_id
    assert not (web_only.directory / "companion.pdf").exists()
    web_manifest = json.loads(
        web_only.manifest.read_text(encoding="utf-8")
    )
    assert web_manifest["available_formats"] == ["web"]
    assert "companion.pdf" not in {
        item["path"] for item in web_manifest["files"]
    }
    assert not project.delivery_pdf.exists()
    assert retry_book.content_digest in project.delivery_html.read_text(
        encoding="utf-8"
    )
    current = project.current_release()
    assert current is not None
    assert current["release_id"] == web_only.release_id
    validated_web = CompanionReleasePublisher(  # type: ignore[arg-type]
        project,
        _FakeRenderer(),
    ).validate_current(current, retry_book)
    assert validated_web.available_formats == ("web",)
    assert validated_web.pdf is None

    full = CompanionReleasePublisher(project, _FakeRenderer()).publish(  # type: ignore[arg-type]
        retry_book,
        run_id="full-retry-run",
    )

    assert full.available_formats == ("pdf", "web")
    assert full.pdf is not None
    assert full.release_id != web_only.release_id
    assert web_only.directory.is_dir()
    assert not (web_only.directory / "companion.pdf").exists()
    assert project.delivery_pdf.read_bytes() == full.pdf.read_bytes()
    assert project.current_release()["release_id"] == full.release_id  # type: ignore[index]


def test_legacy_release_manifest_and_current_pointer_remain_readable(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    current_release = publisher.publish(book, run_id="new-run")
    current_manifest = json.loads(
        current_release.manifest.read_text(encoding="utf-8")
    )
    legacy_identity = release_module._legacy_release_identity(book)
    legacy_id = release_module._release_id(legacy_identity)
    legacy_directory = project.releases_root / legacy_id
    shutil.copytree(current_release.directory, legacy_directory)
    legacy_manifest = legacy_directory / "manifest.json"
    legacy_manifest.write_bytes(
        release_module.canonical_json_bytes(
            {
                "schema_version": "arc.companion.release_manifest.v1",
                "release_id": legacy_id,
                "identity": legacy_identity,
                "files": current_manifest["files"],
            }
        )
        + b"\n"
    )
    legacy_pdf = legacy_directory / "companion.pdf"
    legacy_web = legacy_directory / "reader" / "index.html"
    project.delivery_pdf.write_bytes(legacy_pdf.read_bytes())
    project.delivery_html.write_bytes(
        release_module._delivery_html_bytes(
            legacy_web,
            legacy_id,
            release_module._LEGACY_DELIVERY_RECIPE,
        )
    )
    project.publish_current(
        release_id=legacy_id,
        manifest=legacy_manifest,
        run_id="legacy-run",
    )

    pointer = project.current_release()
    assert pointer is not None
    validated = publisher.validate_current(pointer, book)

    assert validated.release_id == legacy_id
    assert validated.available_formats == ("pdf", "web")
    assert release_module.validate_current_delivery(project, pointer) == (
        "pdf",
        "web",
    )


def test_v2_delivery_release_remains_valid_after_standalone_delivery_upgrade(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    current = publisher.publish(book, run_id="new-run")
    current_manifest = json.loads(current.manifest.read_text(encoding="utf-8"))
    legacy_identity = release_module._release_identity_for_recipe(
        book,
        ("pdf", "web"),
        release_module._BASE_DELIVERY_RECIPE,
    )
    legacy_id = release_module._release_id(legacy_identity)
    legacy_directory = project.releases_root / legacy_id
    shutil.copytree(current.directory, legacy_directory)
    legacy_manifest = legacy_directory / "manifest.json"
    legacy_manifest.write_bytes(
        release_module.canonical_json_bytes(
            {
                "schema_version": release_module.RELEASE_MANIFEST_SCHEMA,
                "release_id": legacy_id,
                "identity": legacy_identity,
                "available_formats": ["pdf", "web"],
                "files": current_manifest["files"],
            }
        )
        + b"\n"
    )
    project.delivery_pdf.write_bytes(
        (legacy_directory / "companion.pdf").read_bytes()
    )
    project.delivery_html.write_bytes(
        release_module._delivery_html_bytes(
            legacy_directory / "reader" / "index.html",
            legacy_id,
            release_module._BASE_DELIVERY_RECIPE,
        )
    )
    project.publish_current(
        release_id=legacy_id,
        manifest=legacy_manifest,
        run_id="legacy-v2-run",
    )

    pointer = project.current_release()
    assert pointer is not None
    assert publisher.validate_current(pointer, book).release_id == legacy_id
    assert release_module.validate_current_delivery(project, pointer) == (
        "pdf",
        "web",
    )


def test_failed_recovery_keeps_active_release_and_delivery_bytes(tmp_path):
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    publisher.publish(_book(), run_id="published-run")
    before_current = project.current_release()
    before_pdf = project.delivery_pdf.read_bytes()
    before_html = project.delivery_html.read_bytes()

    class AlwaysFail:
        name = "companion-recovery-test.v1"

        def execute(self, _context):
            return Failed(RunError("expected", "repair before publishing"))

    repository = RunRepository(project.jobs_root)
    engine = RunEngine(repository)
    handler = AlwaysFail()
    failed = engine.execute(
        RunSpec("failed-build", handler.name, {}), handler
    )
    first_result = cli_module._snapshot_result(project, failed)
    retried = engine.resume("failed-build", handler)
    second_result = cli_module._snapshot_result(project, retried)

    assert first_result.status.value == "failed"
    assert second_result.status.value == "failed"
    assert retried.recovery_epoch == 1
    assert project.current_release() == before_current
    assert project.delivery_pdf.read_bytes() == before_pdf
    assert project.delivery_html.read_bytes() == before_html


def test_reused_release_repairs_missing_and_corrupt_delivery_copies(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    first = publisher.publish(book, run_id="run-one")
    project.delivery_pdf.unlink()
    project.delivery_html.write_text("corrupt", encoding="utf-8")

    repaired = publisher.publish(book, run_id="run-two")

    assert repaired.reused
    assert repaired.release_id == first.release_id
    assert project.delivery_pdf.read_bytes() == repaired.pdf.read_bytes()
    assert "<base" not in project.delivery_html.read_text(
        encoding="utf-8"
    ).casefold()


def test_new_release_replaces_delivery_but_keeps_old_release(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    first = publisher.publish(_book(), run_id="run-one")
    second_book = replace(_book(), title="Second fixture")

    second = publisher.publish(second_book, run_id="run-two")

    assert second.release_id != first.release_id
    assert first.directory.is_dir()
    assert second.directory.is_dir()
    assert project.delivery_pdf.read_bytes() == second.pdf.read_bytes()
    delivered_html = project.delivery_html.read_text(encoding="utf-8")
    assert "<base" not in delivered_html.casefold()
    assert second_book.content_digest in delivered_html
    assert first.release_id not in delivered_html
    current = project.current_release()
    assert current is not None
    assert current["release_id"] == second.release_id
    assert current["run_id"] == "run-two"


@pytest.mark.parametrize("target_name", ["companion.pdf", "companion.html"])
def test_unknown_preexisting_delivery_target_is_rejected(
    tmp_path: Path,
    target_name: str,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    target = project.root / target_name
    target.write_bytes(b"user-owned content")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]

    with pytest.raises(CompanionReleaseError) as exc_info:
        publisher.publish(_book(), run_id="run-conflict")

    assert exc_info.value.code == "delivery_conflict"
    assert target.read_bytes() == b"user-owned content"
    assert not project.current.exists()
    assert not project.releases_root.exists()


def test_delivery_failure_keeps_current_pointer_last_and_retry_repairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    first = publisher.publish(_book(), run_id="run-one")
    second_book = replace(_book(), title="Second fixture")
    second_id = release_id_for(second_book)
    original_replace = publisher._replace_delivery_file

    def fail_html(staged: Path, target: Path) -> None:
        if target == project.delivery_html:
            raise OSError("delivery interruption")
        original_replace(staged, target)

    monkeypatch.setattr(publisher, "_replace_delivery_file", fail_html)

    with pytest.raises(OSError, match="delivery interruption"):
        publisher.publish(second_book, run_id="run-two")

    assert not tuple(project.runtime_root.glob(".delivery.*"))
    current = project.current_release()
    assert current is not None
    assert current["release_id"] == first.release_id
    assert current["run_id"] == "run-one"
    assert project.delivery_pdf.read_bytes() == first.pdf.read_bytes()
    assert _book().content_digest in project.delivery_html.read_text(
        encoding="utf-8"
    )

    monkeypatch.undo()
    repaired = publisher.publish(second_book, run_id="run-two")
    assert repaired.reused
    assert project.current_release()["release_id"] == second_id  # type: ignore[index]
    assert second_book.content_digest in project.delivery_html.read_text(
        encoding="utf-8"
    )


def test_first_publish_failure_removes_both_new_delivery_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    original_replace = publisher._replace_delivery_file

    def fail_html(staged: Path, target: Path) -> None:
        if target == project.delivery_html:
            raise OSError("delivery interruption")
        original_replace(staged, target)

    monkeypatch.setattr(publisher, "_replace_delivery_file", fail_html)

    with pytest.raises(OSError, match="delivery interruption"):
        publisher.publish(_book(), run_id="run-one")

    assert not project.delivery_pdf.exists()
    assert not project.delivery_html.exists()
    assert not project.current.exists()
    assert not tuple(project.runtime_root.glob(".delivery.*"))

    monkeypatch.undo()
    repaired = publisher.publish(_book(), run_id="run-one")
    assert repaired.reused
    assert project.delivery_pdf.read_bytes() == repaired.pdf.read_bytes()
    assert _book().content_digest in project.delivery_html.read_text(
        encoding="utf-8"
    )


def test_delivery_verification_failure_restores_prior_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    first = publisher.publish(_book(), run_id="run-one")
    old_pdf = project.delivery_pdf.read_bytes()
    old_html = project.delivery_html.read_bytes()
    old_current = project.current.read_bytes()
    second_book = replace(_book(), title="Second fixture")

    monkeypatch.setattr(
        publisher,
        "_verify_delivery",
        lambda _release: (_ for _ in ()).throw(
            CompanionReleaseError("delivery_invalid", "verification failed")
        ),
    )

    with pytest.raises(CompanionReleaseError, match="verification failed"):
        publisher.publish(second_book, run_id="run-two")

    assert project.delivery_pdf.read_bytes() == old_pdf == first.pdf.read_bytes()
    assert project.delivery_html.read_bytes() == old_html
    assert project.current.read_bytes() == old_current


def test_current_publish_failure_restores_prior_pair_and_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    first = publisher.publish(_book(), run_id="run-one")
    old_pdf = project.delivery_pdf.read_bytes()
    old_html = project.delivery_html.read_bytes()
    old_current = project.current.read_bytes()
    original_publish_current = CompanionProjectPaths.publish_current

    def fail_after_publish(self, **kwargs) -> None:
        original_publish_current(self, **kwargs)
        raise OSError("current pointer interruption")

    monkeypatch.setattr(
        CompanionProjectPaths,
        "publish_current",
        fail_after_publish,
    )

    with pytest.raises(OSError, match="current pointer interruption"):
        publisher.publish(
            replace(_book(), title="Second fixture"),
            run_id="run-two",
        )

    assert project.delivery_pdf.read_bytes() == old_pdf == first.pdf.read_bytes()
    assert project.delivery_html.read_bytes() == old_html
    assert project.current.read_bytes() == old_current


def test_web_only_current_publish_failure_restores_prior_pdf_and_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    first = CompanionReleasePublisher(project, _FakeRenderer()).publish(  # type: ignore[arg-type]
        _book(),
        run_id="run-one",
    )
    old_pdf = project.delivery_pdf.read_bytes()
    old_html = project.delivery_html.read_bytes()
    old_current = project.current.read_bytes()
    original_publish_current = CompanionProjectPaths.publish_current

    def fail_after_publish(self, **kwargs) -> None:
        original_publish_current(self, **kwargs)
        raise OSError("current pointer interruption")

    monkeypatch.setattr(
        CompanionProjectPaths,
        "publish_current",
        fail_after_publish,
    )

    with pytest.raises(OSError, match="current pointer interruption"):
        CompanionReleasePublisher(  # type: ignore[arg-type]
            project,
            _WebOnlyRenderer(),
        ).publish(
            replace(_book(), title="Web-only fixture"),
            run_id="run-two",
        )

    assert project.delivery_pdf.read_bytes() == old_pdf == first.pdf.read_bytes()
    assert project.delivery_html.read_bytes() == old_html
    assert project.current.read_bytes() == old_current


def test_first_current_publish_failure_removes_new_delivery_and_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    original_publish_current = CompanionProjectPaths.publish_current

    def fail_after_publish(self, **kwargs) -> None:
        original_publish_current(self, **kwargs)
        raise OSError("current pointer interruption")

    monkeypatch.setattr(
        CompanionProjectPaths,
        "publish_current",
        fail_after_publish,
    )

    with pytest.raises(OSError, match="current pointer interruption"):
        publisher.publish(_book(), run_id="run-one")

    assert not project.delivery_pdf.exists()
    assert not project.delivery_html.exists()
    assert not project.current.exists()


def test_validate_current_verifies_root_delivery_copies(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()
    publisher.publish(book, run_id="run-one")
    current = project.current_release()
    assert current is not None
    assert publisher.validate_current(current, book).release_id == current["release_id"]

    project.delivery_pdf.write_bytes(b"corrupt")
    with pytest.raises(CompanionReleaseError) as pdf_error:
        publisher.validate_current(current, book)
    assert pdf_error.value.code == "delivery_invalid"

    publisher.publish(book, run_id="run-repair")
    project.delivery_html.unlink()
    current = project.current_release()
    assert current is not None
    with pytest.raises(CompanionReleaseError) as html_error:
        publisher.validate_current(current, book)
    assert html_error.value.code == "delivery_invalid"


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
