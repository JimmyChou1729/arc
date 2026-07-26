"""Public durable control for standalone translation step lineages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from arc_jobs import (
    ArtifactSourceRef,
    ImmutableArtifactStore,
    RunEngine,
    RunRepository,
    RunSnapshot,
    RunSpec,
    RunStatus,
    RunView,
    ValidationReport,
    canonical_json_bytes,
)

from .contracts import (
    BlocksRequest,
    ExecutionOptions,
    GenerationRecipe,
    GlossaryRequest,
    LanguageRequest,
    decode_blocks_semantic_input,
    decode_glossary_semantic_input,
    decode_language_semantic_input,
)
from .handlers import (
    BLOCKS_HANDLER,
    GLOSSARY_HANDLER,
    LANGUAGE_HANDLER,
    BuildGlossaryHandler,
    DetectLanguageHandler,
    TranslateBlocksHandler,
)
from .workflow import (
    BlocksResult,
    GlossaryResult,
    KeywordProvider,
    LanguageResult,
    TranslationWorkflowError,
)


class TranslationServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TranslationService:
    """Prepare, execute, resume, inspect, and validate translation steps."""

    def __init__(self, repository: RunRepository | str | Path) -> None:
        self.repository = (
            repository
            if isinstance(repository, RunRepository)
            else RunRepository(repository)
        )
        self.engine = RunEngine(self.repository)

    def prepare_language(
        self,
        request: LanguageRequest,
        *,
        recipe: GenerationRecipe = GenerationRecipe(),
        run_id: str | None = None,
    ) -> RunSnapshot:
        handler = DetectLanguageHandler(request, recipe)
        return self.repository.create(
            RunSpec(
                run_id or _run_id("language", handler.semantic_input()),
                LANGUAGE_HANDLER,
                handler.semantic_input(),
            )
        )

    def prepare_glossary(
        self,
        request: GlossaryRequest,
        *,
        recipe: GenerationRecipe = GenerationRecipe(),
        run_id: str | None = None,
    ) -> RunSnapshot:
        handler = BuildGlossaryHandler(request, recipe)
        return self.repository.create(
            RunSpec(
                run_id or _run_id("glossary", handler.semantic_input()),
                GLOSSARY_HANDLER,
                handler.semantic_input(),
            )
        )

    def prepare_blocks(
        self,
        request: BlocksRequest,
        *,
        recipe: GenerationRecipe = GenerationRecipe(),
        run_id: str | None = None,
    ) -> RunSnapshot:
        handler = TranslateBlocksHandler(request, recipe)
        return self.repository.create(
            RunSpec(
                run_id or _run_id("blocks", handler.semantic_input()),
                BLOCKS_HANDLER,
                handler.semantic_input(),
            )
        )

    def execute(
        self,
        run_id: str,
        *,
        execution: ExecutionOptions = ExecutionOptions(),
        task_service: Any | None = None,
        keyword_provider: KeywordProvider | None = None,
    ) -> RunSnapshot:
        spec = self.repository.read_spec(run_id)
        handler = self._handler(
            spec,
            execution=execution,
            task_service=task_service,
            keyword_provider=keyword_provider,
        )
        return self.engine.execute(spec, handler)

    def resume(
        self,
        run_id: str,
        *,
        input: Mapping[str, Any] | None = None,
        execution: ExecutionOptions = ExecutionOptions(),
        task_service: Any | None = None,
        keyword_provider: KeywordProvider | None = None,
    ) -> RunSnapshot:
        spec = self.repository.read_working_spec(run_id)
        handler = self._handler(
            spec,
            execution=execution,
            task_service=task_service,
            keyword_provider=keyword_provider,
        )
        return self.engine.resume(run_id, handler, input=input)

    def inspect(self, run_id: str) -> RunView:
        return self.repository.inspect(run_id)

    def stop(self, run_id: str, *, reason: str | None = None) -> RunView:
        return self.repository.request_stop(run_id, reason=reason)

    def validate(self, run_id: str) -> ValidationReport:
        return self.repository.validate(run_id)

    def result(
        self, run_id: str
    ) -> LanguageResult | GlossaryResult | BlocksResult:
        spec = self.repository.read_spec(run_id)
        snapshot = self.repository.inspect(run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED or snapshot.result_ref is None:
            raise TranslationServiceError(
                "result_unavailable", "run has no successful result"
            )
        store = ImmutableArtifactStore(
            self.repository.run_directory(run_id),
            repository_root=self.repository.root,
        )
        try:
            value = json.loads(
                store.read_bytes(snapshot.result_ref).decode("utf-8")
            )
            if not isinstance(value, Mapping):
                raise ValueError("result must be an object")
            if spec.handler == LANGUAGE_HANDLER:
                return LanguageResult.from_document(value)
            if spec.handler == GLOSSARY_HANDLER:
                return GlossaryResult.from_document(value)
            if spec.handler == BLOCKS_HANDLER:
                return BlocksResult.from_document(value)
            raise ValueError("unsupported run handler")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            TranslationWorkflowError,
        ) as exc:
            raise TranslationServiceError(
                "result_invalid", "run result artifact is invalid"
            ) from exc

    def result_source(self, run_id: str) -> ArtifactSourceRef:
        snapshot = self.repository.inspect(run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED or snapshot.result_ref is None:
            raise TranslationServiceError(
                "prerequisite_missing",
                "required translation step has no successful result",
            )
        return ArtifactSourceRef(
            run_id,
            snapshot.result_ref.artifact_id,
            snapshot.result_ref.digest,
        )

    def _handler(
        self,
        spec: RunSpec,
        *,
        execution: ExecutionOptions,
        task_service: Any | None,
        keyword_provider: KeywordProvider | None,
    ):
        try:
            if spec.handler == LANGUAGE_HANDLER:
                request, recipe = decode_language_semantic_input(
                    spec.semantic_input
                )
                return DetectLanguageHandler(
                    request,
                    recipe,
                    execution=execution,
                    task_service=task_service,
                )
            if spec.handler == GLOSSARY_HANDLER:
                request, recipe = decode_glossary_semantic_input(
                    spec.semantic_input
                )
                return BuildGlossaryHandler(
                    request,
                    recipe,
                    execution=execution,
                    task_service=task_service,
                    keyword_provider=keyword_provider,
                )
            if spec.handler == BLOCKS_HANDLER:
                request, recipe = decode_blocks_semantic_input(
                    spec.semantic_input
                )
                return TranslateBlocksHandler(
                    request,
                    recipe,
                    execution=execution,
                    task_service=task_service,
                )
        except ValueError as exc:
            raise TranslationServiceError(
                "run_spec_invalid", "translation run spec is invalid"
            ) from exc
        raise TranslationServiceError(
            "run_handler_invalid", "run is not an arc-translate step"
        )


def _run_id(prefix: str, semantic_input: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(semantic_input)).hexdigest()
    return f"translate-{prefix}-{digest[:24]}"


__all__ = ["TranslationService", "TranslationServiceError"]
