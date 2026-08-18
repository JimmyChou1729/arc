"""Source-anchored Companion generation for ARC Render publications."""

from typing import Any

from .project import CompanionProjectError, CompanionProjectPaths
from .publication import (
    BUILD_RESULT_SCHEMA,
    SUPPLEMENT_COVERAGE_SCHEMA,
    CompanionPublicationError,
    PublishedCompanion,
    materialize_published_companion,
)

__version__ = "1.0.4"


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
    if name in {
        "REVIEWED_COMPANION_SUPPLEMENT_SCHEMA",
        "ReviewedCompanionSupplement",
        "ReviewedOwnedResource",
        "ReviewedSourceDraft",
        "ReviewedSourceUnit",
        "ReviewedSupplementEntry",
        "decode_reviewed_companion_supplement",
        "encode_reviewed_companion_supplement",
        "reviewed_anchor_fingerprint",
        "reviewed_source_inventory_digest",
        "validate_reviewed_companion_supplement",
    }:
        from . import reviewed_supplements

        return getattr(reviewed_supplements, name)
    if name in {
        "PUBLICATION_REVISION_BUNDLE_SCHEMA",
        "PUBLICATION_REVISION_REQUEST_SCHEMA",
        "PUBLICATION_REVISION_RESULT_SCHEMA",
        "CompanionFragmentReplacement",
        "CompanionPublicationRevisionError",
        "CompanionPublicationRevisionRequest",
        "CompanionPublicationRevisionResult",
        "decode_publication_revision_request",
        "encode_publication_revision_request",
        "encode_publication_revision_result",
    }:
        from . import publication_revisions

        return getattr(publication_revisions, name)
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
    "CompanionFragmentReplacement",
    "CompanionProjectError",
    "CompanionProjectPaths",
    "CompanionPublicationError",
    "CompanionPublicationRevisionError",
    "CompanionPublicationRevisionRequest",
    "CompanionPublicationRevisionResult",
    "CompanionService",
    "CompanionServiceError",
    "CompanionTranslationRuntimeError",
    "PublishedCompanion",
    "PUBLICATION_REVISION_BUNDLE_SCHEMA",
    "PUBLICATION_REVISION_REQUEST_SCHEMA",
    "PUBLICATION_REVISION_RESULT_SCHEMA",
    "REVIEWED_COMPANION_SUPPLEMENT_SCHEMA",
    "SUPPLEMENT_COVERAGE_SCHEMA",
    "ReviewedCompanionSupplement",
    "ReviewedOwnedResource",
    "ReviewedSourceDraft",
    "ReviewedSourceUnit",
    "ReviewedSupplementEntry",
    "companion_run_id",
    "decode_reviewed_companion_supplement",
    "decode_publication_revision_request",
    "encode_publication_revision_request",
    "encode_publication_revision_result",
    "encode_reviewed_companion_supplement",
    "materialize_published_companion",
    "reviewed_anchor_fingerprint",
    "reviewed_source_inventory_digest",
    "validate_reviewed_companion_supplement",
]
