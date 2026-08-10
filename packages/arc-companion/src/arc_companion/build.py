"""Current split translation and guide Companion build handler."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
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
    CachedDocumentStructureRef,
    DocumentStructureCache,
    EquationLabelReviewService,
    PdftoppmFullPageRenderer,
    RichDocument,
    ReferenceMaterialCache,
    ReferenceCacheError,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepositoryError,
    apply_visual_equation_labels,
    detect_suspicious_equation_labels,
    cached_document_ref_to_document,
    cached_document_structure_ref_to_document,
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
    mapping,
    mapping_list,
    read_json,
    task_id,
)
from .generation_validation import (
    CompanionContentError,
    validate_author_identity,
    validate_chapter_guide,
    validate_chapter_guide_review_audit,
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
    model_block_access_index,
    model_chapter_block_index,
    model_source_index,
    model_source_view,
    model_translation_index,
    model_translation_view,
    validate_model_source_index,
    validate_model_translation_index,
)
from .prompts import (
    AUTHOR_IDENTITY_PROMPT_VERSION,
    AUTHOR_IDENTITY_SCHEMA,
    CHAPTER_GUIDE_PROPOSAL_SCHEMA,
    CHAPTER_GUIDE_REVIEW_AUDIT_SCHEMA,
    author_identity_prompt,
    chapter_guide_proposer_instructions,
    chapter_guide_reviewer_instructions,
)
from .reader_labels import ReaderLabelError, resolve_reader_labels
from .rich_text import RichTextError
from .publication import (
    CompanionPublicationError,
    build_result_document,
    publish_companion,
)
from .request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_handler_semantic_input,
    normalize_handler_semantic_input,
)
from .source_planning import (
    SourceChapter,
    block_prompt_document,
    equation_label_provenance,
    plan_source_chapters,
    plan_structured_source_chapters,
)
from .source_identity import resolve_document_identity
from .translation_adapter import (
    ArcTranslateAdapter,
    CompanionTranslationAdapter,
)
from .translation_results import (
    CompanionTranslationResultError,
    load_translation_selection,
)
COMPANION_BUILD_HANDLER = "arc.companion.build.v14"
COMPANION_BUILD_DIAGNOSTICS_SCHEMA = "arc.companion.build_diagnostics.v1"
_DIAGNOSTICS_ARTIFACT = "diagnostics/build"
_EFFECTIVE_SOURCE_ARTIFACT = "source/effective"
_MODEL_SOURCE_VIEW_ARTIFACT = "source/model-view"
_MODEL_SOURCE_INDEX_ARTIFACT = "source/model-index"
_MODEL_TRANSLATION_ROOT = "translation/chapters"
_ORIGINAL_SOURCE_ARTIFACT = "source/original"
_AUTHOR_IDENTITY_ARTIFACT = "identity/authors"
_RESULT_ARTIFACT = "result"
_TRANSLATION_LANE_CONTRACT = "arc.companion.translation_lane.v1"


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
        try:
            durable_semantic_input = normalize_handler_semantic_input(
                context.semantic_input
            )
        except (TypeError, ValueError):
            durable_semantic_input = None
        if durable_semantic_input != self.semantic_input():
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
            if self.request.structure_ref is None:
                chapters = plan_source_chapters(source)
            else:
                paper = ArcPaperService(
                    cache_root=self.execution.paper_cache_root
                )
                overlay = DocumentStructureCache(paper.cache_root).read(
                    self.request.structure_ref
                )
                chapters = plan_structured_source_chapters(
                    source,
                    overlay,
                    companion_section_ids=(
                        self.request.companion_section_ids
                    ),
                )
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
            document_inputs = model_inputs

            glossary: dict[str, Any]
            if translation_required:
                glossary_options: dict[str, Any] = {}
                if self.request.structure_ref is not None:
                    glossary_options.update(
                        structure_ref=self.request.structure_ref,
                        section_ids=self.request.companion_section_ids,
                    )
                glossary_outcome = self.translation_adapter.build_glossary(
                    context,
                    source,
                    language=language,
                    target_language=self.request.target_language,
                    approx_count=self.recipe.approx_term_count,
                    model=self.recipe.model,
                    execution=self.llm_options,
                    resume_input=resume_input,
                    **glossary_options,
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
                glossary,
                blocks,
                source=source,
                language=language,
                translation_required=translation_required,
                model_inputs=document_inputs,
            )
            if isinstance(chapters_outcome, (Paused, Failed)):
                return chapters_outcome

            glossary_contracts = (
                _glossary_contracts(glossary, source)
                if translation_required
                else ()
            )
            cited_ids = _first_visible_citation_ids(chapters_outcome)
            bibliography = _chapter_reference_contracts(
                context,
                chapters,
                cited_ids=cited_ids,
            )
            published = publish_companion(
                context,
                source=source,
                title=title,
                authors=authors,
                source_language=str(language["language_tag"]),
                target_language=self.request.target_language,
                translation_mode=(
                    "enabled" if translation_required else "skipped"
                ),
                reader_labels=reader_labels,
                chapters=chapters_outcome,
                glossary=glossary_contracts,
                bibliography=bibliography,
                reviewed_supplements=self.request.reviewed_supplements,
                paper_cache_root=self.execution.paper_cache_root,
            )
            result_ref = context.artifacts.publish_json(
                _RESULT_ARTIFACT, build_result_document(published)
            )
            return Succeeded(result_ref)
        except CompanionContentError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except CompanionPublicationError as exc:
            return Failed(RunError("companion_publication_invalid", str(exc)))
        except CachedDocumentError as exc:
            return Failed(
                RunError(
                    getattr(exc, "code", "document_structure_invalid"),
                    str(exc),
                )
            )
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

    def _model_translation_inputs(
        self,
        context: RunContext,
        source: RichDocument,
        chapters: Sequence[SourceChapter],
        translations: Mapping[str, Mapping[str, Any]],
    ) -> tuple[
        tuple[LLMInputArtifact, ...],
        Mapping[str, Mapping[str, Any]],
    ]:
        """Freeze one independently reusable accepted view per chapter."""

        by_chapter = {
            chapter.chapter_id: load_translation_selection(
                context,
                translations[f"translation-{chapter.chapter_id}"],
                source=source,
                block_ids=chapter.block_ids,
                target_language=self.request.target_language,
            ).view_records
            for chapter in chapters
        }
        paper = ArcPaperService(
            cache_root=self.execution.paper_cache_root
        )
        inputs: list[LLMInputArtifact] = []
        indexes: dict[str, Mapping[str, Any]] = {}
        for chapter in chapters:
            chapter_set = (chapter,)
            view, access = model_translation_view(
                chapter_set,
                {chapter.chapter_id: by_chapter[chapter.chapter_id]},
            )
            payload = view.encode("utf-8")
            artifact_root = (
                f"{_MODEL_TRANSLATION_ROOT}/{chapter.chapter_id}"
            )
            view_artifact = f"{artifact_root}/model-view"
            view_ref = context.artifacts.find(view_artifact)
            if view_ref is None:
                view_ref = context.artifacts.publish_bytes(
                    view_artifact,
                    payload,
                    media_type="text/markdown",
                )
            elif context.artifacts.read_bytes(view_ref) != payload:
                raise CompanionContentError(
                    "model_translation_view_mismatch",
                    "Frozen chapter translation differs from completed translations.",
                )
            cached_source = paper.repository.store_bytes(
                payload,
                source_format=SourceFormat.MARKDOWN,
                origin=SourceOrigin(
                    SourceOriginKind.REPOSITORY,
                    locator=(
                        f"arc-companion:{context.run_id}:{view_artifact}"
                    ),
                ),
            )
            cached_document = cached_document_ref_to_document(
                paper.cache_document(cached_source)
            )
            index = model_translation_index(
                view=view,
                chapters=chapter_set,
                access_by_chapter=access,
                source_document_sha256=source.document_digest,
                target_language=self.request.target_language,
                cached_document=cached_document,
            )
            index_artifact = f"{artifact_root}/model-index"
            index_ref = context.artifacts.find(index_artifact)
            if index_ref is None:
                index_ref = context.artifacts.publish_json(
                    index_artifact, index
                )
            else:
                frozen = read_json(
                    context, index_ref, "chapter model translation index"
                )
                validate_model_translation_index(
                    frozen,
                    view=view,
                    chapters=chapter_set,
                    source_document_sha256=source.document_digest,
                    target_language=self.request.target_language,
                )
                if frozen != index:
                    raise CompanionContentError(
                        "model_translation_index_mismatch",
                        "Frozen chapter translation index differs from current cache identity.",
                    )
            inputs.append(
                _llm_input(
                    context,
                    _chapter_translation_input_id(chapter.chapter_id),
                    index_ref,
                )
            )
            indexes[chapter.chapter_id] = index
        return tuple(inputs), indexes

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
        author_block_ids = tuple(
            item.block_id for item in source.blocks[:64]
        )
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
                block_access=model_block_access_index(
                    source, author_block_ids
                ),
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

    def _chapter_lanes(
        self,
        context: RunContext,
        resume_input: Any,
        chapters: tuple[SourceChapter, ...],
        glossary: Mapping[str, Any],
        blocks: Mapping[str, Any],
        *,
        source: RichDocument,
        language: Mapping[str, Any],
        translation_required: bool,
        model_inputs: tuple[LLMInputArtifact, ...],
    ) -> tuple[dict[str, Any], ...] | Paused | Failed:
        by_chapter = {item.chapter_id: item for item in chapters}
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
        guide_model_inputs = model_inputs
        translation_indexes: Mapping[
            str, Mapping[str, Any]
        ] | None = None
        if translation_required:
            translation_units = tuple(
                WorkUnit(
                    f"translation-{chapter.chapter_id}",
                    {
                        "chapter_id": chapter.chapter_id,
                        "block_ids": list(chapter.block_ids),
                        "lane": "translation",
                        "translation_lane_contract": (
                            _TRANSLATION_LANE_CONTRACT
                        ),
                        "target_language": self.request.target_language,
                        "language": language_identity,
                        "glossary_digest": glossary_digest,
                        "content_contract": self.request.content_contract,
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
                try:
                    return load_translation_selection(
                        context,
                        outcome,
                        source=source,
                        block_ids=chapter.block_ids,
                        target_language=self.request.target_language,
                    ).result.to_document()
                except CompanionTranslationResultError as exc:
                    return UnitResult(
                        unit.unit_id,
                        "failed",
                        error=RunError(
                            "translation_result_invalid", str(exc)
                        ),
                    )

            translations = context.run_group(
                "chapter-translations-v3",
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
            (
                translation_inputs,
                translation_indexes,
            ) = self._model_translation_inputs(
                context,
                source,
                chapters,
                completed_results,
            )
            guide_model_inputs = (*model_inputs, *translation_inputs)

        guide_loops: list[LoopSpec] = []
        guide_contexts: dict[str, Mapping[str, Any]] = {}
        existing_guide_batch = context.artifacts.find(
            "proposer-reviewer/batch/result"
        )
        replay_guide_batch = existing_guide_batch is not None
        for chapter in chapters:
            artifact_id = f"chapters/{chapter.chapter_id}/guide-accepted"
            existing = (
                None
                if replay_guide_batch and chapter.generate_guide
                else context.artifacts.find(artifact_id)
            )
            if existing is not None:
                completed_results[f"guide-{chapter.chapter_id}"] = read_json(
                    context, existing, "accepted chapter guide"
                )
                continue
            if not chapter.generate_guide:
                program_candidate = self._augment_chapter_candidate(
                    chapter,
                    {
                        "chapter_guide": None,
                        "section_guides": [],
                        "companions": [],
                        "references": [],
                    },
                )
                empty_guide = validate_chapter_guide(
                    program_candidate,
                    chapter_id=chapter.chapter_id,
                    block_ids=chapter.block_ids,
                    chapter_anchor_block_id=(
                        chapter.display_anchor_block_id
                    ),
                    section_block_ids=chapter.section_block_ids,
                )
                empty_guide = _attach_cached_reference_materials(
                    empty_guide,
                    cache_root=self.execution.paper_cache_root,
                )
                _verify_cached_reference_materials(
                    empty_guide,
                    cache_root=self.execution.paper_cache_root,
                )
                if existing is None:
                    context.artifacts.publish_json(artifact_id, empty_guide)
                elif read_json(
                    context, existing, "accepted structural guide"
                ) != empty_guide:
                    return Failed(
                        RunError(
                            "chapter_guide_replay_mismatch",
                            "structural chapter guide changed on replay",
                        )
                    )
                completed_results[f"guide-{chapter.chapter_id}"] = empty_guide
                continue
            if existing is not None:
                completed_results[f"guide-{chapter.chapter_id}"] = read_json(
                    context, existing, "accepted chapter guide"
                )
            guide_context = self._chapter_model_context(
                context,
                source,
                chapter,
                language_identity=language_identity,
                glossary=chapter_entries[chapter.chapter_id],
                translation_index=(
                    None
                    if translation_indexes is None
                    else translation_indexes[chapter.chapter_id]
                ),
            )
            guide_contexts[chapter.chapter_id] = guide_context
            guide_loops.append(
                LoopSpec(
                    loop_id=chapter.chapter_id,
                    context=guide_context,
                    proposers=(
                        WorkerSpec(
                            "guide-proposer",
                            chapter_guide_proposer_instructions(
                                self.recipe.chapter_guide_prompt
                            ),
                            CHAPTER_GUIDE_PROPOSAL_SCHEMA,
                            self.recipe.model,
                        ),
                    ),
                    reviewer=WorkerSpec(
                        "guide-reviewer",
                        chapter_guide_reviewer_instructions(
                            self.recipe.chapter_guide_review_prompt
                        ),
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
                    input_ids=tuple(
                        item.input_id for item in model_inputs
                    ) + (
                        ()
                        if translation_indexes is None
                        else (
                            _chapter_translation_input_id(
                                chapter.chapter_id
                            ),
                        )
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
                        guide_model_inputs,
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
                chapter = by_chapter[chapter_id]
                program_candidate = self._augment_chapter_candidate(
                    chapter, proposal
                )
                proposal_companions = mapping_list(
                    proposal.get("companions"),
                    f"final guide companions for {chapter_id}",
                )
                proposal_sections = mapping_list(
                    proposal.get("section_guides"),
                    f"final guide section guides for {chapter_id}",
                )
                program_companions = tuple(
                    item
                    for item in mapping_list(
                        program_candidate.get("companions"),
                        f"augmented guide companions for {chapter_id}",
                    )
                    if item not in proposal_companions
                )
                program_sections = tuple(
                    item
                    for item in mapping_list(
                        program_candidate.get("section_guides"),
                        f"augmented guide section guides for {chapter_id}",
                    )
                    if item not in proposal_sections
                )
                candidate_id = f"chapters/{chapter_id}/guide-final.json"
                candidate_path = context.working.find_candidate(candidate_id)
                if candidate_path is None:
                    candidate = program_candidate
                    candidate_path = context.working.write_candidate_json(
                        candidate_id, candidate
                    )
                else:
                    stored_candidate = context.working.read_candidate_json(
                        candidate_id
                    )
                    candidate = self._augment_chapter_candidate(
                        chapter, stored_candidate
                    )
                    if candidate != stored_candidate:
                        candidate_path = (
                            context.working.write_candidate_json(
                                candidate_id, candidate
                            )
                        )
                try:
                    validate_chapter_guide_review_audit(
                        loop_result.final_review,
                        proposal=candidate,
                        part_count=len(chapter.block_ids),
                        section_count=len(chapter.section_block_ids),
                        program_companions=program_companions,
                        program_section_guides=program_sections,
                    )
                    accepted_guide = validate_chapter_guide(
                        candidate,
                        chapter_id=chapter_id,
                        block_ids=chapter.block_ids,
                        chapter_anchor_block_id=(
                            chapter.display_anchor_block_id
                        ),
                        section_block_ids=chapter.section_block_ids,
                    )
                    accepted_guide = _attach_cached_reference_materials(
                        accepted_guide,
                        cache_root=self.execution.paper_cache_root,
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
            rebuild=replay_guide_batch,
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

    def _augment_chapter_candidate(
        self,
        chapter: SourceChapter,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Task-local hook for verified deterministic proposal additions."""

        return dict(candidate)

    def _chapter_model_context(
        self,
        context: RunContext,
        source: RichDocument,
        chapter: SourceChapter,
        *,
        language_identity: Mapping[str, Any],
        glossary: Sequence[Mapping[str, Any]],
        translation_index: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        access = model_chapter_block_index(source, chapter)
        sections = [
            {
                "section_number": index,
                "title": title,
                "part_number": chapter.block_ids.index(block_id) + 1,
            }
            for index, (block_id, title) in enumerate(
                zip(
                    chapter.section_block_ids,
                    chapter.section_titles,
                    strict=True,
                ),
                1,
            )
        ]
        parts = [
            {
                "part_number": index,
                "kind": item["kind"],
                "line_start": item["line_start"],
                "line_end": item["line_end"],
                "selector": item["selector"],
                "equation_label": item["equation_label"],
            }
            for index, item in enumerate(access, 1)
        ]
        arc_commands = _chapter_arc_commands(
            context,
            chapter,
            access,
            cache_root=self.execution.paper_cache_root,
            structure_ref=self.request.structure_ref,
        )
        arc_commands["translation"] = _chapter_translation_commands(
            chapter,
            translation_index,
            cache_root=self.execution.paper_cache_root,
        )
        return {
            "target_language": self.request.target_language,
            "language_result": dict(language_identity),
            "intent": self.request.effective_intent,
            "chapter": {
                "title": chapter.title,
                "sections": sections,
                "parts": parts,
            },
            "arc_commands": arc_commands,
            "glossary": list(glossary),
        }

    def _publish_completed_chapters(
        self,
        context: RunContext,
        chapters: Sequence[SourceChapter],
        results: Mapping[str, Mapping[str, Any]],
        blocks: Mapping[str, Any],
        *,
        source: RichDocument,
        translation_required: bool,
        rebuild: bool = False,
    ) -> tuple[dict[str, Any], ...] | Failed:
        """Publish every chapter whose independent lanes already succeeded."""

        accepted: list[dict[str, Any]] = []
        for chapter in chapters:
            guide_key = f"guide-{chapter.chapter_id}"
            translation_key = f"translation-{chapter.chapter_id}"
            if guide_key not in results or (
                translation_required and translation_key not in results
            ):
                continue
            guide = results[guide_key]
            translation_result = (
                dict(
                    mapping(
                        results[translation_key],
                        "translation result",
                    )
                )
                if translation_required
                else None
            )
            chapter_value = {
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "block_ids": list(chapter.block_ids),
                "display_anchor_block_id": (
                    chapter.display_anchor_block_id
                ),
                "section_block_ids": list(chapter.section_block_ids),
                "section_titles": list(chapter.section_titles),
                "section_levels": list(chapter.section_levels),
                "translation_result": translation_result,
                "learning_units": mapping_list(
                    guide["learning_units"], "learning units"
                ),
            }
            accepted_id = f"chapters/{chapter.chapter_id}/accepted"
            existing = (
                None
                if rebuild
                else context.artifacts.find(accepted_id)
            )
            if existing is None:
                context.artifacts.publish_json(accepted_id, chapter_value)
            else:
                frozen = read_json(
                    context, existing, "accepted chapter"
                )
                if frozen != chapter_value:
                    return Failed(
                        RunError(
                            "chapter_join_mismatch",
                            "deterministic chapter join changed on replay",
                        )
                    )
            accepted.append(chapter_value)
        return tuple(accepted)


def _chapter_arc_commands(
    context: RunContext,
    chapter: SourceChapter,
    access: Sequence[Mapping[str, Any]],
    *,
    cache_root: Path | None,
    structure_ref: CachedDocumentStructureRef | None,
) -> dict[str, Any]:
    """Return filled, executable source commands plus research syntax."""

    index_ref = context.artifacts.find(_MODEL_SOURCE_INDEX_ARTIFACT)
    if index_ref is None:
        raise CompanionContentError(
            "model_source_index_missing",
            "chapter commands require the frozen model source index",
        )
    index = read_json(context, index_ref, "model source index")
    cached = index.get("cached_document")
    if not isinstance(cached, Mapping):
        return {
            "availability": "fallback_only",
            "instructions": (
                "Use the verified text-only companion-source workspace input; "
                "no exact cached source command is available."
            ),
            "source": [],
            "full_document_search_examples": {
                "availability": "fallback_only",
                "instructions": (
                    "Search the attached verified text-only source with the "
                    "host's ordinary text search."
                ),
                "single_term": None,
                "alternative_terms": [],
            },
            "research_examples": _research_command_examples(),
        }
    paper = ArcPaperService(cache_root=cache_root)
    document_json = json.dumps(
        dict(cached),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    structure_json = (
        json.dumps(
            cached_document_structure_ref_to_document(structure_ref),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if structure_ref is not None
        else None
    )

    def source_range(start: int, end: int) -> list[str]:
        return [
            "arc-paper",
            "read-cached-source-range",
            "--document-ref",
            document_json,
            "--cache-root",
            str(paper.cache_root),
            "--text-only",
            str(start),
            str(end),
        ]

    source_commands: list[dict[str, Any]] = []
    for part_number, item in enumerate(access, 1):
        start = item.get("line_start")
        end = item.get("line_end")
        if isinstance(start, int) and isinstance(end, int):
            argv = source_range(start, end)
            source_commands.append(
                _command(
                    f"part-{part_number}",
                    argv,
                    part_numbers=[part_number],
                )
            )
    section_starts = [
        chapter.block_ids.index(block_id)
        for block_id in chapter.section_block_ids
    ]
    for section_index, start_index in enumerate(section_starts, 1):
        end_index = (
            section_starts[section_index]
            if section_index < len(section_starts)
            else len(access)
        )
        section_access = access[start_index:end_index]
        section_lines = [
            (item.get("line_start"), item.get("line_end"))
            for item in section_access
            if isinstance(item.get("line_start"), int)
            and isinstance(item.get("line_end"), int)
        ]
        if section_lines:
            source_commands.append(
                _command(
                    f"section-{section_index}-complete",
                    source_range(
                        min(item[0] for item in section_lines),
                        max(item[1] for item in section_lines),
                    ),
                    part_numbers=list(
                        range(start_index + 1, end_index + 1)
                    ),
                )
            )
    lines = [
        (item.get("line_start"), item.get("line_end"))
        for item in access
        if isinstance(item.get("line_start"), int)
        and isinstance(item.get("line_end"), int)
    ]
    if lines:
        source_commands.append(
            _command(
                "complete-current-chapter",
                source_range(
                    min(item[0] for item in lines),
                    max(item[1] for item in lines),
                ),
                part_numbers=list(range(1, len(access) + 1)),
            )
        )
    toc_argv = [
        "arc-paper",
        "get-cached-table-of-contents",
        "--document-ref",
        document_json,
        "--cache-root",
        str(paper.cache_root),
    ]
    if structure_json is not None:
        toc_argv.extend(["--structure-ref", structure_json])
    source_commands.extend(
        (
            _command(
                "table-of-contents",
                toc_argv,
            ),
            _command(
                "search-current-title",
                [
                    "arc-paper",
                    "search-cached-document",
                    "--document-ref",
                    document_json,
                    "--cache-root",
                    str(paper.cache_root),
                    chapter.title,
                ],
            ),
        )
    )

    def search_example(command_id: str, query: str) -> dict[str, Any]:
        return _command(
            command_id,
            [
                "arc-paper",
                "search-cached-document",
                "--document-ref",
                document_json,
                "--cache-root",
                str(paper.cache_root),
                query,
            ],
        )

    return {
        "availability": "exact",
        "instructions": (
            "Run these commands directly. Prefer exact parts; use the complete "
            "chapter when more context is needed."
        ),
        "source": source_commands,
        "full_document_search_examples": {
            "availability": "exact",
            "instructions": (
                "Replace only the final query argument. Searches are literal. "
                "For A or B, run both alternative commands."
            ),
            "single_term": search_example(
                "search-full-document-single-term",
                "<term>",
            ),
            "alternative_terms": [
                search_example(
                    "search-full-document-alternative-a",
                    "<term-A>",
                ),
                search_example(
                    "search-full-document-alternative-b",
                    "<term-B>",
                ),
            ],
        },
        "research_examples": _research_command_examples(),
    }


def _chapter_translation_commands(
    chapter: SourceChapter,
    index: Mapping[str, Any] | None,
    *,
    cache_root: Path | None,
) -> dict[str, Any]:
    """Return executable ranges for the frozen reader-visible translation."""

    if index is None:
        return {
            "availability": "not_required",
            "instructions": (
                "No separate translation is required for this document."
            ),
            "parts": [],
        }
    cached = mapping(
        index.get("cached_document"), "model translation cached document"
    )
    chapter_values = mapping_list(
        index.get("chapters"), "model translation chapters"
    )
    chapter_value = next(
        (
            item
            for item in chapter_values
            if item.get("chapter_id") == chapter.chapter_id
        ),
        None,
    )
    if chapter_value is None:
        raise CompanionContentError(
            "model_translation_chapter_missing",
            f"Frozen translation has no chapter {chapter.chapter_id}.",
        )
    access = mapping_list(
        chapter_value.get("parts"), "model translation chapter parts"
    )
    if [item.get("block_id") for item in access] != list(
        chapter.block_ids
    ):
        raise CompanionContentError(
            "model_translation_chapter_mismatch",
            "Frozen translation part order differs from the source chapter.",
        )
    paper = ArcPaperService(cache_root=cache_root)
    document_json = json.dumps(
        cached,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    def translated_range(start: int, end: int) -> list[str]:
        return [
            "arc-paper",
            "read-cached-source-range",
            "--document-ref",
            document_json,
            "--cache-root",
            str(paper.cache_root),
            "--text-only",
            str(start),
            str(end),
        ]

    commands = []
    for part_number, item in enumerate(access, 1):
        start = item.get("line_start")
        end = item.get("line_end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise CompanionContentError(
                "model_translation_range_invalid",
                "Frozen translation part has no exact line range.",
            )
        commands.append(
            _command(
                f"part-{part_number}",
                translated_range(start, end),
                part_numbers=[part_number],
            )
        )
    section_starts = [
        chapter.block_ids.index(block_id)
        for block_id in chapter.section_block_ids
    ]
    for section_index, start_index in enumerate(section_starts, 1):
        end_index = (
            section_starts[section_index]
            if section_index < len(section_starts)
            else len(access)
        )
        commands.append(
            _command(
                f"section-{section_index}-complete",
                translated_range(
                    min(
                        int(item["line_start"])
                        for item in access[start_index:end_index]
                    ),
                    max(
                        int(item["line_end"])
                        for item in access[start_index:end_index]
                    ),
                ),
                part_numbers=list(
                    range(start_index + 1, end_index + 1)
                ),
            )
        )
    if access:
        commands.append(
            _command(
                "complete-current-chapter",
                translated_range(
                    min(int(item["line_start"]) for item in access),
                    max(int(item["line_end"]) for item in access),
                ),
                part_numbers=list(range(1, len(access) + 1)),
            )
        )
    return {
        "availability": "exact",
        "instructions": (
            "This is the frozen reader-visible translation. Read it together "
            "with the original source and use its established proper names "
            "and terminology in every guide field."
        ),
        "translation_view_sha256": index.get(
            "translation_view_sha256"
        ),
        "target_language": index.get("target_language"),
        "parts": commands,
    }


def _command(
    command_id: str,
    argv: Sequence[str],
    *,
    part_numbers: Sequence[int] = (),
) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "argv": list(argv),
        "shell": shlex.join(argv),
        "part_numbers": list(part_numbers),
    }


def _research_command_examples() -> list[dict[str, Any]]:
    return [
        _command(
            "lookup-reference-by-doi",
            ["arc-paper", "lookup-reference", "--doi", "<doi>"],
        ),
        _command(
            "acquire-reference-by-url",
            ["arc-paper", "acquire-reference", "--url", "<url>"],
        ),
        _command(
            "admit-downloaded-reference",
            [
                "arc-paper",
                "admit-reference",
                "<downloaded-file>",
                "--url",
                "<url>",
            ],
        ),
        _command(
            "materialize-cached-reference",
            [
                "arc-paper",
                "materialize-reference",
                "--resource-ref",
                "<CachedResourceRef JSON>",
                "--output",
                "<agent-workspace-file>",
            ],
        ),
    ]


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


def _attach_cached_reference_materials(
    guide: Mapping[str, Any],
    *,
    cache_root: Path | None,
) -> dict[str, Any]:
    """Attach an already-admitted shared handle without model bookkeeping."""

    cache = ReferenceMaterialCache(cache_root)
    value = dict(guide)
    references: list[dict[str, Any]] = []
    for raw in mapping_list(
        guide.get("references"), "chapter references"
    ):
        reference = dict(raw)
        material = None
        try:
            dois = reference.get("dois") or []
            arxiv_ids = reference.get("arxiv_ids") or []
            urls = _reference_urls(str(reference.get("source") or ""))
            if dois:
                material = cache.lookup(doi=str(dois[0]))
            elif arxiv_ids:
                material = cache.lookup(arxiv_id=str(arxiv_ids[0]))
            elif urls:
                material = cache.lookup(url=urls[0])
            else:
                material = cache.lookup(title=str(reference["title"]))
        except (OSError, ReferenceCacheError, TypeError, ValueError):
            # Cache reuse is an optimization. A valid citation remains
            # publishable when no shared material is available.
            material = None
        if material is not None:
            reference["cached_material"] = (
                cached_reference_material_to_document(material)
            )
        references.append(reference)
    value["references"] = references
    return value


def _reference_urls(source: str) -> list[str]:
    return [
        item.rstrip(".,;)")
        for item in re.findall(r"https?://[^\s<>()\]]+", source)
    ]


def _chapter_reference_contracts(
    context: RunContext,
    chapters: Sequence[SourceChapter],
    *,
    cited_ids: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    by_id: dict[str, dict[str, Any]] = {}
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
            value = {
                "evidence_id": str(item["reference_id"]),
                "title": str(item["title"]),
                "source": str(item["source"]),
                "dois": [str(value) for value in item["dois"]],
                "arxiv_ids": [
                    str(value) for value in item["arxiv_ids"]
                ],
                "cached_document": (
                    mapping(
                        item["cached_document"],
                        "cached document reference",
                    )
                    if item["cached_document"] is not None
                    else None
                ),
                "cached_material": (
                    mapping(
                        item["cached_material"],
                        "cached reference material",
                    )
                    if item["cached_material"] is not None
                    else None
                ),
            }
            evidence_id = str(value["evidence_id"])
            existing = by_id.get(evidence_id)
            if existing is not None and existing != value:
                raise CompanionContentError(
                    "chapter_reference_identity_collision",
                    "chapters disagree about shared reference metadata",
                )
            by_id[evidence_id] = value
    missing = [reference_id for reference_id in cited_ids if reference_id not in by_id]
    if missing:
        raise CompanionContentError(
            "chapter_reference_missing",
            f"cited chapter references are missing metadata: {missing}",
        )
    return tuple(by_id[reference_id] for reference_id in cited_ids)


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
) -> tuple[dict[str, Any], ...]:
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
        anchors = [
            block_id
            for block_id, block in block_documents.items()
            if block_id in source_refs
            or term.casefold()
            in _literal_strings(block.get("payload")).casefold()
        ]
        if not anchors:
            continue
        values.append(
            {
                "entry_id": str(item["term_id"]),
                "term": term,
                "translated_term": str(
                    item.get("preferred_translation") or ""
                ),
                "definition": str(item["target_definition"]),
                "anchor_ids": anchors,
                "citations": [],
            }
        )
    return tuple(values)


def _source_block_document(source: Any, block: Any) -> dict[str, Any]:
    return block_prompt_document(
        block,
        equation_label_provenance=equation_label_provenance(source, block.block_id),
    )


def _first_visible_citation_ids(
    chapters: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    values: list[str] = []
    for chapter in chapters:
        for unit in mapping_list(
            chapter.get("learning_units"), "learning units"
        ):
            values.extend(
                _string_list(unit.get("citations"), "learning citations")
            )
    return tuple(dict.fromkeys(values))


def _string_list(value: Any, description: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"{description} must contain non-empty strings",
        )
    return list(value)


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


def _chapter_translation_input_id(chapter_id: str) -> str:
    return f"companion-translation-index-{chapter_id}"


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
    "CompanionBuildHandler",
    "validate_build_diagnostics",
]
