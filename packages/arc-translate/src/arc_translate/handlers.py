"""Thin outer handlers for the three standalone durable commands."""

from __future__ import annotations

from typing import Any

from arc_jobs import (
    ArtifactSourceRef,
    Failed,
    Paused,
    RunContext,
    RunError,
    RunStatus,
    Succeeded,
)

from .contracts import (
    BlocksRequest,
    ExecutionOptions,
    GenerationRecipe,
    GlossaryRequest,
    LanguageRequest,
    blocks_semantic_input,
    glossary_semantic_input,
    language_semantic_input,
)
from .workflow import (
    BlocksResult,
    GlossaryResult,
    KeywordProvider,
    LanguageResult,
    TranslationWorkflowError,
    TranslationWorkflowService,
    outer_resume_input,
)


LANGUAGE_HANDLER = "arc.translate.detect_language.v1"
GLOSSARY_HANDLER = "arc.translate.build_glossary.v1"
BLOCKS_HANDLER = "arc.translate.translate_blocks.v1"


class DetectLanguageHandler:
    name = LANGUAGE_HANDLER

    def __init__(
        self,
        request: LanguageRequest,
        recipe: GenerationRecipe = GenerationRecipe(),
        *,
        execution: ExecutionOptions = ExecutionOptions(),
        task_service: Any | None = None,
    ) -> None:
        self.request = request
        self.recipe = recipe
        self.execution = execution
        self.workflow = TranslationWorkflowService(task_service=task_service)

    def semantic_input(self):
        return language_semantic_input(self.request, self.recipe)

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "language_binding_mismatch",
                    "handler bindings do not match the durable language request",
                )
            )
        try:
            outcome = self.workflow.detect_language(
                context,
                self.request.source,
                target_language=self.request.target_language,
                model=self.recipe.model,
                execution=self.execution.llm,
                resume_input=outer_resume_input(context),
            )
            return _outer_outcome(context, outcome, "language/result")
        except TranslationWorkflowError as exc:
            return Failed(RunError(exc.code, str(exc)))


class BuildGlossaryHandler:
    name = GLOSSARY_HANDLER

    def __init__(
        self,
        request: GlossaryRequest,
        recipe: GenerationRecipe = GenerationRecipe(),
        *,
        execution: ExecutionOptions = ExecutionOptions(),
        task_service: Any | None = None,
        keyword_provider: KeywordProvider | None = None,
    ) -> None:
        self.request = request
        self.recipe = recipe
        self.execution = execution
        self.workflow = TranslationWorkflowService(
            task_service=task_service,
            keyword_provider=keyword_provider,
        )

    def semantic_input(self):
        return glossary_semantic_input(self.request, self.recipe)

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "glossary_binding_mismatch",
                    "handler bindings do not match the durable glossary request",
                )
            )
        prerequisite = _read_prerequisite(
            context, self.request.language_result, "language result"
        )
        if isinstance(prerequisite, RunError):
            return Failed(prerequisite)
        try:
            language = LanguageResult.from_document(prerequisite)
            outcome = self.workflow.build_glossary(
                context,
                self.request.source,
                language=language,
                target_language=self.request.target_language,
                approx_count=self.request.approx_count,
                model=self.recipe.model,
                execution=self.execution.llm,
                resume_input=outer_resume_input(context),
                term_input_budget_bytes=(
                    self.recipe.glossary_input_budget_bytes
                ),
            )
            return _outer_outcome(context, outcome, "glossary/result")
        except (ValueError, TranslationWorkflowError) as exc:
            return Failed(
                RunError(
                    getattr(exc, "code", "language_result_invalid"),
                    str(exc),
                )
            )


class TranslateBlocksHandler:
    name = BLOCKS_HANDLER

    def __init__(
        self,
        request: BlocksRequest,
        recipe: GenerationRecipe = GenerationRecipe(),
        *,
        execution: ExecutionOptions = ExecutionOptions(),
        task_service: Any | None = None,
    ) -> None:
        self.request = request
        self.recipe = recipe
        self.execution = execution
        self.workflow = TranslationWorkflowService(task_service=task_service)

    def semantic_input(self):
        return blocks_semantic_input(self.request, self.recipe)

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "blocks_binding_mismatch",
                    "handler bindings do not match the durable blocks request",
                )
            )
        language_doc = _read_prerequisite(
            context, self.request.language_result, "language result"
        )
        if isinstance(language_doc, RunError):
            return Failed(language_doc)
        glossary_doc = _read_prerequisite(
            context, self.request.glossary_result, "glossary result"
        )
        if isinstance(glossary_doc, RunError):
            return Failed(glossary_doc)
        try:
            outcome = self.workflow.translate_blocks(
                context,
                self.request.source,
                language=LanguageResult.from_document(language_doc),
                glossary=GlossaryResult.from_document(glossary_doc),
                target_language=self.request.target_language,
                model=self.recipe.model,
                execution=self.execution.llm,
                resume_input=outer_resume_input(context),
                input_budget_bytes=(
                    self.recipe.translation_input_budget_bytes
                ),
            )
            return _outer_outcome(context, outcome, "translation/result")
        except (ValueError, TranslationWorkflowError) as exc:
            return Failed(
                RunError(
                    getattr(exc, "code", "prerequisite_result_invalid"),
                    str(exc),
                )
            )


def _outer_outcome(
    context: RunContext,
    outcome: LanguageResult | GlossaryResult | BlocksResult | Paused | RunError,
    artifact_id: str,
):
    if isinstance(outcome, Paused):
        return outcome
    if isinstance(outcome, RunError):
        return Failed(outcome)
    ref = context.artifacts.find(artifact_id)
    if ref is None:
        return Failed(
            RunError(
                "result_artifact_missing",
                f"workflow did not publish {artifact_id}",
            )
        )
    return Succeeded(ref)


def _read_prerequisite(
    context: RunContext,
    source: ArtifactSourceRef,
    description: str,
) -> dict[str, Any] | RunError:
    try:
        view = context.repository.inspect(source.source_run_id)
        snapshot = view.snapshot
        if (
            snapshot.status is not RunStatus.SUCCEEDED
            or snapshot.result_ref is None
            or snapshot.result_ref.artifact_id != source.source_artifact_id
            or snapshot.result_ref.digest != source.expected_digest
        ):
            return RunError(
                "prerequisite_not_verified",
                f"{description} prerequisite is not a verified successful result",
            )
        verified = context.artifacts.read_source(source)
        import json

        value = json.loads(verified.content.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("prerequisite JSON must be an object")
        return value
    except Exception as exc:
        return RunError(
            getattr(exc, "code", "prerequisite_not_verified"),
            f"{description} prerequisite could not be verified: {exc}",
        )


__all__ = [
    "BLOCKS_HANDLER",
    "GLOSSARY_HANDLER",
    "LANGUAGE_HANDLER",
    "BuildGlossaryHandler",
    "DetectLanguageHandler",
    "TranslateBlocksHandler",
]
