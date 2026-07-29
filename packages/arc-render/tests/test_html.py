from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import pytest
from arc_paper import (
    RichAsset,
    RichBlock,
    RichBlockKind,
    RichDocument,
    RichSection,
    SourceArtifact,
    SourceFormat,
    SourceLocator,
    SourceOrigin,
    SourceOriginKind,
)

from arc_render import (
    FragmentAnchor,
    FragmentRevision,
    Layer,
    Publication,
    anchor_block_from_rich_block,
    fragment_revision_ref,
    source_identity_from_rich_document,
)
from arc_render.html import (
    HTMLRenderError,
    render_publication_html,
    validate_standalone_html,
)
from arc_render.workspace import (
    relative_fragment_path,
    write_fragment_revision,
    write_layer,
    write_publication,
)


def _rich_document(asset_payload: bytes | None = None) -> RichDocument:
    source_payload = b"# Reader\n\nSource paragraph.\n"
    source = SourceArtifact(
        SourceFormat.MARKDOWN,
        hashlib.sha256(source_payload).hexdigest(),
        len(source_payload),
        "text/markdown",
        SourceOrigin(SourceOriginKind.REPOSITORY),
    )
    blocks = [
        RichBlock(
            "block-heading",
            0,
            RichBlockKind.HEADING,
            ("section-reader",),
            SourceLocator(SourceFormat.MARKDOWN, 1, 1, 1, 8),
            {"text": "Reader", "level": 1},
        ),
        RichBlock(
            "block-paragraph",
            1,
            RichBlockKind.PARAGRAPH,
            ("section-reader",),
            SourceLocator(SourceFormat.MARKDOWN, 3, 1, 3, 17),
            {
                "text": "Source paragraph.",
                "inline_spans": [
                    {
                        "kind": "text",
                        "start": 0,
                        "end": 17,
                        "text": "Source paragraph.",
                    }
                ],
            },
        ),
    ]
    assets = ()
    if asset_payload is not None:
        digest = hashlib.sha256(asset_payload).hexdigest()
        assets = (RichAsset(digest, "image/png", "figure.png", len(asset_payload)),)
        blocks.append(
            RichBlock(
                "block-figure",
                2,
                RichBlockKind.FIGURE,
                ("section-reader",),
                SourceLocator(SourceFormat.MARKDOWN, 5, 1, 5, 20),
                {
                    "asset_digest": digest,
                    "alt_text": "A source figure",
                    "caption": "Figure caption",
                    "target": "figure.png",
                    "media_type": "image/png",
                    "logical_name": "figure.png",
                    "size": len(asset_payload),
                },
            )
        )
    return RichDocument(
        source,
        tuple(blocks),
        (
            RichSection(
                "section-reader",
                "Reader",
                1,
                0,
                ("section-reader",),
                0,
                len(blocks),
            ),
        ),
        assets,
    )


def _revision(
    document: RichDocument,
    *,
    body: str = "译文带有公式 $x^2$ 和引用 [@ref-1]。",
    revision: int = 1,
    parent: str | None = None,
) -> FragmentRevision:
    block = document.blocks[1]
    return FragmentRevision(
        source=source_identity_from_rich_document(document),
        fragment_id="translation-block-paragraph",
        revision=revision,
        parent_semantic_digest=parent,
        anchor=FragmentAnchor(
            "block",
            block.block_id,
            (anchor_block_from_rich_block(block),),
        ),
        priority=10,
        role="translation",
        language="zh-CN",
        title=None,
        citation_ids=("ref-1",),
        provenance={"producer": "arc-translate"},
        markdown_body=body,
    )


def _workspace(
    tmp_path: Path,
    *,
    asset_payload: bytes | None = None,
    add_second_revision: bool = False,
) -> tuple[Path, Publication, FragmentRevision]:
    document = _rich_document(asset_payload)
    first = _revision(document)
    first_path = write_fragment_revision(tmp_path, first)
    layer = Layer(
        first.source,
        "arc-translate",
        (
            fragment_revision_ref(
                relative_fragment_path(tmp_path, first_path),
                first,
            ),
        ),
    )
    layer_path = tmp_path / "layers" / "translation.json"
    write_layer(layer_path, layer)
    resources = ()
    if asset_payload is not None:
        asset = document.assets[0]
        asset_path = tmp_path / "source-assets" / asset.logical_name
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(asset_payload)
        resources = (
            {
                "artifact_digest": asset.artifact_digest,
                "path": "source-assets/figure.png",
            },
        )
    publication = Publication(
        source_document=document,
        layers=(layer.reference("layers/translation.json"),),
        glossary=(
            {
                "entry_id": "term-reader",
                "term": "Reader",
                "translated_term": "读者",
                "definition": "阅读文本的人。",
            },
        ),
        bibliography=(
            {
                "evidence_id": "ref-1",
                "title": "Reference",
                "source": "https://example.test/reference",
                "dois": [],
                "arxiv_ids": [],
            },
        ),
        labels={"document_title": "Portable Reader"},
        resources=resources,
        reader_profile={
            "title": "Portable Reader",
            "source_language": "en",
            "target_language": "zh-CN",
        },
    )
    if add_second_revision:
        second = _revision(
            document,
            body="修订后的译文 [@ref-1]。",
            revision=2,
            parent=first.semantic_digest,
        )
        write_fragment_revision(tmp_path, second)
    else:
        second = first
    publication_path = tmp_path / "publication.json"
    write_publication(publication_path, publication)
    return publication_path, publication, second


def _payload(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="arc-render-payload" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1).replace(r"<\/script", "</script"))


def test_rendered_html_is_standalone_and_embeds_atomic_markdown(
    tmp_path: Path,
) -> None:
    asset_payload = b"\x89PNG\r\n\x1a\nportable-source-figure"
    publication_path, publication, selected = _workspace(
        tmp_path,
        asset_payload=asset_payload,
        add_second_revision=True,
    )
    output = tmp_path / "reader.html"

    result = render_publication_html(publication_path, output)
    text = output.read_text(encoding="utf-8")
    payload = _payload(text)

    assert result.publication_digest == publication.publication_digest
    assert result.selected_revision_digests == (selected.semantic_digest,)
    assert 'src="assets/' not in text
    assert 'href="assets/' not in text
    assert "window.markdownit" in text
    assert "window.katex" in text
    assert "showDirectoryPicker" in text
    assert "@media print" in text
    assert "--print-to-pdf" not in text
    assert "data:image/png;base64," in text
    assert payload["publication"]["publication_digest"] == (
        publication.publication_digest
    )
    assert payload["selected_revision_digests"] == [selected.semantic_digest]
    revisions = payload["revisions"]
    assert [item["metadata"]["revision"] for item in revisions] == [1, 2]
    assert revisions[-1]["markdown_body"] == "修订后的译文 [@ref-1]。"


def test_source_only_publication_renders_without_layers(tmp_path: Path) -> None:
    publication = Publication(
        _rich_document(),
        labels={"document_title": "Source only"},
        reader_profile={"title": "Source only", "source_language": "en"},
    )
    path = tmp_path / "publication.json"
    write_publication(path, publication)

    result = render_publication_html(path, tmp_path / "source-only.html")

    assert result.selected_revision_digests == ()
    assert (tmp_path / "source-only.html").is_file()


def test_renderer_ignores_stale_machine_fragments_but_keeps_user_notes(
    tmp_path: Path,
) -> None:
    publication_path, _publication, active = _workspace(tmp_path)
    stale = FragmentRevision(
        source=active.source,
        fragment_id="translation-stale-generation",
        revision=1,
        parent_semantic_digest=None,
        anchor=active.anchor,
        priority=10,
        role="translation",
        language=active.language,
        title=None,
        citation_ids=active.citation_ids,
        provenance={"producer": "arc-translate"},
        markdown_body="不再由当前 Layer 声明的旧译文 [@ref-1]。",
    )
    note = FragmentRevision(
        source=active.source,
        fragment_id="user-browser-note",
        revision=1,
        parent_semantic_digest=None,
        anchor=active.anchor,
        priority=110,
        role="note",
        language=active.language,
        title="Reader note",
        citation_ids=(),
        provenance={"producer": "arc-render-browser"},
        markdown_body="用户新增的外挂。",
    )
    write_fragment_revision(tmp_path, stale)
    write_fragment_revision(tmp_path, note)

    result = render_publication_html(
        publication_path, tmp_path / "filtered.html"
    )
    payload = _payload((tmp_path / "filtered.html").read_text(encoding="utf-8"))
    fragment_ids = {
        item["metadata"]["fragment_id"] for item in payload["revisions"]
    }

    assert result.selected_revision_digests == (
        active.semantic_digest,
        note.semantic_digest,
    )
    assert fragment_ids == {active.fragment_id, note.fragment_id}


def test_missing_source_asset_fails_before_publishing_html(tmp_path: Path) -> None:
    publication_path, _, _ = _workspace(
        tmp_path,
        asset_payload=b"\x89PNG\r\n\x1a\nmissing-later",
    )
    (tmp_path / "source-assets" / "figure.png").unlink()
    output = tmp_path / "reader.html"

    with pytest.raises(HTMLRenderError, match="missing|unreadable"):
        render_publication_html(publication_path, output)

    assert not output.exists()


def test_fragment_provenance_must_match_current_rich_source(tmp_path: Path) -> None:
    document = _rich_document()
    normal = _revision(document)
    frozen = anchor_block_from_rich_block(document.blocks[1])
    from arc_render import AnchorBlock

    wrong = FragmentRevision(
        source=normal.source,
        fragment_id=normal.fragment_id,
        revision=1,
        parent_semantic_digest=None,
        anchor=FragmentAnchor(
            "block",
            document.blocks[1].block_id,
            (
                AnchorBlock(
                    frozen.block_id,
                    frozen.kind,
                    frozen.ordinal,
                    frozen.locator,
                    "0" * 64,
                ),
            ),
        ),
        priority=normal.priority,
        role=normal.role,
        language=normal.language,
        title=normal.title,
        citation_ids=normal.citation_ids,
        provenance=normal.provenance,
        markdown_body=normal.markdown_body,
    )
    path = write_fragment_revision(tmp_path, wrong)
    layer = Layer(
        wrong.source,
        "arc-translate",
        (
            fragment_revision_ref(
                relative_fragment_path(tmp_path, path),
                wrong,
            ),
        ),
    )
    write_layer(tmp_path / "layers" / "translation.json", layer)
    publication = Publication(
        document,
        layers=(layer.reference("layers/translation.json"),),
        bibliography=(
            {
                "evidence_id": "ref-1",
                "title": "Reference",
                "source": "https://example.test/reference",
            },
        ),
    )
    publication_path = tmp_path / "publication.json"
    write_publication(publication_path, publication)

    with pytest.raises(HTMLRenderError, match="provenance differs"):
        render_publication_html(publication_path, tmp_path / "reader.html")


def test_fragment_markdown_citations_must_match_declared_ids(
    tmp_path: Path,
) -> None:
    document = _rich_document()
    revision = _revision(document, body="正文含有 [@other]。")
    path = write_fragment_revision(tmp_path, revision)
    layer = Layer(
        revision.source,
        "arc-translate",
        (
            fragment_revision_ref(
                relative_fragment_path(tmp_path, path),
                revision,
            ),
        ),
    )
    write_layer(tmp_path / "layers" / "translation.json", layer)
    publication = Publication(
        document,
        layers=(layer.reference("layers/translation.json"),),
        bibliography=(
            {
                "evidence_id": "ref-1",
                "title": "Reference",
                "source": "https://example.test/reference",
            },
            {
                "evidence_id": "other",
                "title": "Other",
                "source": "https://example.test/other",
            },
        ),
    )
    publication_path = tmp_path / "publication.json"
    write_publication(publication_path, publication)

    with pytest.raises(HTMLRenderError, match="Markdown citations"):
        render_publication_html(publication_path, tmp_path / "reader.html")


def test_layer_rejects_contradictory_fragment_producer(
    tmp_path: Path,
) -> None:
    document = _rich_document()
    normal = _revision(document)
    revision = FragmentRevision(
        source=normal.source,
        fragment_id=normal.fragment_id,
        revision=normal.revision,
        parent_semantic_digest=normal.parent_semantic_digest,
        anchor=normal.anchor,
        priority=normal.priority,
        role=normal.role,
        language=normal.language,
        title=normal.title,
        citation_ids=normal.citation_ids,
        provenance={"producer": "another-producer"},
        markdown_body=normal.markdown_body,
    )
    path = write_fragment_revision(tmp_path, revision)
    layer = Layer(
        revision.source,
        "arc-translate",
        (
            fragment_revision_ref(
                relative_fragment_path(tmp_path, path),
                revision,
            ),
        ),
    )
    write_layer(tmp_path / "layers" / "translation.json", layer)
    publication_path = tmp_path / "publication.json"
    write_publication(
        publication_path,
        Publication(
            document,
            layers=(layer.reference("layers/translation.json"),),
            bibliography=(
                {
                    "evidence_id": "ref-1",
                    "title": "Reference",
                    "source": "https://example.test/reference",
                },
            ),
        ),
    )

    with pytest.raises(HTMLRenderError, match="layer producer"):
        render_publication_html(publication_path, tmp_path / "reader.html")


@pytest.mark.parametrize("tamper", ["revision", "resource"])
def test_standalone_validation_detects_payload_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    publication_path, publication, _selected = _workspace(
        tmp_path,
        asset_payload=b"\x89PNG\r\n\x1a\nvalidated",
    )
    output = tmp_path / "reader.html"
    render_publication_html(publication_path, output)
    text = output.read_text(encoding="utf-8")
    payload = _payload(text)
    if tamper == "revision":
        payload["revisions"][0]["markdown_body"] = "tampered"
    else:
        payload["resources"][0]["data_uri"] = "data:image/png;base64,AAAA"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("</script", r"<\/script")
    text = re.sub(
        r'(<script id="arc-render-payload" type="application/json">)'
        r".*?(</script>)",
        lambda match: match.group(1) + encoded + match.group(2),
        text,
        count=1,
        flags=re.DOTALL,
    )
    output.write_text(text, encoding="utf-8")

    with pytest.raises(HTMLRenderError, match="fragment identity|resource"):
        validate_standalone_html(publication, output)
