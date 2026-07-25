"""Source-anchored textbook companions from ARC rich documents."""

from typing import Any

from .contracts import (
    ACCEPTED_BOOK_SCHEMA,
    AcceptedBook,
    AcceptedChapter,
    ChapterPlan,
    CompanionContentCodec,
    EvidenceRequest,
    EvidenceSource,
    GlossaryEntry,
    LearningUnit,
    PlannedLearningUnit,
    SourceAnchor,
    TranslatedBlock,
)
from .project import CompanionProjectError, CompanionProjectPaths
from .release import (
    CompanionRelease,
    CompanionReleasePublisher,
    release_id_for,
)
from .renderer import (
    CompanionRenderError,
    CompanionRenderer,
    RenderedCompanion,
)
from .validation import (
    AcceptedBookValidationError,
    ValidationIssue,
    require_valid_accepted_book,
    validate_accepted_book,
)

__version__ = "1.0.1"


def __getattr__(name: str) -> Any:
    """Keep render-only imports independent of the LLM runtime."""

    if name in {"COMPANION_BUILD_HANDLER", "CompanionBuildHandler"}:
        from .build_v2 import (
            COMPANION_BUILD_HANDLER_V2,
            CompanionBuildHandlerV2,
        )

        return {
            "COMPANION_BUILD_HANDLER": COMPANION_BUILD_HANDLER_V2,
            "CompanionBuildHandler": CompanionBuildHandlerV2,
        }[name]
    if name in {
        "LEGACY_COMPANION_BUILD_HANDLER",
        "LegacyCompanionBuildHandler",
    }:
        from .build import COMPANION_BUILD_HANDLER, CompanionBuildHandler

        return {
            "LEGACY_COMPANION_BUILD_HANDLER": COMPANION_BUILD_HANDLER,
            "LegacyCompanionBuildHandler": CompanionBuildHandler,
        }[name]
    if name in {
        "COMPANION_CONTENT_CONTRACT",
        "NEUTRAL_TEXTBOOK_INTENT",
        "CompanionExecutionOptions",
    }:
        from . import request_contracts

        return getattr(request_contracts, name)
    if name in {"CompanionBuildRequest", "CompanionGenerationRecipe"}:
        from .request_contracts_v2 import (
            CompanionBuildRequestV2,
            CompanionGenerationRecipeV2,
        )

        return {
            "CompanionBuildRequest": CompanionBuildRequestV2,
            "CompanionGenerationRecipe": CompanionGenerationRecipeV2,
        }[name]
    if name in {
        "LegacyCompanionBuildRequest",
        "LegacyCompanionGenerationRecipe",
    }:
        from .request_contracts_v1 import (
            CompanionBuildRequest,
            CompanionGenerationRecipe,
        )

        return {
            "LegacyCompanionBuildRequest": CompanionBuildRequest,
            "LegacyCompanionGenerationRecipe": CompanionGenerationRecipe,
        }[name]
    if name in {
        "CompanionService",
        "CompanionServiceError",
        "companion_run_id",
    }:
        from . import service

        return getattr(service, name)
    raise AttributeError(name)

__all__ = [
    "ACCEPTED_BOOK_SCHEMA",
    "COMPANION_BUILD_HANDLER",
    "COMPANION_CONTENT_CONTRACT",
    "NEUTRAL_TEXTBOOK_INTENT",
    "AcceptedBook",
    "AcceptedBookValidationError",
    "AcceptedChapter",
    "ChapterPlan",
    "CompanionBuildHandler",
    "CompanionBuildRequest",
    "CompanionContentCodec",
    "CompanionExecutionOptions",
    "CompanionGenerationRecipe",
    "CompanionProjectError",
    "CompanionProjectPaths",
    "CompanionRelease",
    "CompanionReleasePublisher",
    "CompanionRenderError",
    "CompanionRenderer",
    "CompanionService",
    "CompanionServiceError",
    "EvidenceRequest",
    "EvidenceSource",
    "GlossaryEntry",
    "LearningUnit",
    "LEGACY_COMPANION_BUILD_HANDLER",
    "LegacyCompanionBuildHandler",
    "LegacyCompanionBuildRequest",
    "LegacyCompanionGenerationRecipe",
    "PlannedLearningUnit",
    "RenderedCompanion",
    "SourceAnchor",
    "TranslatedBlock",
    "ValidationIssue",
    "companion_run_id",
    "release_id_for",
    "require_valid_accepted_book",
    "validate_accepted_book",
]
