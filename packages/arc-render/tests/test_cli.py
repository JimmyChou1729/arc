from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from arc_paper import (
    RichDocument,
    RichDocumentParserService,
    SourceRepository,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    rich_document_to_document,
)

from arc_render import BrowserValidation
from arc_render import cli
from arc_render.cli import main


def _source(path: Path) -> Path:
    payload = b"source"
    document = RichDocument(
        SourceArtifact(
            SourceFormat.MARKDOWN,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "text/markdown",
            SourceOrigin(SourceOriginKind.REPOSITORY),
        ),
        (),
    )
    path.write_text(
        json.dumps(rich_document_to_document(document)),
        encoding="utf-8",
    )
    return path


def test_cli_composes_renders_and_validates_source_only_publication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path / "source.json")
    publication = tmp_path / "publication.json"
    html = tmp_path / "reader.html"

    assert main([
        "compose",
        "--source", str(source),
        "--output", str(publication),
    ]) == 0
    compose = json.loads(capsys.readouterr().out)
    assert compose["layer_count"] == 0

    assert main([
        "render",
        "--publication", str(publication),
        "--html", str(html),
    ]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["html"] == str(html.resolve())

    assert main([
        "validate",
        "--publication", str(publication),
        "--html", str(html),
    ]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["publication_digest"] == compose["publication_digest"]


def test_cli_renders_direct_rich_source_with_copied_asset(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rich_source = tmp_path / "rich-source"
    rich_source.mkdir()
    asset_payload = b"\x89PNG\r\nexplicit source figure"
    (rich_source / "figure.png").write_bytes(asset_payload)
    markdown = rich_source / "paper.md"
    markdown.write_text("# Result\n\n![Measured result](figure.png)\n", encoding="utf-8")

    repository = SourceRepository(tmp_path / "paper-cache")
    document = RichDocumentParserService(repository).parse_source(
        repository.import_path(markdown)
    )
    workspace = tmp_path / "publication-workspace"
    workspace.mkdir()
    source = workspace / "source.json"
    source.write_text(
        json.dumps(rich_document_to_document(document)),
        encoding="utf-8",
    )
    copied_asset = workspace / "resources" / "figure.png"
    copied_asset.parent.mkdir()
    copied_asset.write_bytes(asset_payload)
    asset = document.assets[0]
    metadata = workspace / "metadata.json"
    metadata.write_text(
        json.dumps({
            "glossary": [],
            "bibliography": [],
            "labels": {},
            "resources": [{
                "artifact_digest": asset.artifact_digest,
                "path": "resources/figure.png",
            }],
            "reader_profile": {},
        }),
        encoding="utf-8",
    )
    publication = workspace / "publication.json"
    html = workspace / "reader.html"

    assert main([
        "compose",
        "--source", str(source),
        "--metadata", str(metadata),
        "--output", str(publication),
    ]) == 0
    capsys.readouterr()
    assert main([
        "render",
        "--publication", str(publication),
        "--html", str(html),
    ]) == 0
    capsys.readouterr()

    rendered = html.read_text(encoding="utf-8")
    assert "data:image/png;base64," in rendered
    assert "Measured result" in rendered


def test_compose_help_and_parser_reject_removed_cached_source_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["compose", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--source-ref" not in help_text
    assert "--cache-root" not in help_text

    with pytest.raises(SystemExit) as source_ref_exit:
        main([
            "compose",
            "--source-ref", str(tmp_path / "source-ref.json"),
            "--output", str(tmp_path / "publication.json"),
        ])
    assert source_ref_exit.value.code == 2

    source = _source(tmp_path / "source.json")
    with pytest.raises(SystemExit) as cache_root_exit:
        main([
            "compose",
            "--source", str(source),
            "--cache-root", str(tmp_path / "cache"),
            "--output", str(tmp_path / "publication.json"),
        ])
    assert cache_root_exit.value.code == 2


def test_cli_has_no_pdf_generation_option(
    tmp_path: Path,
) -> None:
    publication = tmp_path / "publication.json"

    with pytest.raises(SystemExit) as error:
        main([
            "render",
            "--publication", str(publication),
            "--html", str(tmp_path / "reader.html"),
            "--pdf", str(tmp_path / "reader.pdf"),
        ])

    assert error.value.code == 2


def test_validate_browser_option_runs_only_when_requested(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "source.json")
    publication = tmp_path / "publication.json"
    html = tmp_path / "reader.html"
    assert main(["compose", "--source", str(source), "--output", str(publication)]) == 0
    capsys.readouterr()
    assert main(["render", "--publication", str(publication), "--html", str(html)]) == 0
    capsys.readouterr()
    calls: list[tuple[Path, str | None, int]] = []

    def validate_browser(
        path: Path, *, browser_executable: str | None, timeout_seconds: int
    ) -> BrowserValidation:
        calls.append((path, browser_executable, timeout_seconds))
        return BrowserValidation("/usr/bin/chromium", timeout_seconds)

    monkeypatch.setattr(cli, "validate_reader_in_browser", validate_browser)

    assert main([
        "validate",
        "--publication", str(publication),
        "--html", str(html),
        "--browser",
        "--browser-executable", "custom-chromium",
        "--browser-timeout", "9",
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert calls == [(html, "custom-chromium", 9)]
    assert result["browser"] == {
        "executable": "/usr/bin/chromium",
        "timeout_seconds": 9,
    }


def test_validate_browser_requires_html(tmp_path: Path) -> None:
    publication = tmp_path / "publication.json"

    with pytest.raises(SystemExit) as error:
        main(["validate", "--publication", str(publication), "--browser"])

    assert error.value.code == 2
