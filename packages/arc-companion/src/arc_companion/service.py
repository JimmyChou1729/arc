"""Public durable service for Companion build control and publications."""

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
)
from arc_llm import LLMTaskService
from arc_render import (
    Publication,
    PublicationWorkspaceState,
    read_publication_workspace_state,
)

from .build import (
    COMPANION_BUILD_HANDLER,
    CompanionBuildHandler,
    validate_build_diagnostics,
)
from .generation_validation import CompanionContentError
from .publication import (
    CompanionPublicationError,
    PublishedCompanion,
    load_published_companion,
    materialize_published_companion,
)
from .project import CompanionProjectPaths
from .publication_revisions import materialize_operator_revisions
from .request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    decode_handler_semantic_input,
    encode_handler_semantic_input,
)
from .translation_adapter import (
    CompanionTranslationAdapter,
    require_translation_runtime,
)


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
        request: CompanionBuildRequest,
        *,
        recipe: CompanionGenerationRecipe | None = None,
        execution: CompanionExecutionOptions = CompanionExecutionOptions(),
        run_id: str | None = None,
        task_service: LLMTaskService | None = None,
        translation_adapter: CompanionTranslationAdapter | None = None,
    ) -> RunSnapshot:
        if translation_adapter is None:
            require_translation_runtime()
        prepared = self.prepare(request, recipe=recipe, run_id=run_id)
        return self.execute(
            prepared.run_id,
            execution=execution,
            task_service=task_service,
            translation_adapter=translation_adapter,
        )

    def prepare(
        self,
        request: CompanionBuildRequest,
        *,
        recipe: CompanionGenerationRecipe | None = None,
        run_id: str | None = None,
    ) -> RunSnapshot:
        """Durably create one build before an external selector points to it."""

        resolved_recipe = _recipe_for_request(request, recipe)
        resolved = run_id or companion_run_id(request, recipe)
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

        if translation_adapter is None:
            require_translation_runtime()
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
        if translation_adapter is None:
            require_translation_runtime()
        spec = self.repository.read_working_spec(run_id)
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
    ) -> CompanionBuildHandler:
        if spec.handler == COMPANION_BUILD_HANDLER:
            request, recipe = decode_handler_semantic_input(
                spec.semantic_input
            )
            return CompanionBuildHandler(
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

    def build_diagnostics(
        self, run_id: str
    ) -> Mapping[str, Any] | None:
        """Return the immutable terminal source-preparation diagnostic."""

        self.repository.inspect(run_id)
        artifacts = ImmutableArtifactStore(
            self.repository.run_directory(run_id),
            repository_root=self.repository.root,
        )
        ref = artifacts.find("diagnostics/build")
        if ref is None:
            return None
        try:
            value = json.loads(
                artifacts.read_bytes(ref).decode("utf-8")
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise CompanionServiceError(
                "build_diagnostics_invalid",
                "run build diagnostics are unreadable",
            ) from exc
        if not isinstance(value, Mapping):
            raise CompanionServiceError(
                "build_diagnostics_invalid",
                "run build diagnostics are invalid",
            )
        try:
            validate_build_diagnostics(value)
        except (CompanionContentError, TypeError, ValueError) as exc:
            raise CompanionServiceError(
                "build_diagnostics_invalid",
                "run build diagnostics are invalid",
            ) from exc
        return dict(value)

    def stop(self, run_id: str, *, reason: str | None = None) -> RunView:
        return self.repository.request_stop(run_id, reason=reason)

    def published_companion(self, run_id: str) -> PublishedCompanion:
        snapshot = self.repository.inspect(run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED or snapshot.result_ref is None:
            raise CompanionServiceError(
                "publication_unavailable",
                "run has no accepted publication",
            )
        artifacts = ImmutableArtifactStore(
            self.repository.run_directory(run_id),
            repository_root=self.repository.root,
        )
        try:
            return load_published_companion(
                artifacts, snapshot.result_ref
            )
        except CompanionPublicationError as exc:
            raise CompanionServiceError(
                "publication_invalid",
                "run publication artifacts are invalid",
            ) from exc

    def publication(self, run_id: str) -> Publication:
        return self.published_companion(run_id).publication

    def materialize_publication(
        self,
        run_id: str,
        workspace: str | Path,
        *,
        project_paths: CompanionProjectPaths | None = None,
    ) -> Path:
        published = self.published_companion(run_id)
        artifacts = ImmutableArtifactStore(
            self.repository.run_directory(run_id),
            repository_root=self.repository.root,
        )
        try:
            publication_path = materialize_published_companion(
                artifacts, published, workspace
            )
            if project_paths is not None:
                materialize_operator_revisions(
                    project_paths, run_id, Path(workspace)
                )
            return publication_path
        except CompanionPublicationError as exc:
            raise CompanionServiceError(
                "publication_invalid",
                "run publication cannot be materialized",
            ) from exc

    def publication_workspace_state(
        self,
        run_id: str,
        workspace: str | Path,
        *,
        project_paths: CompanionProjectPaths | None = None,
    ) -> PublicationWorkspaceState:
        publication_path = self.materialize_publication(
            run_id,
            workspace,
            project_paths=project_paths,
        )
        return read_publication_workspace_state(publication_path)


def companion_run_id(
    request: CompanionBuildRequest,
    recipe: CompanionGenerationRecipe | None,
) -> str:
    resolved_recipe = _recipe_for_request(request, recipe)
    semantic_input = encode_handler_semantic_input(request, resolved_recipe)
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "handler": COMPANION_BUILD_HANDLER,
                "semantic_input": semantic_input,
            }
        )
    ).hexdigest()
    return f"companion-{digest[:24]}"


def _recipe_for_request(
    request: CompanionBuildRequest,
    recipe: CompanionGenerationRecipe | None,
) -> CompanionGenerationRecipe:
    if not isinstance(request, CompanionBuildRequest):
        raise ValueError("unsupported Companion build request")
    if recipe is None:
        return CompanionGenerationRecipe()
    if not isinstance(recipe, CompanionGenerationRecipe):
        raise ValueError("build request requires a Companion recipe")
    return recipe


__all__ = [
    "CompanionService",
    "CompanionServiceError",
    "companion_run_id",
]
