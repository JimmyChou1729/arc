from __future__ import annotations

import hashlib

import pytest
from arc_jobs import ArtifactDigest, ArtifactRef
from arc_paper import ArcPaperService, RichDocumentParserService
from arc_render import (
    AnchorKind,
    FragmentAnchor,
    FragmentRevision,
    Layer,
    anchor_block_from_rich_block,
    encode_fragment_revision,
    fragment_revision_filename,
    fragment_revision_ref,
    read_fragment_revision,
    read_layer,
    source_identity_from_rich_document,
)
from arc_translate.delivery import (
    TranslationDeliveryError,
    publish_translation_layer,
    validate_translation_layer,
)
from arc_translate.project import TranslationProject
from arc_translate.workflow import (
    TranslationResult,
    TranslationRevisionArtifact,
)


def test_translation_runtime_is_hidden_and_can_share_an_arc_project(tmp_path) -> None:
    root = tmp_path / "project"
    (root / ".arc" / "companion").mkdir(parents=True)

    project = TranslationProject.open(root)

    assert project.runtime_root == root / ".arc" / "translate"
    assert project.marker == project.runtime_root / "project.json"
    assert not (root / "arc-translate-project.json").exists()
    assert not (project.runtime_root / "paper-cache").exists()


def test_successful_translation_delivery_is_native_layer_and_revision(
    tmp_path,
) -> None:
    source_path = tmp_path / "paper.md"
    source_path.write_text("# Source\n\nSource paragraph.\n", encoding="utf-8")
    paper = ArcPaperService(cache_root=tmp_path / "cache")
    artifact = paper.import_source(source_path)
    document = RichDocumentParserService(paper.repository).parse_source(
        artifact
    )
    block = document.blocks[1]
    source = source_identity_from_rich_document(document)
    revision = FragmentRevision(
        source=source,
        fragment_id="translation-fixture",
        revision=1,
        parent_semantic_digest=None,
        anchor=FragmentAnchor(
            AnchorKind.BLOCK,
            block.block_id,
            (anchor_block_from_rich_block(block),),
        ),
        priority=10,
        role="translation",
        language="zh-CN",
        title=None,
        citation_ids=(),
        provenance={
            "producer": "arc-translate",
            "source_language": "en",
            "translation_mode": "enabled",
        },
        markdown_body="翻译后的段落。",
    )
    filename = fragment_revision_filename(revision)
    path = f"fragments/{revision.fragment_id}/{filename}"
    reference = fragment_revision_ref(path, revision)
    payload = encode_fragment_revision(revision).encode("utf-8")
    artifact_ref = ArtifactRef(
        "translation/fragments/fixture/revision-000001",
        ArtifactDigest(
            "sha256",
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        ),
        "text/markdown",
        "artifacts/objects/fixture",
    )
    result = TranslationResult(
        source_language="en",
        target_language="zh-CN",
        mode="enabled",
        coverage="document",
        layer=Layer(source, "arc-translate", (reference,)),
        revision_artifacts=(
            TranslationRevisionArtifact(reference, artifact_ref),
        ),
    )
    project = TranslationProject.open(tmp_path / "project")

    delivery = publish_translation_layer(
        project, result=result, revision_payloads=(payload,)
    )

    assert delivery == project.root / "translation.layer.json"
    assert read_layer(delivery) == result.layer
    published = read_fragment_revision(project.root / path)
    assert published.markdown_body == "翻译后的段落。"
    assert published.priority == 10
    validate_translation_layer(project, result=result)


def test_selected_translation_cannot_replace_document_layer(tmp_path) -> None:
    project, result, payload = _delivery_fixture(tmp_path)
    publish_translation_layer(
        project, result=result, revision_payloads=(payload,)
    )
    before = project.translation_layer.read_bytes()
    selected = TranslationResult(
        source_language=result.source_language,
        target_language=result.target_language,
        mode=result.mode,
        coverage="selection",
        layer=result.layer,
        revision_artifacts=result.revision_artifacts,
    )

    with pytest.raises(TranslationDeliveryError, match="selected-block"):
        publish_translation_layer(
            project, result=selected, revision_payloads=(payload,)
        )

    assert project.translation_layer.read_bytes() == before


def test_invalid_revision_is_rejected_before_layer_replacement(tmp_path) -> None:
    project, result, payload = _delivery_fixture(tmp_path)
    publish_translation_layer(
        project, result=result, revision_payloads=(payload,)
    )
    before = project.translation_layer.read_bytes()
    original = read_fragment_revision(
        project.root / result.revision_artifacts[0].revision.path
    )
    invalid = FragmentRevision(
        source=original.source,
        fragment_id="invalid-translation-fixture",
        revision=1,
        parent_semantic_digest=None,
        anchor=original.anchor,
        priority=10,
        role="companion",
        language=original.language,
        title=None,
        citation_ids=(),
        provenance=original.provenance,
        markdown_body=original.markdown_body,
    )
    invalid_path = (
        f"fragments/{invalid.fragment_id}/"
        f"{fragment_revision_filename(invalid)}"
    )
    invalid_ref = fragment_revision_ref(invalid_path, invalid)
    invalid_payload = encode_fragment_revision(invalid).encode("utf-8")
    invalid_artifact = ArtifactRef(
        "translation/fragments/invalid/revision-000001",
        ArtifactDigest(
            "sha256",
            hashlib.sha256(invalid_payload).hexdigest(),
            len(invalid_payload),
        ),
        "text/markdown",
        "artifacts/objects/invalid",
    )
    invalid_result = TranslationResult(
        source_language="en",
        target_language="zh-CN",
        mode="enabled",
        coverage="document",
        layer=Layer(original.source, "arc-translate", (invalid_ref,)),
        revision_artifacts=(
            TranslationRevisionArtifact(invalid_ref, invalid_artifact),
        ),
    )

    with pytest.raises(TranslationDeliveryError, match="payload is invalid"):
        publish_translation_layer(
            project,
            result=invalid_result,
            revision_payloads=(invalid_payload,),
        )

    assert project.translation_layer.read_bytes() == before


def _delivery_fixture(
    tmp_path,
) -> tuple[TranslationProject, TranslationResult, bytes]:
    source_path = tmp_path / "fixture-source.md"
    source_path.write_text("# Source\n\nSource paragraph.\n", encoding="utf-8")
    paper = ArcPaperService(cache_root=tmp_path / "fixture-cache")
    artifact = paper.import_source(source_path)
    document = RichDocumentParserService(paper.repository).parse_source(
        artifact
    )
    block = document.blocks[1]
    source = source_identity_from_rich_document(document)
    revision = FragmentRevision(
        source=source,
        fragment_id="translation-delivery-fixture",
        revision=1,
        parent_semantic_digest=None,
        anchor=FragmentAnchor(
            AnchorKind.BLOCK,
            block.block_id,
            (anchor_block_from_rich_block(block),),
        ),
        priority=10,
        role="translation",
        language="zh-CN",
        title=None,
        citation_ids=(),
        provenance={
            "producer": "arc-translate",
            "source_language": "en",
            "translation_mode": "enabled",
        },
        markdown_body="翻译后的段落。",
    )
    path = (
        f"fragments/{revision.fragment_id}/"
        f"{fragment_revision_filename(revision)}"
    )
    reference = fragment_revision_ref(path, revision)
    payload = encode_fragment_revision(revision).encode("utf-8")
    artifact_ref = ArtifactRef(
        "translation/fragments/delivery/revision-000001",
        ArtifactDigest(
            "sha256",
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        ),
        "text/markdown",
        "artifacts/objects/delivery",
    )
    result = TranslationResult(
        source_language="en",
        target_language="zh-CN",
        mode="enabled",
        coverage="document",
        layer=Layer(source, "arc-translate", (reference,)),
        revision_artifacts=(
            TranslationRevisionArtifact(reference, artifact_ref),
        ),
    )
    return TranslationProject.open(tmp_path / "delivery-project"), result, payload
