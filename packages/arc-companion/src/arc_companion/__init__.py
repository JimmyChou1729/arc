"""Source-anchored Companion generation for ARC Render publications."""

from typing import Any

from .project import CompanionProjectError, CompanionProjectPaths
from .publication import (
    BUILD_RESULT_SCHEMA,
    CompanionPublicationError,
    PublishedCompanion,
    materialize_published_companion,
)

__version__ = "1.0.3"


def __getattr__(name: str) -> Any:
    """Load LLM-dependent build APIs only when requested."""

    if name in {
        "COMPANION_BUILD_DIAGNOSTICS_SCHEMA",
        "COMPANION_BUILD_HANDLER",
        "CompanionBuildHandler",
    }:
        from . import build

        return getattr(build, name)
    if name in {
        "COMPANION_CONTENT_CONTRACT",
        "NEUTRAL_TEXTBOOK_INTENT",
        "CompanionBuildRequest",
        "CompanionExecutionOptions",
        "CompanionGenerationRecipe",
    }:
        from . import request_contracts

        return getattr(request_contracts, name)
    if name in {
        "CompanionService",
        "CompanionServiceError",
        "companion_run_id",
    }:
        from . import service

        return getattr(service, name)
    if name == "CompanionTranslationRuntimeError":
        from .translation_adapter import CompanionTranslationRuntimeError

        return CompanionTranslationRuntimeError
    raise AttributeError(name)


__all__ = [
    "BUILD_RESULT_SCHEMA",
    "COMPANION_BUILD_DIAGNOSTICS_SCHEMA",
    "COMPANION_BUILD_HANDLER",
    "COMPANION_CONTENT_CONTRACT",
    "NEUTRAL_TEXTBOOK_INTENT",
    "CompanionBuildHandler",
    "CompanionBuildRequest",
    "CompanionExecutionOptions",
    "CompanionGenerationRecipe",
    "CompanionProjectError",
    "CompanionProjectPaths",
    "CompanionPublicationError",
    "CompanionService",
    "CompanionServiceError",
    "CompanionTranslationRuntimeError",
    "PublishedCompanion",
    "companion_run_id",
    "materialize_published_companion",
]
