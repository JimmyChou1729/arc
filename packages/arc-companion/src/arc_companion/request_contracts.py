"""Current Companion request contracts with explicit legacy access."""

from .request_contracts_v1 import (
    COMPANION_CONTENT_CONTRACT,
    DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES,
    NEUTRAL_TEXTBOOK_INTENT,
    CompanionBuildRequest as LegacyCompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe as LegacyCompanionGenerationRecipe,
    decode_handler_semantic_input as decode_handler_semantic_input_v1,
    encode_handler_semantic_input as encode_handler_semantic_input_v1,
)
from .request_contracts_v2 import (
    COMPANION_BUILD_REQUEST_SCHEMA_V2 as COMPANION_BUILD_REQUEST_SCHEMA,
    COMPANION_GENERATION_RECIPE_SCHEMA_V2 as COMPANION_GENERATION_RECIPE_SCHEMA,
    CompanionBuildRequestV2 as CompanionBuildRequest,
    CompanionGenerationRecipeV2 as CompanionGenerationRecipe,
    decode_build_request_v2 as decode_build_request,
    decode_generation_recipe_v2 as decode_generation_recipe,
    decode_handler_semantic_input_v2 as decode_handler_semantic_input,
    encode_build_request_v2 as encode_build_request,
    encode_generation_recipe_v2 as encode_generation_recipe,
    encode_handler_semantic_input_v2 as encode_handler_semantic_input,
)


__all__ = [
    "COMPANION_BUILD_REQUEST_SCHEMA",
    "COMPANION_CONTENT_CONTRACT",
    "COMPANION_GENERATION_RECIPE_SCHEMA",
    "DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES",
    "NEUTRAL_TEXTBOOK_INTENT",
    "CompanionBuildRequest",
    "CompanionExecutionOptions",
    "CompanionGenerationRecipe",
    "LegacyCompanionBuildRequest",
    "LegacyCompanionGenerationRecipe",
    "decode_build_request",
    "decode_generation_recipe",
    "decode_handler_semantic_input",
    "decode_handler_semantic_input_v1",
    "encode_handler_semantic_input",
    "encode_build_request",
    "encode_generation_recipe",
    "encode_handler_semantic_input_v1",
]
