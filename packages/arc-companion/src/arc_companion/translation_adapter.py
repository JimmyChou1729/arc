"""Narrow adapter from Companion orchestration to arc-translate's facade."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from arc_jobs import Paused, RunContext, RunError
from arc_llm import (
    LLMExecutionOptions,
    LLMTaskService,
    ModelSelection,
    ResumeInput,
)
from arc_paper import RichDocument


TranslationStep = Mapping[str, Any] | Paused | RunError


class CompanionTranslationRuntimeError(RuntimeError):
    """The default translation adapter cannot load its public dependency."""

    code = "runtime_dependency_missing"


def require_translation_runtime() -> None:
    """Verify the complete public arc-translate facade without creating state."""

    try:
        from arc_translate import (
            GlossaryResult,
            LanguageResult,
            TranslationSource,
            TranslationWorkflowService,
        )
    except ImportError as exc:
        raise CompanionTranslationRuntimeError(
            "arc-companion requires a complete compatible arc-translate "
            "runtime; install arc-companion with its declared dependencies"
        ) from exc
    # Keep the import check explicit and resistant to import optimizers.
    _ = (
        GlossaryResult,
        LanguageResult,
        TranslationSource,
        TranslationWorkflowService,
    )


class CompanionTranslationAdapter(Protocol):
    """Only the translation capabilities Companion's v2 handler consumes."""

    def detect_language(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        target_language: str,
        model: ModelSelection,
        execution: LLMExecutionOptions,
        resume_input: ResumeInput | None,
    ) -> TranslationStep: ...

    def build_glossary(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        language: Mapping[str, Any],
        target_language: str,
        approx_count: int,
        model: ModelSelection,
        execution: LLMExecutionOptions,
        resume_input: ResumeInput | None,
    ) -> TranslationStep: ...

    def translate_blocks(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        block_ids: Sequence[str],
        language: Mapping[str, Any],
        glossary: Mapping[str, Any],
        target_language: str,
        model: ModelSelection,
        execution: LLMExecutionOptions,
        resume_input: ResumeInput | None,
        artifact_prefix: str,
    ) -> TranslationStep: ...


class ArcTranslateAdapter:
    """Lazy public-facade adapter; importing Companion stays render-only."""

    def __init__(
        self,
        task_service: LLMTaskService | None = None,
        *,
        paper_cache_root: str | Path | None = None,
    ) -> None:
        self.task_service = task_service
        self.paper_cache_root = (
            Path(paper_cache_root)
            if paper_cache_root is not None
            else None
        )

    def detect_language(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        target_language: str,
        model: ModelSelection,
        execution: LLMExecutionOptions,
        resume_input: ResumeInput | None,
    ) -> TranslationStep:
        service, translation_source = self._service_and_source(source)
        outcome = service.detect_language(
            context,
            translation_source,
            target_language=target_language,
            model=model,
            execution=execution,
            resume_input=resume_input,
            artifact_prefix="translation-v2/language",
        )
        return _normalized_outcome(outcome)

    def build_glossary(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        language: Mapping[str, Any],
        target_language: str,
        approx_count: int,
        model: ModelSelection,
        execution: LLMExecutionOptions,
        resume_input: ResumeInput | None,
    ) -> TranslationStep:
        service, translation_source = self._service_and_source(source)
        from arc_translate import LanguageResult

        outcome = service.build_glossary(
            context,
            translation_source,
            language=LanguageResult.from_document(language),
            target_language=target_language,
            approx_count=approx_count,
            model=model,
            execution=execution,
            resume_input=resume_input,
            artifact_prefix="translation-v2/glossary",
        )
        return _normalized_outcome(outcome)

    def translate_blocks(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        block_ids: Sequence[str],
        language: Mapping[str, Any],
        glossary: Mapping[str, Any],
        target_language: str,
        model: ModelSelection,
        execution: LLMExecutionOptions,
        resume_input: ResumeInput | None,
        artifact_prefix: str,
    ) -> TranslationStep:
        service, translation_source = self._service_and_source(source)
        from arc_translate import GlossaryResult, LanguageResult

        outcome = service.translate_blocks(
            context,
            translation_source,
            language=LanguageResult.from_document(language),
            glossary=GlossaryResult.from_document(glossary),
            target_language=target_language,
            model=model,
            execution=execution,
            resume_input=resume_input,
            block_ids=tuple(block_ids),
            artifact_prefix=artifact_prefix,
        )
        return _normalized_outcome(outcome)

    def _service_and_source(self, source: RichDocument) -> tuple[Any, Any]:
        # arc-translate is intentionally imported only through its public root
        # facade. Companion does not depend on its handlers, prompts, or codecs.
        from arc_translate import TranslationSource, TranslationWorkflowService
        from arc_paper import KeywordInventoryService, TermInventoryStore

        return (
            TranslationWorkflowService(
                task_service=self.task_service,
                keyword_provider=KeywordInventoryService(
                    TermInventoryStore(self.paper_cache_root),
                    task_service=self.task_service,
                ),
            ),
            TranslationSource(rich=source),
        )


def _normalized_outcome(value: Any) -> TranslationStep:
    if isinstance(value, (Paused, RunError)):
        return value
    to_document = getattr(value, "to_document", None)
    if not callable(to_document):
        raise TypeError("arc-translate result lacks to_document()")
    document = to_document()
    if not isinstance(document, Mapping):
        raise TypeError("arc-translate result document must be an object")
    return dict(document)


__all__ = [
    "ArcTranslateAdapter",
    "CompanionTranslationRuntimeError",
    "CompanionTranslationAdapter",
    "TranslationStep",
    "require_translation_runtime",
]
