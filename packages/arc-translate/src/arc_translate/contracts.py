"""Closed semantic and result contracts for standalone translation steps."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from arc_jobs import (
    ArtifactDigest,
    ArtifactSourceRef,
    JsonValue,
    decode_artifact_digest,
    encode_artifact_digest,
)
from arc_llm import LLMExecutionOptions, ModelSelection
from arc_paper import (
    RichDocument,
    rich_document_from_document,
    rich_document_to_document,
)


SOURCE_SCHEMA = "arc.translate.source.v2"
LANGUAGE_REQUEST_SCHEMA = "arc.translate.language_request.v1"
GLOSSARY_REQUEST_SCHEMA = "arc.translate.glossary_request.v1"
BLOCKS_REQUEST_SCHEMA = "arc.translate.blocks_request.v1"
GENERATION_RECIPE_SCHEMA = "arc.translate.generation_recipe.v1"
LANGUAGE_RESULT_SCHEMA = "arc.translate.language_result.v1"
GLOSSARY_RESULT_SCHEMA = "arc.translate.glossary_result.v1"
TRANSLATION_RESULT_SCHEMA = "arc.translate.translation_result.v1"

DEFAULT_GLOSSARY_INPUT_BUDGET_BYTES = 32_000
DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES = 32_000

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class TranslationSource:
    """The immutable RichDocument that exclusively anchors translation."""

    rich: RichDocument

    def __post_init__(self) -> None:
        if not isinstance(self.rich, RichDocument):
            raise ValueError("rich must be a RichDocument")

    @property
    def source_digest(self) -> str:
        return self.rich.source.artifact_digest

    @property
    def document_digest(self) -> str:
        return self.rich.document_digest


@dataclass(frozen=True)
class LanguageRequest:
    source: TranslationSource
    target_language: str

    def __post_init__(self) -> None:
        _require_language(self.target_language, "target_language")
        object.__setattr__(self, "target_language", self.target_language.strip())


@dataclass(frozen=True)
class GlossaryRequest:
    source: TranslationSource
    target_language: str
    approx_count: int
    language_result: ArtifactSourceRef

    def __post_init__(self) -> None:
        _require_language(self.target_language, "target_language")
        if (
            isinstance(self.approx_count, bool)
            or not isinstance(self.approx_count, int)
            or not 1 <= self.approx_count <= 200
        ):
            raise ValueError("approx_count must be between 1 and 200")
        if not isinstance(self.language_result, ArtifactSourceRef):
            raise ValueError("language_result must be an ArtifactSourceRef")
        object.__setattr__(self, "target_language", self.target_language.strip())


@dataclass(frozen=True)
class BlocksRequest:
    source: TranslationSource
    target_language: str
    language_result: ArtifactSourceRef
    glossary_result: ArtifactSourceRef

    def __post_init__(self) -> None:
        _require_language(self.target_language, "target_language")
        if not isinstance(self.language_result, ArtifactSourceRef):
            raise ValueError("language_result must be an ArtifactSourceRef")
        if not isinstance(self.glossary_result, ArtifactSourceRef):
            raise ValueError("glossary_result must be an ArtifactSourceRef")
        object.__setattr__(self, "target_language", self.target_language.strip())


@dataclass(frozen=True)
class GenerationRecipe:
    model: ModelSelection = field(default_factory=ModelSelection)
    glossary_input_budget_bytes: int = DEFAULT_GLOSSARY_INPUT_BUDGET_BYTES
    translation_input_budget_bytes: int = DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.model, ModelSelection):
            raise ValueError("model must be a ModelSelection")
        for name in (
            "glossary_input_budget_bytes",
            "translation_input_budget_bytes",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 4_096 <= value <= 1_000_000
            ):
                raise ValueError(f"{name} must be between 4096 and 1000000")


@dataclass(frozen=True)
class ExecutionOptions:
    llm: LLMExecutionOptions = field(default_factory=LLMExecutionOptions)

    def __post_init__(self) -> None:
        if not isinstance(self.llm, LLMExecutionOptions):
            raise ValueError("llm must be LLMExecutionOptions")


def source_to_document(source: TranslationSource) -> dict[str, JsonValue]:
    return {
        "schema_version": SOURCE_SCHEMA,
        "rich": rich_document_to_document(source.rich),
    }


def source_from_document(value: Mapping[str, Any]) -> TranslationSource:
    document = _exact(value, {"schema_version", "rich"}, "source")
    if document["schema_version"] != SOURCE_SCHEMA:
        raise ValueError("unsupported translation source schema")
    raw_rich = document["rich"]
    if not isinstance(raw_rich, Mapping):
        raise ValueError("source rich projection must be an object")
    return TranslationSource(rich_document_from_document(raw_rich))


def artifact_source_to_document(
    source: ArtifactSourceRef,
) -> dict[str, JsonValue]:
    return {
        "source_run_id": source.source_run_id,
        "source_artifact_id": source.source_artifact_id,
        "expected_digest": encode_artifact_digest(source.expected_digest),
    }


def artifact_source_from_document(value: Any) -> ArtifactSourceRef:
    document = _exact(
        _mapping(value, "artifact source"),
        {"source_run_id", "source_artifact_id", "expected_digest"},
        "artifact source",
    )
    run_id = _string(document, "source_run_id")
    artifact_id = _string(document, "source_artifact_id")
    digest = decode_artifact_digest(document["expected_digest"])
    return ArtifactSourceRef(run_id, artifact_id, digest)


def recipe_to_document(recipe: GenerationRecipe) -> dict[str, JsonValue]:
    return {
        "schema_version": GENERATION_RECIPE_SCHEMA,
        "model": {
            "provider": recipe.model.provider,
            "model": recipe.model.model,
            "tier": recipe.model.tier,
        },
        "glossary_input_budget_bytes": recipe.glossary_input_budget_bytes,
        "translation_input_budget_bytes": recipe.translation_input_budget_bytes,
    }


def recipe_from_document(value: Mapping[str, Any]) -> GenerationRecipe:
    document = _exact(
        value,
        {
            "schema_version",
            "model",
            "glossary_input_budget_bytes",
            "translation_input_budget_bytes",
        },
        "generation recipe",
    )
    if document["schema_version"] != GENERATION_RECIPE_SCHEMA:
        raise ValueError("unsupported generation recipe schema")
    model = _exact(
        _mapping(document["model"], "generation model"),
        {"provider", "model", "tier"},
        "generation model",
    )
    exact_model = model["model"]
    if exact_model is not None and not isinstance(exact_model, str):
        raise ValueError("model.model must be a string or null")
    return GenerationRecipe(
        model=ModelSelection(
            provider=_string(model, "provider"),
            model=exact_model,
            tier=_string(model, "tier"),  # type: ignore[arg-type]
        ),
        glossary_input_budget_bytes=_integer(
            document, "glossary_input_budget_bytes"
        ),
        translation_input_budget_bytes=_integer(
            document, "translation_input_budget_bytes"
        ),
    )


def language_semantic_input(
    request: LanguageRequest, recipe: GenerationRecipe
) -> dict[str, JsonValue]:
    return {
        "request": {
            "schema_version": LANGUAGE_REQUEST_SCHEMA,
            "source": source_to_document(request.source),
            "target_language": request.target_language,
        },
        "generation_recipe": recipe_to_document(recipe),
    }


def decode_language_semantic_input(
    value: Mapping[str, Any],
) -> tuple[LanguageRequest, GenerationRecipe]:
    outer = _exact(value, {"request", "generation_recipe"}, "semantic input")
    request = _exact(
        _mapping(outer["request"], "language request"),
        {"schema_version", "source", "target_language"},
        "language request",
    )
    if request["schema_version"] != LANGUAGE_REQUEST_SCHEMA:
        raise ValueError("unsupported language request schema")
    return (
        LanguageRequest(
            source_from_document(_mapping(request["source"], "source")),
            _string(request, "target_language"),
        ),
        recipe_from_document(
            _mapping(outer["generation_recipe"], "generation recipe")
        ),
    )


def glossary_semantic_input(
    request: GlossaryRequest, recipe: GenerationRecipe
) -> dict[str, JsonValue]:
    return {
        "request": {
            "schema_version": GLOSSARY_REQUEST_SCHEMA,
            "source": source_to_document(request.source),
            "target_language": request.target_language,
            "approx_count": request.approx_count,
            "language_result": artifact_source_to_document(
                request.language_result
            ),
        },
        "generation_recipe": recipe_to_document(recipe),
    }


def decode_glossary_semantic_input(
    value: Mapping[str, Any],
) -> tuple[GlossaryRequest, GenerationRecipe]:
    outer = _exact(value, {"request", "generation_recipe"}, "semantic input")
    request = _exact(
        _mapping(outer["request"], "glossary request"),
        {
            "schema_version",
            "source",
            "target_language",
            "approx_count",
            "language_result",
        },
        "glossary request",
    )
    if request["schema_version"] != GLOSSARY_REQUEST_SCHEMA:
        raise ValueError("unsupported glossary request schema")
    return (
        GlossaryRequest(
            source_from_document(_mapping(request["source"], "source")),
            _string(request, "target_language"),
            _integer(request, "approx_count"),
            artifact_source_from_document(request["language_result"]),
        ),
        recipe_from_document(
            _mapping(outer["generation_recipe"], "generation recipe")
        ),
    )


def blocks_semantic_input(
    request: BlocksRequest, recipe: GenerationRecipe
) -> dict[str, JsonValue]:
    return {
        "request": {
            "schema_version": BLOCKS_REQUEST_SCHEMA,
            "source": source_to_document(request.source),
            "target_language": request.target_language,
            "language_result": artifact_source_to_document(
                request.language_result
            ),
            "glossary_result": artifact_source_to_document(
                request.glossary_result
            ),
        },
        "generation_recipe": recipe_to_document(recipe),
    }


def decode_blocks_semantic_input(
    value: Mapping[str, Any],
) -> tuple[BlocksRequest, GenerationRecipe]:
    outer = _exact(value, {"request", "generation_recipe"}, "semantic input")
    request = _exact(
        _mapping(outer["request"], "blocks request"),
        {
            "schema_version",
            "source",
            "target_language",
            "language_result",
            "glossary_result",
        },
        "blocks request",
    )
    if request["schema_version"] != BLOCKS_REQUEST_SCHEMA:
        raise ValueError("unsupported blocks request schema")
    return (
        BlocksRequest(
            source_from_document(_mapping(request["source"], "source")),
            _string(request, "target_language"),
            artifact_source_from_document(request["language_result"]),
            artifact_source_from_document(request["glossary_result"]),
        ),
        recipe_from_document(
            _mapping(outer["generation_recipe"], "generation recipe")
        ),
    )


def artifact_source(
    run_id: str, artifact_id: str, digest: ArtifactDigest
) -> ArtifactSourceRef:
    return ArtifactSourceRef(run_id, artifact_id, digest)


def _require_language(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty language tag")


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
    "DEFAULT_GLOSSARY_INPUT_BUDGET_BYTES",
    "DEFAULT_TRANSLATION_INPUT_BUDGET_BYTES",
    "GLOSSARY_RESULT_SCHEMA",
    "LANGUAGE_RESULT_SCHEMA",
    "TRANSLATION_RESULT_SCHEMA",
    "BlocksRequest",
    "ExecutionOptions",
    "GenerationRecipe",
    "GlossaryRequest",
    "LanguageRequest",
    "TranslationSource",
    "artifact_source",
    "blocks_semantic_input",
    "decode_blocks_semantic_input",
    "decode_glossary_semantic_input",
    "decode_language_semantic_input",
    "glossary_semantic_input",
    "language_semantic_input",
    "source_from_document",
    "source_to_document",
]
