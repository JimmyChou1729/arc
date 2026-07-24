"""Semantic, generation, and operational contracts for Companion builds."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from arc_llm import LLMExecutionOptions, ModelSelection
from arc_paper import RichDocument, rich_document_from_document, rich_document_to_document

from .prompts_v1 import (
    CHAPTER_DRAFT_PROMPT_VERSION,
    CHAPTER_PLAN_PROMPT_VERSION,
    CHAPTER_REVIEW_PROMPT_VERSION,
    GLOSSARY_PROMPT_VERSION,
    LANGUAGE_PROMPT_VERSION,
    TRANSLATION_PROMPT_VERSION,
)


COMPANION_BUILD_REQUEST_SCHEMA = "arc.companion.build_request.v1"
COMPANION_GENERATION_RECIPE_SCHEMA = "arc.companion.generation_recipe.v2"
COMPANION_CONTENT_CONTRACT = "arc.companion.source_anchored_textbook.v1"
DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES = 32_000
NEUTRAL_TEXTBOOK_INTENT = (
    "Explain the source faithfully as a neutral textbook companion for an "
    "engaged reader, adding selective help only where it improves understanding."
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CompanionBuildRequest:
    """Stable content request; physical paths and runtime controls are excluded."""

    source: RichDocument
    validator_digests: tuple[str, ...] = ()
    target_language: str = "zh-CN"
    user_intent: str = ""
    content_contract: str = COMPANION_CONTENT_CONTRACT

    def __post_init__(self) -> None:
        if not isinstance(self.source, RichDocument):
            raise ValueError("source must be a RichDocument")
        validators = tuple(sorted(set(self.validator_digests)))
        if any(_SHA256.fullmatch(item) is None for item in validators):
            raise ValueError("validator_digests must contain SHA-256 digests")
        if not isinstance(self.target_language, str) or not self.target_language.strip():
            raise ValueError("target_language must be a non-empty language tag")
        if not isinstance(self.user_intent, str):
            raise ValueError("user_intent must be a string")
        if self.content_contract != COMPANION_CONTENT_CONTRACT:
            raise ValueError("unsupported Companion content contract")
        object.__setattr__(self, "validator_digests", validators)
        object.__setattr__(self, "target_language", self.target_language.strip())
        object.__setattr__(self, "user_intent", self.user_intent.strip())

    @property
    def effective_intent(self) -> str:
        return self.user_intent or NEUTRAL_TEXTBOOK_INTENT


@dataclass(frozen=True)
class CompanionGenerationRecipe:
    """Prompt contract plus the caller's explicit model/provider requirement."""

    model: ModelSelection = field(default_factory=ModelSelection)
    language_prompt: str = LANGUAGE_PROMPT_VERSION
    chapter_plan_prompt: str = CHAPTER_PLAN_PROMPT_VERSION
    glossary_prompt: str = GLOSSARY_PROMPT_VERSION
    translation_prompt: str = TRANSLATION_PROMPT_VERSION
    translation_input_budget_bytes: int = DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES
    chapter_draft_prompt: str = CHAPTER_DRAFT_PROMPT_VERSION
    chapter_review_prompt: str = CHAPTER_REVIEW_PROMPT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.model, ModelSelection):
            raise ValueError("model must be a ModelSelection")
        if (
            isinstance(self.translation_input_budget_bytes, bool)
            or not isinstance(self.translation_input_budget_bytes, int)
            or not 4_096 <= self.translation_input_budget_bytes <= 1_000_000
        ):
            raise ValueError(
                "translation_input_budget_bytes must be between 4096 and 1000000"
            )
        expected = {
            "language_prompt": LANGUAGE_PROMPT_VERSION,
            "chapter_plan_prompt": CHAPTER_PLAN_PROMPT_VERSION,
            "glossary_prompt": GLOSSARY_PROMPT_VERSION,
            "translation_prompt": TRANSLATION_PROMPT_VERSION,
            "chapter_draft_prompt": CHAPTER_DRAFT_PROMPT_VERSION,
            "chapter_review_prompt": CHAPTER_REVIEW_PROMPT_VERSION,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"unsupported {name} contract")


@dataclass(frozen=True)
class CompanionExecutionOptions:
    """Non-semantic runtime policy for one invocation."""

    workers: int = 4
    llm: LLMExecutionOptions = field(default_factory=LLMExecutionOptions)
    cache_root: Path | None = None

    def __post_init__(self) -> None:
        if isinstance(self.workers, bool) or not isinstance(self.workers, int):
            raise ValueError("workers must be an integer")
        if not 1 <= self.workers <= 24:
            raise ValueError("workers must be between 1 and 24")
        if not isinstance(self.llm, LLMExecutionOptions):
            raise ValueError("llm must be LLMExecutionOptions")
        if self.cache_root is not None:
            object.__setattr__(self, "cache_root", Path(self.cache_root))


def encode_build_request(request: CompanionBuildRequest) -> dict[str, Any]:
    return {
        "schema_version": COMPANION_BUILD_REQUEST_SCHEMA,
        "source": rich_document_to_document(request.source),
        "validator_digests": list(request.validator_digests),
        "target_language": request.target_language,
        "user_intent": request.effective_intent,
        "content_contract": request.content_contract,
    }


def decode_build_request(document: Mapping[str, Any]) -> CompanionBuildRequest:
    value = _exact(
        document,
        {
            "schema_version",
            "source",
            "validator_digests",
            "target_language",
            "user_intent",
            "content_contract",
        },
        "build request",
    )
    if value["schema_version"] != COMPANION_BUILD_REQUEST_SCHEMA:
        raise ValueError("unsupported Companion build-request schema")
    source = value["source"]
    validators = value["validator_digests"]
    if not isinstance(source, Mapping):
        raise ValueError("build request source must be an object")
    if not isinstance(validators, list) or any(
        not isinstance(item, str) for item in validators
    ):
        raise ValueError("validator_digests must be an array of strings")
    return CompanionBuildRequest(
        source=rich_document_from_document(source),
        validator_digests=tuple(validators),
        target_language=_string(value, "target_language"),
        user_intent=_string(value, "user_intent"),
        content_contract=_string(value, "content_contract"),
    )


def encode_generation_recipe(
    recipe: CompanionGenerationRecipe,
) -> dict[str, Any]:
    return {
        "schema_version": COMPANION_GENERATION_RECIPE_SCHEMA,
        "model": {
            "provider": recipe.model.provider,
            "model": recipe.model.model,
            "tier": recipe.model.tier,
        },
        "language_prompt": recipe.language_prompt,
        "chapter_plan_prompt": recipe.chapter_plan_prompt,
        "glossary_prompt": recipe.glossary_prompt,
        "translation_prompt": recipe.translation_prompt,
        "translation_input_budget_bytes": recipe.translation_input_budget_bytes,
        "chapter_draft_prompt": recipe.chapter_draft_prompt,
        "chapter_review_prompt": recipe.chapter_review_prompt,
    }


def decode_generation_recipe(
    document: Mapping[str, Any],
) -> CompanionGenerationRecipe:
    value = _exact(
        document,
        {
            "schema_version",
            "model",
            "language_prompt",
            "chapter_plan_prompt",
            "glossary_prompt",
            "translation_prompt",
            "translation_input_budget_bytes",
            "chapter_draft_prompt",
            "chapter_review_prompt",
        },
        "generation recipe",
    )
    if value["schema_version"] != COMPANION_GENERATION_RECIPE_SCHEMA:
        raise ValueError("unsupported Companion generation-recipe schema")
    raw_model = value["model"]
    if not isinstance(raw_model, Mapping):
        raise ValueError("generation recipe model must be an object")
    model = _exact(raw_model, {"provider", "model", "tier"}, "model")
    exact_model = model["model"]
    if exact_model is not None and not isinstance(exact_model, str):
        raise ValueError("model.model must be a string or null")
    return CompanionGenerationRecipe(
        model=ModelSelection(
            provider=_string(model, "provider"),
            model=exact_model,
            tier=_string(model, "tier"),  # type: ignore[arg-type]
        ),
        language_prompt=_string(value, "language_prompt"),
        chapter_plan_prompt=_string(value, "chapter_plan_prompt"),
        glossary_prompt=_string(value, "glossary_prompt"),
        translation_prompt=_string(value, "translation_prompt"),
        translation_input_budget_bytes=_integer(
            value, "translation_input_budget_bytes"
        ),
        chapter_draft_prompt=_string(value, "chapter_draft_prompt"),
        chapter_review_prompt=_string(value, "chapter_review_prompt"),
    )


def encode_handler_semantic_input(
    request: CompanionBuildRequest,
    recipe: CompanionGenerationRecipe,
) -> dict[str, Any]:
    return {
        "request": encode_build_request(request),
        "generation_recipe": encode_generation_recipe(recipe),
    }


def decode_handler_semantic_input(
    document: Mapping[str, Any],
) -> tuple[CompanionBuildRequest, CompanionGenerationRecipe]:
    value = _exact(document, {"request", "generation_recipe"}, "semantic input")
    request = value["request"]
    recipe = value["generation_recipe"]
    if not isinstance(request, Mapping) or not isinstance(recipe, Mapping):
        raise ValueError("semantic request and generation recipe must be objects")
    return decode_build_request(request), decode_generation_recipe(recipe)


def _exact(
    value: Mapping[str, Any], fields: set[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{description} has invalid fields")
    return dict(value)


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


__all__ = [
    "COMPANION_BUILD_REQUEST_SCHEMA",
    "COMPANION_CONTENT_CONTRACT",
    "COMPANION_GENERATION_RECIPE_SCHEMA",
    "DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES",
    "NEUTRAL_TEXTBOOK_INTENT",
    "CompanionBuildRequest",
    "CompanionExecutionOptions",
    "CompanionGenerationRecipe",
    "decode_build_request",
    "decode_generation_recipe",
    "decode_handler_semantic_input",
    "encode_build_request",
    "encode_generation_recipe",
    "encode_handler_semantic_input",
]
