from __future__ import annotations

import hashlib
import json

from arc_paper import (
    RichAsset,
    RichBlock,
    RichBlockKind,
    RichDocument,
    SourceArtifact,
    SourceFormat,
    SourceLocator,
    SourceOrigin,
    SourceOriginKind,
)

from arc_companion.model_source import (
    model_chapter_block_index,
    model_source_index,
    model_source_view,
    validate_model_source_index,
)
from arc_companion.source_planning import plan_source_chapters


def _document() -> RichDocument:
    source_bytes = b"# Source\n\nA difficult sentence.\n"
    source = SourceArtifact(
        SourceFormat.MARKDOWN,
        hashlib.sha256(source_bytes).hexdigest(),
        len(source_bytes),
        "text/markdown",
        SourceOrigin(SourceOriginKind.LOCAL_IMPORT),
    )
    asset_digest = "a" * 64
    blocks = (
        RichBlock(
            "heading",
            0,
            RichBlockKind.HEADING,
            (),
            SourceLocator(SourceFormat.MARKDOWN, 1, 1, 1, 8),
            {"text": "Source", "level": 1},
        ),
        RichBlock(
            "paragraph",
            1,
            RichBlockKind.PARAGRAPH,
            (),
            SourceLocator(SourceFormat.MARKDOWN, 3, 1, 3, 21),
            {
                "text": "A difficult sentence.",
                "inline_spans": [
                    {
                        "kind": "text",
                        "start": 0,
                        "end": 21,
                        "text": "A difficult sentence.",
                    }
                ],
            },
        ),
        RichBlock(
            "equation",
            2,
            RichBlockKind.EQUATION,
            (),
            SourceLocator(
                SourceFormat.MARKDOWN, selector="equation:source"
            ),
            {"tex": "x=1", "display": True, "label": "old"},
        ),
        RichBlock(
            "figure",
            3,
            RichBlockKind.FIGURE,
            (),
            SourceLocator(
                SourceFormat.MARKDOWN, selector="figure:source"
            ),
            {
                "asset_digest": asset_digest,
                "alt_text": "A diagram",
                "caption": "Deterministic figure caption",
                "target": "secret/image.png",
                "media_type": "image/png",
                "logical_name": "image.png",
                "size": 100,
            },
        ),
    )
    return RichDocument(
        source,
        blocks,
        assets=(RichAsset(asset_digest, "image/png", "image.png", 100),),
        metadata={
            "equation_label_reconciliation": {
                "equation": {
                    "source_label": "old",
                    "effective_label": "2",
                    "pdf_label": "2",
                }
            }
        },
    )


def test_model_source_view_is_text_only_and_preserves_effective_labels() -> None:
    document = _document()
    view = model_source_view(document, plan_source_chapters(document))

    assert "A difficult sentence." in view
    assert "Equation label: 2" in view
    assert "Deterministic figure caption" in view
    assert "secret/image.png" not in view
    assert "a" * 64 not in view
    assert "ARC_BLOCK id=paragraph" in view


def test_model_source_index_has_identity_and_no_body_or_asset_metadata() -> None:
    document = _document()
    chapters = plan_source_chapters(document)
    cached = {
        "source_format": "markdown",
        "source_sha256": document.source.artifact_digest,
        "source_size": document.source.size,
        "media_type": document.source.media_type,
        "parser_contract": "arc.paper.rich-parse.v1",
        "parsed_document_sha256": "b" * 64,
    }
    index = model_source_index(
        document,
        chapters,
        cache_document=cached,
        cache_relationship="equation_label_overlay",
    )

    validate_model_source_index(
        index, document=document, chapters=chapters
    )
    encoded = json.dumps(index, ensure_ascii=False)
    assert "A difficult sentence." not in encoded
    assert "secret/image.png" not in encoded
    assert "a" * 64 not in encoded
    assert "blocks" not in index
    assert "chapters" not in index
    assert index["chapter_count"] == len(chapters)
    assert index["block_count"] == len(document.blocks)
    equation = next(
        item
        for item in model_chapter_block_index(document, chapters[0])
        if item["block_id"] == "equation"
    )
    assert equation["equation_label"] == "2"
    assert index["cached_document"] == cached


def test_model_source_index_supports_verified_text_fallback_only() -> None:
    document = _document()
    chapters = plan_source_chapters(document)
    index = model_source_index(
        document,
        chapters,
        cache_document=None,
        cache_relationship="fallback_only",
    )

    assert index["cached_document"] is None
    assert index["cache_operations"] == {}
    validate_model_source_index(
        index, document=document, chapters=chapters
    )
