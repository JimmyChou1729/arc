from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_ocr_proofread.cli import main
from arc_ocr_proofread.source import ProofreadSourceError, load_mineru_source


def test_missing_page_map_stops_before_project_creation(tmp_path: Path, capsys) -> None:
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book.pdf"
    project = tmp_path / "project"
    markdown.write_text("text", encoding="utf-8")
    pdf.write_bytes(b"pdf")

    code = main(
        [
            "proofread",
            str(markdown),
            "--pdf",
            str(pdf),
            "--content-list",
            str(tmp_path / "missing.json"),
            "--project-dir",
            str(project),
        ]
    )

    assert code == 1
    assert not project.exists()
    value = json.loads(capsys.readouterr().out)
    assert value["error"]["code"] == "page_map_missing"


def test_rejects_unsafe_asset_path(tmp_path: Path, monkeypatch) -> None:
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book.pdf"
    content = tmp_path / "book_content_list.json"
    markdown.write_text("text", encoding="utf-8")
    pdf.write_bytes(b"pdf")
    content.write_text(
        json.dumps([{"type": "image", "img_path": "../escape.png", "page_idx": 0}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "arc_ocr_proofread.source._pdf_page_count", lambda _path: 1
    )

    with pytest.raises(ProofreadSourceError, match="unsafe"):
        load_mineru_source(markdown, pdf, content)


def test_rejects_page_map_outside_pdf(tmp_path: Path, monkeypatch) -> None:
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book.pdf"
    content = tmp_path / "book_content_list.json"
    markdown.write_text("text", encoding="utf-8")
    pdf.write_bytes(b"pdf")
    content.write_text(
        json.dumps([{"type": "text", "text": "text", "page_idx": 1}]),
        encoding="utf-8",
    )
    monkeypatch.setattr("arc_ocr_proofread.source._pdf_page_count", lambda _path: 1)

    with pytest.raises(ProofreadSourceError, match="exceeds"):
        load_mineru_source(markdown, pdf, content)
