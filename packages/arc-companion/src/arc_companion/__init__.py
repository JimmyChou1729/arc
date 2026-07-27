"""Source-anchored textbook companions from ARC rich documents."""

from typing import Any

from .contracts import (
    ACCEPTED_BOOK_SCHEMA,
    AcceptedBook,
    AcceptedChapter,
    ChapterPlan,
    CompanionContentCodec,
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
from .standalone_html import (
    StandaloneHtmlError,
    standalone_html_bytes,
    write_standalone_html,
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

    if name in {
        "COMPANION_BUILD_DIAGNOSTICS_SCHEMA",
        "COMPANION_BUILD_HANDLER",
        "CompanionBuildHandler",
    }:
        from .build import (
            COMPANION_BUILD_DIAGNOSTICS_SCHEMA,
            COMPANION_BUILD_HANDLER,
            CompanionBuildHandler,
        )

        return {
            "COMPANION_BUILD_DIAGNOSTICS_SCHEMA": (
                COMPANION_BUILD_DIAGNOSTICS_SCHEMA
            ),
            "COMPANION_BUILD_HANDLER": COMPANION_BUILD_HANDLER,
            "CompanionBuildHandler": CompanionBuildHandler,
        }[name]
    if name in {
        "COMPANION_CONTENT_CONTRACT",
        "NEUTRAL_TEXTBOOK_INTENT",
        "CompanionExecutionOptions",
    }:
        from . import request_contracts

        return getattr(request_contracts, name)
    if name in {"CompanionBuildRequest", "CompanionGenerationRecipe"}:
        from .request_contracts import (
            CompanionBuildRequest,
            CompanionGenerationRecipe,
        )

        return {
            "CompanionBuildRequest": CompanionBuildRequest,
            "CompanionGenerationRecipe": CompanionGenerationRecipe,
        }[name]
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
    if name in {
        "TranslationReuseError",
        "TranslationReusePlan",
        "TranslationReuseReceipt",
        "TranslationReuseSource",
    }:
        from . import translation_reuse

        return getattr(translation_reuse, name)
    raise AttributeError(name)

__all__ = [
    "ACCEPTED_BOOK_SCHEMA",
    "COMPANION_BUILD_DIAGNOSTICS_SCHEMA",
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
    "CompanionTranslationRuntimeError",
    "EvidenceSource",
    "GlossaryEntry",
    "LearningUnit",
    "PlannedLearningUnit",
    "RenderedCompanion",
    "SourceAnchor",
    "StandaloneHtmlError",
    "TranslatedBlock",
    "TranslationReuseError",
    "TranslationReusePlan",
    "TranslationReuseReceipt",
    "TranslationReuseSource",
    "ValidationIssue",
    "companion_run_id",
    "release_id_for",
    "require_valid_accepted_book",
    "validate_accepted_book",
    "standalone_html_bytes",
    "write_standalone_html",
]
