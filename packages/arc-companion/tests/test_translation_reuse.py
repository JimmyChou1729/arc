from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from arc_jobs import (
    ImmutableArtifactStore,
    RunContext,
    RunEngine,
    RunError,
    RunStatus,
    Succeeded,
    encode_artifact_ref,
)
from arc_paper import (
    RichDocument,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)
from arc_translate import BlocksResult, GlossaryResult, LanguageResult

from arc_companion.build import (
    COMPANION_BUILD_DIAGNOSTICS_SCHEMA,
    COMPANION_BUILD_HANDLER,
)
from arc_companion.cli import main
from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    CompanionContentCodec,
    GlossaryEntry,
    SourceAnchor,
    TranslatedBlock,
)
from arc_companion.project import CompanionProjectPaths
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_build_request,
)
from arc_companion.service import CompanionService, companion_run_id
from arc_companion.source_planning import plan_source_chapters
from arc_companion.translation_reuse import (
    StagedTranslationReuseAdapter,
    TranslationReuseSource,
)


def _document(
    tmp_path: Path,
    text: str = "# First\n\nA quantum field.\n",
) -> RichDocument:
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        text.encode("utf-8"),
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT,
            locator="source.md",
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


class _PublishedTranslationHandler:
    name = COMPANION_BUILD_HANDLER

    def __init__(
        self,
        source: RichDocument,
        *,
        target_language: str,
        approx_term_count: int,
    ) -> None:
        self.source = source
        self.target_language = target_language
        self.approx_term_count = approx_term_count

    def execute(self, context: RunContext) -> Succeeded:
        source = self.source
        context.artifacts.publish_json(
            "diagnostics/build",
            {
                "schema_version": COMPANION_BUILD_DIAGNOSTICS_SCHEMA,
                "status": "not_required",
                "source_document_digest": source.document_digest,
                "effective_document_digest": source.document_digest,
                "trigger_reasons": [],
                "warnings": [],
                "visual_review": None,
            },
        )
        context.artifacts.publish_json(
            "translation-v2/language/result",
            LanguageResult(
                source.document_digest,
                source.source.artifact_digest,
                "en",
                "known",
                0.99,
                self.target_language,
                "enabled",
            ).to_document(),
        )
        first_block_id = source.blocks[0].block_id
        glossary_entry = {
            "term_id": "term-field",
            "term": "field",
            "aliases": [],
            "occurrence_count": 1,
            "source_refs": [first_block_id],
            "matched_sentences": ["A quantum field."],
            "preferred_translation": "场",
            "target_definition": "量子理论中的场。",
        }
        context.artifacts.publish_json(
            "translation-v2/glossary/result",
            GlossaryResult(
                source.document_digest,
                source.source.artifact_digest,
                self.target_language,
                self.approx_term_count,
                "a" * 64,
                (glossary_entry,),
            ).to_document(),
        )

        accepted_chapters = []
        for chapter in plan_source_chapters(source):
            translations = tuple(
                {
                    "block_id": block_id,
                    "text": f"译文 {index + 1}",
                }
                for index, block_id in enumerate(chapter.block_ids)
            )
            context.artifacts.publish_json(
                f"chapters/{chapter.chapter_id}/translation/result",
                BlocksResult(
                    source.document_digest,
                    source.source.artifact_digest,
                    "en",
                    self.target_language,
                    "enabled",
                    translations,
                ).to_document(),
            )
            blocks = {
                block.block_id: block
                for block in source.blocks
            }
            accepted_chapters.append(
                AcceptedChapter(
                    chapter_id=chapter.chapter_id,
                    title=chapter.title,
                    source_anchors=tuple(
                        SourceAnchor.from_rich_block(blocks[block_id])
                        for block_id in chapter.block_ids
                    ),
                    translations=tuple(
                        TranslatedBlock(
                            block_id=item["block_id"],
                            text=item["text"],
                        )
                        for item in translations
                    ),
                )
            )
        book = AcceptedBook(
            document_digest=source.document_digest,
            title="Reusable source",
            source_language="en",
            target_language=self.target_language,
            translation_mode="enabled",
            chapters=tuple(accepted_chapters),
            glossary=(
                GlossaryEntry(
                    entry_id="term-field",
                    term="field",
                    translated_term="场",
                    definition="量子理论中的场。",
                    anchor_ids=(first_block_id,),
                ),
            ),
        )
        book_ref = context.artifacts.publish_bytes(
            "book/accepted",
            CompanionContentCodec.dumps(book).encode("utf-8"),
            media_type="application/json",
        )
        result_ref = context.artifacts.publish_json(
            "result",
            {
                "schema_version": "arc.companion.build_result.v1",
                "accepted_book": encode_artifact_ref(book_ref),
            },
        )
        return Succeeded(result_ref)


def _successful_source(
    tmp_path: Path,
) -> tuple[
    CompanionProjectPaths,
    CompanionBuildRequest,
    CompanionGenerationRecipe,
]:
    source = _document(tmp_path)
    request = CompanionBuildRequest(
        source,
        target_language="zh-CN",
        user_intent="Explain the source.",
    )
    recipe = CompanionGenerationRecipe(approx_term_count=3)
    paths = CompanionProjectPaths.open(tmp_path / "source-project")
    service = CompanionService(paths.jobs_root)
    snapshot = service.prepare(request, recipe=recipe)
    snapshot = RunEngine(service.repository).execute(
        service.repository.read_spec(snapshot.run_id),
        _PublishedTranslationHandler(
            source,
            target_language=request.target_language,
            approx_term_count=recipe.approx_term_count,
        ),
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    paths.select_run(snapshot.run_id)
    return paths, request, recipe


def _source_payloads(
    paths: CompanionProjectPaths,
    run_id: str,
    source: RichDocument,
) -> dict[str, bytes]:
    service = CompanionService(paths.jobs_root)
    snapshot = service.inspect(run_id).snapshot
    store = ImmutableArtifactStore(
        service.repository.run_directory(run_id),
        repository_root=service.repository.root,
    )
    ids = [
        "translation-v2/language/result",
        "translation-v2/glossary/result",
        *[
            f"chapters/{chapter.chapter_id}/translation/result"
            for chapter in plan_source_chapters(source)
        ],
    ]
    prefix = (
        ""
        if snapshot.recovery_epoch == 0
        else f"recovery-{snapshot.recovery_epoch}/"
    )
    output = {}
    for logical_id in ids:
        ref = store.find(f"{prefix}{logical_id}")
        assert ref is not None
        output[logical_id] = store.read_bytes(ref)
    return output


class _FailTranslationAdapter:
    def detect_language(self, *_args, **_kwargs):
        raise AssertionError("provider-backed adapter must not be used")

    def build_glossary(self, *_args, **_kwargs):
        raise AssertionError("provider-backed adapter must not be used")

    def translate_blocks(self, *_args, **_kwargs):
        raise AssertionError("provider-backed adapter must not be used")


def test_exact_translation_reuse_is_target_owned_and_provider_free(
    tmp_path: Path,
) -> None:
    source_paths, source_request, recipe = _successful_source(tmp_path)
    source_run_id = source_paths.current_run_id
    assert source_run_id is not None
    source_payloads = _source_payloads(
        source_paths, source_run_id, source_request.source
    )

    target_paths = CompanionProjectPaths.open(tmp_path / "target-project")
    target_service = CompanionService(target_paths.jobs_root)
    target_request = replace(
        source_request,
        user_intent="Use a different guide intent.",
    )
    reuse_source = TranslationReuseSource(source_paths.root)
    plan = target_service.plan_translation_reuse(
        reuse_source, target_request, recipe
    )
    target_request = replace(
        target_request, translation_reuse_digest=plan.reuse_digest
    )
    prepared = target_service.prepare(target_request, recipe=recipe)
    target_service.stage_translation_reuse(
        prepared.run_id, reuse_source, plan=plan
    )

    handler = target_service._handler(
        target_service.repository.read_spec(prepared.run_id),
        execution=CompanionExecutionOptions(),
        task_service=None,
        translation_adapter=_FailTranslationAdapter(),
    )
    assert isinstance(
        handler.translation_adapter, StagedTranslationReuseAdapter
    )

    detached_source = tmp_path / "detached-source-project"
    source_paths.root.rename(detached_source)
    context = RunContext(
        target_service.repository,
        prepared,
        resume_input=None,
    )
    adapter = handler.translation_adapter
    assert adapter is not None
    language = adapter.detect_language(
        context,
        source_request.source,
        target_language=target_request.target_language,
    )
    assert not isinstance(language, RunError)
    glossary = adapter.build_glossary(
        context,
        source_request.source,
        target_language=target_request.target_language,
        approx_count=recipe.approx_term_count,
    )
    assert not isinstance(glossary, RunError)
    for chapter in plan_source_chapters(source_request.source):
        translated = adapter.translate_blocks(
            context,
            source_request.source,
            block_ids=chapter.block_ids,
            target_language=target_request.target_language,
            artifact_prefix=f"chapters/{chapter.chapter_id}/translation",
        )
        assert not isinstance(translated, RunError)

    for artifact_id, expected in source_payloads.items():
        ref = context.artifacts.find(artifact_id)
        assert ref is not None
        assert context.artifacts.read_bytes(ref) == expected
    receipt = target_service.translation_reuse_receipt(prepared.run_id)
    assert receipt is not None
    assert receipt.document["reuse_digest"] == plan.reuse_digest
    assert receipt.document["source"]["run_id"] == source_run_id
    assert receipt.document["target_run_id"] == prepared.run_id


def test_staged_reuse_rejects_a_different_effective_source(
    tmp_path: Path,
) -> None:
    source_paths, source_request, recipe = _successful_source(tmp_path)
    target_paths = CompanionProjectPaths.open(tmp_path / "target-project")
    service = CompanionService(target_paths.jobs_root)
    reuse_source = TranslationReuseSource(source_paths.root)
    plan = service.plan_translation_reuse(reuse_source, source_request, recipe)
    request = replace(
        source_request, translation_reuse_digest=plan.reuse_digest
    )
    prepared = service.prepare(request, recipe=recipe)
    service.stage_translation_reuse(prepared.run_id, reuse_source, plan=plan)
    adapter = StagedTranslationReuseAdapter(
        plan.reuse_digest,
        approx_term_count=recipe.approx_term_count,
    )
    different = _document(
        tmp_path / "different",
        "# First\n\nA classically different field.\n",
    )

    outcome = adapter.detect_language(
        RunContext(service.repository, prepared, resume_input=None),
        different,
        target_language=request.target_language,
    )

    assert isinstance(outcome, RunError)
    assert outcome.code == "translation_reuse_effective_source_mismatch"
    assert service.translation_reuse_receipt(prepared.run_id) is None


def test_reuse_digest_is_semantic_without_changing_default_request_identity(
    tmp_path: Path,
) -> None:
    source = _document(tmp_path)
    plain = CompanionBuildRequest(source)
    encoded = encode_build_request(plain)
    digest = "b" * 64
    reused = replace(plain, translation_reuse_digest=digest)
    recipe = CompanionGenerationRecipe()

    assert encoded["schema_version"] == "arc.companion.build_request.v2"
    assert "translation_reuse_digest" not in encoded
    assert encode_build_request(reused)["translation_reuse_digest"] == digest
    assert companion_run_id(plain, recipe) != companion_run_id(reused, recipe)


def test_translation_reuse_is_an_explicit_build_only_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["build", "--help"]) == 0
    assert "--reuse-translation-from" in capsys.readouterr().out
    assert main(["resume", "--help"]) == 0
    assert "--reuse-translation-from" not in capsys.readouterr().out
