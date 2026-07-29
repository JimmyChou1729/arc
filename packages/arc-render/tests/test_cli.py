from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from arc_paper import (
    RichDocument,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    rich_document_to_document,
)

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
