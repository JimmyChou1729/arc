"""Durable scientific translation workflows."""

__version__ = "1.0.1"

from .contracts import (  # noqa: E402
    BLOCKS_RESULT_SCHEMA,
    GLOSSARY_RESULT_SCHEMA,
    LANGUAGE_RESULT_SCHEMA,
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
    BlocksResult,
    GlossaryResult,
    KeywordProvider,
    LanguageResult,
    TranslationWorkflowError,
    TranslationWorkflowService,
)

__all__ = [
    "BLOCKS_HANDLER",
    "BLOCKS_RESULT_SCHEMA",
    "GLOSSARY_HANDLER",
    "GLOSSARY_RESULT_SCHEMA",
    "LANGUAGE_HANDLER",
    "LANGUAGE_RESULT_SCHEMA",
    "PROJECT_SCHEMA",
    "REVIEW_SUPERVISION_SCHEMA",
    "BlocksRequest",
    "BlocksResult",
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
    "TranslationWorkflowError",
    "TranslationWorkflowService",
    "deterministic_language_samples",
    "resolve_translation_source",
    "same_primary_language",
    "source_blocks",
]
