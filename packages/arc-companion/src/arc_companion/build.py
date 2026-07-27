"""Current split translation and guide Companion build handler."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from arc_jobs import (
    ArtifactRef,
    ArtifactSourceRef,
    Failed,
    FailureMode,
    GroupResult,
    Paused,
    RunContext,
    RunError,
    Succeeded,
    UnitResult,
    WorkUnit,
    canonical_json_bytes,
)
from arc_llm import (
    ArcRuntimeEnvironment,
    JsonOutput,
    LLMFailed,
    LLMInputArtifact,
    LLMPaused,
    LLMRequest,
    LLMTaskService,
)
from arc_paper import (
    ArcPaperService,
    CachedDocumentError,
    EquationLabelReviewService,
    PdftoppmFullPageRenderer,
    RichDocument,
    ReferenceMaterialCache,
    ReferenceCacheError,
    SourceFormat,
    SourceRepositoryError,
    apply_visual_equation_labels,
    detect_suspicious_equation_labels,
    cached_document_ref_to_document,
    cached_reference_material_from_document,
    cached_reference_material_to_document,
    rich_document_from_document,
    rich_document_to_document,
)
from arc_proposer_reviewer import (
    BatchFailurePolicy,
    BatchRequest,
    ExecutionOptions as ProposerReviewerExecutionOptions,
    LoopSpec,
    ProposerFailurePolicy,
    ProposerReviewerService,
    RevisionContextMode,
    WorkerSpec,
)
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION
from arc_proposer_reviewer.protocol import decode_batch_result

from ._build_support import (
    accepted_chapter_document,
    mapping,
    mapping_list,
    read_json,
    ref_document,
    task_id,
)
from .contracts import (
    AcceptedBook,
    AcceptedChapter,
    CompanionContentCodec,
    EvidenceSource,
    GlossaryEntry,
    LearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from .generation_validation import (
    CompanionContentError,
    validate_author_identity,
    validate_chapter_guide,
    validate_chapter_plan,
)
from .llm_runtime import (
    CompanionLLMError,
    SemanticTaskCompleted,
    awaiting_from_pause,
    execute_semantically_validated_task,
    outer_resume_input,
    run_error_from_failure,
)
from .model_source import (
    model_chapter_block_index,
    model_source_index,
    model_source_view,
    validate_model_source_index,
)
from .prompts import (
    AUTHOR_IDENTITY_PROMPT_VERSION,
    AUTHOR_IDENTITY_SCHEMA,
    CHAPTER_GUIDE_PROPOSAL_SCHEMA,
    CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA,
    CHAPTER_PLAN_SCHEMA,
    author_identity_prompt,
    chapter_guide_proposer_instructions,
    chapter_guide_reviewer_instructions,
    chapter_plan_prompt,
)
from .reader_labels import ReaderLabelError, resolve_reader_labels
from .reading_order import first_visible_citation_ids
from .rich_text import RichTextError
from .request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_handler_semantic_input,
)
from .source_planning import (
    SourceChapter,
    block_prompt_document,
    equation_label_provenance,
    plan_source_chapters,
)
from .source_identity import resolve_document_identity
from .translation_adapter import (
    ArcTranslateAdapter,
    CompanionTranslationAdapter,
)
from .validation import require_valid_accepted_book


COMPANION_BUILD_HANDLER = "arc.companion.build.v8"
COMPATIBLE_COMPANION_BUILD_HANDLERS = frozenset(
    {
        COMPANION_BUILD_HANDLER,
        "arc.companion.build.v7",
        "arc.companion.build.v5",
        "arc.companion.build.v4",
        "arc.companion.build.v3",
    }
)
COMPANION_BUILD_DIAGNOSTICS_SCHEMA = "arc.companion.build_diagnostics.v1"
_BOOK_ARTIFACT = "book/accepted"
_DIAGNOSTICS_ARTIFACT = "diagnostics/build"
_EFFECTIVE_SOURCE_ARTIFACT = "source/effective"
_MODEL_SOURCE_VIEW_ARTIFACT = "source/model-view"
_MODEL_SOURCE_INDEX_ARTIFACT = "source/model-index"
_ORIGINAL_SOURCE_ARTIFACT = "source/original"
_AUTHOR_IDENTITY_ARTIFACT = "identity/authors"
_PRIOR_COMPANION_ARTIFACT = "translation-reuse/prior-companion"
_PRIOR_REFERENCE_ARTIFACT = "translation-reuse/prior-reference"
_RESULT_ARTIFACT = "result"


class CompanionBuildHandler:
    """Coordinate arc-translate and reviewed Companion guide generation."""

    name = COMPANION_BUILD_HANDLER

    def __init__(
        self,
        request: CompanionBuildRequest,
        recipe: CompanionGenerationRecipe = CompanionGenerationRecipe(),
        *,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        task_service: LLMTaskService | None = None,
        translation_adapter: CompanionTranslationAdapter | None = None,
    ) -> None:
        self.request = request
        self.recipe = recipe
        self.execution = execution
        self.llm_options = _companion_llm_options(execution)
        self.task_service = task_service or LLMTaskService()
        self.translation_adapter = translation_adapter or ArcTranslateAdapter(
            self.task_service,
            paper_cache_root=self.execution.paper_cache_root,
        )

    def semantic_input(self) -> dict[str, Any]:
        return encode_handler_semantic_input(self.request, self.recipe)

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "companion_build_binding_mismatch",
                    "Handler bindings do not match the durable build request.",
                )
            )
        existing = context.artifacts.find(_RESULT_ARTIFACT)
        if existing is not None:
            return Succeeded(existing)
        try:
            resume_input = outer_resume_input(context)
            prepared_source = self._prepare_source(context, resume_input)
            if isinstance(prepared_source, Paused):
                return prepared_source
            source = prepared_source
            chapters = plan_source_chapters(source)
            model_inputs = self._model_source_inputs(
                context, source, chapters
            )
            try:
                reader_labels = resolve_reader_labels(
                    self.request.target_language,
                    self.request.reader_labels,
                )
            except ReaderLabelError as exc:
                raise CompanionContentError(
                    "reader_labels_invalid", str(exc)
                ) from exc
            identity = resolve_document_identity(source)
            title = identity.title or reader_labels["untitled_document"]
            authors_outcome = self._authors(
                context,
                resume_input,
                source,
                title=title,
                auto_candidates=identity.candidate_authors,
                author_basis=identity.author_basis,
                inputs=model_inputs,
            )
            if isinstance(authors_outcome, (Paused, Failed)):
                return authors_outcome
            authors = authors_outcome
            blocks = {
                item.block_id: item for item in source.blocks
            }

            language = self.translation_adapter.detect_language(
                context,
                source,
                target_language=self.request.target_language,
                model=self.recipe.model,
                execution=self.llm_options,
                resume_input=resume_input,
            )
            if isinstance(language, Paused):
                return language
            if isinstance(language, RunError):
                return Failed(language)
            language = mapping(language, "language result")
            translation_required = language.get("mode") == "enabled"
            if language.get("mode") not in {"enabled", "skipped"}:
                raise CompanionContentError(
                    "language_result_invalid",
                    "arc-translate language mode is invalid",
                )
            prior_companion = _prior_companion_reference(context)
            prior_input = _prior_reference_input(
                context, prior_companion
            )
            document_inputs = model_inputs + (
                (prior_input,) if prior_input is not None else ()
            )

            plans = self._plans(
                context,
                resume_input,
                source,
                chapters,
                blocks,
                title,
                prior_companion,
                inputs=document_inputs,
            )
            if isinstance(plans, (Paused, Failed)):
                return plans

            glossary: dict[str, Any]
            if translation_required:
                glossary_outcome = self.translation_adapter.build_glossary(
                    context,
                    source,
                    language=language,
                    target_language=self.request.target_language,
                    approx_count=self.recipe.approx_term_count,
                    model=self.recipe.model,
                    execution=self.llm_options,
                    resume_input=resume_input,
                )
                if isinstance(glossary_outcome, Paused):
                    return glossary_outcome
                if isinstance(glossary_outcome, RunError):
                    return Failed(glossary_outcome)
                glossary = mapping(glossary_outcome, "glossary result")
                _glossary_entries(glossary)
            else:
                # Same-primary-language runs deliberately never invoke keyword,
                # glossary, or translation work.
                glossary = _empty_glossary(source)

            chapters_outcome = self._chapter_lanes(
                context,
                resume_input,
                chapters,
                plans,
                glossary,
                blocks,
                source=source,
                language=language,
                translation_required=translation_required,
                prior_companion=prior_companion,
                model_inputs=document_inputs,
            )
            if isinstance(chapters_outcome, (Paused, Failed)):
                return chapters_outcome

            book_ref = context.artifacts.find(_BOOK_ARTIFACT)
            if book_ref is None:
                glossary_contracts = (
                    _glossary_contracts(glossary, source)
                    if translation_required
                    else ()
                )
                cited_ids = first_visible_citation_ids(
                    chapters_outcome,
                    glossary_contracts,
                )
                bibliography = _chapter_reference_contracts(
                    context,
                    chapters,
                    cited_ids=cited_ids,
                )
                book = AcceptedBook(
                    document_digest=source.document_digest,
                    title=title,
                    authors=authors,
                    source_language=str(language["language_tag"]),
                    target_language=self.request.target_language,
                    reader_labels=reader_labels,
                    translation_mode=(
                        "enabled" if translation_required else "skipped"
                    ),
                    chapters=chapters_outcome,
                    glossary=glossary_contracts,
                    bibliography=bibliography,
                )
                require_valid_accepted_book(
                    book,
                    expected_block_ids=[
                        item.block_id
                        for item in source.blocks
                    ],
                )
                book_ref = context.artifacts.publish_bytes(
                    _BOOK_ARTIFACT,
                    CompanionContentCodec.dumps(book).encode("utf-8"),
                    media_type="application/json",
                )
            result_ref = context.artifacts.publish_json(
                _RESULT_ARTIFACT,
                {
                    "schema_version": "arc.companion.build_result.v1",
                    "accepted_book": ref_document(book_ref),
                },
            )
            return Succeeded(result_ref)
        except CompanionContentError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except RichTextError as exc:
            return Failed(RunError("learning_markdown_invalid", str(exc)))
        except CompanionLLMError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except (KeyError, TypeError, ValueError) as exc:
            return Failed(RunError("companion_content_invalid", str(exc)))

    def _model_source_inputs(
        self,
        context: RunContext,
        source: RichDocument,
        chapters: Sequence[SourceChapter],
    ) -> tuple[LLMInputArtifact, ...]:
        """Freeze body-free index plus text-only source fallback."""

        paper = ArcPaperService(cache_root=self.execution.paper_cache_root)
        cached_document: Mapping[str, Any] | None = None
        try:
            source_bytes = paper.repository.read_bytes(source.source)
            original = context.artifacts.find(_ORIGINAL_SOURCE_ARTIFACT)
            if original is None:
                original = context.artifacts.publish_bytes(
                    _ORIGINAL_SOURCE_ARTIFACT,
                    source_bytes,
                    media_type=source.source.media_type,
                )
            if (
                original.digest.value != source.source.artifact_digest
                or original.digest.size_bytes != source.source.size
            ):
                raise CompanionContentError(
                    "model_source_original_mismatch",
                    "Frozen original source does not match its source identity.",
                )
            cached_document = cached_document_ref_to_document(
                paper.cache_document(source.source)
            )
        except (
            CachedDocumentError,
            SourceRepositoryError,
            OSError,
        ):
            # The verified text projection remains sufficient for model work.
            # Cache access is an optimization and must not create a host turn.
            cached_document = None

        cache_relationship = (
            "fallback_only"
            if cached_document is None
            else (
                "exact"
                if source.document_digest
                == self.request.source.document_digest
                else "equation_label_overlay"
            )
        )
        index = model_source_index(
            source,
            chapters,
            cache_document=cached_document,
            cache_relationship=cache_relationship,
        )
        index_ref = context.artifacts.find(_MODEL_SOURCE_INDEX_ARTIFACT)
        if index_ref is None:
            index_ref = context.artifacts.publish_json(
                _MODEL_SOURCE_INDEX_ARTIFACT, index
            )
        else:
            frozen_index = read_json(
                context, index_ref, "model source index"
            )
            validate_model_source_index(
                frozen_index, document=source, chapters=chapters
            )
            if frozen_index != index:
                raise CompanionContentError(
                    "model_source_index_mismatch",
                    "Frozen model source index differs from current cache identity.",
                )
        inputs = [
            _llm_input(context, "companion-source-index", index_ref)
        ]
        if cached_document is None:
            source_text = model_source_view(source, chapters).encode("utf-8")
            source_ref = context.artifacts.find(_MODEL_SOURCE_VIEW_ARTIFACT)
            if source_ref is None:
                source_ref = context.artifacts.publish_bytes(
                    _MODEL_SOURCE_VIEW_ARTIFACT,
                    source_text,
                    media_type="text/markdown",
                )
            elif context.artifacts.read_bytes(source_ref) != source_text:
                raise CompanionContentError(
                    "model_source_view_mismatch",
                    "Frozen model source view differs from the effective source.",
                )
            inputs.append(
                _llm_input(context, "companion-source", source_ref)
            )
        return tuple(inputs)

    def _authors(
        self,
        context: RunContext,
        resume_input: Any,
        source: RichDocument,
        *,
        title: str,
        auto_candidates: Sequence[str],
        author_basis: str,
        inputs: tuple[LLMInputArtifact, ...],
    ) -> tuple[str, ...] | Paused | Failed:
        """Resolve visible publication authors without asking the guide model."""

        if self.request.authors:
            return tuple(self.request.authors)
        existing = context.artifacts.find(_AUTHOR_IDENTITY_ARTIFACT)
        if existing is not None:
            value = validate_author_identity(
                read_json(context, existing, "author identity"),
                block_ids=[item.block_id for item in source.blocks],
            )
            return (
                tuple(value["authors"])
                if value["confidence"] == "high"
                else ()
            )

        semantic = {
            "document_digest": source.document_digest,
            "title": title,
            "auto_candidates": list(auto_candidates),
            "author_basis": author_basis,
            "prompt_contract": getattr(
                self.recipe,
                "author_identity_prompt",
                AUTHOR_IDENTITY_PROMPT_VERSION,
            ),
        }
        request = LLMRequest(
            task_id("author-identity", semantic),
            author_identity_prompt(
                title=title,
                auto_candidates=[
                    {
                        "author": author,
                        "basis": author_basis,
                    }
                    for author in auto_candidates
                ],
            ),
            JsonOutput(AUTHOR_IDENTITY_SCHEMA, repair="format"),
            self.recipe.model,
            inputs=inputs,
        )
        outcome = execute_semantically_validated_task(
            self.task_service,
            context,
            request,
            candidate_id="identity/author.json",
            description="author identity",
            validate=lambda raw: validate_author_identity(
                raw,
                block_ids=[item.block_id for item in source.blocks],
            ),
            resume_input=resume_input,
            options=self.llm_options,
        )
        if isinstance(outcome, Paused):
            return outcome
        if isinstance(outcome, LLMFailed):
            return Failed(run_error_from_failure(outcome))
        assert isinstance(outcome, SemanticTaskCompleted)
        value = outcome.value
        candidate_path = outcome.candidate_paths[-1]
        context.artifacts.publish_json(_AUTHOR_IDENTITY_ARTIFACT, value)
        context.artifacts.publish_json(
            "diagnostics/author",
            {
                "schema_version": "arc.companion.author_diagnostics.v1",
                "status": (
                    "confirmed"
                    if value["confidence"] == "high"
                    else "unconfirmed"
                ),
                "confidence": value["confidence"],
                "basis": value["basis"],
                "candidate_path": str(candidate_path),
            },
        )
        return (
            tuple(value["authors"])
            if value["confidence"] == "high"
            else ()
        )

    def _prepare_source(
        self,
        context: RunContext,
        resume_input: Any,
    ) -> RichDocument | Paused:
        existing = context.artifacts.find(_DIAGNOSTICS_ARTIFACT)
        if existing is not None:
            diagnostics = read_json(context, existing, "build diagnostics")
            validate_build_diagnostics(diagnostics)
            if (
                diagnostics["source_document_digest"]
                != self.request.source.document_digest
            ):
                raise CompanionContentError(
                    "build_diagnostics_mismatch",
                    "Build diagnostics do not match the requested source.",
                )
            if diagnostics["status"] != "applied":
                if (
                    diagnostics["effective_document_digest"]
                    != self.request.source.document_digest
                ):
                    raise CompanionContentError(
                        "build_diagnostics_mismatch",
                        "Retained-label diagnostics changed the effective source.",
                    )
                return self.request.source
            source_ref = context.artifacts.find(_EFFECTIVE_SOURCE_ARTIFACT)
            if source_ref is None:
                raise CompanionContentError(
                    "effective_source_missing",
                    "Applied equation-label diagnostics have no effective source.",
                )
            source = rich_document_from_document(
                read_json(context, source_ref, "effective source")
            )
            if (
                source.document_digest
                != diagnostics["effective_document_digest"]
            ):
                raise CompanionContentError(
                    "effective_source_mismatch",
                    "Effective source does not match build diagnostics.",
                )
            return source

        reasons = detect_suspicious_equation_labels(self.request.source)
        if not reasons:
            self._publish_build_diagnostics(
                context,
                status="not_required",
                source=self.request.source,
                trigger_reasons=(),
                warnings=(),
                visual_review=None,
            )
            return self.request.source

        warning = ""
        outcome = None
        if len(self.request.validator_digests) != 1:
            warning = (
                "PDF visual equation-label review was not run: exactly one PDF "
                "validator is required; retaining web equation labels."
            )
        else:
            digest = self.request.validator_digests[0]
            repository = ArcPaperService(
                cache_root=self.execution.paper_cache_root
            ).repository
            try:
                pdf = repository.get(SourceFormat.PDF, digest)
                pdf_bytes = repository.read_bytes(pdf)
            except SourceRepositoryError as exc:
                warning = (
                    "PDF visual equation-label review was not run "
                    f"({exc.code}): {exc}; retaining web equation labels."
                )
            else:
                outcome = EquationLabelReviewService(
                    PdftoppmFullPageRenderer(),
                    llm=self.task_service,
                ).review(
                    context,
                    self.request.source,
                    pdf_digest=digest,
                    pdf_bytes=pdf_bytes,
                    model=self.recipe.model,
                    resume_input=resume_input,
                    options=self.llm_options,
                )
                if isinstance(outcome, LLMPaused):
                    return Paused(awaiting_from_pause(outcome))

        if outcome is not None and outcome.complete:
            source = apply_visual_equation_labels(
                self.request.source, outcome
            )
            context.artifacts.publish_json(
                _EFFECTIVE_SOURCE_ARTIFACT,
                rich_document_to_document(source),
            )
            self._publish_build_diagnostics(
                context,
                status="applied",
                source=source,
                trigger_reasons=reasons,
                warnings=outcome.warnings,
                visual_review=outcome.diagnostics_document,
            )
            return source

        warnings = (warning,) if warning else (
            tuple(outcome.warnings)
            if outcome is not None and outcome.warnings
            else (
                "PDF visual equation-label review did not produce a complete "
                "unambiguous mapping; retaining web equation labels.",
            )
        )
        self._publish_build_diagnostics(
            context,
            status="retained_web_labels",
            source=self.request.source,
            trigger_reasons=reasons,
            warnings=warnings,
            visual_review=(
                outcome.diagnostics_document
                if outcome is not None
                else None
            ),
        )
        return self.request.source

    def _publish_build_diagnostics(
        self,
        context: RunContext,
        *,
        status: str,
        source: RichDocument,
        trigger_reasons: tuple[str, ...],
        warnings: tuple[str, ...],
        visual_review: Mapping[str, Any] | None,
    ) -> None:
        document = {
            "schema_version": COMPANION_BUILD_DIAGNOSTICS_SCHEMA,
            "status": status,
            "source_document_digest": self.request.source.document_digest,
            "effective_document_digest": source.document_digest,
            "trigger_reasons": list(trigger_reasons),
            "warnings": list(warnings),
            "visual_review": (
                dict(visual_review)
                if visual_review is not None
                else None
            ),
        }
        validate_build_diagnostics(document)
        context.artifacts.publish_json(_DIAGNOSTICS_ARTIFACT, document)

    def _plans(
        self,
        context: RunContext,
        resume_input: Any,
        source: RichDocument,
        chapters: tuple[SourceChapter, ...],
        blocks: Mapping[str, Any],
        document_title: str,
        prior_companion: Mapping[str, Any] | None,
        *,
        inputs: tuple[LLMInputArtifact, ...],
    ) -> tuple[dict[str, Any], ...] | Paused | Failed:
        units = tuple(
            WorkUnit(
                chapter.chapter_id,
                {
                    "chapter_id": chapter.chapter_id,
                    "block_ids": list(chapter.block_ids),
                    "target_language": self.request.target_language,
                    "intent": self.request.effective_intent,
                    "content_contract": self.request.content_contract,
                    "prompt_contract": self.recipe.chapter_plan_prompt,
                    "prior_companion_digest": _optional_document_digest(
                        prior_companion
                    ),
                },
            )
            for chapter in chapters
        )
        by_id = {item.chapter_id: item for item in chapters}

        def worker(unit: WorkUnit):
            chapter = by_id[unit.unit_id]
            artifact_id = f"plans/{chapter.chapter_id}"
            existing = context.artifacts.find(artifact_id)
            if existing is not None:
                return read_json(context, existing, "chapter plan")
            request = LLMRequest(
                task_id("plan", unit.semantic_input),
                chapter_plan_prompt(
                    chapter_id=chapter.chapter_id,
                    title=chapter.title,
                    document_title=document_title,
                    document_outline=[
                        item.title for item in chapters
                    ],
                    block_ids=chapter.block_ids,
                    block_access=model_chapter_block_index(
                        source, chapter
                    ),
                    target_language=self.request.target_language,
                    intent=self.request.effective_intent,
                    has_prior_companion=prior_companion is not None,
                ),
                JsonOutput(CHAPTER_PLAN_SCHEMA, repair="format"),
                self.recipe.model,
                inputs=inputs,
            )
            outcome = execute_semantically_validated_task(
                self.task_service,
                context,
                request,
                candidate_id=f"chapters/{chapter.chapter_id}/plan.json",
                description=f"chapter plan {chapter.chapter_id}",
                validate=lambda raw: validate_chapter_plan(
                    raw,
                    chapter_id=chapter.chapter_id,
                    block_ids=chapter.block_ids,
                ),
                # Caller-owned routing identity is a deterministic repair, not
                # model-authored scientific content.
                normalize=lambda raw: {
                    **raw,
                    "chapter_id": chapter.chapter_id,
                },
                resume_input=resume_input,
                options=self.llm_options,
            )
            if isinstance(outcome, Paused):
                return outcome
            if isinstance(outcome, LLMFailed):
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=run_error_from_failure(outcome),
                )
            assert isinstance(outcome, SemanticTaskCompleted)
            value = outcome.value
            context.artifacts.publish_json(artifact_id, value)
            return value

        result = context.run_group(
            "chapter-plans",
            units,
            worker,
            max_workers=self.execution.workers,
            failure_mode=FailureMode.FAIL_FAST,
        )
        if isinstance(result, Paused):
            return result
        assert isinstance(result, GroupResult)
        failure = next(
            (item for item in result.units if item.status != "succeeded"),
            None,
        )
        if failure is not None:
            return Failed(
                failure.error
                or RunError("chapter_plan_failed", "chapter plan failed")
            )
        by_result = {
            item.unit_id: mapping(item.value, "chapter plan")
            for item in result.units
        }
        return tuple(by_result[item.chapter_id] for item in chapters)

    def _chapter_lanes(
        self,
        context: RunContext,
        resume_input: Any,
        chapters: tuple[SourceChapter, ...],
        plans: Sequence[Mapping[str, Any]],
        glossary: Mapping[str, Any],
        blocks: Mapping[str, Any],
        *,
        source: RichDocument,
        language: Mapping[str, Any],
        translation_required: bool,
        prior_companion: Mapping[str, Any] | None,
        model_inputs: tuple[LLMInputArtifact, ...],
    ) -> tuple[AcceptedChapter, ...] | Paused | Failed:
        by_chapter = {item.chapter_id: item for item in chapters}
        by_plan = {str(item["chapter_id"]): item for item in plans}
        entries = _glossary_entries(glossary)
        chapter_entries = {
            chapter.chapter_id: _literal_glossary_entries(
                entries,
                [
                    _source_block_document(source, blocks[block_id])
                    for block_id in chapter.block_ids
                ],
            )
            for chapter in chapters
        }
        language_identity = {
            key: language[key]
            for key in (
                "schema_version",
                "document_digest",
                "language_tag",
                "classification",
                "target_language",
                "mode",
            )
            if key in language
        }
        glossary_digest = hashlib.sha256(
            canonical_json_bytes(dict(glossary))
        ).hexdigest()
        completed_results: dict[str, Mapping[str, Any]] = {}
        if translation_required:
            translation_units = tuple(
                WorkUnit(
                    f"translation-{chapter.chapter_id}",
                    {
                        "chapter_id": chapter.chapter_id,
                        "block_ids": list(chapter.block_ids),
                        "lane": "translation",
                        "target_language": self.request.target_language,
                        "language": language_identity,
                        "glossary_digest": glossary_digest,
                        "content_contract": self.request.content_contract,
                        "prior_companion_digest": (
                            _optional_document_digest(prior_companion)
                        ),
                    },
                )
                for chapter in chapters
            )

            def translation_worker(unit: WorkUnit):
                chapter_id = unit.unit_id.removeprefix("translation-")
                chapter = by_chapter[chapter_id]
                outcome = self.translation_adapter.translate_blocks(
                    context,
                    source,
                    block_ids=chapter.block_ids,
                    language=language,
                    glossary=glossary,
                    target_language=self.request.target_language,
                    model=self.recipe.model,
                    execution=self.llm_options,
                    resume_input=resume_input,
                    artifact_prefix=f"chapters/{chapter_id}/translation",
                )
                if isinstance(outcome, Paused):
                    return outcome
                if isinstance(outcome, RunError):
                    return UnitResult(unit.unit_id, "failed", error=outcome)
                return _validated_translations(
                    outcome, block_ids=chapter.block_ids
                )

            translations = context.run_group(
                "chapter-translations-v2",
                translation_units,
                translation_worker,
                max_workers=self.execution.workers,
                failure_mode=FailureMode.FAIL_FAST,
            )
            if isinstance(translations, Paused):
                return translations
            assert isinstance(translations, GroupResult)
            translation_failure = next(
                (
                    item
                    for item in translations.units
                    if item.status != "succeeded"
                ),
                None,
            )
            if translation_failure is not None:
                return Failed(
                    translation_failure.error
                    or RunError(
                        "chapter_translation_failed",
                        "chapter translation failed",
                    )
                )
            completed_results.update(
                {
                    item.unit_id: mapping(
                        item.value, "chapter translation result"
                    )
                    for item in translations.units
                }
            )

        guide_loops: list[LoopSpec] = []
        guide_contexts: dict[str, Mapping[str, Any]] = {}
        existing_guide_batch = context.artifacts.find(
            "proposer-reviewer/batch/result"
        )
        replay_guide_batch = existing_guide_batch is not None
        for chapter in chapters:
            artifact_id = f"chapters/{chapter.chapter_id}/guide-accepted"
            existing = context.artifacts.find(artifact_id)
            if existing is not None and not replay_guide_batch:
                completed_results[f"guide-{chapter.chapter_id}"] = read_json(
                    context, existing, "accepted chapter guide"
                )
                continue
            plan = by_plan[chapter.chapter_id]
            if existing is not None:
                completed_results[f"guide-{chapter.chapter_id}"] = read_json(
                    context, existing, "accepted chapter guide"
                )
            guide_context = {
                "target_language": self.request.target_language,
                "language_result": language_identity,
                "plan": dict(plan),
                "block_ids": list(chapter.block_ids),
                "block_access": model_chapter_block_index(source, chapter),
                "glossary": list(chapter_entries[chapter.chapter_id]),
                "has_prior_companion": prior_companion is not None,
            }
            guide_contexts[chapter.chapter_id] = guide_context
            guide_loops.append(
                LoopSpec(
                    loop_id=chapter.chapter_id,
                    context=guide_context,
                    proposers=(
                        WorkerSpec(
                            "guide-proposer",
                            chapter_guide_proposer_instructions(),
                            CHAPTER_GUIDE_PROPOSAL_SCHEMA,
                            self.recipe.model,
                        ),
                    ),
                    reviewer=WorkerSpec(
                        "guide-reviewer",
                        chapter_guide_reviewer_instructions(),
                        CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA,
                        self.recipe.model,
                    ),
                    max_rounds=self.recipe.chapter_guide_max_rounds,
                    allow_early_stop=True,
                    on_proposer_failure=(
                        ProposerFailurePolicy.FAIL_LOOP
                    ),
                    review_final_round=(
                        self.recipe.chapter_guide_review_final_round
                    ),
                    revision_context_mode=(
                        RevisionContextMode.FULL_REVIEW_ENVELOPE
                    ),
                )
            )

        if guide_loops:
            guide_result_ref = existing_guide_batch
            if guide_result_ref is None:
                guide_outcome = ProposerReviewerService(
                    self.task_service
                ).execute(
                    context,
                    BatchRequest(
                        BATCH_SCHEMA_VERSION,
                        "companion-chapter-guides",
                        tuple(guide_loops),
                        BatchFailurePolicy.FAIL_FAST,
                        model_inputs,
                    ),
                    options=ProposerReviewerExecutionOptions(
                        max_concurrent_loops=self.execution.workers,
                        max_concurrent_workers=1,
                        llm=self.llm_options,
                    ),
                )
                if isinstance(guide_outcome, Paused):
                    return guide_outcome
                if isinstance(guide_outcome, Failed):
                    return guide_outcome
                assert isinstance(guide_outcome, Succeeded)
                guide_result_ref = guide_outcome.result_ref
            if guide_result_ref is None:
                return Failed(
                    RunError(
                        "chapter_guide_batch_invalid",
                        "proposer-reviewer returned no batch result",
                    )
                )
            guide_batch = decode_batch_result(
                read_json(
                    context,
                    guide_result_ref,
                    "chapter guide proposer-reviewer result",
                )
            )
            loop_results = {
                item.loop_id: item for item in guide_batch.loops
            }
            for chapter_id, guide_context in guide_contexts.items():
                loop_result = loop_results.get(chapter_id)
                if loop_result is None:
                    return Failed(
                        RunError(
                            "chapter_guide_batch_incomplete",
                            f"missing guide result for {chapter_id}",
                        )
                    )
                if loop_result.error is not None:
                    return Failed(loop_result.error)
                proposal = mapping(
                    loop_result.final_proposals.get("guide-proposer"),
                    f"final guide proposal for {chapter_id}",
                )
                candidate_id = f"chapters/{chapter_id}/guide-final.json"
                candidate_path = context.working.find_candidate(candidate_id)
                if candidate_path is None:
                    candidate = _normalize_chapter_reference_ids(
                        {
                            **proposal,
                            # Routing identity is deterministic caller data,
                            # not model-authored semantic content.
                            "chapter_id": chapter_id,
                        }
                    )
                    candidate_path = context.working.write_candidate_json(
                        candidate_id, candidate
                    )
                else:
                    stored_candidate = context.working.read_candidate_json(
                        candidate_id
                    )
                    candidate = _normalize_chapter_reference_ids(
                        {
                            **stored_candidate,
                            "chapter_id": chapter_id,
                        }
                    )
                    if candidate != stored_candidate:
                        candidate_path = (
                            context.working.write_candidate_json(
                                candidate_id, candidate
                            )
                        )
                try:
                    accepted_guide = validate_chapter_guide(
                        candidate,
                        plan=mapping(guide_context["plan"], "chapter plan"),
                    )
                    _verify_cached_reference_materials(
                        accepted_guide,
                        cache_root=self.execution.paper_cache_root,
                    )
                except CompanionContentError as exc:
                    return Failed(
                        RunError(
                            exc.code,
                            str(exc),
                            {
                                "candidate_path": str(candidate_path),
                                "chapter_id": chapter_id,
                            },
                        )
                    )
                artifact_id = f"chapters/{chapter_id}/guide-accepted"
                context.artifacts.publish_json(artifact_id, accepted_guide)
                completed_results[f"guide-{chapter_id}"] = accepted_guide

        joined = self._publish_completed_chapters(
            context,
            chapters,
            completed_results,
            blocks,
            source=source,
            translation_required=translation_required,
        )
        if isinstance(joined, Failed):
            return joined
        if len(joined) != len(chapters):
            return Failed(
                RunError(
                    "chapter_join_incomplete",
                    "completed chapter lanes did not cover every chapter",
                )
            )
        return joined

    def _publish_completed_chapters(
        self,
        context: RunContext,
        chapters: Sequence[SourceChapter],
        results: Mapping[str, Mapping[str, Any]],
        blocks: Mapping[str, Any],
        *,
        source: RichDocument,
        translation_required: bool,
    ) -> tuple[AcceptedChapter, ...] | Failed:
        """Publish every chapter whose independent lanes already succeeded."""

        accepted: list[AcceptedChapter] = []
        page_by_block = {
            item.block_id: item.page_number
            for item in source.page_map
        }
        for chapter in chapters:
            guide_key = f"guide-{chapter.chapter_id}"
            translation_key = f"translation-{chapter.chapter_id}"
            if guide_key not in results or (
                translation_required and translation_key not in results
            ):
                continue
            guide = results[guide_key]
            translations = (
                mapping_list(
                    results[translation_key]["translations"],
                    "translations",
                )
                if translation_required
                else []
            )
            chapter_value = AcceptedChapter(
                chapter_id=chapter.chapter_id,
                title=chapter.title,
                guide=None,
                source_anchors=tuple(
                    SourceAnchor.from_rich_block(
                        blocks[block_id],
                        page_number=page_by_block.get(block_id),
                        equation_label_provenance=equation_label_provenance(
                            source, block_id
                        ),
                    )
                    for block_id in chapter.block_ids
                ),
                translations=tuple(
                    TranslatedBlock(
                        block_id=str(item["block_id"]),
                        text=str(item["text"]),
                    )
                    for item in translations
                ),
                learning_units=tuple(
                    LearningUnit(
                        unit_id=str(item["unit_id"]),
                        title=str(item["title"]),
                        anchor_ids=tuple(item["anchor_block_ids"]),
                        placement=str(item["placement"]),
                        content_markdown=str(item["content_markdown"]),
                        citations=tuple(item["citations"]),
                    )
                    for item in mapping_list(
                        guide["learning_units"], "learning units"
                    )
                ),
            )
            accepted_id = f"chapters/{chapter.chapter_id}/accepted"
            existing = context.artifacts.find(accepted_id)
            if existing is None:
                context.artifacts.publish_json(
                    accepted_id, accepted_chapter_document(chapter_value)
                )
            else:
                frozen = read_json(
                    context, existing, "accepted chapter"
                )
                if frozen != accepted_chapter_document(chapter_value):
                    return Failed(
                        RunError(
                            "chapter_join_mismatch",
                            "deterministic chapter join changed on replay",
                        )
                    )
            accepted.append(chapter_value)
        return tuple(accepted)


def _normalize_chapter_reference_ids(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Assign stable publication IDs while preserving model-authored metadata."""

    result = dict(value)
    references = mapping_list(result.get("references"), "chapter references")
    local_to_published: dict[str, str] = {}
    normalized_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for reference in references:
        local_id = str(reference.get("reference_id") or "").strip()
        identity = {
            "title": str(reference.get("title") or "").strip(),
            "source": str(reference.get("source") or "").strip(),
            "dois": sorted(
                {
                    str(item).strip().casefold()
                    for item in reference.get("dois", [])
                    if isinstance(item, str) and item.strip()
                }
            ),
            "arxiv_ids": sorted(
                {
                    str(item).strip().casefold()
                    for item in reference.get("arxiv_ids", [])
                    if isinstance(item, str) and item.strip()
                }
            ),
        }
        published_id = (
            "reference-"
            + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:20]
        )
        local_to_published[local_id] = published_id
        normalized = {
            **reference,
            "reference_id": published_id,
            "dois": identity["dois"],
            "arxiv_ids": identity["arxiv_ids"],
        }
        existing = normalized_by_id.get(published_id)
        if existing is not None and existing != normalized:
            raise CompanionContentError(
                "chapter_reference_identity_collision",
                "references with the same publication identity disagree",
            )
        if existing is None:
            order.append(published_id)
            normalized_by_id[published_id] = normalized

    units = []
    for raw in mapping_list(result.get("learning_units"), "learning units"):
        unit = dict(raw)
        markdown = str(unit.get("content_markdown") or "")
        for local_id, published_id in local_to_published.items():
            markdown = markdown.replace(
                f"[@{local_id}]",
                f"[@{published_id}]",
            )
        unit["content_markdown"] = markdown
        units.append(unit)
    result["learning_units"] = units
    result["references"] = [
        normalized_by_id[reference_id] for reference_id in order
    ]
    return result


def _verify_cached_reference_materials(
    guide: Mapping[str, Any],
    *,
    cache_root: Path | None,
) -> None:
    cache = ReferenceMaterialCache(cache_root)
    for reference in mapping_list(
        guide.get("references"), "chapter references"
    ):
        raw = reference.get("cached_material")
        if raw is None:
            continue
        try:
            material = cached_reference_material_from_document(
                mapping(raw, "cached reference material")
            )
            for resource in material.resources:
                cache.read_resource(resource)
            identity = material.identity
            if identity.dois:
                resolved = cache.lookup(doi=identity.dois[0])
            elif identity.arxiv_id:
                resolved = cache.lookup(arxiv_id=identity.arxiv_id)
            elif identity.urls:
                resolved = cache.lookup(url=identity.urls[0])
            else:
                resolved = cache.lookup(title=identity.title)
        except (OSError, ReferenceCacheError, TypeError, ValueError) as exc:
            raise CompanionContentError(
                "chapter_reference_cache_invalid",
                "cached_material is not present and readable in the configured "
                f"shared cache: {exc}",
            ) from exc
        if (
            resolved is None
            or cached_reference_material_to_document(resolved)
            != cached_reference_material_to_document(material)
        ):
            raise CompanionContentError(
                "chapter_reference_cache_invalid",
                "cached_material does not match the material admitted to the "
                "configured shared cache",
            )


def _chapter_reference_contracts(
    context: RunContext,
    chapters: Sequence[SourceChapter],
    *,
    cited_ids: Sequence[str],
) -> tuple[EvidenceSource, ...]:
    by_id: dict[str, EvidenceSource] = {}
    for chapter in chapters:
        ref = context.artifacts.find(
            f"chapters/{chapter.chapter_id}/guide-accepted"
        )
        if ref is None:
            raise CompanionContentError(
                "chapter_reference_missing",
                f"accepted guide is missing for {chapter.chapter_id}",
            )
        guide = read_json(context, ref, "accepted chapter guide")
        for item in mapping_list(guide.get("references"), "chapter references"):
            value = EvidenceSource(
                evidence_id=str(item["reference_id"]),
                title=str(item["title"]),
                source=str(item["source"]),
                dois=tuple(str(value) for value in item["dois"]),
                arxiv_ids=tuple(
                    str(value) for value in item["arxiv_ids"]
                ),
                cached_document=(
                    mapping(
                        item["cached_document"],
                        "cached document reference",
                    )
                    if item["cached_document"] is not None
                    else None
                ),
                cached_material=(
                    mapping(
                        item["cached_material"],
                        "cached reference material",
                    )
                    if item["cached_material"] is not None
                    else None
                ),
            )
            existing = by_id.get(value.evidence_id)
            if existing is not None and existing != value:
                raise CompanionContentError(
                    "chapter_reference_identity_collision",
                    "chapters disagree about shared reference metadata",
                )
            by_id[value.evidence_id] = value
    missing = [reference_id for reference_id in cited_ids if reference_id not in by_id]
    if missing:
        raise CompanionContentError(
            "chapter_reference_missing",
            f"cited chapter references are missing metadata: {missing}",
        )
    return tuple(by_id[reference_id] for reference_id in cited_ids)


def _validated_translations(
    value: Mapping[str, Any], *, block_ids: Sequence[str]
) -> dict[str, Any]:
    result = mapping(value, "translation result")
    translations = mapping_list(
        result.get("translations"), "translations"
    )
    if [item.get("block_id") for item in translations] != list(block_ids):
        raise CompanionContentError(
            "translation_coverage_invalid",
            "arc-translate did not exactly cover the chapter source order",
        )
    if any(
        set(item) != {"block_id", "text"}
        or not isinstance(item.get("text"), str)
        or not item["text"].strip()
        for item in translations
    ):
        raise CompanionContentError(
            "translation_coverage_invalid",
            "arc-translate returned invalid translated blocks",
        )
    return {"translations": translations}


def _empty_glossary(source: Any) -> dict[str, Any]:
    return {
        "schema_version": "arc.translate.glossary_result.v1",
        "document_digest": source.document_digest,
        "source_digest": source.source.artifact_digest,
        "target_language": "",
        "approx_count": 0,
        "entries": [],
    }


def _glossary_entries(
    glossary: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return mapping_list(glossary.get("entries"), "glossary entries")


def _literal_glossary_entries(
    entries: Sequence[Mapping[str, Any]],
    source_blocks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    text = "\n".join(
        _literal_strings(block.get("payload")) for block in source_blocks
    )
    return tuple(
        dict(entry)
        for entry in entries
        if isinstance(entry.get("term"), str)
        and str(entry["term"]).casefold() in text.casefold()
    )


def _literal_strings(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_literal_strings(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return "\n".join(_literal_strings(item) for item in value)
    return ""


def _glossary_contracts(
    glossary: Mapping[str, Any], source: Any
) -> tuple[GlossaryEntry, ...]:
    block_documents = {
        block.block_id: _source_block_document(source, block)
        for block in source.blocks
    }
    values = []
    for item in _glossary_entries(glossary):
        term = str(item["term"]).strip()
        source_refs = {
            str(value)
            for value in item.get("source_refs", [])
            if isinstance(value, str)
        }
        anchors = tuple(
            block_id
            for block_id, block in block_documents.items()
            if block_id in source_refs
            or term.casefold()
            in _literal_strings(block.get("payload")).casefold()
        )
        if not anchors:
            continue
        values.append(
            GlossaryEntry(
                entry_id=str(item["term_id"]),
                term=term,
                translated_term=str(
                    item.get("preferred_translation") or ""
                ),
                definition=str(item["target_definition"]),
                anchor_ids=anchors,
                citations=(),
            )
        )
    return tuple(values)


def _source_block_document(source: Any, block: Any) -> dict[str, Any]:
    return block_prompt_document(
        block,
        equation_label_provenance=equation_label_provenance(source, block.block_id),
    )


def _prior_companion_reference(
    context: RunContext,
) -> dict[str, Any] | None:
    """Expose a reused Companion as optional model context, never as state."""

    ref = context.artifacts.find(_PRIOR_COMPANION_ARTIFACT)
    if ref is None:
        return None
    try:
        book = CompanionContentCodec.loads(context.artifacts.read_bytes(ref))
    except (OSError, TypeError, ValueError) as exc:
        raise CompanionContentError(
            "prior_companion_invalid",
            "The staged prior Companion reference is invalid.",
        ) from exc
    return {
        "schema_version": "arc.companion.prior_reference.v1",
        "title": book.title,
        "authors": list(book.authors),
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "guide": chapter.guide,
                "learning_units": [
                    {
                        "unit_id": unit.unit_id,
                        "title": unit.title,
                        "anchor_block_ids": list(unit.anchor_ids),
                        "placement": unit.placement,
                        "content_markdown": unit.content_markdown,
                        "citations": list(unit.citations),
                    }
                    for unit in chapter.learning_units
                ],
            }
            for chapter in book.chapters
        ],
        "bibliography": [
            {
                "evidence_id": item.evidence_id,
                "title": item.title,
                "source": item.source,
                "dois": list(item.dois),
                "arxiv_ids": list(item.arxiv_ids),
                "cached_document": (
                    dict(item.cached_document)
                    if item.cached_document is not None
                    else None
                ),
                "cached_material": (
                    dict(item.cached_material)
                    if item.cached_material is not None
                    else None
                ),
            }
            for item in book.bibliography
        ],
    }


def _prior_reference_input(
    context: RunContext,
    prior_companion: Mapping[str, Any] | None,
) -> LLMInputArtifact | None:
    if prior_companion is None:
        return None
    ref = context.artifacts.find(_PRIOR_REFERENCE_ARTIFACT)
    if ref is None:
        ref = context.artifacts.publish_json(
            _PRIOR_REFERENCE_ARTIFACT, dict(prior_companion)
        )
    elif read_json(context, ref, "prior Companion reference") != dict(
        prior_companion
    ):
        raise CompanionContentError(
            "prior_companion_reference_mismatch",
            "Frozen prior Companion reference differs from staged reuse input.",
        )
    return _llm_input(context, "prior-companion", ref)


def _artifact_input(
    context: RunContext,
    input_id: str,
    artifact_id: str,
) -> LLMInputArtifact:
    ref = context.artifacts.find(artifact_id)
    if ref is None:
        raise CompanionContentError(
            "model_input_missing",
            f"Required model input artifact is missing: {artifact_id}.",
        )
    return _llm_input(context, input_id, ref)


def _llm_input(
    context: RunContext,
    input_id: str,
    ref: ArtifactRef,
) -> LLMInputArtifact:
    return LLMInputArtifact(
        input_id,
        ArtifactSourceRef(context.run_id, ref.artifact_id, ref.digest),
        ref.media_type,
    )


def _companion_llm_options(
    execution: CompanionExecutionOptions,
):
    """Expose the exact paper cache and installed CLI to direct workers."""

    values = dict(execution.llm.runtime_environment.values)
    cache_root = ArcPaperService(
        cache_root=execution.paper_cache_root
    ).cache_root
    values["ARC_PAPER_CACHE"] = str(cache_root)
    path_value = values.get("PATH") or ""
    command = shutil.which("arc-paper", path=path_value)
    if command is None:
        executable_name = (
            "arc-paper.exe" if os.name == "nt" else "arc-paper"
        )
        candidate = Path(sys.executable).resolve().parent / executable_name
        if candidate.is_file():
            command = str(candidate)
    if command is not None:
        command_dir = str(Path(command).resolve().parent)
        path_parts = path_value.split(os.pathsep) if path_value else []
        if command_dir not in path_parts:
            values["PATH"] = os.pathsep.join(
                [command_dir, *path_parts]
            )
    return replace(
        execution.llm,
        runtime_environment=ArcRuntimeEnvironment(values),
    )


def _optional_document_digest(
    value: Mapping[str, Any] | None,
) -> str | None:
    return (
        hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()
        if value is not None
        else None
    )


def validate_build_diagnostics(value: Mapping[str, Any]) -> None:
    fields = {
        "schema_version",
        "status",
        "source_document_digest",
        "effective_document_digest",
        "trigger_reasons",
        "warnings",
        "visual_review",
    }
    if set(value) != fields:
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Build diagnostics contain invalid fields.",
        )
    if value["schema_version"] != COMPANION_BUILD_DIAGNOSTICS_SCHEMA:
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Build diagnostics use an unsupported schema.",
        )
    if value["status"] not in {
        "not_required",
        "applied",
        "retained_web_labels",
    }:
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Build diagnostics contain an invalid status.",
        )
    for key in ("source_document_digest", "effective_document_digest"):
        digest = value[key]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CompanionContentError(
                "build_diagnostics_invalid",
                "Build diagnostics contain an invalid document digest.",
            )
    for key in ("trigger_reasons", "warnings"):
        items = value[key]
        if (
            not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
        ):
            raise CompanionContentError(
                "build_diagnostics_invalid",
                f"Build diagnostics contain invalid {key}.",
            )
    if value["visual_review"] is not None and not isinstance(
        value["visual_review"], Mapping
    ):
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Build diagnostics contain an invalid visual review.",
        )
    status = value["status"]
    reasons = value["trigger_reasons"]
    warnings = value["warnings"]
    visual_review = value["visual_review"]
    if status == "not_required" and (
        reasons
        or warnings
        or visual_review is not None
        or value["effective_document_digest"]
        != value["source_document_digest"]
    ):
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Not-required build diagnostics contain review state.",
        )
    if status == "retained_web_labels" and (
        not reasons
        or not warnings
        or value["effective_document_digest"]
        != value["source_document_digest"]
    ):
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Retained-label build diagnostics are incomplete.",
        )
    if status == "applied" and (
        not reasons or visual_review is None
    ):
        raise CompanionContentError(
            "build_diagnostics_invalid",
            "Applied build diagnostics lack visual evidence.",
        )


__all__ = [
    "COMPANION_BUILD_DIAGNOSTICS_SCHEMA",
    "COMPANION_BUILD_HANDLER",
    "COMPATIBLE_COMPANION_BUILD_HANDLERS",
    "CompanionBuildHandler",
    "validate_build_diagnostics",
]
