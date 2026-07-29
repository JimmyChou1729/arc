from __future__ import annotations

import copy
import hashlib

import pytest
from arc_paper import (
    RichDocument,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
)
from arc_render import (
    AnchorBlock,
    FragmentAnchor,
    FragmentRevision,
    FragmentRevisionRef,
    Layer,
    Publication,
    anchor_block_from_rich_block,
    fragment_revision_filename,
    layer_from_document,
    layer_to_document,
    publication_from_document,
    publication_to_document,
    source_identity_from_rich_document,
)


def rich_document() -> RichDocument:
    payload = b"# Source\n"
    return RichDocument(
        source=SourceArtifact(
            source_format=SourceFormat.MARKDOWN,
            artifact_digest=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            media_type="text/markdown",
            origin=SourceOrigin(SourceOriginKind.REPOSITORY),
        ),
        blocks=(),
    )


def anchor() -> FragmentAnchor:
    return FragmentAnchor(
        kind="block",
        target_id="block-1",
        related_blocks=(
            AnchorBlock(
                block_id="block-1",
                kind="paragraph",
                ordinal=0,
                locator={
                    "source_format": "markdown",
                    "line_start": 1,
                    "line_end": 1,
                },
                content_fingerprint="a" * 64,
            ),
        ),
    )


def revision(*, priority: int = 10) -> FragmentRevision:
    return FragmentRevision(
        source=source_identity_from_rich_document(rich_document()),
        fragment_id="fragment-8e89188c",
        revision=1,
        parent_semantic_digest=None,
        anchor=anchor(),
        priority=priority,
        role="translation",
        language="zh-CN",
        title=None,
        citation_ids=(),
        provenance={"producer": "test"},
        markdown_body="正文\n",
    )


@pytest.mark.parametrize("priority", [0, -1, True])
def test_fragment_priority_must_be_positive(priority: int) -> None:
    with pytest.raises(ValueError, match="priority"):
        revision(priority=priority)


def test_fragment_id_is_safe_for_atomic_workspace_paths() -> None:
    item = revision()
    with pytest.raises(ValueError, match="portable identifier"):
        FragmentRevision(
            item.source,
            "../escape",
            item.revision,
            item.parent_semantic_digest,
            item.anchor,
            item.priority,
            item.role,
            item.language,
            item.title,
            item.citation_ids,
            item.provenance,
            item.markdown_body,
        )


def test_anchor_kind_is_closed_and_block_target_is_related() -> None:
    with pytest.raises(ValueError):
        FragmentAnchor("page", "block-1", anchor().related_blocks)
    with pytest.raises(ValueError, match="target"):
        FragmentAnchor("block", "missing", anchor().related_blocks)
    assert FragmentAnchor("section", "section-1", ()).related_block_ids == ()
    with pytest.raises(ValueError, match="block kind"):
        AnchorBlock("block-1", "unsupported", 0, {}, "a" * 64)


def test_layer_codec_is_exact_and_digest_verified() -> None:
    item = revision()
    layer = Layer(
        source=item.source,
        producer="arc-translate",
        initial_revisions=(
            FragmentRevisionRef(
                f"fragments/{fragment_revision_filename(item)}",
                item.fragment_id,
                1,
                item.semantic_digest,
            ),
        ),
    )
    encoded = layer_to_document(layer)
    assert layer_from_document(encoded) == layer
    encoded["producer"] = "tampered"
    with pytest.raises(ValueError, match="digest"):
        layer_from_document(encoded)


def test_layer_only_accepts_initial_revisions() -> None:
    item = revision()
    with pytest.raises(ValueError, match="initial"):
        Layer(
            item.source,
            "test",
            (
                FragmentRevisionRef(
                    (
                        "fragments/revision-000002-"
                        f"{item.semantic_digest}.md"
                    ),
                    item.fragment_id,
                    2,
                    item.semantic_digest,
                ),
            ),
        )


def test_anchor_block_adapter_preserves_rich_block_provenance() -> None:
    from arc_paper import RichBlock, RichBlockKind, SourceLocator

    block = RichBlock(
        block_id="block-1",
        ordinal=0,
        kind=RichBlockKind.PARAGRAPH,
        section_path=(),
        locator=SourceLocator(SourceFormat.MARKDOWN, 1, 1, 1, 5),
        payload={
            "text": "test",
            "inline_spans": [
                {"kind": "text", "start": 0, "end": 4, "text": "test"}
            ],
        },
    )
    anchored = anchor_block_from_rich_block(block)
    assert anchored.kind == "paragraph"
    assert anchored.locator["line_start"] == 1
    assert len(anchored.content_fingerprint) == 64


def test_source_only_publication_round_trips_self_contained_source() -> None:
    publication = Publication(source_document=rich_document())
    assert publication.layers == ()
    assert publication.outline == ()
    encoded = publication_to_document(publication)
    assert "source_document" in encoded
    assert publication_from_document(encoded) == publication


def test_publication_rejects_digest_tampering_and_unknown_fields() -> None:
    encoded = publication_to_document(Publication(rich_document()))
    tampered = copy.deepcopy(encoded)
    tampered["labels"]["eq:one"] = "block-1"
    with pytest.raises(ValueError, match="digest"):
        publication_from_document(tampered)
    encoded["renderer_recipe"] = {}
    with pytest.raises(ValueError, match="fields"):
        publication_from_document(encoded)
