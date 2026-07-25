"""Public durable service for Companion build control and accepted content."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arc_jobs import (
    ImmutableArtifactStore,
    RunEngine,
    RunRepository,
    RunSnapshot,
    RunSpec,
    RunStatus,
    RunView,
    canonical_json_bytes,
    decode_artifact_ref,
)
from arc_llm import LLMTaskService

from .build import COMPANION_BUILD_HANDLER, CompanionBuildHandler
from .build_v2 import (
    COMPANION_BUILD_HANDLER_V2,
    CompanionBuildHandlerV2,
)
from .contracts import AcceptedBook, CompanionContentCodec
from .request_contracts_v1 import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    decode_handler_semantic_input,
    encode_handler_semantic_input,
)
from .request_contracts_v2 import (
    CompanionBuildRequestV2,
    CompanionGenerationRecipeV2,
    decode_handler_semantic_input_v2,
    encode_handler_semantic_input_v2,
)
from .translation_adapter import CompanionTranslationAdapter


class CompanionServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CompanionService:
    """Build, resume, inspect, stop, and load one Companion lineage."""

    def __init__(self, repository: RunRepository | str | Path) -> None:
        self.repository = (
            repository
            if isinstance(repository, RunRepository)
            else RunRepository(repository)
        )
        self.engine = RunEngine(self.repository)

    def build(
        self,
        request: CompanionBuildRequest | CompanionBuildRequestV2,
        *,
        recipe: (
            CompanionGenerationRecipe | CompanionGenerationRecipeV2 | None
        ) = None,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        run_id: str | None = None,
        task_service: LLMTaskService | None = None,
        translation_adapter: CompanionTranslationAdapter | None = None,
    ) -> RunSnapshot:
        prepared = self.prepare(request, recipe=recipe, run_id=run_id)
        return self.execute(
            prepared.run_id,
            execution=execution,
            task_service=task_service,
            translation_adapter=translation_adapter,
        )

    def prepare(
        self,
        request: CompanionBuildRequest | CompanionBuildRequestV2,
        *,
        recipe: (
            CompanionGenerationRecipe | CompanionGenerationRecipeV2 | None
        ) = None,
        run_id: str | None = None,
    ) -> RunSnapshot:
        """Durably create one build before an external selector points to it."""

        resolved_recipe = _recipe_for_request(request, recipe)
        resolved = run_id or companion_run_id(request, recipe)
        if isinstance(request, CompanionBuildRequestV2):
            assert isinstance(resolved_recipe, CompanionGenerationRecipeV2)
            spec = RunSpec(
                resolved,
                COMPANION_BUILD_HANDLER_V2,
                encode_handler_semantic_input_v2(
                    request, resolved_recipe
                ),
            )
        else:
            assert isinstance(resolved_recipe, CompanionGenerationRecipe)
            spec = RunSpec(
                resolved,
                COMPANION_BUILD_HANDLER,
                encode_handler_semantic_input(request, resolved_recipe),
            )
        return self.repository.create(spec)

    def execute(
        self,
        run_id: str,
        *,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        task_service: LLMTaskService | None = None,
        translation_adapter: CompanionTranslationAdapter | None = None,
    ) -> RunSnapshot:
        """Execute or replay one already prepared Companion build."""

        spec = self.repository.read_spec(run_id)
        handler = self._handler(
            spec,
            execution=execution,
            task_service=task_service,
            translation_adapter=translation_adapter,
        )
        return self.engine.execute(spec, handler)

    def resume(
        self,
        run_id: str,
        *,
        input: Mapping[str, Any] | None = None,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        task_service: LLMTaskService | None = None,
        translation_adapter: CompanionTranslationAdapter | None = None,
    ) -> RunSnapshot:
        spec = self.repository.read_spec(run_id)
        handler = self._handler(
            spec,
            execution=execution,
            task_service=task_service,
            translation_adapter=translation_adapter,
        )
        return self.engine.resume(run_id, handler, input=input)

    def _handler(
        self,
        spec: RunSpec,
        *,
        execution: CompanionExecutionOptions,
        task_service: LLMTaskService | None,
        translation_adapter: CompanionTranslationAdapter | None,
    ) -> CompanionBuildHandler | CompanionBuildHandlerV2:
        if spec.handler == COMPANION_BUILD_HANDLER:
            request, recipe = decode_handler_semantic_input(
                spec.semantic_input
            )
            return CompanionBuildHandler(
                request,
                recipe,
                execution=execution,
                task_service=task_service,
            )
        if spec.handler == COMPANION_BUILD_HANDLER_V2:
            request, recipe = decode_handler_semantic_input_v2(
                spec.semantic_input
            )
            return CompanionBuildHandlerV2(
                request,
                recipe,
                execution=execution,
                task_service=task_service,
                translation_adapter=translation_adapter,
            )
        raise CompanionServiceError(
            "run_handler_invalid", "run is not a Companion build"
        )

    def inspect(self, run_id: str) -> RunView:
        return self.repository.inspect(run_id)

    def stop(self, run_id: str, *, reason: str | None = None) -> RunView:
        return self.repository.request_stop(run_id, reason=reason)

    def accepted_book(self, run_id: str) -> AcceptedBook:
        snapshot = self.repository.inspect(run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED or snapshot.result_ref is None:
            raise CompanionServiceError(
                "accepted_book_unavailable",
                "run has no accepted book",
            )
        artifacts = ImmutableArtifactStore(
            self.repository.run_directory(run_id),
            repository_root=self.repository.root,
        )
        try:
            result = json.loads(
                artifacts.read_bytes(snapshot.result_ref).decode("utf-8")
            )
            if not isinstance(result, Mapping) or set(result) != {
                "schema_version",
                "accepted_book",
            }:
                raise ValueError("invalid result fields")
            if result["schema_version"] != "arc.companion.build_result.v1":
                raise ValueError("unsupported result schema")
            raw_ref = result["accepted_book"]
            if not isinstance(raw_ref, Mapping):
                raise ValueError("accepted_book ref must be an object")
            book_ref = decode_artifact_ref(raw_ref)
            return CompanionContentCodec.loads(artifacts.read_bytes(book_ref))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise CompanionServiceError(
                "accepted_book_invalid",
                "run accepted-book artifact is invalid",
            ) from exc


def companion_run_id(
    request: CompanionBuildRequest | CompanionBuildRequestV2,
    recipe: CompanionGenerationRecipe | CompanionGenerationRecipeV2 | None,
) -> str:
    resolved_recipe = _recipe_for_request(request, recipe)
    semantic_input = (
        encode_handler_semantic_input_v2(request, resolved_recipe)
        if isinstance(request, CompanionBuildRequestV2)
        else encode_handler_semantic_input(request, resolved_recipe)
    )
    digest = hashlib.sha256(
        canonical_json_bytes(semantic_input)
    ).hexdigest()
    return f"companion-{digest[:24]}"


def _recipe_for_request(
    request: CompanionBuildRequest | CompanionBuildRequestV2,
    recipe: CompanionGenerationRecipe | CompanionGenerationRecipeV2 | None,
) -> CompanionGenerationRecipe | CompanionGenerationRecipeV2:
    if isinstance(request, CompanionBuildRequestV2):
        if recipe is None:
            return CompanionGenerationRecipeV2()
        if not isinstance(recipe, CompanionGenerationRecipeV2):
            raise ValueError("v2 build request requires a v2 recipe")
        return recipe
    if not isinstance(request, CompanionBuildRequest):
        raise ValueError("unsupported Companion build request")
    if recipe is None:
        return CompanionGenerationRecipe()
    if not isinstance(recipe, CompanionGenerationRecipe):
        raise ValueError("v1 build request requires a v1 recipe")
    return recipe


__all__ = [
    "CompanionService",
    "CompanionServiceError",
    "companion_run_id",
]
