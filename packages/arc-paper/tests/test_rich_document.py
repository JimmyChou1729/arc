from __future__ import annotations

import hashlib

import pytest

from arc_paper import (
    PDF_VALIDATOR_MISSING_WARNING,
    PDFTextLayer,
    RICH_DOCUMENT_SCHEMA,
    RichBlockKind,
    RichDocumentParserService,
    RichDocumentValidationError,
    SourceBundle,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    rich_block_from_document,
    rich_block_to_document,
    rich_document_from_document,
    rich_document_to_document,
)


class FakePDFTextExtractor:
    def __init__(self, values: dict[bytes, PDFTextLayer]):
        self.values = values
        self.calls: list[bytes] = []

    def extract(self, payload: bytes) -> PDFTextLayer:
        self.calls.append(payload)
        return self.values[payload]


def _store(repository, payload, source_format, *, locator=""):
    return repository.store_bytes(
        payload,
        source_format=source_format,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT,
            locator=locator,
        ),
    )


def test_markdown_rich_parse_preserves_blocks_links_math_and_assets(tmp_path):
    source = tmp_path / "paper.md"
    image = tmp_path / "diagram.png"
    image.write_bytes(b"\x89PNG\r\nfixture")
    source.write_text(
        "\n".join(
            [
                "# Dynamics",
                "Read the [notes](https://example.test/notes) with $E=mc^2$.",
                "",
                "- first",
                "- [second](appendix.html)",
                "",
                "$$",
                r"x^2 + y^2 = z^2",
                "$$",
                "",
                "| Name | Value |",
                "| --- | --- |",
                "| mass | m |",
                "",
                "```python",
                "print('example')",
                "```",
                "",
                '![phase portrait](diagram.png "Figure 1")',
            ]
        ),
        encoding="utf-8",
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = repository.import_path(source)

    outcome = RichDocumentParserService(repository).parse(
        SourceBundle(primary=artifact)
    )
    document = outcome.document

    assert document.schema_version == RICH_DOCUMENT_SCHEMA
    assert [block.kind for block in document.blocks] == [
        RichBlockKind.HEADING,
        RichBlockKind.PARAGRAPH,
        RichBlockKind.LIST,
        RichBlockKind.EQUATION,
        RichBlockKind.TABLE,
        RichBlockKind.CODE,
        RichBlockKind.FIGURE,
    ]
    paragraph = document.blocks[1]
    assert paragraph.payload["links"][0]["target"] == "https://example.test/notes"
    assert paragraph.payload["inline_math"][0]["tex"] == "E=mc^2"
    assert document.blocks[3].payload["tex"] == "x^2 + y^2 = z^2"
    assert document.blocks[4].payload["rows"] == (("mass", "m"),)
    assert document.blocks[5].payload["language"] == "python"
    assert len(document.assets) == 1
    asset = document.assets[0]
    assert asset.artifact_digest == hashlib.sha256(image.read_bytes()).hexdigest()
    assert repository.read_asset_bytes(
        repository.get_asset(asset.artifact_digest)
    ) == image.read_bytes()
    assert document.blocks[-1].payload["asset_digest"] == asset.artifact_digest
    assert outcome.warnings == (PDF_VALIDATOR_MISSING_WARNING,)
    assert all(block.section_path == document.sections[0].path for block in document.blocks)


def test_html_rich_parse_preserves_equation_table_figure_and_selector(tmp_path):
    source = tmp_path / "paper.html"
    image = tmp_path / "plot.svg"
    image.write_text("<svg/>", encoding="utf-8")
    source.write_text(
        """
        <article>
          <h1 id="intro">Introduction</h1>
          <p>See <a href="https://example.test">source</a>
             and <math alttext="a+b"></math>.</p>
          <table class="ltx_equation" id="eq1">
            <tr><td><math alttext="F=ma"></math></td></tr>
          </table>
          <table><caption>Inputs</caption>
            <tr><th>x</th><th>y</th></tr><tr><td>1</td><td>2</td></tr>
          </table>
          <figure><img src="plot.svg" alt="plot"><figcaption>Result</figcaption></figure>
        </article>
        """,
        encoding="utf-8",
    )
    repository = SourceRepository(tmp_path / "cache")
    artifact = repository.import_path(source)

    document = RichDocumentParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [block.kind for block in document.blocks] == [
        RichBlockKind.HEADING,
        RichBlockKind.PARAGRAPH,
        RichBlockKind.EQUATION,
        RichBlockKind.TABLE,
        RichBlockKind.FIGURE,
    ]
    assert document.blocks[0].locator.selector == "#intro"
    assert document.blocks[1].payload["inline_math"][0]["tex"] == "a+b"
    assert document.blocks[2].payload == {
        "tex": "F=ma",
        "display": True,
        "label": "eq1",
    }
    assert document.blocks[3].payload["headers"] == ("x", "y")
    assert document.blocks[3].payload["caption"] == "Inputs"
    assert document.blocks[4].payload["caption"] == "Result"
    assert document.assets[0].media_type == "image/svg+xml"


def test_flattened_tex_rich_parse_and_multifile_rejection(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    artifact = _store(
        repository,
        b"\n".join(
            [
                br"\section{Model}",
                br"The \href{https://example.test}{reference} uses \(x+y\).",
                br"\begin{equation}",
                br"E = mc^2 \label{energy}",
                br"\end{equation}",
                br"\begin{enumerate}",
                br"\item First",
                br"\item Second",
                br"\end{enumerate}",
            ]
        ),
        SourceFormat.TEX,
    )

    document = RichDocumentParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    assert [block.kind for block in document.blocks] == [
        RichBlockKind.HEADING,
        RichBlockKind.PARAGRAPH,
        RichBlockKind.EQUATION,
        RichBlockKind.LIST,
    ]
    assert document.blocks[1].payload["links"][0]["target"] == "https://example.test"
    assert document.blocks[2].payload["label"] == "energy"
    assert document.blocks[3].payload["ordered"] is True

    project = _store(
        repository,
        br"\section{Main}" b"\n" br"\input{chapter}",
        SourceFormat.TEX,
    )
    with pytest.raises(Exception) as error:
        RichDocumentParserService(repository).parse(
            SourceBundle(primary=project)
        )
    assert getattr(error.value, "code", "") == "unsupported_tex_project"


def test_rich_document_and_block_codecs_are_strict_and_path_free(tmp_path):
    source = tmp_path / "paper.md"
    source.write_text("# Codec\nText.", encoding="utf-8")
    repository = SourceRepository(tmp_path / "cache")
    artifact = repository.import_path(source)
    document = RichDocumentParserService(repository).parse(
        SourceBundle(primary=artifact)
    ).document

    encoded = rich_document_to_document(document)
    decoded = rich_document_from_document(encoded)
    encoded_block = rich_block_to_document(document.blocks[1])

    assert decoded.document_digest == document.document_digest
    assert decoded.source.origin.kind is SourceOriginKind.REPOSITORY
    assert str(source) not in str(encoded)
    assert (
        rich_block_from_document(encoded_block).block_id
        == document.blocks[1].block_id
    )
    with pytest.raises(ValueError, match="invalid fields"):
        rich_document_from_document({**encoded, "unknown": True})
    with pytest.raises(ValueError, match="invalid fields"):
        rich_block_from_document({**encoded_block, "unknown": True})
    invalid_payload = {
        **encoded_block,
        "payload": {**encoded_block["payload"], "unknown": True},
    }
    with pytest.raises(ValueError, match="invalid fields"):
        rich_block_from_document(invalid_payload)
    corrupt = {
        **encoded,
        "blocks": [
            encoded["blocks"][0],
            {
                **encoded["blocks"][1],
                "payload": {
                    **encoded["blocks"][1]["payload"],
                    "text": "changed",
                },
            },
        ],
    }
    with pytest.raises(ValueError, match="digest"):
        rich_document_from_document(corrupt)


def test_rich_document_identity_excludes_source_path(tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "nested" / "second.md"
    second.parent.mkdir()
    payload = b"# Identity\nSame body."
    first.write_bytes(payload)
    second.write_bytes(payload)
    repository = SourceRepository(tmp_path / "cache")
    service = RichDocumentParserService(repository)

    first_document = service.parse(
        SourceBundle(primary=repository.import_path(first))
    ).document
    second_document = service.parse(
        SourceBundle(primary=repository.import_path(second))
    ).document

    assert first_document.document_digest == second_document.document_digest
    assert [block.block_id for block in first_document.blocks] == [
        block.block_id for block in second_document.blocks
    ]


def test_matching_pdf_builds_page_map_and_mismatch_fails(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository,
        b"# Introduction\nText.\n\n# Method\nMore text.\n",
        SourceFormat.MARKDOWN,
    )
    matching_payload = b"%PDF matching"
    matching = _store(repository, matching_payload, SourceFormat.PDF)
    mismatch_payload = b"%PDF mismatch"
    mismatch = _store(repository, mismatch_payload, SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {
            matching_payload: PDFTextLayer(
                ("Introduction\nText.", "Method\nMore text.")
            ),
            mismatch_payload: PDFTextLayer(("Completely unrelated",)),
        }
    )
    service = RichDocumentParserService(
        repository, pdf_text_extractor=extractor
    )

    outcome = service.parse(
        SourceBundle(primary=primary, validators=(matching,))
    )

    assert len(outcome.document.page_map) == len(outcome.document.blocks)
    pages = {
        entry.block_id: entry.page_number for entry in outcome.document.page_map
    }
    assert pages[outcome.document.blocks[0].block_id] == 1
    assert pages[outcome.document.blocks[-1].block_id] == 2
    assert PDF_VALIDATOR_MISSING_WARNING not in outcome.warnings

    with pytest.raises(RichDocumentValidationError) as error:
        service.parse(SourceBundle(primary=primary, validators=(mismatch,)))
    assert error.value.code == "pdf_validator_mismatch"


def test_heading_free_source_reconciles_by_body_text(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository,
        b"A distinctive compact source body.",
        SourceFormat.MARKDOWN,
    )
    pdf_payload = b"%PDF heading-free"
    pdf = _store(repository, pdf_payload, SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {pdf_payload: PDFTextLayer(("A distinctive compact source body.",))}
    )

    outcome = RichDocumentParserService(
        repository, pdf_text_extractor=extractor
    ).parse(SourceBundle(primary=primary, validators=(pdf,)))

    assert len(outcome.document.sections) == 1
    assert outcome.document.sections[0].title == "Document"
    assert outcome.document.page_map[0].page_number == 1


def test_ambiguous_or_invalid_pdf_fails_deterministically(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository,
        b"# Introduction\nText.\n",
        SourceFormat.MARKDOWN,
    )
    ambiguous_payload = b"%PDF ambiguous"
    ambiguous = _store(repository, ambiguous_payload, SourceFormat.PDF)
    invalid_payload = b"not actually PDF"
    invalid = _store(repository, invalid_payload, SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        {
            ambiguous_payload: PDFTextLayer(
                ("Introduction page one", "Introduction page two")
            ),
            invalid_payload: PDFTextLayer(("Introduction",)),
        }
    )
    service = RichDocumentParserService(
        repository, pdf_text_extractor=extractor
    )

    with pytest.raises(RichDocumentValidationError) as error:
        service.parse(SourceBundle(primary=primary, validators=(ambiguous,)))
    assert error.value.code == "pdf_validator_ambiguous"

    with pytest.raises(RichDocumentValidationError) as error:
        service.parse(SourceBundle(primary=primary, validators=(invalid,)))
    assert error.value.code == "pdf_validator_invalid"
    assert invalid_payload not in extractor.calls


def test_source_repository_asset_manifest_is_strict_and_verified(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    stored = repository.store_asset_bytes(
        b"asset bytes", media_type="application/octet-stream"
    )

    assert repository.get_asset(stored.artifact_digest) == stored
    assert repository.read_asset_bytes(stored) == b"asset bytes"
    object_dir = repository._asset_object_dir(stored.artifact_digest)  # noqa: SLF001
    (object_dir / "asset").write_bytes(b"corrupt")

    with pytest.raises(Exception) as error:
        repository.read_asset_bytes(stored)
    assert getattr(error.value, "code", "") == "asset_corrupt"
