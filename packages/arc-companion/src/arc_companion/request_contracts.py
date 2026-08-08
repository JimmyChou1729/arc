"""Semantic and execution contracts for current Companion builds."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from arc_llm import LLMExecutionOptions, ModelSelection
from arc_paper import (
    CachedDocumentStructureRef,
    EQUATION_LABEL_VISUAL_PROMPT_VERSION,
    RichDocument,
    cached_document_structure_ref_from_document,
    cached_document_structure_ref_to_document,
    rich_document_from_document,
    rich_document_to_document,
)

from .prompts import (
    AUTHOR_IDENTITY_PROMPT_VERSION,
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
)
from .reader_labels import resolve_reader_labels
from .reviewed_supplements import (
    ReviewedCompanionSupplement,
    decode_reviewed_companion_supplement,
    encode_reviewed_companion_supplement,
    validate_reviewed_companion_supplement,
)


COMPANION_BUILD_REQUEST_SCHEMA = "arc.companion.build_request.v8"
LEGACY_COMPANION_BUILD_REQUEST_SCHEMA_V7 = (
    "arc.companion.build_request.v7"
)
COMPANION_GENERATION_RECIPE_SCHEMA = "arc.companion.generation_recipe.v17"
COMPANION_CONTENT_CONTRACT = "arc.companion.source_anchored_textbook.v1"
NEUTRAL_TEXTBOOK_INTENT = (
    "Explain the source faithfully as a neutral textbook companion for an "
    "engaged reader, adding selective help only where it improves understanding."
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CompanionBuildRequest:
    """Stable request for the split translation and guide lanes."""

    source: RichDocument
    validator_digests: tuple[str, ...] = ()
    target_language: str = "zh-CN"
    user_intent: str = ""
    content_contract: str = COMPANION_CONTENT_CONTRACT
    authors: tuple[str, ...] = ()
    reader_labels: Mapping[str, str] | None = None
    structure_ref: CachedDocumentStructureRef | None = None
    companion_section_ids: tuple[str, ...] | None = None
    reviewed_supplements: tuple[ReviewedCompanionSupplement, ...] = ()

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
        if isinstance(self.authors, (str, bytes, bytearray)):
            raise ValueError("authors must be a sequence of author names")
        authors = tuple(self.authors)
        if any(
            not isinstance(author, str) or not author.strip()
            for author in authors
        ):
            raise ValueError("authors must contain non-empty strings")
        labels: Mapping[str, str] | None = self.reader_labels
        if labels is not None:
            if not isinstance(labels, Mapping) or not labels:
                raise ValueError(
                    "reader_labels must be a non-empty string mapping or null"
                )
            normalized_labels: dict[str, str] = {}
            for key, value in labels.items():
                if (
                    not isinstance(key, str)
                    or not key.strip()
                    or not isinstance(value, str)
                    or not value.strip()
                ):
                    raise ValueError(
                        "reader_labels must contain non-empty string keys and values"
                    )
                normalized_key = key.strip()
                if normalized_key in normalized_labels:
                    raise ValueError(
                        "reader_labels contains duplicate normalized keys"
                    )
                normalized_labels[normalized_key] = value.strip()
            labels = MappingProxyType(
                dict(
                    sorted(
                        resolve_reader_labels(
                            self.target_language,
                            normalized_labels,
                        ).items()
                    )
                )
            )
        object.__setattr__(self, "validator_digests", validators)
        object.__setattr__(
            self, "target_language", self.target_language.strip()
        )
        object.__setattr__(self, "user_intent", self.user_intent.strip())
        object.__setattr__(
            self,
            "authors",
            tuple(author.strip() for author in authors),
        )
        object.__setattr__(self, "reader_labels", labels)
        if self.structure_ref is not None and not isinstance(
            self.structure_ref, CachedDocumentStructureRef
        ):
            raise ValueError(
                "structure_ref must be a CachedDocumentStructureRef or null"
            )
        if self.structure_ref is not None and (
            self.structure_ref.document.source_sha256
            != self.source.source.artifact_digest
            or self.structure_ref.document.source_size
            != self.source.source.size
        ):
            raise ValueError(
                "structure_ref document differs from the Companion source"
            )
        if self.structure_ref is None and self.companion_section_ids is not None:
            raise ValueError(
                "companion_section_ids requires structure_ref"
            )
        section_ids = (
            None
            if self.companion_section_ids is None
            else tuple(self.companion_section_ids)
        )
        if section_ids is not None and (
            any(
                not isinstance(item, str) or not item.strip()
                for item in section_ids
            )
            or len(section_ids) != len(set(section_ids))
        ):
            raise ValueError(
                "companion_section_ids must be unique non-empty strings or null"
            )
        object.__setattr__(self, "companion_section_ids", section_ids)
        supplements = tuple(self.reviewed_supplements)
        if any(
            not isinstance(item, ReviewedCompanionSupplement)
            for item in supplements
        ):
            raise ValueError(
                "reviewed_supplements must contain ReviewedCompanionSupplement values"
            )
        supplement_ids = [item.supplement_id for item in supplements]
        if len(supplement_ids) != len(set(supplement_ids)):
            raise ValueError("reviewed_supplements contains duplicate IDs")
        for supplement in supplements:
            validate_reviewed_companion_supplement(supplement, self.source)
        object.__setattr__(self, "reviewed_supplements", supplements)

    @property
    def effective_intent(self) -> str:
        return self.user_intent or NEUTRAL_TEXTBOOK_INTENT


@dataclass(frozen=True)
class CompanionGenerationRecipe:
    """Guide prompt identities plus approximate glossary size."""

    model: ModelSelection = field(default_factory=ModelSelection)
    approx_term_count: int = 50
    author_identity_prompt: str = AUTHOR_IDENTITY_PROMPT_VERSION
    chapter_guide_prompt: str = CHAPTER_GUIDE_PROMPT_VERSION
    chapter_guide_review_prompt: str = (
        CHAPTER_GUIDE_REVIEW_PROMPT_VERSION
    )
    chapter_guide_max_rounds: int = 3
    chapter_guide_review_final_round: bool = False
    equation_label_visual_prompt: str = (
        EQUATION_LABEL_VISUAL_PROMPT_VERSION
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
        if (
            isinstance(self.chapter_guide_max_rounds, bool)
            or self.chapter_guide_max_rounds != 3
        ):
            raise ValueError(
                "chapter_guide_max_rounds must be 3"
            )
        if self.chapter_guide_review_final_round is not False:
            raise ValueError(
                "chapter_guide_review_final_round must be false"
            )
        expected = {
            "author_identity_prompt": AUTHOR_IDENTITY_PROMPT_VERSION,
            "chapter_guide_prompt": CHAPTER_GUIDE_PROMPT_VERSION,
            "chapter_guide_review_prompt": (
                CHAPTER_GUIDE_REVIEW_PROMPT_VERSION
            ),
            "equation_label_visual_prompt": (
                EQUATION_LABEL_VISUAL_PROMPT_VERSION
            ),
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"unsupported {name} contract")


@dataclass(frozen=True)
class CompanionExecutionOptions:
    """Non-semantic runtime policy for one invocation."""

    workers: int = 16
    llm: LLMExecutionOptions = field(default_factory=LLMExecutionOptions)
    paper_cache_root: Path | None = None

    def __post_init__(self) -> None:
        if isinstance(self.workers, bool) or not isinstance(
            self.workers, int
        ):
            raise ValueError("workers must be an integer")
        if not 1 <= self.workers <= 24:
            raise ValueError("workers must be between 1 and 24")
        if not isinstance(self.llm, LLMExecutionOptions):
            raise ValueError("llm must be LLMExecutionOptions")
        if self.paper_cache_root is not None:
            object.__setattr__(
                self, "paper_cache_root", Path(self.paper_cache_root)
            )


def encode_build_request(
    request: CompanionBuildRequest,
) -> dict[str, Any]:
    return {
        "schema_version": COMPANION_BUILD_REQUEST_SCHEMA,
        "source": rich_document_to_document(request.source),
        "validator_digests": list(request.validator_digests),
        "target_language": request.target_language,
        "user_intent": request.effective_intent,
        "content_contract": request.content_contract,
        "authors": list(request.authors),
        "reader_labels": (
            dict(request.reader_labels)
            if request.reader_labels is not None
            else None
        ),
        "structure_ref": (
            cached_document_structure_ref_to_document(request.structure_ref)
            if request.structure_ref is not None
            else None
        ),
        "companion_section_ids": (
            list(request.companion_section_ids)
            if request.companion_section_ids is not None
            else None
        ),
        "reviewed_supplements": [
            encode_reviewed_companion_supplement(item)
            for item in request.reviewed_supplements
        ],
    }


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
        "approx_term_count": recipe.approx_term_count,
        "author_identity_prompt": recipe.author_identity_prompt,
        "chapter_guide_prompt": recipe.chapter_guide_prompt,
        "chapter_guide_review_prompt": (
            recipe.chapter_guide_review_prompt
        ),
        "chapter_guide_max_rounds": recipe.chapter_guide_max_rounds,
        "chapter_guide_review_final_round": (
            recipe.chapter_guide_review_final_round
        ),
        "equation_label_visual_prompt": (
            recipe.equation_label_visual_prompt
        ),
    }


def encode_handler_semantic_input(
    request: CompanionBuildRequest,
    recipe: CompanionGenerationRecipe,
) -> dict[str, Any]:
    return {
        "request": encode_build_request(request),
        "generation_recipe": encode_generation_recipe(recipe),
    }


def decode_build_request(
    document: Mapping[str, Any],
) -> CompanionBuildRequest:
    fields = {
        "schema_version",
        "source",
        "validator_digests",
        "target_language",
        "user_intent",
        "content_contract",
        "authors",
        "reader_labels",
        "structure_ref",
        "companion_section_ids",
    }
    schema_version = document.get("schema_version")
    if schema_version == COMPANION_BUILD_REQUEST_SCHEMA:
        fields.add("reviewed_supplements")
    elif schema_version != LEGACY_COMPANION_BUILD_REQUEST_SCHEMA_V7:
        raise ValueError("unsupported Companion build-request schema")
    request = _exact(document, fields, "build request")
    source = _mapping(request["source"], "rich source")
    validators = request["validator_digests"]
    if not isinstance(validators, list) or any(
        not isinstance(item, str) for item in validators
    ):
        raise ValueError("validator_digests must be an array of strings")
    raw_authors = request["authors"]
    if not isinstance(raw_authors, list) or any(
        not isinstance(item, str) for item in raw_authors
    ):
        raise ValueError("authors must be an array of strings")
    authors = tuple(raw_authors)
    reader_labels = _optional_string_mapping(
        request["reader_labels"], "reader_labels"
    )
    return CompanionBuildRequest(
        source=rich_document_from_document(source),
        validator_digests=tuple(validators),
        target_language=_string(request, "target_language"),
        user_intent=_string(request, "user_intent"),
        content_contract=_string(request, "content_contract"),
        authors=authors,
        reader_labels=reader_labels,
        structure_ref=(
            cached_document_structure_ref_from_document(
                _mapping(request["structure_ref"], "structure ref")
            )
            if request["structure_ref"] is not None
            else None
        ),
        companion_section_ids=(
            tuple(_string_sequence(
                request["companion_section_ids"],
                "companion_section_ids",
            ))
            if request["companion_section_ids"] is not None
            else None
        ),
        reviewed_supplements=tuple(
            decode_reviewed_companion_supplement(item)
            for item in _mapping_sequence(
                request.get("reviewed_supplements", []),
                "reviewed_supplements",
            )
        ),
    )


def decode_generation_recipe(
    document: Mapping[str, Any],
) -> CompanionGenerationRecipe:
    fields = {
        "schema_version",
        "model",
        "approx_term_count",
        "author_identity_prompt",
        "chapter_guide_prompt",
        "chapter_guide_review_prompt",
        "equation_label_visual_prompt",
        "chapter_guide_max_rounds",
        "chapter_guide_review_final_round",
    }
    raw_recipe = _exact(document, fields, "generation recipe")
    if raw_recipe["schema_version"] != COMPANION_GENERATION_RECIPE_SCHEMA:
        raise ValueError("unsupported Companion generation-recipe schema")
    model = _exact(
        _mapping(raw_recipe["model"], "model"),
        {"provider", "model", "tier"},
        "model",
    )
    exact_model = model["model"]
    if exact_model is not None and not isinstance(exact_model, str):
        raise ValueError("model.model must be a string or null")
    for key in fields - {
        "schema_version",
        "model",
        "approx_term_count",
        "chapter_guide_max_rounds",
        "chapter_guide_review_final_round",
    }:
        _string(raw_recipe, key)
    return CompanionGenerationRecipe(
        model=ModelSelection(
            provider=_string(model, "provider"),
            model=exact_model,
            tier=_string(model, "tier"),  # type: ignore[arg-type]
        ),
        approx_term_count=_integer(raw_recipe, "approx_term_count"),
        author_identity_prompt=_string(raw_recipe, "author_identity_prompt"),
        chapter_guide_prompt=_string(raw_recipe, "chapter_guide_prompt"),
        chapter_guide_review_prompt=_string(
            raw_recipe, "chapter_guide_review_prompt"
        ),
        chapter_guide_max_rounds=_integer(
            raw_recipe, "chapter_guide_max_rounds"
        ),
        chapter_guide_review_final_round=_strict_bool(
            raw_recipe, "chapter_guide_review_final_round"
        ),
        equation_label_visual_prompt=_string(
            raw_recipe, "equation_label_visual_prompt"
        ),
    )


def decode_handler_semantic_input(
    document: Mapping[str, Any],
) -> tuple[CompanionBuildRequest, CompanionGenerationRecipe]:
    value = _exact(
        document, {"request", "generation_recipe"}, "semantic input"
    )
    return (
        decode_build_request(
            _mapping(value["request"], "build request")
        ),
        decode_generation_recipe(
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


def _mapping_sequence(
    value: Any, description: str
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ValueError(f"{description} must be an array of objects")
    return tuple(value)


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


def _strict_bool(value: Mapping[str, Any], key: str) -> bool:
    item = value.get(key)
    if type(item) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return item


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise ValueError(f"{key} must be a string or null")
    return item


def _optional_string_mapping(
    value: Any, description: str
) -> Mapping[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(f"{description} must be an object of strings or null")
    return dict(value)


def _string_sequence(value: Any, description: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{description} must be an array of non-empty strings")
    return [item.strip() for item in value]


__all__ = [
    "COMPANION_BUILD_REQUEST_SCHEMA",
    "COMPANION_CONTENT_CONTRACT",
    "COMPANION_GENERATION_RECIPE_SCHEMA",
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
