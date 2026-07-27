from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_paper import (
    ArcPaperService,
    CachedDocumentStructureRef,
    DocumentStructureError,
    PDFTextLayer,
    cached_document_structure_ref_from_document,
    cached_document_structure_ref_to_document,
)
from arc_paper.cli import main


MARKDOWN = """# Contents

# Part One 1

# Opening

Opening body evidence alpha beta gamma.

# INNER TOPIC

Internal explanation.

# Closing

Closing body evidence delta epsilon zeta.
"""


class _PDFExtractor:
    contract_id = "structure-test-pdf.v1"

    def extract(self, payload: bytes) -> PDFTextLayer:
        del payload
        return PDFTextLayer(
            (
                "Contents Part One Opening Closing",
                "PART ONE",
                "Opening body evidence alpha beta gamma",
                "Closing body evidence delta epsilon zeta",
            )
        )


class _Qpdf:
    def extract(self, payload: bytes) -> dict[str, object]:
        del payload
        return {
            "outlines": [
                {
                    "title": "Contents",
                    "destpageposfrom1": 1,
                    "kids": [],
                },
                {
                    "title": "I PART ONE",
                    "destpageposfrom1": 2,
                    "kids": [
                        {
                            "title": "1. Opening",
                            "destpageposfrom1": 3,
                            "kids": [],
                        },
                        {
                            "title": "2. Closing",
                            "destpageposfrom1": 4,
                            "kids": [],
                        },
                    ],
                },
            ]
        }


def _documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ArcPaperService, object, object]:
    monkeypatch.setattr(
        "arc_paper.document_structure.QpdfOutlineExtractor", _Qpdf
    )
    source = tmp_path / "book.md"
    source.write_text(MARKDOWN, encoding="utf-8")
    outline = tmp_path / "book.pdf"
    outline.write_bytes(b"%PDF structure fixture")
    service = ArcPaperService(
        cache_root=tmp_path / "cache",
        pdf_text_extractor=_PDFExtractor(),
    )
    return (
        service,
        service.cache_document(service.import_source(source)),
        service.cache_document(service.import_source(outline)),
    )


def test_structure_overlay_rebuilds_hierarchy_without_changing_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, document, outline = _documents(tmp_path, monkeypatch)

    structure = service.reconstruct_cached_structure(document, outline)
    encoded = cached_document_structure_ref_to_document(structure)
    assert cached_document_structure_ref_from_document(encoded) == structure
    assert isinstance(structure, CachedDocumentStructureRef)
    assert structure.document == document

    toc = service.get_cached_table_of_contents(
        document, structure=structure
    )
    assert [(item.level, item.title) for item in toc.entries] == [
        (1, "Contents"),
        (1, "Part One 1"),
        (2, "Opening"),
        (3, "INNER TOPIC"),
        (2, "Closing"),
    ]
    opening = service.get_cached_section(
        document, "Opening", structure=structure
    )
    assert opening.text.startswith("# Opening")
    assert "# INNER TOPIC" in opening.text
    assert "# Closing" not in opening.text


def test_structure_overlay_cache_hit_does_not_reinvoke_qpdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, document, outline = _documents(tmp_path, monkeypatch)
    first = service.reconstruct_cached_structure(document, outline)

    class _FailQpdf:
        def extract(self, payload: bytes) -> dict[str, object]:
            raise AssertionError("valid cached overlay should be reused")

    monkeypatch.setattr(
        "arc_paper.document_structure.QpdfOutlineExtractor", _FailQpdf
    )
    assert service.reconstruct_cached_structure(document, outline) == first


def test_corrupt_structure_object_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, document, outline = _documents(tmp_path, monkeypatch)
    reference = service.reconstruct_cached_structure(document, outline)
    object_path = (
        tmp_path
        / "cache"
        / "document-structure"
        / "v1"
        / "objects"
        / reference.structure_sha256[:2]
        / reference.structure_sha256
        / "overlay.json"
    )
    object_path.write_text("corrupt", encoding="utf-8")

    assert service.reconstruct_cached_structure(document, outline) == reference
    assert service.get_cached_table_of_contents(
        document, structure=reference
    ).entries


def test_explicit_structure_ambiguity_fails_instead_of_flat_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "arc_paper.document_structure.QpdfOutlineExtractor",
        lambda: _DuplicateQpdf(),
    )
    source = tmp_path / "ambiguous.md"
    source.write_text("# Repeated\n\nsame body\n\n# Repeated\n\nsame body\n")
    outline = tmp_path / "ambiguous.pdf"
    outline.write_bytes(b"%PDF ambiguous")
    service = ArcPaperService(
        cache_root=tmp_path / "cache",
        pdf_text_extractor=_AmbiguousPDFExtractor(),
    )
    document_ref = service.cache_document(service.import_source(source))
    outline_ref = service.cache_document(service.import_source(outline))

    with pytest.raises(DocumentStructureError) as error:
        service.reconstruct_cached_structure(document_ref, outline_ref)
    assert error.value.code == "document_structure_alignment_ambiguous"


class _DuplicateQpdf:
    def extract(self, payload: bytes) -> dict[str, object]:
        del payload
        return {
            "outlines": [
                {
                    "title": "Repeated",
                    "destpageposfrom1": 1,
                    "kids": [],
                }
            ]
        }


class _AmbiguousPDFExtractor:
    contract_id = "ambiguous-pdf.v1"

    def extract(self, payload: bytes) -> PDFTextLayer:
        del payload
        return PDFTextLayer(("same body",))


def test_reference_cli_admits_looks_up_and_materializes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "reference.txt"
    source.write_text("portable reference", encoding="utf-8")
    cache_root = tmp_path / "cache"

    assert (
        main(
            [
                "admit-reference",
                str(source),
                "--doi",
                "10.1234/example",
                "--cache-root",
                str(cache_root),
            ]
        )
        == 0
    )
    admitted = json.loads(capsys.readouterr().out)["data"]
    assert admitted["identity"]["dois"] == ["10.1234/example"]

    assert (
        main(
            [
                "lookup-reference",
                "--doi",
                "10.1234/example",
                "--cache-root",
                str(cache_root),
            ]
        )
        == 0
    )
    found = json.loads(capsys.readouterr().out)["data"]
    assert found == admitted

    output = tmp_path / "materialized.txt"
    assert (
        main(
            [
                "materialize-reference",
                "--resource-ref",
                json.dumps(found["resources"][0]),
                "--output",
                str(output),
                "--cache-root",
                str(cache_root),
            ]
        )
        == 0
    )
    materialized = json.loads(capsys.readouterr().out)["data"]
    assert materialized["bytes_written"] == len(b"portable reference")
    assert output.read_text(encoding="utf-8") == "portable reference"
