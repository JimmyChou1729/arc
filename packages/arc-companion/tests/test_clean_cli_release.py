from __future__ import annotations

import json
from pathlib import Path

from arc_companion.cli import _parser, main
from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    SourceAnchor,
)
from arc_companion.project import CompanionProjectPaths
from arc_companion.release import CompanionReleasePublisher


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


def test_release_is_immutable_reused_and_current_updates_last(
    tmp_path: Path,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    publisher = CompanionReleasePublisher(project, _FakeRenderer())  # type: ignore[arg-type]
    book = _book()

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
