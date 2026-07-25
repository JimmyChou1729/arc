"""Semantic contracts for new Companion v2 build lineages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from arc_llm import ModelSelection
from arc_paper import (
    RichDocument,
    rich_document_from_document,
    rich_document_to_document,
)

from .prompts_v2 import (
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
    CHAPTER_PLAN_PROMPT_VERSION,
)
from .request_contracts_v1 import (
    COMPANION_CONTENT_CONTRACT,
    NEUTRAL_TEXTBOOK_INTENT,
    CompanionExecutionOptions,
)


COMPANION_BUILD_REQUEST_SCHEMA_V2 = "arc.companion.build_request.v2"
COMPANION_GENERATION_RECIPE_SCHEMA_V2 = (
    "arc.companion.generation_recipe.v3"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CompanionBuildRequestV2:
    """Stable v2 request for the split translation and guide lanes."""

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
            raise ValueError(
                "validator_digests must contain SHA-256 digests"
            )
        if (
            not isinstance(self.target_language, str)
            or not self.target_language.strip()
        ):
            raise ValueError(
                "target_language must be a non-empty language tag"
            )
        if not isinstance(self.user_intent, str):
            raise ValueError("user_intent must be a string")
        if self.content_contract != COMPANION_CONTENT_CONTRACT:
            raise ValueError("unsupported Companion content contract")
        object.__setattr__(self, "validator_digests", validators)
        object.__setattr__(
            self, "target_language", self.target_language.strip()
        )
        object.__setattr__(self, "user_intent", self.user_intent.strip())

    @property
    def effective_intent(self) -> str:
        return self.user_intent or NEUTRAL_TEXTBOOK_INTENT


@dataclass(frozen=True)
class CompanionGenerationRecipeV2:
    """Guide prompt identities plus approximate glossary size."""

    model: ModelSelection = field(default_factory=ModelSelection)
    approx_term_count: int = 50
    chapter_plan_prompt: str = CHAPTER_PLAN_PROMPT_VERSION
    chapter_guide_prompt: str = CHAPTER_GUIDE_PROMPT_VERSION
    chapter_guide_review_prompt: str = (
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION
    )

    def __post_init__(self) -> None:
        if not isinstance(self.model, ModelSelection):
            raise ValueError("model must be a ModelSelection")
        if (
            isinstance(self.approx_term_count, bool)
            or not isinstance(self.approx_term_count, int)
            or not 1 <= self.approx_term_count <= 200
        ):
            raise ValueError(
                "approx_term_count must be between 1 and 200"
            )
        expected = {
            "chapter_plan_prompt": CHAPTER_PLAN_PROMPT_VERSION,
            "chapter_guide_prompt": CHAPTER_GUIDE_PROMPT_VERSION,
            "chapter_guide_review_prompt": (
                CHAPTER_GUIDE_REVIEW_PROMPT_VERSION
            ),
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"unsupported {name} contract")


def encode_build_request_v2(
    request: CompanionBuildRequestV2,
) -> dict[str, Any]:
    return {
        "schema_version": COMPANION_BUILD_REQUEST_SCHEMA_V2,
        "source": rich_document_to_document(request.source),
        "validator_digests": list(request.validator_digests),
        "target_language": request.target_language,
        "user_intent": request.effective_intent,
        "content_contract": request.content_contract,
    }


def encode_generation_recipe_v2(
    recipe: CompanionGenerationRecipeV2,
) -> dict[str, Any]:
    return {
        "schema_version": COMPANION_GENERATION_RECIPE_SCHEMA_V2,
        "model": {
            "provider": recipe.model.provider,
            "model": recipe.model.model,
            "tier": recipe.model.tier,
        },
        "approx_term_count": recipe.approx_term_count,
        "chapter_plan_prompt": recipe.chapter_plan_prompt,
        "chapter_guide_prompt": recipe.chapter_guide_prompt,
        "chapter_guide_review_prompt": (
            recipe.chapter_guide_review_prompt
        ),
    }


def encode_handler_semantic_input_v2(
    request: CompanionBuildRequestV2,
    recipe: CompanionGenerationRecipeV2,
) -> dict[str, Any]:
    return {
        "request": encode_build_request_v2(request),
        "generation_recipe": encode_generation_recipe_v2(recipe),
    }


def decode_build_request_v2(
    document: Mapping[str, Any],
) -> CompanionBuildRequestV2:
    request = _exact(
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
    if request["schema_version"] != COMPANION_BUILD_REQUEST_SCHEMA_V2:
        raise ValueError("unsupported Companion v2 build-request schema")
    source = _mapping(request["source"], "rich source")
    validators = request["validator_digests"]
    if not isinstance(validators, list) or any(
        not isinstance(item, str) for item in validators
    ):
        raise ValueError("validator_digests must be an array of strings")
    return CompanionBuildRequestV2(
        source=rich_document_from_document(source),
        validator_digests=tuple(validators),
        target_language=_string(request, "target_language"),
        user_intent=_string(request, "user_intent"),
        content_contract=_string(request, "content_contract"),
    )


def decode_generation_recipe_v2(
    document: Mapping[str, Any],
) -> CompanionGenerationRecipeV2:
    raw_recipe = _exact(
        document,
        {
            "schema_version",
            "model",
            "approx_term_count",
            "chapter_plan_prompt",
            "chapter_guide_prompt",
            "chapter_guide_review_prompt",
        },
        "generation recipe",
    )
    if (
        raw_recipe["schema_version"]
        != COMPANION_GENERATION_RECIPE_SCHEMA_V2
    ):
        raise ValueError(
            "unsupported Companion v2 generation-recipe schema"
        )
    model = _exact(
        _mapping(raw_recipe["model"], "model"),
        {"provider", "model", "tier"},
        "model",
    )
    exact_model = model["model"]
    if exact_model is not None and not isinstance(exact_model, str):
        raise ValueError("model.model must be a string or null")
    return CompanionGenerationRecipeV2(
        model=ModelSelection(
            provider=_string(model, "provider"),
            model=exact_model,
            tier=_string(model, "tier"),  # type: ignore[arg-type]
        ),
        approx_term_count=_integer(raw_recipe, "approx_term_count"),
        chapter_plan_prompt=_string(
            raw_recipe, "chapter_plan_prompt"
        ),
        chapter_guide_prompt=_string(
            raw_recipe, "chapter_guide_prompt"
        ),
        chapter_guide_review_prompt=_string(
            raw_recipe, "chapter_guide_review_prompt"
        ),
    )


def decode_handler_semantic_input_v2(
    document: Mapping[str, Any],
) -> tuple[CompanionBuildRequestV2, CompanionGenerationRecipeV2]:
    value = _exact(
        document, {"request", "generation_recipe"}, "semantic input"
    )
    return (
        decode_build_request_v2(
            _mapping(value["request"], "build request")
        ),
        decode_generation_recipe_v2(
            _mapping(value["generation_recipe"], "generation recipe")
        ),
    )


def _exact(
    value: Mapping[str, Any], fields: set[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{description} has invalid fields")
    return dict(value)


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} must be an object")
    return value


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
    "COMPANION_BUILD_REQUEST_SCHEMA_V2",
    "COMPANION_GENERATION_RECIPE_SCHEMA_V2",
    "CompanionBuildRequestV2",
    "CompanionExecutionOptions",
    "CompanionGenerationRecipeV2",
    "decode_build_request_v2",
    "decode_generation_recipe_v2",
    "decode_handler_semantic_input_v2",
    "encode_build_request_v2",
    "encode_generation_recipe_v2",
    "encode_handler_semantic_input_v2",
]
