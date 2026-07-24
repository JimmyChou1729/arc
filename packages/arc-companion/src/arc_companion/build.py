"""One durable, replay-safe source-anchored Companion build handler."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from arc_jobs import (
    Awaiting,
    Failed,
    FailureMode,
    GroupResult,
    JsonValue,
    Paused,
    ResumeReason,
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
    ResumeInput,
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
    apply_safe_review,
    validate_chapter_draft,
    validate_chapter_plan,
    validate_glossary,
    validate_language_result,
)
from .llm_runtime import (
    CompanionLLMError,
    awaiting_from_pause,
    ensure_not_cancelled,
    execute_task,
    outer_resume_input,
    run_error_from_failure,
)
from .prompts_v1 import (
    CHAPTER_DRAFT_SCHEMA,
    CHAPTER_PLAN_SCHEMA,
    CHAPTER_REVIEW_SCHEMA,
    GLOSSARY_SCHEMA,
    LANGUAGE_SCHEMA,
    TRANSLATION_SCHEMA,
    chapter_draft_prompt,
    chapter_plan_prompt,
    chapter_review_prompt,
    glossary_prompt,
    language_prompt,
    translation_prompt,
)
from .request_contracts import (
    DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES,
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_handler_semantic_input,
)
from .source_planning import (
    SourceChapter,
    block_prompt_document,
    deterministic_language_samples,
    plan_source_chapters,
    same_primary_language,
)
from .validation import require_valid_accepted_book


COMPANION_BUILD_HANDLER = "arc.companion.build.v1"
_LANGUAGE_ARTIFACT = "planning/language"
_GLOSSARY_ARTIFACT = "planning/glossary"
_EVIDENCE_ARTIFACT = "planning/evidence"
_EVIDENCE_REQUEST_ARTIFACT = "planning/evidence-request"
_BOOK_ARTIFACT = "book/accepted"
_RESULT_ARTIFACT = "result"
_SUPERVISION_SCHEMA = "arc.companion.review_supervision.v1"
_EVIDENCE_INTERACTION_SCHEMA = "arc.companion.evidence_response.v1"
# Maximum UTF-8 size of a complete translation prompt. Rich blocks are
# indivisible: a single block larger than this budget fails with a clear
# content error. Changing this policy requires a new translation prompt
# contract so existing run lineages keep exact replay semantics.
TRANSLATION_WINDOW_INPUT_BUDGET_BYTES = DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES


class CompanionBuildHandler:
    """Complete LLM workflow inside one parent ``arc-jobs`` run."""

    name = COMPANION_BUILD_HANDLER

    def __init__(
        self,
        request: CompanionBuildRequest,
        recipe: CompanionGenerationRecipe = CompanionGenerationRecipe(),
        *,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        task_service: LLMTaskService | None = None,
    ) -> None:
        self.request = request
        self.recipe = recipe
        self.execution = execution
        self.task_service = task_service or LLMTaskService()

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
            chapters = plan_source_chapters(self.request.source)
            blocks = {item.block_id: item for item in self.request.source.blocks}

            language_outcome = self._language(context, resume_input)
            if isinstance(language_outcome, (Paused, Failed)):
                return language_outcome
            language = language_outcome
            translation_required = not (
                language["classification"] == "known"
                and same_primary_language(
                    language["language_tag"], self.request.target_language
                )
            )

            plans_outcome = self._plans(
                context, resume_input, chapters, blocks
            )
            if isinstance(plans_outcome, (Paused, Failed)):
                return plans_outcome
            plans = plans_outcome

            evidence_outcome = self._evidence(context, plans)
            if isinstance(evidence_outcome, Paused):
                return evidence_outcome
            evidence = evidence_outcome

            glossary_outcome = self._glossary(
                context, resume_input, plans, evidence
            )
            if isinstance(glossary_outcome, (Paused, Failed)):
                return glossary_outcome
            glossary = glossary_outcome

            chapters_outcome = self._chapters(
                context,
                resume_input,
                chapters,
                plans,
                glossary,
                evidence,
                blocks,
                language_result=language,
                translation_required=translation_required,
            )
            if isinstance(chapters_outcome, (Paused, Failed)):
                return chapters_outcome

            book_ref = context.artifacts.find(_BOOK_ARTIFACT)
            if book_ref is None:
                book = AcceptedBook(
                    document_digest=self.request.source.document_digest,
                    title=_document_title(self.request),
                    source_language=language["language_tag"],
                    target_language=self.request.target_language,
                    translation_mode=(
                        "enabled" if translation_required else "skipped"
                    ),
                    chapters=chapters_outcome,
                    glossary=_glossary_contracts(glossary),
                    bibliography=_bibliography_contracts(evidence),
                )
                require_valid_accepted_book(
                    book,
                    expected_block_ids=[
                        item.block_id for item in self.request.source.blocks
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
                    "accepted_book": _ref_document(book_ref),
                },
            )
            return Succeeded(result_ref)
        except CompanionContentError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except CompanionLLMError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except (TypeError, ValueError) as exc:
            return Failed(
                RunError("companion_content_invalid", str(exc))
            )

    def _language(
        self, context: RunContext, resume_input: ResumeInput | None
    ) -> dict[str, Any] | Paused | Failed:
        existing = context.artifacts.find(_LANGUAGE_ARTIFACT)
        if existing is not None:
            return _read_json(context, existing, "language result")
        request = LLMRequest(
            _task_id(
                "language",
                {
                    "document_digest": self.request.source.document_digest,
                    "prompt_contract": self.recipe.language_prompt,
                },
            ),
            language_prompt(deterministic_language_samples(self.request.source)),
            JsonOutput(LANGUAGE_SCHEMA, repair="local"),
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
            value = validate_language_result(outcome.value)
            context.artifacts.publish_json(_LANGUAGE_ARTIFACT, value)
            return value
        if isinstance(outcome, LLMPaused):
            return Paused(awaiting_from_pause(outcome))
        if isinstance(outcome, LLMFailed):
            return Failed(run_error_from_failure(outcome))
        ensure_not_cancelled(outcome, "source-language detection")
        raise RuntimeError("unknown language outcome")

    def _plans(
        self,
        context: RunContext,
        resume_input: ResumeInput | None,
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
            existing = context.artifacts.find(
                f"plans/{chapter.chapter_id}"
            )
            if existing is not None:
                return _read_json(context, existing, "chapter plan")
            request = LLMRequest(
                _task_id("plan", unit.semantic_input),
                chapter_plan_prompt(
                    chapter_id=chapter.chapter_id,
                    title=chapter.title,
                    blocks=[
                        block_prompt_document(blocks[item])
                        for item in chapter.block_ids
                    ],
                    target_language=self.request.target_language,
                    intent=self.request.effective_intent,
                ),
                JsonOutput(CHAPTER_PLAN_SCHEMA, repair="local"),
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
                context.artifacts.publish_json(
                    f"plans/{chapter.chapter_id}", value
                )
                return value
            if isinstance(outcome, LLMPaused):
                return Paused(awaiting_from_pause(outcome))
            if isinstance(outcome, LLMFailed):
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=run_error_from_failure(outcome),
                )
            ensure_not_cancelled(outcome, f"chapter plan {chapter.chapter_id}")
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
            item.unit_id: _mapping(item.value, "chapter plan")
            for item in result.units
        }
        return tuple(by_result[item.chapter_id] for item in chapters)

    def _evidence(
        self,
        context: RunContext,
        plans: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...] | Paused:
        requests = [
            dict(item)
            for plan in plans
            for item in _mapping_list(
                plan.get("evidence_requests"), "evidence requests"
            )
        ]
        request_ids = [str(item["request_id"]) for item in requests]
        if len(set(request_ids)) != len(request_ids):
            raise CompanionContentError(
                "evidence_request_invalid",
                "evidence request IDs must be unique across the book",
            )
        existing = context.artifacts.find(_EVIDENCE_ARTIFACT)
        if existing is not None:
            document = _read_json(context, existing, "frozen evidence")
            return tuple(
                _validate_evidence_responses(
                    document.get("items"), requests=requests
                )
            )
        if not requests:
            context.artifacts.publish_json(
                _EVIDENCE_ARTIFACT,
                {
                    "schema_version": _EVIDENCE_INTERACTION_SCHEMA,
                    "items": [],
                },
            )
            return ()

        digest = hashlib.sha256(canonical_json_bytes(requests)).hexdigest()
        resume_key = f"evidence-{digest[:24]}"
        value = context.resume_input
        if value is not None and value.get("resume_key") == resume_key:
            if set(value) != {"schema_version", "resume_key", "responses"}:
                raise CompanionContentError(
                    "evidence_response_invalid",
                    "evidence response has invalid fields",
                )
            if value.get("schema_version") != _EVIDENCE_INTERACTION_SCHEMA:
                raise CompanionContentError(
                    "evidence_response_invalid",
                    "evidence response schema is unsupported",
                )
            items = _validate_evidence_responses(
                value.get("responses"), requests=requests
            )
            context.artifacts.publish_json(
                _EVIDENCE_ARTIFACT,
                {
                    "schema_version": _EVIDENCE_INTERACTION_SCHEMA,
                    "items": items,
                },
            )
            return tuple(items)

        request_ref = context.artifacts.find(_EVIDENCE_REQUEST_ARTIFACT)
        if request_ref is None:
            request_ref = context.artifacts.publish_json(
                _EVIDENCE_REQUEST_ARTIFACT,
                {
                    "schema_version": "arc.companion.evidence_request.v1",
                    "resume_key": resume_key,
                    "response_schema": _evidence_response_schema(request_ids),
                    "requests": [
                        {
                            "request_id": item["request_id"],
                            "kind": item["kind"],
                            "query": item["query"],
                            "purpose": item["purpose"],
                            "anchors": list(item["anchor_block_ids"]),
                        }
                        for item in requests
                    ],
                },
            )
        return Paused(
            Awaiting(
                ResumeReason.INTERACTION_REQUIRED,
                resume_key,
                True,
                request_ref,
                _EVIDENCE_INTERACTION_SCHEMA,
                {"request_count": len(requests)},
            )
        )

    def _glossary(
        self,
        context: RunContext,
        resume_input: ResumeInput | None,
        plans: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...] | Paused | Failed:
        existing = context.artifacts.find(_GLOSSARY_ARTIFACT)
        if existing is not None:
            document = _read_json(context, existing, "glossary")
            return tuple(_mapping_list(document.get("entries"), "glossary"))
        candidates = [
            item
            for plan in plans
            for item in _mapping_list(
                plan.get("glossary_candidates"), "glossary candidates"
            )
        ]
        request = LLMRequest(
            _task_id(
                "glossary",
                {
                    "document_digest": self.request.source.document_digest,
                    "candidates": candidates,
                    "evidence_digest": _evidence_digest(evidence),
                    "target_language": self.request.target_language,
                    "prompt_contract": self.recipe.glossary_prompt,
                },
            ),
            glossary_prompt(
                candidates=candidates,
                target_language=self.request.target_language,
                evidence=evidence,
            ),
            JsonOutput(GLOSSARY_SCHEMA, repair="local"),
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
            entries = validate_glossary(
                outcome.value,
                document_block_ids=[
                    item.block_id for item in self.request.source.blocks
                ],
                evidence_ids=[str(item["evidence_id"]) for item in evidence],
            )
            context.artifacts.publish_json(
                _GLOSSARY_ARTIFACT, {"entries": list(entries)}
            )
            return entries
        if isinstance(outcome, LLMPaused):
            return Paused(awaiting_from_pause(outcome))
        if isinstance(outcome, LLMFailed):
            return Failed(run_error_from_failure(outcome))
        ensure_not_cancelled(outcome, "book glossary")
        raise RuntimeError("unknown glossary outcome")

    def _chapters(
        self,
        context: RunContext,
        resume_input: ResumeInput | None,
        chapters: tuple[SourceChapter, ...],
        plans: Sequence[Mapping[str, Any]],
        glossary: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
        blocks: Mapping[str, Any],
        *,
        language_result: Mapping[str, Any],
        translation_required: bool,
    ) -> tuple[AcceptedChapter, ...] | Paused | Failed:
        glossary_digest = hashlib.sha256(
            canonical_json_bytes(list(glossary))
        ).hexdigest()
        by_plan = {
            str(item["chapter_id"]): item for item in plans
        }
        evidence_by_request = {
            str(item["request_id"]): item for item in evidence
        }
        evidence_by_chapter = {
            chapter.chapter_id: tuple(
                evidence_by_request[str(request["request_id"])]
                for request in _mapping_list(
                    by_plan[chapter.chapter_id].get("evidence_requests"),
                    "evidence requests",
                )
            )
            for chapter in chapters
        }
        units = tuple(
            WorkUnit(
                chapter.chapter_id,
                {
                    "chapter_id": chapter.chapter_id,
                    "block_ids": list(chapter.block_ids),
                    "target_language": self.request.target_language,
                    "language_result": dict(language_result),
                    "translation_required": translation_required,
                    "intent": self.request.effective_intent,
                    "glossary_digest": glossary_digest,
                    "evidence_digest": _evidence_digest(
                        evidence_by_chapter[chapter.chapter_id]
                    ),
                    "content_contract": self.request.content_contract,
                    "translation_prompt_contract": self.recipe.translation_prompt,
                    "translation_input_budget_bytes": (
                        self.recipe.translation_input_budget_bytes
                    ),
                    "draft_prompt_contract": self.recipe.chapter_draft_prompt,
                    "review_prompt_contract": self.recipe.chapter_review_prompt,
                },
            )
            for chapter in chapters
        )
        by_id = {item.chapter_id: item for item in chapters}

        def worker(unit: WorkUnit):
            chapter = by_id[unit.unit_id]
            accepted_id = f"chapters/{chapter.chapter_id}/accepted"
            existing = context.artifacts.find(accepted_id)
            if existing is not None:
                return _read_json(context, existing, "accepted chapter")
            plan = by_plan[chapter.chapter_id]
            chapter_evidence = evidence_by_chapter[chapter.chapter_id]
            source_documents = [
                block_prompt_document(blocks[item])
                for item in chapter.block_ids
            ]
            planned_source_documents = _planned_source_documents(
                plan, source_documents
            )
            translations_outcome = self._chapter_translations(
                context,
                resume_input,
                chapter=chapter,
                source_documents=source_documents,
                glossary=glossary,
                language_result=language_result,
                translation_required=translation_required,
            )
            if isinstance(translations_outcome, Paused):
                return translations_outcome
            if isinstance(translations_outcome, RunError):
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=translations_outcome,
                )
            translations = translations_outcome
            draft_semantic_input = {
                **dict(unit.semantic_input),
                "translations_digest": hashlib.sha256(
                    canonical_json_bytes(translations)
                ).hexdigest(),
            }
            draft_request = LLMRequest(
                _task_id("draft", draft_semantic_input),
                chapter_draft_prompt(
                    plan=plan,
                    blocks=planned_source_documents,
                    glossary=glossary,
                    target_language=self.request.target_language,
                    language_result=language_result,
                    evidence=chapter_evidence,
                ),
                JsonOutput(CHAPTER_DRAFT_SCHEMA, repair="local"),
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
            ensure_not_cancelled(outcome, f"chapter draft {chapter.chapter_id}")
            assert isinstance(outcome, LLMCompleted)
            draft_value = _mapping(outcome.value, "chapter draft")
            draft_value["translations"] = translations
            draft = validate_chapter_draft(
                draft_value,
                plan=plan,
                block_ids=chapter.block_ids,
                translation_required=translation_required,
                evidence_ids=[
                    str(item["evidence_id"]) for item in chapter_evidence
                ],
            )

            review_request = LLMRequest(
                _task_id(
                    "review",
                    {
                        **dict(unit.semantic_input),
                        "draft_digest": hashlib.sha256(
                            canonical_json_bytes(draft)
                        ).hexdigest(),
                    },
                ),
                chapter_review_prompt(
                    plan=plan,
                    draft=draft,
                    blocks=planned_source_documents,
                    glossary=glossary,
                ),
                JsonOutput(CHAPTER_REVIEW_SCHEMA, repair="local"),
                self.recipe.model,
            )
            review_outcome = execute_task(
                self.task_service,
                context,
                review_request,
                resume_input=resume_input,
                options=self.execution.llm,
            )
            if isinstance(review_outcome, LLMPaused):
                return Paused(awaiting_from_pause(review_outcome))
            if isinstance(review_outcome, LLMFailed):
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=run_error_from_failure(review_outcome),
                )
            ensure_not_cancelled(
                review_outcome, f"chapter review {chapter.chapter_id}"
            )
            assert isinstance(review_outcome, LLMCompleted)
            try:
                reviewed, _summary = apply_safe_review(
                    draft,
                    review_outcome.value,
                    allowed_translation_block_ids={
                        str(item["block_id"])
                        for item in planned_source_documents
                    },
                )
                # Treat applying the reviewer patch and re-validating the
                # complete draft contract as one transaction.  A patch can
                # preserve every immutable ID yet still make the content
                # unusable (for example, by replacing required text with an
                # empty string).
                reviewed = validate_chapter_draft(
                    reviewed,
                    plan=plan,
                    block_ids=chapter.block_ids,
                    translation_required=translation_required,
                    evidence_ids=[
                        str(item["evidence_id"]) for item in chapter_evidence
                    ],
                )
                if translation_required:
                    _validate_translation_text_structure(
                        reviewed["translations"],
                        source_documents,
                    )
            except CompanionContentError as exc:
                supervision = self._review_supervision(
                    context,
                    chapter.chapter_id,
                    draft,
                    review_outcome.value,
                    exc,
                )
                if isinstance(supervision, Paused):
                    return supervision
                reviewed = supervision
            page_by_block = {
                item.block_id: item.page_number
                for item in self.request.source.page_map
            }
            accepted = AcceptedChapter(
                chapter_id=chapter.chapter_id,
                title=chapter.title,
                guide=reviewed["guide"],
                source_anchors=tuple(
                    SourceAnchor.from_rich_block(
                        blocks[block_id],
                        page_number=page_by_block.get(block_id),
                    )
                    for block_id in chapter.block_ids
                ),
                translations=tuple(
                    TranslatedBlock(
                        block_id=item["block_id"], text=item["text"]
                    )
                    for item in reviewed["translations"]
                ),
                learning_units=tuple(
                    LearningUnit(
                        unit_id=item["unit_id"],
                        kind=item["kind"],
                        title=item["title"],
                        anchor_ids=tuple(item["anchor_block_ids"]),
                        content=item["content"],
                        citations=tuple(item["citations"]),
                    )
                    for item in reviewed["learning_units"]
                ),
            )
            document = _accepted_chapter_document(accepted)
            context.artifacts.publish_json(accepted_id, document)
            return document

        result = context.run_group(
            "accepted-chapters",
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
                or RunError("chapter_build_failed", "chapter build failed")
            )
        by_result = {
            item.unit_id: _accepted_chapter_from_document(
                _mapping(item.value, "accepted chapter")
            )
            for item in result.units
        }
        return tuple(by_result[item.chapter_id] for item in chapters)

    def _chapter_translations(
        self,
        context: RunContext,
        resume_input: ResumeInput | None,
        *,
        chapter: SourceChapter,
        source_documents: Sequence[Mapping[str, Any]],
        glossary: Sequence[Mapping[str, Any]],
        language_result: Mapping[str, Any],
        translation_required: bool,
    ) -> list[dict[str, Any]] | Paused | RunError:
        if not translation_required:
            return []
        try:
            windows = _translation_windows(
                chapter_id=chapter.chapter_id,
                blocks=source_documents,
                glossary=glossary,
                target_language=self.request.target_language,
                language_result=language_result,
                budget_bytes=self.recipe.translation_input_budget_bytes,
            )
        except CompanionContentError as exc:
            return RunError(exc.code, str(exc))
        translations: list[dict[str, Any]] = []
        for ordinal, window in enumerate(windows):
            artifact_id = (
                f"chapters/{chapter.chapter_id}/"
                f"translation-windows/{ordinal:04d}"
            )
            expected_ids = [str(item["block_id"]) for item in window]
            existing = context.artifacts.find(artifact_id)
            if existing is not None:
                document = _read_json(
                    context, existing, "translation window"
                )
                try:
                    translations.extend(
                        _validate_translation_window(document, window)
                    )
                except CompanionContentError as exc:
                    return RunError(exc.code, str(exc))
                continue
            semantic_input = {
                "chapter_id": chapter.chapter_id,
                "window_ordinal": ordinal,
                "block_ids": expected_ids,
                "target_language": self.request.target_language,
                "language_result": dict(language_result),
                "glossary_digest": hashlib.sha256(
                    canonical_json_bytes(list(glossary))
                ).hexdigest(),
                "content_contract": self.request.content_contract,
                "prompt_contract": self.recipe.translation_prompt,
                "input_budget_bytes": (
                    self.recipe.translation_input_budget_bytes
                ),
            }
            request = LLMRequest(
                _task_id("translation", semantic_input),
                translation_prompt(
                    chapter_id=chapter.chapter_id,
                    window_ordinal=ordinal,
                    blocks=window,
                    glossary=glossary,
                    target_language=self.request.target_language,
                    language_result=language_result,
                ),
                JsonOutput(TRANSLATION_SCHEMA, repair="local"),
                self.recipe.model,
            )
            outcome = execute_task(
                self.task_service,
                context,
                request,
                resume_input=resume_input,
                options=self.execution.llm,
            )
            if isinstance(outcome, LLMPaused):
                return Paused(awaiting_from_pause(outcome))
            if isinstance(outcome, LLMFailed):
                return run_error_from_failure(outcome)
            ensure_not_cancelled(
                outcome,
                f"translation window {chapter.chapter_id}/{ordinal}",
            )
            assert isinstance(outcome, LLMCompleted)
            outcome_document = _mapping(
                outcome.value, "translation window"
            )
            try:
                value = _validate_translation_window(
                    outcome_document, window
                )
            except CompanionContentError as exc:
                return RunError(exc.code, str(exc))
            context.artifacts.publish_json(artifact_id, outcome_document)
            translations.extend(value)
        return translations

    def _review_supervision(
        self,
        context: RunContext,
        chapter_id: str,
        draft: Mapping[str, Any],
        review: Any,
        error: CompanionContentError,
    ) -> dict[str, Any] | Paused:
        digest = hashlib.sha256(
            canonical_json_bytes(
                {"chapter_id": chapter_id, "review": review}
            )
        ).hexdigest()[:24]
        resume_key = f"review-{digest}"
        value = context.resume_input
        if value is not None and value.get("resume_key") == resume_key:
            if set(value) != {"schema_version", "resume_key", "action"}:
                raise CompanionContentError(
                    "review_supervision_invalid",
                    "review supervision response has invalid fields",
                )
            if (
                value.get("schema_version") != _SUPERVISION_SCHEMA
                or value.get("action") != "discard_review"
            ):
                raise CompanionContentError(
                    "review_supervision_invalid",
                    "only discard_review is supported for an unsafe patch",
                )
            return dict(draft)
        request_ref = context.artifacts.find(
            f"chapters/{chapter_id}/review-supervision"
        )
        if request_ref is None:
            request_ref = context.artifacts.publish_json(
                f"chapters/{chapter_id}/review-supervision",
                {
                    "schema_version": _SUPERVISION_SCHEMA,
                    "resume_key": resume_key,
                    "reason": error.code,
                    "message": str(error),
                    "allowed_actions": ["discard_review"],
                },
            )
        return Paused(
            Awaiting(
                ResumeReason.SUPERVISION_REQUIRED,
                resume_key,
                True,
                request_ref,
                _SUPERVISION_SCHEMA,
                {"chapter_id": chapter_id, "code": error.code},
            )
        )


def _task_id(prefix: str, semantic: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(dict(semantic))).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _translation_windows(
    *,
    chapter_id: str,
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
    budget_bytes: int,
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """Pack indivisible RichBlocks under the complete prompt byte budget."""

    windows: list[tuple[Mapping[str, Any], ...]] = []
    current: list[Mapping[str, Any]] = []
    for block in blocks:
        candidate = [*current, block]
        if _translation_prompt_size(
            chapter_id=chapter_id,
            window_ordinal=len(windows),
            blocks=candidate,
            glossary=glossary,
            target_language=target_language,
            language_result=language_result,
        ) <= budget_bytes:
            current = candidate
            continue
        if current:
            windows.append(tuple(current))
            current = [block]
        else:
            current = [block]
        if _translation_prompt_size(
            chapter_id=chapter_id,
            window_ordinal=len(windows),
            blocks=current,
            glossary=glossary,
            target_language=target_language,
            language_result=language_result,
        ) > budget_bytes:
            block_id = str(block.get("block_id", "<unknown>"))
            raise CompanionContentError(
                "translation_block_exceeds_input_budget",
                (
                    f"source block {block_id} exceeds the "
                    f"{budget_bytes}-byte translation input budget"
                ),
            )
    if current:
        windows.append(tuple(current))
    return tuple(windows)


def _planned_source_documents(
    plan: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep only source blocks explicitly anchored by selective chapter work."""

    anchored: set[str] = set()
    for field in ("learning_units", "evidence_requests"):
        for item in _mapping_list(plan.get(field), field.replace("_", " ")):
            block_ids = item.get("anchor_block_ids")
            if not isinstance(block_ids, list):
                raise CompanionContentError(
                    "chapter_plan_invalid",
                    f"{field} anchor_block_ids must be an array",
                )
            anchored.update(str(block_id) for block_id in block_ids)
    return [block for block in blocks if block.get("block_id") in anchored]


def _translation_prompt_size(
    *,
    chapter_id: str,
    window_ordinal: int,
    blocks: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language_result: Mapping[str, Any],
) -> int:
    return len(
        translation_prompt(
            chapter_id=chapter_id,
            window_ordinal=window_ordinal,
            blocks=blocks,
            glossary=glossary,
            target_language=target_language,
            language_result=language_result,
        ).encode("utf-8")
    )


def _validate_translation_window(
    value: Any,
    source_blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    document = _mapping(value, "translation window")
    if set(document) != {"translations"}:
        raise CompanionContentError(
            "translation_coverage_invalid",
            "translation window has invalid fields",
        )
    translations = _mapping_list(
        document["translations"], "translation window"
    )
    expected_block_ids = [str(item["block_id"]) for item in source_blocks]
    if [item.get("block_id") for item in translations] != expected_block_ids:
        raise CompanionContentError(
            "translation_coverage_invalid",
            "translation window block IDs must exactly match source order",
        )
    if any(
        not isinstance(item.get("text"), str)
        or not item["text"].strip()
        or set(item) != {"block_id", "text", "source_identity"}
        for item in translations
    ):
        raise CompanionContentError(
            "translation_coverage_invalid",
            "translation window text must be a non-empty string",
        )
    for translated, source in zip(translations, source_blocks, strict=True):
        identity = _source_identity(source)
        if translated.get("source_identity") != identity:
            raise CompanionContentError(
                "translation_source_identity_invalid",
                (
                    "translation window changed formula, code, link, "
                    f"or asset identity for {source['block_id']}"
                ),
            )
        _validate_translation_text(str(translated["text"]), source)
    return [
        {"block_id": item["block_id"], "text": item["text"]}
        for item in translations
    ]


def _validate_translation_text_structure(
    translations: Sequence[Mapping[str, Any]],
    source_blocks: Sequence[Mapping[str, Any]],
) -> None:
    """Recheck immutable source structure after text-only reviewer patches.

    The accepted draft intentionally does not retain the model's
    ``source_identity`` echo.  Source structure is therefore recomputed from
    the frozen blocks and checked directly against each reviewed text.
    """

    expected = [str(item["block_id"]) for item in source_blocks]
    if [item.get("block_id") for item in translations] != expected:
        raise CompanionContentError(
            "translation_coverage_invalid",
            "reviewed translation block IDs must exactly match source order",
        )
    for translated, source in zip(translations, source_blocks, strict=True):
        _validate_translation_text(str(translated["text"]), source)


def _validate_translation_text(
    text: str,
    source: Mapping[str, Any],
) -> None:
    identity = _source_identity(source)
    if identity["code_text"] is not None and text != identity["code_text"]:
        raise CompanionContentError(
            "translation_source_identity_invalid",
            f"translation changed code text for {source['block_id']}",
        )
    if any(
        token not in text
        for token in [
            *identity["equations"],
            *identity["link_targets"],
        ]
    ):
        raise CompanionContentError(
            "translation_source_identity_invalid",
            (
                "translation text omitted a formula or link target for "
                f"{source['block_id']}"
            ),
        )
    # Figure identity is carried by the immutable source block rather than
    # natural-language translation text.  Recomputing it here ensures the
    # structural check covers the asset contract without retaining or trusting
    # a reviewer-editable model echo.
    if identity["asset_digest"] is not None and not str(
        identity["asset_digest"]
    ).strip():
        raise CompanionContentError(
            "translation_source_identity_invalid",
            f"translation source asset identity is invalid for {source['block_id']}",
        )


def _source_identity(block: Mapping[str, Any]) -> dict[str, Any]:
    kind = block.get("kind")
    payload = _mapping(block.get("payload"), "source block payload")
    equations: list[str] = []
    link_targets: list[str] = []
    if kind == "equation":
        equations.append(str(payload["tex"]))
    elif kind == "paragraph":
        equations.extend(
            str(item["tex"])
            for item in _mapping_list(
                payload.get("inline_math"), "inline math"
            )
        )
        link_targets.extend(
            str(item["target"])
            for item in _mapping_list(payload.get("links"), "links")
        )
    elif kind == "list":
        for raw_item in _mapping_list(payload.get("items"), "list items"):
            equations.extend(
                str(item["tex"])
                for item in _mapping_list(
                    raw_item.get("inline_math"), "inline math"
                )
            )
            link_targets.extend(
                str(item["target"])
                for item in _mapping_list(
                    raw_item.get("links"), "links"
                )
            )
    return {
        "equations": equations,
        "code_text": str(payload["text"]) if kind == "code" else None,
        "link_targets": link_targets,
        "asset_digest": (
            str(payload["asset_digest"])
            if kind == "figure" and payload.get("asset_digest")
            else None
        ),
        "asset_target": (
            str(payload["target"])
            if kind == "figure" and payload.get("target")
            else None
        ),
    }


def _document_title(request: CompanionBuildRequest) -> str:
    value = request.source.metadata.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Companion"


def _glossary_contracts(
    glossary: Sequence[Mapping[str, Any]],
) -> tuple[GlossaryEntry, ...]:
    values = []
    for item in glossary:
        term = str(item["term"]).strip()
        entry_id = "term-" + hashlib.sha256(
            term.casefold().encode("utf-8")
        ).hexdigest()[:20]
        values.append(
            GlossaryEntry(
                entry_id=entry_id,
                term=term,
                translated_term=str(
                    item.get("preferred_translation") or ""
                ),
                definition=str(item["definition"]),
                anchor_ids=tuple(item["anchor_block_ids"]),
                citations=tuple(str(value) for value in item["citations"]),
            )
        )
    return tuple(values)


def _bibliography_contracts(
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[EvidenceSource, ...]:
    return tuple(
        EvidenceSource(
            evidence_id=str(item["evidence_id"]),
            title=str(item["title"]),
            source=str(item["source"]),
        )
        for item in evidence
    )


def _evidence_digest(evidence: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(evidence))).hexdigest()


def _validate_evidence_responses(
    value: Any,
    *,
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    responses = _mapping_list(value, "evidence responses")
    expected_ids = [str(item["request_id"]) for item in requests]
    by_request: dict[str, dict[str, Any]] = {}
    evidence_ids: set[str] = set()
    expected_fields = {
        "request_id",
        "evidence_id",
        "title",
        "content",
        "source",
    }
    for response in responses:
        if set(response) != expected_fields:
            raise CompanionContentError(
                "evidence_response_invalid",
                "each evidence response must contain exactly request_id, "
                "evidence_id, title, content, and source",
            )
        if any(
            not isinstance(response[field], str)
            or not str(response[field]).strip()
            for field in expected_fields
        ):
            raise CompanionContentError(
                "evidence_response_invalid",
                "evidence response fields must be non-empty strings",
            )
        request_id = str(response["request_id"])
        evidence_id = str(response["evidence_id"])
        if request_id in by_request:
            raise CompanionContentError(
                "evidence_response_invalid",
                f"duplicate evidence response for request {request_id}",
            )
        if evidence_id in evidence_ids:
            raise CompanionContentError(
                "evidence_response_invalid",
                f"duplicate evidence ID {evidence_id}",
            )
        by_request[request_id] = {
            field: str(response[field]).strip() for field in expected_fields
        }
        evidence_ids.add(evidence_id)
    if set(by_request) != set(expected_ids) or len(by_request) != len(expected_ids):
        raise CompanionContentError(
            "evidence_response_invalid",
            "evidence responses must exactly cover every planned request ID",
        )
    return [by_request[request_id] for request_id in expected_ids]


def _evidence_response_schema(request_ids: Sequence[str]) -> dict[str, Any]:
    nonempty = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "properties": {
            "schema_version": {
                "const": _EVIDENCE_INTERACTION_SCHEMA,
            },
            "resume_key": nonempty,
            "responses": {
                "type": "array",
                "minItems": len(request_ids),
                "maxItems": len(request_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "enum": list(request_ids),
                        },
                        "evidence_id": nonempty,
                        "title": nonempty,
                        "content": nonempty,
                        "source": nonempty,
                    },
                    "required": [
                        "request_id",
                        "evidence_id",
                        "title",
                        "content",
                        "source",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["schema_version", "resume_key", "responses"],
        "additionalProperties": False,
    }


def _accepted_chapter_document(chapter: AcceptedChapter) -> dict[str, Any]:
    placeholder = AcceptedBook(
        document_digest="0" * 64,
        title="chapter",
        source_language="und",
        target_language="und",
        translation_mode=(
            "enabled" if chapter.translations else "skipped"
        ),
        chapters=(chapter,),
        bibliography=(),
    )
    return CompanionContentCodec.to_document(placeholder)["chapters"][0]


def _accepted_chapter_from_document(value: Mapping[str, Any]) -> AcceptedChapter:
    document = {
        "schema_version": "arc.companion.accepted_book.v1",
        "document_digest": "0" * 64,
        "title": "chapter",
        "source_language": "und",
        "target_language": "und",
        "translation_mode": (
            "enabled" if value.get("translations") else "skipped"
        ),
        "chapters": [dict(value)],
        "glossary": [],
        "bibliography": [],
    }
    return CompanionContentCodec.from_document(document).chapters[0]


def _ref_document(ref: Any) -> dict[str, JsonValue]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": {
            "algorithm": ref.digest.algorithm,
            "value": ref.digest.value,
            "size_bytes": ref.digest.size_bytes,
        },
        "media_type": ref.media_type,
        "relative_path": ref.relative_path,
    }


def _read_json(context: RunContext, ref: Any, description: str) -> dict[str, Any]:
    try:
        value = json.loads(context.artifacts.read_bytes(ref).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"cannot decode {description}: {exc}",
        ) from exc
    return _mapping(value, description)


def _mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanionContentError(
            "companion_artifact_invalid", f"{description} must be an object"
        )
    return dict(value)


def _mapping_list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"{description} must be an array of objects",
        )
    return [dict(item) for item in value]


__all__ = [
    "COMPANION_BUILD_HANDLER",
    "TRANSLATION_WINDOW_INPUT_BUDGET_BYTES",
    "CompanionBuildHandler",
]
