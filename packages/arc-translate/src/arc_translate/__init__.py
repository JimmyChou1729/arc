"""Durable scientific translation workflows."""

__version__ = "1.0.4"

from .contracts import (  # noqa: E402
    GLOSSARY_RESULT_SCHEMA,
    LANGUAGE_RESULT_SCHEMA,
    TRANSLATION_RESULT_SCHEMA,
    BlocksRequest,
    ExecutionOptions,
    GenerationRecipe,
    GlossaryRequest,
    LanguageRequest,
    TranslationSource,
)
from .handlers import (  # noqa: E402
    BLOCKS_HANDLER,
    GLOSSARY_HANDLER,
    LANGUAGE_HANDLER,
    BuildGlossaryHandler,
    DetectLanguageHandler,
    TranslateBlocksHandler,
)
from .project import (  # noqa: E402
    PROJECT_SCHEMA,
    TranslationProject,
    TranslationProjectError,
)
from .service import TranslationService, TranslationServiceError  # noqa: E402
from .source import (  # noqa: E402
    TranslationSourceError,
    deterministic_language_samples,
    resolve_translation_source,
    same_primary_language,
    source_blocks,
)
from .workflow import (  # noqa: E402
    REVIEW_SUPERVISION_SCHEMA,
    GlossaryResult,
    KeywordProvider,
    LanguageResult,
    TranslationWorkflowError,
    TranslationWorkflowService,
    TranslationResult,
    TranslationRevisionArtifact,
)

__all__ = [
    "BLOCKS_HANDLER",
    "GLOSSARY_HANDLER",
    "GLOSSARY_RESULT_SCHEMA",
    "LANGUAGE_HANDLER",
    "LANGUAGE_RESULT_SCHEMA",
    "TRANSLATION_RESULT_SCHEMA",
    "PROJECT_SCHEMA",
    "REVIEW_SUPERVISION_SCHEMA",
    "BlocksRequest",
    "BuildGlossaryHandler",
    "DetectLanguageHandler",
    "ExecutionOptions",
    "GenerationRecipe",
    "GlossaryRequest",
    "GlossaryResult",
    "KeywordProvider",
    "LanguageRequest",
    "LanguageResult",
    "TranslateBlocksHandler",
    "TranslationProject",
    "TranslationProjectError",
    "TranslationService",
    "TranslationServiceError",
    "TranslationSource",
    "TranslationSourceError",
    "TranslationResult",
    "TranslationRevisionArtifact",
    "TranslationWorkflowError",
    "TranslationWorkflowService",
    "deterministic_language_samples",
    "resolve_translation_source",
    "same_primary_language",
    "source_blocks",
]
