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
from .contracts import AcceptedBook, CompanionContentCodec
from .request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    decode_handler_semantic_input,
    encode_handler_semantic_input,
)


class CompanionServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CompanionService:
    """Build, resume, inspect, cancel, and load one Companion lineage."""

    def __init__(self, repository: RunRepository | str | Path) -> None:
        self.repository = (
            repository
            if isinstance(repository, RunRepository)
            else RunRepository(repository)
        )
        self.engine = RunEngine(self.repository)

    def build(
        self,
        request: CompanionBuildRequest,
        *,
        recipe: CompanionGenerationRecipe = CompanionGenerationRecipe(),
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        run_id: str | None = None,
        task_service: LLMTaskService | None = None,
    ) -> RunSnapshot:
        handler = CompanionBuildHandler(
            request,
            recipe,
            execution=execution,
            task_service=task_service,
        )
        resolved = run_id or companion_run_id(request, recipe)
        spec = RunSpec(resolved, handler.name, handler.semantic_input())
        self.repository.create(spec)
        return self.engine.execute(spec, handler)

    def resume(
        self,
        run_id: str,
        *,
        input: Mapping[str, Any] | None = None,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        task_service: LLMTaskService | None = None,
    ) -> RunSnapshot:
        spec = self.repository.read_spec(run_id)
        if spec.handler != COMPANION_BUILD_HANDLER:
            raise CompanionServiceError(
                "run_handler_invalid", "run is not a Companion build"
            )
        request, recipe = decode_handler_semantic_input(spec.semantic_input)
        handler = CompanionBuildHandler(
            request,
            recipe,
            execution=execution,
            task_service=task_service,
        )
        return self.engine.resume(run_id, handler, input=input)

    def inspect(self, run_id: str) -> RunView:
        return self.repository.inspect(run_id)

    def cancel(self, run_id: str, *, reason: str | None = None) -> RunView:
        return self.repository.request_cancel(run_id, reason=reason)

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
    request: CompanionBuildRequest,
    recipe: CompanionGenerationRecipe,
) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(encode_handler_semantic_input(request, recipe))
    ).hexdigest()
    return f"companion-{digest[:24]}"


__all__ = [
    "CompanionService",
    "CompanionServiceError",
    "companion_run_id",
]
