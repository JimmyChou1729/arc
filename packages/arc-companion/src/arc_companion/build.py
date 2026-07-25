"""Current split translation and guide Companion build handler."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from arc_jobs import (
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
    JsonOutput,
    LLMCompleted,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMTaskService,
)
from arc_paper import (
    EquationLabelReviewService,
    PdftoppmFullPageRenderer,
    RichDocument,
    SourceFormat,
    SourceRepository,
    SourceRepositoryError,
    apply_visual_equation_labels,
    detect_suspicious_equation_labels,
    rich_document_from_document,
    rich_document_to_document,
)

from ._build_support import (
    accepted_chapter_document,
    bibliography_contracts,
    collect_evidence,
    evidence_digest,
    mapping,
    mapping_list,
    planned_source_documents,
    read_json,
    ref_document,
    review_supervision,
    task_id,
)
from .contracts import (
    AcceptedBook,
    AcceptedChapter,
    CompanionContentCodec,
    GlossaryEntry,
    LearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from .generation_validation import (
    CompanionContentError,
    apply_safe_guide_review,
    validate_chapter_guide,
    validate_chapter_plan,
)
from .llm_runtime import (
    CompanionLLMError,
    awaiting_from_pause,
    ensure_not_stopped,
    execute_task,
    outer_resume_input,
    run_error_from_failure,
)
from .prompts import (
    CHAPTER_GUIDE_REVIEW_SCHEMA,
    CHAPTER_GUIDE_SCHEMA,
    CHAPTER_PLAN_SCHEMA,
    chapter_guide_prompt,
    chapter_guide_review_prompt,
    chapter_plan_prompt,
)
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
from .translation_adapter import (
    ArcTranslateAdapter,
    CompanionTranslationAdapter,
)
from .validation import require_valid_accepted_book


COMPANION_BUILD_HANDLER = "arc.companion.build.v3"
COMPANION_BUILD_DIAGNOSTICS_SCHEMA = "arc.companion.build_diagnostics.v1"
_BOOK_ARTIFACT = "book/accepted"
_DIAGNOSTICS_ARTIFACT = "diagnostics/build"
_EFFECTIVE_SOURCE_ARTIFACT = "source/effective"
_RESULT_ARTIFACT = "result"


class CompanionBuildHandler:
    """Coordinate arc-translate and guide generation without shared review."""

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
        self.task_service = task_service or LLMTaskService()
        self.translation_adapter = translation_adapter or ArcTranslateAdapter(
            self.task_service,
            cache_root=self.execution.cache_root,
        )

    def semantic_input(self) -> dict[str, Any]:
        return encode_handler_semantic_input(self.request, self.recipe)

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "companion_build_binding_mismatch",
                    "Handler bindings do not match the durable v3 build request.",
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
            blocks = {
                item.block_id: item for item in source.blocks
            }

            language = self.translation_adapter.detect_language(
                context,
                source,
                target_language=self.request.target_language,
                model=self.recipe.model,
                execution=self.execution.llm,
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

            plans = self._plans(
                context, resume_input, source, chapters, blocks
            )
            if isinstance(plans, (Paused, Failed)):
                return plans
            evidence = collect_evidence(context, plans)
            if isinstance(evidence, Paused):
                return evidence

            glossary: dict[str, Any]
            if translation_required:
                glossary_outcome = self.translation_adapter.build_glossary(
                    context,
                    source,
                    language=language,
                    target_language=self.request.target_language,
                    approx_count=self.recipe.approx_term_count,
                    model=self.recipe.model,
                    execution=self.execution.llm,
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
                evidence,
                blocks,
                source=source,
                language=language,
                translation_required=translation_required,
            )
            if isinstance(chapters_outcome, (Paused, Failed)):
                return chapters_outcome

            book_ref = context.artifacts.find(_BOOK_ARTIFACT)
            if book_ref is None:
                book = AcceptedBook(
                    document_digest=source.document_digest,
                    title=_document_title(source),
                    source_language=str(language["language_tag"]),
                    target_language=self.request.target_language,
                    translation_mode=(
                        "enabled" if translation_required else "skipped"
                    ),
                    chapters=chapters_outcome,
                    glossary=(
                        _glossary_contracts(
                            glossary, source
                        )
                        if translation_required
                        else ()
                    ),
                    bibliography=bibliography_contracts(evidence),
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
        except CompanionLLMError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except (KeyError, TypeError, ValueError) as exc:
            return Failed(RunError("companion_content_invalid", str(exc)))

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
        elif self.execution.cache_root is None:
            warning = (
                "PDF visual equation-label review was not run: the paper cache "
                "root is unavailable; retaining web equation labels."
            )
        else:
            digest = self.request.validator_digests[0]
            repository = SourceRepository(self.execution.cache_root)
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
                    options=self.execution.llm,
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
                    blocks=[
                        _source_block_document(source, blocks[item])
                        for item in chapter.block_ids
                    ],
                    target_language=self.request.target_language,
                    intent=self.request.effective_intent,
                ),
                JsonOutput(CHAPTER_PLAN_SCHEMA, repair="format"),
                self.recipe.model,
            )
            outcome = execute_task(
                self.task_service,
                context,
                request,
                resume_input=resume_input,
                options=self.execution.llm,
            )
            if isinstance(outcome, LLMCompleted):
                value = validate_chapter_plan(
                    outcome.value,
                    chapter_id=chapter.chapter_id,
                    block_ids=chapter.block_ids,
                )
                context.artifacts.publish_json(artifact_id, value)
                return value
            if isinstance(outcome, LLMPaused):
                return Paused(awaiting_from_pause(outcome))
            if isinstance(outcome, LLMFailed):
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=run_error_from_failure(outcome),
                )
            ensure_not_stopped(outcome, f"chapter plan {chapter.chapter_id}")
            raise RuntimeError("unknown chapter-plan outcome")

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
        evidence: Sequence[Mapping[str, Any]],
        blocks: Mapping[str, Any],
        *,
        source: RichDocument,
        language: Mapping[str, Any],
        translation_required: bool,
    ) -> tuple[AcceptedChapter, ...] | Paused | Failed:
        by_chapter = {item.chapter_id: item for item in chapters}
        by_plan = {str(item["chapter_id"]): item for item in plans}
        evidence_by_request = {
            str(item["request_id"]): item for item in evidence
        }
        evidence_by_chapter = {
            chapter.chapter_id: tuple(
                evidence_by_request[str(request["request_id"])]
                for request in mapping_list(
                    by_plan[chapter.chapter_id].get(
                        "evidence_requests"
                    ),
                    "evidence requests",
                )
            )
            for chapter in chapters
        }
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
        units: list[WorkUnit] = []
        for chapter in chapters:
            common = {
                "chapter_id": chapter.chapter_id,
                "block_ids": list(chapter.block_ids),
                "target_language": self.request.target_language,
                "language": language_identity,
                "glossary_digest": glossary_digest,
                "content_contract": self.request.content_contract,
            }
            if translation_required:
                units.append(
                    WorkUnit(
                        f"translation-{chapter.chapter_id}",
                        {
                            **common,
                            "lane": "translation",
                            "lane_id": (
                                f"translation:{chapter.chapter_id}"
                            ),
                        },
                    )
                )
            units.append(
                WorkUnit(
                    f"guide-{chapter.chapter_id}",
                    {
                        **common,
                        "lane": "guide",
                        "lane_id": f"guide:{chapter.chapter_id}",
                        "intent": self.request.effective_intent,
                        "evidence_digest": evidence_digest(
                            evidence_by_chapter[chapter.chapter_id]
                        ),
                        "guide_prompt_contract": (
                            self.recipe.chapter_guide_prompt
                        ),
                        "review_prompt_contract": (
                            self.recipe.chapter_guide_review_prompt
                        ),
                    },
                )
            )

        def worker(unit: WorkUnit):
            lane, chapter_id = unit.unit_id.split("-", 1)
            chapter = by_chapter[chapter_id]
            if lane == "translation":
                outcome = self.translation_adapter.translate_blocks(
                    context,
                    source,
                    block_ids=chapter.block_ids,
                    language=language,
                    glossary=glossary,
                    target_language=self.request.target_language,
                    model=self.recipe.model,
                    execution=self.execution.llm,
                    resume_input=resume_input,
                    artifact_prefix=(
                        f"chapters/{chapter_id}/translation"
                    ),
                )
                if isinstance(outcome, Paused):
                    return outcome
                if isinstance(outcome, RunError):
                    return UnitResult(
                        unit.unit_id, "failed", error=outcome
                    )
                return _validated_translations(
                    outcome, block_ids=chapter.block_ids
                )
            return self._guide_lane(
                context,
                resume_input,
                unit,
                chapter,
                by_plan[chapter_id],
                evidence_by_chapter[chapter_id],
                chapter_entries[chapter_id],
                blocks,
                language,
                source,
            )

        result = context.run_group(
            "post-glossary-chapter-lanes",
            tuple(units),
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
                or RunError("chapter_lane_failed", "chapter lane failed")
            )
        results = {
            item.unit_id: mapping(item.value, "chapter lane result")
            for item in result.units
        }
        accepted: list[AcceptedChapter] = []
        page_by_block = {
            item.block_id: item.page_number
            for item in source.page_map
        }
        for chapter in chapters:
            guide = results[f"guide-{chapter.chapter_id}"]
            translations = (
                mapping_list(
                    results[f"translation-{chapter.chapter_id}"][
                        "translations"
                    ],
                    "translations",
                )
                if translation_required
                else []
            )
            chapter_value = AcceptedChapter(
                chapter_id=chapter.chapter_id,
                title=chapter.title,
                guide=str(guide["guide"]),
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
                        kind=str(item["kind"]),
                        title=str(item["title"]),
                        anchor_ids=tuple(item["anchor_block_ids"]),
                        content=str(item["content"]),
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

    def _guide_lane(
        self,
        context: RunContext,
        resume_input: Any,
        unit: WorkUnit,
        chapter: SourceChapter,
        plan: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        glossary: Sequence[Mapping[str, Any]],
        blocks: Mapping[str, Any],
        language: Mapping[str, Any],
        source: RichDocument,
    ) -> Mapping[str, Any] | Paused | UnitResult:
        artifact_id = f"chapters/{chapter.chapter_id}/guide-accepted"
        existing = context.artifacts.find(artifact_id)
        if existing is not None:
            return read_json(context, existing, "accepted chapter guide")
        source_documents = [
            _source_block_document(source, blocks[item])
            for item in chapter.block_ids
        ]
        planned_documents = planned_source_documents(
            plan, source_documents
        )
        draft_request = LLMRequest(
            task_id("guide", unit.semantic_input),
            chapter_guide_prompt(
                plan=plan,
                blocks=planned_documents,
                glossary=glossary,
                target_language=self.request.target_language,
                language_result=language,
                evidence=evidence,
            ),
            JsonOutput(CHAPTER_GUIDE_SCHEMA, repair="format"),
            self.recipe.model,
        )
        outcome = execute_task(
            self.task_service,
            context,
            draft_request,
            resume_input=resume_input,
            options=self.execution.llm,
        )
        if isinstance(outcome, LLMPaused):
            return Paused(awaiting_from_pause(outcome))
        if isinstance(outcome, LLMFailed):
            return UnitResult(
                unit.unit_id,
                "failed",
                error=run_error_from_failure(outcome),
            )
        ensure_not_stopped(outcome, f"chapter guide {chapter.chapter_id}")
        assert isinstance(outcome, LLMCompleted)
        draft = validate_chapter_guide(
            outcome.value,
            plan=plan,
            evidence_ids=[str(item["evidence_id"]) for item in evidence],
        )
        review_request = LLMRequest(
            task_id(
                "guide-review",
                {
                    **dict(unit.semantic_input),
                    "guide_digest": hashlib.sha256(
                        canonical_json_bytes(draft)
                    ).hexdigest(),
                },
            ),
            chapter_guide_review_prompt(
                plan=plan,
                draft=draft,
                blocks=planned_documents,
                glossary=glossary,
            ),
            JsonOutput(CHAPTER_GUIDE_REVIEW_SCHEMA, repair="format"),
            self.recipe.model,
        )
        reviewed_outcome = execute_task(
            self.task_service,
            context,
            review_request,
            resume_input=resume_input,
            options=self.execution.llm,
        )
        if isinstance(reviewed_outcome, LLMPaused):
            return Paused(awaiting_from_pause(reviewed_outcome))
        if isinstance(reviewed_outcome, LLMFailed):
            return UnitResult(
                unit.unit_id,
                "failed",
                error=run_error_from_failure(reviewed_outcome),
            )
        ensure_not_stopped(
            reviewed_outcome,
            f"chapter guide review {chapter.chapter_id}",
        )
        assert isinstance(reviewed_outcome, LLMCompleted)
        try:
            reviewed, _summary = apply_safe_guide_review(
                draft, reviewed_outcome.value
            )
            reviewed = validate_chapter_guide(
                reviewed,
                plan=plan,
                evidence_ids=[
                    str(item["evidence_id"]) for item in evidence
                ],
            )
        except CompanionContentError as exc:
            supervision = review_supervision(
                context,
                chapter.chapter_id,
                draft,
                reviewed_outcome.value,
                exc,
            )
            if isinstance(supervision, Paused):
                return supervision
            reviewed = supervision
        context.artifacts.publish_json(artifact_id, reviewed)
        return reviewed


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


def _document_title(source: RichDocument) -> str:
    value = source.metadata.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Companion"


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
