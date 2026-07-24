"""Source-anchored textbook companions from ARC rich documents."""

from typing import Any

from .contracts import (
    ACCEPTED_BOOK_SCHEMA,
    AcceptedBook,
    AcceptedChapter,
    ChapterPlan,
    CompanionContentCodec,
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
    "GlossaryEntry",
    "LearningUnit",
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
