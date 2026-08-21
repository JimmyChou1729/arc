from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from arc_paper import (
    ArcPaperService,
    CachedDocumentError,
    CachedDocumentRef,
    DocumentTarget,
    PDFTextLayer,
    SourceFormat,
    SourceRepositoryError,
    cached_document_ref_from_document,
    cached_document_ref_to_document,
    dispatch_operation,
    get_operation,
    read_cached_source_range,
)
from arc_paper._parsed_document_cache import DERIVED_CACHE_REBUILT_WARNING
from arc_paper.cli import main


SOURCE = """# Opening

Alpha appears in the first section.

## Second

Beta is explained here.
"""


def _cached(tmp_path: Path) -> tuple[ArcPaperService, CachedDocumentRef]:
    source_path = tmp_path / "book.md"
    source_path.write_text(SOURCE, encoding="utf-8")
    service = ArcPaperService(cache_root=tmp_path / "cache")
    source = service.import_source(source_path)
    return service, service.cache_document(source)


def _target(reference: CachedDocumentRef) -> DocumentTarget:
    return DocumentTarget(kind="document", document=reference)


def test_cached_document_ref_round_trip_and_cache_only_reads(
    tmp_path: Path,
) -> None:
    service, reference = _cached(tmp_path)
    encoded = cached_document_ref_to_document(reference)

    assert cached_document_ref_from_document(encoded) == reference
    assert encoded == {
        "source_format": "markdown",
        "source_sha256": reference.source_sha256,
        "source_size": len(SOURCE.encode("utf-8")),
        "media_type": "text/markdown",
        "parser_contract": "ac.document.parser.v7",
        "parsed_document_sha256": reference.parsed_document_sha256,
    }

    toc = service.get_table_of_contents(_target(reference))
    assert [item.title for item in toc.entries] == ["Opening", "Second"]
    section = service.get_section(_target(reference), "Second")
    assert section.ordinal == 1
    assert section.text.endswith("Beta is explained here.")
    source_range = service.read_cached_source_range(reference, 1, 3)
    assert source_range.total_lines == 7
    assert source_range.text == "# Opening\n\nAlpha appears in the first section."
    search = service.search_full_text_targets(("beta",), targets=(_target(reference),))
    assert [item.location_id for item in search.occurrences] == [
        toc.entries[1].section_id
    ]


def test_target_specific_search_does_not_scan_other_cached_documents(
    tmp_path: Path,
) -> None:
    service, reference = _cached(tmp_path)
    other = tmp_path / "other.md"
    other.write_text("# Other\n\nUniqueOnlyElsewhere.\n", encoding="utf-8")
    service.import_source(other)

    result = service.search_full_text_targets(
        ("UniqueOnlyElsewhere",), targets=(_target(reference),)
    )

    assert result.occurrences == ()


def test_cached_document_accepts_repeated_identical_math_on_one_line(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "repeated.md"
    source_path.write_text(
        "# Repeated math\n\nThe same value $x$ appears again as $x$.\n",
        encoding="utf-8",
    )
    service = ArcPaperService(cache_root=tmp_path / "cache")

    reference = service.cache_document(service.import_source(source_path))

    assert reference.parser_contract == "ac.document.parser.v7"
    assert service.get_table_of_contents(_target(reference)).entries


def test_cached_document_rebuilds_only_derived_projection(
    tmp_path: Path,
) -> None:
    service, reference = _cached(tmp_path)
    source = service.repository.get(
        reference.source_format, reference.source_sha256
    )
    parser_cache = service.parser._caches[reference.parser_contract]
    entry = parser_cache._entry_dir(parser_cache.cache_key(source))
    (entry / "document.json").write_text("corrupt", encoding="utf-8")

    result = service.get_table_of_contents(_target(reference))

    assert result.warnings == (DERIVED_CACHE_REBUILT_WARNING,)
    assert result.entries


def test_cached_document_never_masks_missing_source_bytes(tmp_path: Path) -> None:
    service, reference = _cached(tmp_path)
    assert service.repository.remove(
        reference.source_format, reference.source_sha256
    )

    with pytest.raises(SourceRepositoryError) as error:
        service.get_table_of_contents(_target(reference))

    assert error.value.code == "source_not_found"


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ({"source_size": 1}, "cached_document_source_mismatch"),
        (
            {"parser_contract": "ac.document.parser.future"},
            "cached_document_parser_contract_mismatch",
        ),
        (
            {"parsed_document_sha256": "0" * 64},
            "cached_document_digest_mismatch",
        ),
    ],
)
def test_cached_document_revalidates_full_logical_identity(
    tmp_path: Path,
    replacement: dict[str, object],
    code: str,
) -> None:
    service, reference = _cached(tmp_path)

    with pytest.raises(CachedDocumentError) as error:
        service.get_table_of_contents(_target(replace(reference, **replacement)))

    assert error.value.code == code


def test_cached_source_range_validates_text_and_bounds(tmp_path: Path) -> None:
    service, reference = _cached(tmp_path)

    with pytest.raises(CachedDocumentError) as error:
        service.read_cached_source_range(reference, 1, 99)
    assert error.value.code == "source_range_out_of_bounds"
    with pytest.raises(CachedDocumentError) as error:
        service.read_cached_source_range(reference, 0, 1)
    assert error.value.code == "invalid_source_range"

    pdf_path = tmp_path / "empty.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    pdf_service = ArcPaperService(
        cache_root=tmp_path / "pdf-cache",
        pdf_text_extractor=_EmptyPDFExtractor(),
    )
    pdf_ref = pdf_service.cache_document(pdf_service.import_source(pdf_path))
    with pytest.raises(CachedDocumentError) as error:
        pdf_service.read_cached_source_range(pdf_ref, 1, 1)
    assert error.value.code == "cached_source_not_text"


def test_cached_markdown_source_range_text_only_uses_rich_figure_projection(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "illustrated.md"
    source_text = "\n".join(
        [
            "# Opening",
            "",
            "Before the figure.",
            "",
            '![extractor image](images/plot.png "Structured figure caption.")',
            "",
            "<details>",
            "<summary>natural_image</summary>",
            "",
            "Machine-only image description.",
            "</details>",
            "",
            "Figure 1. An authored caption.",
            "",
            "$$",
            "E = mc^2",
            "$$",
            "",
            "```python",
            "print('kept')",
            "```",
        ]
    )
    source_path.write_text(source_text, encoding="utf-8")
    cache_root = tmp_path / "cache"
    service = ArcPaperService(cache_root=cache_root)
    reference = service.cache_document(service.import_source(source_path))

    raw = service.read_cached_source_range(
        reference, 1, len(source_text.splitlines())
    )
    projected = service.read_cached_source_range(
        reference,
        1,
        len(source_text.splitlines()),
        text_only=True,
    )
    wrapped = read_cached_source_range(
        reference,
        1,
        len(source_text.splitlines()),
        text_only=True,
        cache_root=cache_root,
    )

    assert raw.text == source_text
    assert projected.text == wrapped.text
    assert "images/plot.png" not in projected.text
    assert "<details>" not in projected.text
    assert "natural_image" not in projected.text
    assert "Machine-only image description." not in projected.text
    assert "Before the figure." in projected.text
    assert "Structured figure caption." in projected.text
    assert "Figure 1. An authored caption." in projected.text
    assert "$$\nE = mc^2\n$$" in projected.text
    assert "```python\nprint('kept')\n```" in projected.text
    assert projected.total_lines == len(source_text.splitlines())


def test_cached_document_registry_and_cli_are_typed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, reference = _cached(tmp_path)
    document = cached_document_ref_to_document(reference)
    parameters = {
        "target": {"kind": "document", "document": document},
        "source_format": None,
        "refresh": False,
        "cache_root": str(tmp_path / "cache"),
    }

    result = dispatch_operation("get-table-of-contents", parameters)
    assert result["source"]["document"] == document
    assert result["entries"]

    assert (
        main(
            [
                "search-full-text",
                "--document-ref",
                json.dumps(document),
                "--cache-root",
                str(tmp_path / "cache"),
                "--term",
                "Alpha",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["data"]["documents"][0]["source"]["document"] == document
    assert output["data"]["occurrences"][0]["matched_terms"] == ["Alpha"]


def test_cached_source_range_text_only_is_published_by_registry_and_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_path = tmp_path / "illustrated.md"
    source_path.write_text(
        "Before.\n\n![](plot.png)\n\n"
        "<details><summary>text_image</summary>\n"
        "Extractor text.\n</details>\n\nAfter.\n",
        encoding="utf-8",
    )
    cache_root = tmp_path / "cache"
    service = ArcPaperService(cache_root=cache_root)
    reference = service.cache_document(service.import_source(source_path))
    document = cached_document_ref_to_document(reference)
    parameters = {
        "document": document,
        "start_line": 1,
        "end_line": 9,
        "text_only": True,
        "cache_root": str(cache_root),
    }

    operation = get_operation("read-cached-source-range")
    assert operation is not None
    assert operation.version == 2
    assert operation.input_codec.schema["properties"]["text_only"] == {
        "type": "boolean",
        "default": False,
    }
    result = dispatch_operation("read-cached-source-range", parameters)
    assert result["text"].strip() == "Before.\n\n\nAfter."

    assert (
        main(
            [
                "read-cached-source-range",
                "--document-ref",
                json.dumps(document),
                "--cache-root",
                str(cache_root),
                "1",
                "9",
                "--text-only",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["data"]["text"] == result["text"]


class _EmptyPDFExtractor:
    contract_id = "empty-pdf-test.v1"

    def extract(self, payload: bytes) -> PDFTextLayer:
        del payload
        return PDFTextLayer(())
