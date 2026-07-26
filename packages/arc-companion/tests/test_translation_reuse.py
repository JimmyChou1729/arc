from __future__ import annotations

import hashlib
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
    atomic_write_json,
    canonical_json_bytes,
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
    _glossary_contracts,
    _prior_companion_reference,
)
from arc_companion.cli import main
from arc_companion.contracts import (
    AcceptedBook,
    AcceptedChapter,
    CompanionContentCodec,
    LearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from arc_companion.project import CompanionProjectPaths
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_build_request,
    encode_handler_semantic_input,
)
from arc_companion.service import CompanionService, companion_run_id
from arc_companion.source_planning import plan_source_chapters
from arc_companion.translation_reuse import (
    StagedTranslationReuseAdapter,
    TranslationReuseError,
    TranslationReuseSource,
    _decode_reuse_source_semantics,
    _read_artifact,
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
        accepted_glossary_translation: str = "场",
    ) -> None:
        self.source = source
        self.target_language = target_language
        self.approx_term_count = approx_term_count
        self.accepted_glossary_translation = accepted_glossary_translation

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
        glossary_result = GlossaryResult(
            source.document_digest,
            source.source.artifact_digest,
            self.target_language,
            self.approx_term_count,
            "a" * 64,
            (glossary_entry,),
        )
        context.artifacts.publish_json(
            "translation-v2/glossary/result",
            glossary_result.to_document(),
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
                    learning_units=(
                        LearningUnit(
                            unit_id="prior-reading",
                            title="旧伴读中的可取舍洞见",
                            anchor_ids=(chapter.block_ids[0],),
                            placement="inline",
                            content_markdown=(
                                "这是供下一轮模型深化、重组或舍弃的旧伴读内容。"
                            ),
                        ),
                    ),
                )
            )
        accepted_glossary = _glossary_contracts(
            glossary_result.to_document(), source
        )
        if self.accepted_glossary_translation != "场":
            accepted_glossary = (
                replace(
                    accepted_glossary[0],
                    translated_term=self.accepted_glossary_translation,
                ),
            )
        book = AcceptedBook(
            document_digest=source.document_digest,
            title="Reusable source",
            source_language="en",
            target_language=self.target_language,
            translation_mode="enabled",
            chapters=tuple(accepted_chapters),
            glossary=accepted_glossary,
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
    *,
    accepted_glossary_translation: str = "场",
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
            accepted_glossary_translation=accepted_glossary_translation,
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
    prior_ref = context.artifacts.find(
        "translation-reuse/prior-companion"
    )
    assert prior_ref is not None
    prior = _prior_companion_reference(context)
    assert prior is not None
    assert prior["chapters"][0]["learning_units"][0][
        "content_markdown"
    ].startswith("这是供下一轮模型")
    assert any(
        item["role"] == "prior_companion"
        for item in plan.bundle["artifacts"]
    )
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

    assert encoded["schema_version"] == "arc.companion.build_request.v4"
    assert encoded["translation_reuse_digest"] is None
    assert encode_build_request(reused)["translation_reuse_digest"] == digest
    assert companion_run_id(plain, recipe) != companion_run_id(reused, recipe)


def test_translation_reuse_is_an_explicit_build_only_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["build", "--help"]) == 0
    assert "--reuse-translation-from" in capsys.readouterr().out
    assert main(["resume", "--help"]) == 0
    assert "--reuse-translation-from" not in capsys.readouterr().out


def test_legacy_v4_recipe_can_supply_exact_translation_identity(
    tmp_path: Path,
) -> None:
    source = _document(tmp_path)
    request = CompanionBuildRequest(source)
    semantic_input = encode_handler_semantic_input(
        request, CompanionGenerationRecipe(approx_term_count=7)
    )
    current = dict(semantic_input["generation_recipe"])
    legacy = {
        key: value
        for key, value in current.items()
        if key
        not in {
            "author_identity_prompt",
            "literature_request_prompt",
            "literature_survey_prompt",
        }
    }
    legacy["schema_version"] = "arc.companion.generation_recipe.v4"
    legacy["chapter_plan_prompt"] = "legacy.chapter-plan"
    legacy["chapter_guide_prompt"] = "legacy.chapter-guide"
    legacy["chapter_guide_review_prompt"] = "legacy.chapter-review"

    decoded_request, approx_count = _decode_reuse_source_semantics(
        {
            "request": semantic_input["request"],
            "generation_recipe": legacy,
        }
    )

    assert (
        decoded_request.source.document_digest
        == request.source.document_digest
    )
    assert (
        decoded_request.source.source.artifact_digest
        == request.source.source.artifact_digest
    )
    assert decoded_request.target_language == request.target_language
    assert approx_count == 7


def test_reuse_reads_translation_from_newest_available_recovery_epoch(
    tmp_path: Path,
) -> None:
    store = ImmutableArtifactStore(tmp_path / "run")
    payload = b'{"translated":"stable"}\n'
    store.publish_bytes(
        "recovery-3/chapters/chapter/translation/result",
        payload,
        media_type="application/json",
    )

    artifact = _read_artifact(
        store,
        4,
        "chapters/chapter/translation/result",
        role="translation",
        chapter_id="chapter",
    )

    assert artifact.source_artifact_id.startswith("recovery-3/")
    assert artifact.payload == payload


def test_reuse_uses_recovery_approved_working_semantics(
    tmp_path: Path,
) -> None:
    source_paths, source_request, recipe = _successful_source(tmp_path)
    run_id = source_paths.current_run_id
    assert run_id is not None
    service = CompanionService(source_paths.jobs_root)
    immutable_spec = service.repository.read_spec(run_id)
    working = service.repository.working_state(run_id)
    working.materialize(immutable_spec)
    working_semantics = encode_handler_semantic_input(
        replace(source_request, user_intent="Recovered guide intent."),
        recipe,
    )
    atomic_write_json(working.semantic_input_path, working_semantics)

    plan = service.plan_translation_reuse(
        TranslationReuseSource(source_paths.root),
        source_request,
        recipe,
    )

    expected_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "handler": immutable_spec.handler,
                "semantic_input": working_semantics,
            }
        )
    ).hexdigest()
    assert plan.bundle["source"]["spec_sha256"] == expected_digest


def test_reuse_rejects_glossary_not_accepted_by_final_book(
    tmp_path: Path,
) -> None:
    source_paths, source_request, recipe = _successful_source(
        tmp_path, accepted_glossary_translation="域"
    )

    with pytest.raises(
        TranslationReuseError,
        match="accepted book glossary differs",
    ):
        CompanionService(source_paths.jobs_root).plan_translation_reuse(
            TranslationReuseSource(source_paths.root),
            source_request,
            recipe,
        )
