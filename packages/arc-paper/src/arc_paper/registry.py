"""Typed operation registry and conservative resolver projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from jsonschema import Draft202012Validator

from . import service


REGISTRY_SCHEMA_VERSION = "arc.paper.operation_registry.v1"
T = TypeVar("T")


class OperationEffect(str, Enum):
    NETWORK = "network"
    CACHE_WRITE = "cache_write"
    CACHE_ADMIN = "cache_admin"
    DESTRUCTIVE = "destructive"
    ARBITRARY_LOCAL_PATH = "arbitrary_local_path"
    RECURSIVE_LLM = "recursive_llm"


DEFAULT_EXCLUDED_EFFECTS = frozenset(
    {
        OperationEffect.CACHE_ADMIN,
        OperationEffect.DESTRUCTIVE,
        OperationEffect.ARBITRARY_LOCAL_PATH,
        OperationEffect.RECURSIVE_LLM,
    }
)


@dataclass(frozen=True)
class JsonCodec(Generic[T]):
    """Strict JSON-object codec with a published schema."""

    schema_id: str
    schema: Mapping[str, Any]
    decoder: Callable[[Mapping[str, Any]], T]
    encoder: Callable[[T], Any]

    def decode(self, value: Mapping[str, Any]) -> T:
        errors = sorted(
            Draft202012Validator(dict(self.schema)).iter_errors(dict(value)),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            detail = "; ".join(error.message for error in errors[:5])
            raise OperationRequestError("invalid_parameters", detail)
        return self.decoder(value)

    def encode(self, value: T) -> Any:
        return self.encoder(value)


@dataclass(frozen=True)
class JsonOutputCodec(Generic[T]):
    """Typed result encoder paired with its published JSON schema."""

    schema_id: str
    schema: Mapping[str, Any]
    encoder: Callable[[T], Any]

    def encode(self, value: T) -> Any:
        encoded = self.encoder(value)
        errors = sorted(
            Draft202012Validator(dict(self.schema)).iter_errors(encoded),
            key=lambda error: [str(item) for item in error.absolute_path],
        )
        if errors:
            detail = "; ".join(error.message for error in errors[:5])
            raise OperationRequestError("invalid_result", detail)
        return encoded


@dataclass(frozen=True)
class OperationSpec(Generic[T]):
    operation_id: str
    version: int
    name: str
    input_codec: JsonCodec[Mapping[str, Any]]
    output_codec: JsonOutputCodec[T]
    callable: Callable[..., T]
    effect_flags: frozenset[OperationEffect] = frozenset()

    def invoke(self, parameters: Mapping[str, Any]) -> Any:
        decoded = self.input_codec.decode(parameters)
        return self.output_codec.encode(self.callable(**decoded))

    def document(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "version": self.version,
            "name": self.name,
            "input": {
                "schema_id": self.input_codec.schema_id,
                "schema": dict(self.input_codec.schema),
            },
            "output": {
                "schema_id": self.output_codec.schema_id,
                "schema": dict(self.output_codec.schema),
            },
            "effect_flags": sorted(flag.value for flag in self.effect_flags),
        }


class OperationRequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if is_dataclass(value):
        return {
            item.name: to_json_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_value(item) for item in value]
    if hasattr(value, "to_document"):
        return to_json_value(value.to_document())
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _object(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _codec(
    name: str, schema: Mapping[str, Any], *, version: int
) -> JsonCodec[Mapping[str, Any]]:
    return JsonCodec(
        f"arc.paper.{name}.parameters.v{version}",
        schema,
        lambda value: dict(value),
        to_json_value,
    )


def _spec(
    name: str,
    schema: Mapping[str, Any],
    callable: Callable[..., Any],
    *,
    output_schema: Mapping[str, Any],
    effects: frozenset[OperationEffect] = frozenset(),
    version: int = 1,
) -> OperationSpec[Any]:
    return OperationSpec(
        operation_id=f"arc-paper.{name}.v{version}",
        version=version,
        name=name,
        input_codec=_codec(name, schema, version=version),
        output_codec=JsonOutputCodec(
            f"arc.paper.{name}.result.v{version}", output_schema, to_json_value
        ),
        callable=callable,
        effect_flags=effects,
    )


_PAPER = {"paper_id": {"type": "string", "minLength": 1}}
_ARXIV = {"arxiv_id": {"type": "string", "minLength": 1}}
_REFRESH = {"refresh": {"type": "boolean", "default": False}}
_NETWORK_CACHE = frozenset(
    {OperationEffect.NETWORK, OperationEffect.CACHE_WRITE}
)

_STRING = {"type": "string"}
_NONEMPTY_STRING = {"type": "string", "minLength": 1}
_INTEGER = {"type": "integer"}
_NULLABLE_INTEGER = {"type": ["integer", "null"]}
_NULLABLE_POSITION = {"type": ["integer", "null"], "minimum": 1}
_STRING_ARRAY = {"type": "array", "items": _STRING}
_SOURCE_ORIGIN_SCHEMA = _object(
    {
        "kind": {"enum": ["local_import", "remote_provider", "repository"]},
        "provider": _STRING,
        "locator": _STRING,
        "metadata": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    required=("kind", "provider", "locator", "metadata"),
)
_SOURCE_ARTIFACT_SCHEMA = _object(
    {
        "source_format": {"enum": ["html", "markdown", "tex", "pdf"]},
        "artifact_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "size": {"type": "integer", "minimum": 0},
        "media_type": _NONEMPTY_STRING,
        "origin": _SOURCE_ORIGIN_SCHEMA,
    },
    required=(
        "source_format",
        "artifact_digest",
        "size",
        "media_type",
        "origin",
    ),
)
_IDENTIFIERS_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": "string"},
}
_METADATA_SCHEMA = _object(
    {
        "paper_id": _STRING,
        "title": _STRING,
        "abstract": _STRING,
        "authors": _STRING_ARRAY,
        "arxiv_id": _STRING,
        "inspire_recid": _STRING,
        "doi": _STRING,
        "identifiers": _IDENTIFIERS_SCHEMA,
        "year": _NULLABLE_INTEGER,
        "published": _STRING,
        "citation_count": _INTEGER,
    },
    required=(
        "paper_id",
        "title",
        "abstract",
        "authors",
        "arxiv_id",
        "inspire_recid",
        "doi",
        "identifiers",
        "year",
        "published",
        "citation_count",
    ),
)
_REFERENCE_SCHEMA = _object(
    {
        "paper_id": _STRING,
        "title": _STRING,
        "raw_inspire_reference": {"type": "object"},
        "record_ref": _STRING,
        "publication_info": {"type": ["array", "object", "string"]},
        "abstract": _STRING,
        "authors": _STRING_ARRAY,
        "arxiv_id": _STRING,
        "inspire_recid": _STRING,
        "doi": _STRING,
        "identifiers": _IDENTIFIERS_SCHEMA,
        "year": _NULLABLE_INTEGER,
        "published": _STRING,
        "citation_count": _NULLABLE_INTEGER,
        "metadata_enriched": {"type": "boolean"},
        "metadata_enrichment_error": _object(
            {"code": _STRING, "message": _STRING},
            required=("code", "message"),
        ),
    },
    required=("paper_id", "title", "raw_inspire_reference"),
)
_SECTION_SCHEMA = _object(
    {
        "section_id": _NONEMPTY_STRING,
        "title": _STRING,
        "level": {"type": "integer", "minimum": 1},
        "text": _STRING,
        "ordinal": {"type": "integer", "minimum": 0},
        "page_start": _NULLABLE_INTEGER,
        "page_end": _NULLABLE_INTEGER,
    },
    required=(
        "section_id",
        "title",
        "level",
        "text",
        "ordinal",
        "page_start",
        "page_end",
    ),
)
_MATCHED_SENTENCE_SCHEMA = _object(
    {
        "text": {"type": "string", "maxLength": 400},
        "section_id": _NONEMPTY_STRING,
        "page_number": _NULLABLE_INTEGER,
        "matched_surface": _NONEMPTY_STRING,
        "clipped": {"type": "boolean"},
    },
    required=(
        "text",
        "section_id",
        "page_number",
        "matched_surface",
        "clipped",
    ),
)
_KEYWORD_TERM_SCHEMA = _object(
    {
        "term_id": _NONEMPTY_STRING,
        "term": {"type": "string", "minLength": 1, "maxLength": 300},
        "aliases": _STRING_ARRAY,
        "occurrence_count": {"type": "integer", "minimum": 0},
        "source_refs": _STRING_ARRAY,
        "matched_sentences": {
            "type": "array",
            "maxItems": 10,
            "items": _MATCHED_SENTENCE_SCHEMA,
        },
    },
    required=(
        "term_id",
        "term",
        "aliases",
        "occurrence_count",
        "source_refs",
        "matched_sentences",
    ),
)
_KEYWORD_RESULT_SCHEMA = _object(
    {
        "schema_version": {"const": "arc.paper.keyword_result.v1"},
        "document_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "source_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "approx_count": {"type": "integer", "minimum": 1, "maximum": 200},
        "planned_count": {"type": "integer", "minimum": 2, "maximum": 300},
        "returned_count": {"type": "integer", "minimum": 0, "maximum": 300},
        "terms": {"type": "array", "maxItems": 300, "items": _KEYWORD_TERM_SCHEMA},
        "inventory_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "warnings": _STRING_ARRAY,
    },
    required=(
        "schema_version",
        "document_digest",
        "source_digest",
        "approx_count",
        "planned_count",
        "returned_count",
        "terms",
        "inventory_digest",
        "warnings",
    ),
)
_TOC_ENTRY_SCHEMA = _object(
    {
        "section_id": _NONEMPTY_STRING,
        "title": _STRING,
        "level": {"type": "integer", "minimum": 1},
        "ordinal": {"type": "integer", "minimum": 0},
        "page_start": _NULLABLE_INTEGER,
        "page_end": _NULLABLE_INTEGER,
    },
    required=(
        "section_id",
        "title",
        "level",
        "ordinal",
        "page_start",
        "page_end",
    ),
)
_MATH_SPAN_SCHEMA = _object(
    {
        "span_id": _NONEMPTY_STRING,
        "kind": {"enum": ["inline", "display"]},
        "source_line_start": _NULLABLE_POSITION,
        "source_column_start": _NULLABLE_POSITION,
        "source_line_end": _NULLABLE_POSITION,
        "source_column_end": _NULLABLE_POSITION,
        "normalized_tex": _NONEMPTY_STRING,
        "context_before": _STRING,
        "context_after": _STRING,
        "source_label": _STRING,
    },
    required=(
        "span_id",
        "kind",
        "source_line_start",
        "source_column_start",
        "source_line_end",
        "source_column_end",
        "normalized_tex",
        "context_before",
        "context_after",
        "source_label",
    ),
)
_PARSED_DOCUMENT_SCHEMA = _object(
    {
        "source": _SOURCE_ARTIFACT_SCHEMA,
        "sections": {"type": "array", "items": _SECTION_SCHEMA},
        "math_spans": {"type": "array", "items": _MATH_SPAN_SCHEMA},
        "pages": {
            "type": "array",
            "items": _object(
                {
                    "page_number": {"type": "integer", "minimum": 1},
                    "text": _STRING,
                },
                required=("page_number", "text"),
            ),
        },
        "warnings": _STRING_ARRAY,
        "metadata": {"type": "object"},
        "schema_version": {"const": "arc.paper.parsed_document.v2"},
        "document_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    required=(
        "source",
        "sections",
        "math_spans",
        "pages",
        "warnings",
        "metadata",
        "schema_version",
        "document_digest",
    ),
)
_RECONCILIATION_ENTRY_SCHEMA = _object(
    {
        "validator": _SOURCE_ARTIFACT_SCHEMA,
        "status": {
            "enum": ["verified", "mismatch", "missing", "ambiguous", "unreviewed"]
        },
        "subject_id": _STRING,
        "message": _STRING,
        "provenance": {"type": "object"},
    },
    required=("validator", "status", "subject_id", "message", "provenance"),
)
_PARSE_OUTCOME_SCHEMA = _object(
    {
        "document": _PARSED_DOCUMENT_SCHEMA,
        "report": _object(
            {
                "primary": _SOURCE_ARTIFACT_SCHEMA,
                "policy": {
                    "enum": ["none", "deterministic_only", "visual_all_pages"]
                },
                "entries": {
                    "type": "array",
                    "items": _RECONCILIATION_ENTRY_SCHEMA,
                },
            },
            required=("primary", "policy", "entries"),
        ),
        "warnings": _STRING_ARRAY,
    },
    required=("document", "report", "warnings"),
)
_ARXIV_PROVENANCE_SCHEMA = _object(
    {
        "canonical_arxiv_id": {
            "type": "string",
            "pattern": "^arXiv:",
        },
        "provider": {"enum": ["arxiv-html", "ar5iv"]},
        "source_format": {"const": "html"},
        "source_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "document_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    required=(
        "canonical_arxiv_id",
        "provider",
        "source_format",
        "source_digest",
        "document_digest",
    ),
)
_FULL_TEXT_MATCH_SCHEMA = _object(
    {
        "document_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "location": {"enum": ["section", "page"]},
        "location_id": _NONEMPTY_STRING,
        "title": _STRING,
        "ordinal": {"type": "integer", "minimum": 0},
        "page_number": _NULLABLE_INTEGER,
        "matched_in": _NONEMPTY_STRING,
        "snippet": _STRING,
    },
    required=(
        "document_digest",
        "source_digest",
        "location",
        "location_id",
        "title",
        "ordinal",
        "page_number",
        "matched_in",
        "snippet",
    ),
)
_CACHED_FULL_TEXT_OCCURRENCE_SCHEMA = _object(
    {
        "source_kind": {"enum": ["arxiv", "local"]},
        "arxiv_ids": {
            "type": "array",
            "items": {"type": "string", "pattern": "^arXiv:"},
        },
        "source_format": {"enum": ["html", "markdown", "tex", "pdf"]},
        "source_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "document_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "location": {"enum": ["section", "page"]},
        "location_id": _NONEMPTY_STRING,
        "title": _STRING,
        "page_number": _NULLABLE_INTEGER,
        "line": {"type": "integer", "minimum": 1},
        "column": {"type": "integer", "minimum": 1},
        "matched_terms": {
            "type": "array",
            "items": _NONEMPTY_STRING,
            "minItems": 1,
        },
        "context": {"type": "string", "maxLength": 400},
    },
    required=(
        "source_kind",
        "arxiv_ids",
        "source_format",
        "source_digest",
        "document_digest",
        "location",
        "location_id",
        "title",
        "page_number",
        "line",
        "column",
        "matched_terms",
        "context",
    ),
)
_EQUATION_MATCH_SCHEMA = _object(
    {
        "document_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "span_id": _NONEMPTY_STRING,
        "kind": {"enum": ["inline", "display"]},
        "normalized_tex": _NONEMPTY_STRING,
        "source_label": _STRING,
        "source_line_start": _NULLABLE_POSITION,
        "source_column_start": _NULLABLE_POSITION,
        "source_line_end": _NULLABLE_POSITION,
        "source_column_end": _NULLABLE_POSITION,
        "context_before": _STRING,
        "context_after": _STRING,
        "matched_in": _NONEMPTY_STRING,
    },
    required=(
        "document_digest",
        "source_digest",
        "span_id",
        "kind",
        "normalized_tex",
        "source_label",
        "source_line_start",
        "source_column_start",
        "source_line_end",
        "source_column_end",
        "context_before",
        "context_after",
        "matched_in",
    ),
)
_CACHE_LOCAL_IDENTITY_SCHEMA = _object(
    {
        "source_format": {"enum": ["html", "markdown", "tex", "pdf"]},
        "media_type": _NONEMPTY_STRING,
        "artifact_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "size": {"type": "integer", "minimum": 0},
    },
    required=("source_format", "media_type", "artifact_digest", "size"),
)
_CACHE_COMPONENT_SCHEMA = _object(
    {
        "name": _NONEMPTY_STRING,
        "cached_at": _NONEMPTY_STRING,
        "storage_entry_ids": {"type": "array", "items": _NONEMPTY_STRING},
    },
    required=("name", "cached_at", "storage_entry_ids"),
)
_CACHE_ENTRY_SCHEMA = _object(
    {
        "entry_id": _NONEMPTY_STRING,
        "kind": {"enum": ["paper", "local"]},
        "paper_id": {"type": ["string", "null"]},
        "local_source_identity": {
            "anyOf": [_CACHE_LOCAL_IDENTITY_SCHEMA, {"type": "null"}]
        },
        "components": {
            "type": "array",
            "items": _CACHE_COMPONENT_SCHEMA,
            "minItems": 1,
        },
        "cached_at": _NONEMPTY_STRING,
        "updateable": {"type": "boolean"},
    },
    required=(
        "entry_id",
        "kind",
        "paper_id",
        "local_source_identity",
        "components",
        "cached_at",
        "updateable",
    ),
)
_CACHE_FILTER_INPUT = _object(
    {
        "paper_ids": {"type": "array", "items": _NONEMPTY_STRING},
        "entry_ids": {"type": "array", "items": _NONEMPTY_STRING},
        "since_seconds": {"type": ["integer", "null"], "minimum": 1},
        "cache_root": {"type": ["string", "null"]},
    }
)

_OPERATIONS = (
    _spec(
        "extract-paper-ids",
        _object({"text": {"type": "string"}}, required=("text",)),
        service.extract_paper_ids,
        output_schema=_STRING_ARRAY,
    ),
    _spec(
        "safe-dir-name",
        _object(
            {
                "ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                }
            },
            required=("ids",),
        ),
        service.paper_ids_safe_dir_name,
        output_schema=_STRING,
    ),
    _spec(
        "get-title",
        _object({**_PAPER, **_REFRESH}, required=("paper_id",)),
        service.get_title,
        output_schema=_STRING,
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "get-abstract",
        _object({**_PAPER, **_REFRESH}, required=("paper_id",)),
        service.get_abstract,
        output_schema=_STRING,
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "get-authors",
        _object({**_PAPER, **_REFRESH}, required=("paper_id",)),
        service.get_authors,
        output_schema=_STRING_ARRAY,
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "get-metadata",
        _object({**_PAPER, **_REFRESH}, required=("paper_id",)),
        service.get_metadata,
        output_schema=_METADATA_SCHEMA,
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "get-citer-count",
        _object({**_PAPER, **_REFRESH}, required=("paper_id",)),
        service.get_citer_count,
        output_schema=_INTEGER,
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "get-references",
        _object(
            {
                **_PAPER,
                **_REFRESH,
                "enrich": {"type": "boolean", "default": False},
            },
            required=("paper_id",),
        ),
        service.get_references,
        output_schema={"type": "array", "items": _REFERENCE_SCHEMA},
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "get-citers",
        _object(
            {
                **_PAPER,
                **_REFRESH,
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "sort": {"enum": ["mostrecent", "mostcited"]},
            },
            required=("paper_id",),
        ),
        service.get_citers,
        output_schema={"type": "array", "items": _METADATA_SCHEMA},
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "search-metadata",
        _object(
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            required=("query",),
        ),
        service.search_metadata,
        output_schema={"type": "array", "items": _METADATA_SCHEMA},
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "search-cached-full-text",
        _object(
            {
                "terms": {
                    "type": "array",
                    "items": _NONEMPTY_STRING,
                    "minItems": 1,
                    "description": (
                        "Literal OR terms. Prefer several specific multi-word "
                        "synonyms, abbreviations, and alternate spellings in one request; "
                        "broad single words may require refinement."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                },
                "case_sensitive": {"type": "boolean", "default": False},
            },
            required=("terms",),
        ),
        service.search_cached_full_text,
        output_schema=_object(
            {
                "mode": {"enum": ["occurrences", "refinement_required"]},
                "terms": {
                    "type": "array",
                    "items": _NONEMPTY_STRING,
                    "minItems": 1,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                },
                "case_sensitive": {"type": "boolean"},
                "total_occurrences": {"type": "integer", "minimum": 0},
                "matched_document_count": {"type": "integer", "minimum": 0},
                "occurrences": {
                    "type": "array",
                    "items": _CACHED_FULL_TEXT_OCCURRENCE_SCHEMA,
                },
                "top_paper_titles": {
                    "type": "array",
                    "items": _NONEMPTY_STRING,
                    "maxItems": 50,
                    "description": (
                        "At most 50 cached display titles in refinement mode; "
                        "no abstracts or summaries are returned."
                    ),
                },
                "context_status": {
                    "enum": [
                        "not_requested",
                        "included",
                        "omitted_too_broad",
                        "omitted_refinement_required",
                    ]
                },
                "message": _NONEMPTY_STRING,
                "warnings": _STRING_ARRAY,
            },
            required=(
                "mode",
                "terms",
                "limit",
                "context_lines",
                "case_sensitive",
                "total_occurrences",
                "matched_document_count",
                "occurrences",
                "top_paper_titles",
                "context_status",
                "message",
                "warnings",
            ),
        ),
    ),
    _spec(
        "get-arxiv-table-of-contents",
        _object({**_ARXIV, **_REFRESH}, required=("arxiv_id",)),
        service.get_arxiv_table_of_contents,
        output_schema=_object(
            {
                "provenance": _ARXIV_PROVENANCE_SCHEMA,
                "entries": {"type": "array", "items": _TOC_ENTRY_SCHEMA},
                "warnings": _STRING_ARRAY,
            },
            required=("provenance", "entries", "warnings"),
        ),
        effects=_NETWORK_CACHE,
        version=3,
    ),
    _spec(
        "get-arxiv-section",
        _object(
            {
                **_ARXIV,
                "selector": {"type": ["string", "integer"]},
                **_REFRESH,
            },
            required=("arxiv_id", "selector"),
        ),
        service.get_arxiv_section,
        output_schema=_object(
            {
                "provenance": _ARXIV_PROVENANCE_SCHEMA,
                "section_id": _NONEMPTY_STRING,
                "title": _STRING,
                "text": _STRING,
                "level": {"type": "integer", "minimum": 1},
                "ordinal": {"type": "integer", "minimum": 0},
                "page_start": _NULLABLE_INTEGER,
                "page_end": _NULLABLE_INTEGER,
                "warnings": _STRING_ARRAY,
            },
            required=(
                "provenance",
                "section_id",
                "title",
                "text",
                "level",
                "ordinal",
                "page_start",
                "page_end",
                "warnings",
            ),
        ),
        effects=_NETWORK_CACHE,
        version=3,
    ),
    _spec(
        "search-arxiv-full-text",
        _object(
            {
                **_ARXIV,
                "query": _NONEMPTY_STRING,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5,
                },
                "case_sensitive": {"type": "boolean", "default": False},
                **_REFRESH,
            },
            required=("arxiv_id", "query"),
        ),
        service.search_arxiv_full_text,
        output_schema=_object(
            {
                "provenance": _ARXIV_PROVENANCE_SCHEMA,
                "query": _NONEMPTY_STRING,
                "matches": {"type": "array", "items": _FULL_TEXT_MATCH_SCHEMA},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5,
                },
                "case_sensitive": {"type": "boolean"},
                "truncated": {"type": "boolean"},
                "warnings": _STRING_ARRAY,
            },
            required=(
                "provenance",
                "query",
                "matches",
                "limit",
                "context_lines",
                "case_sensitive",
                "truncated",
                "warnings",
            ),
        ),
        effects=_NETWORK_CACHE,
        version=3,
    ),
    _spec(
        "search-arxiv-equations",
        _object(
            {
                **_ARXIV,
                "query": _NONEMPTY_STRING,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "case_sensitive": {"type": "boolean", "default": False},
                **_REFRESH,
            },
            required=("arxiv_id", "query"),
        ),
        service.search_arxiv_equations,
        output_schema=_object(
            {
                "provenance": _ARXIV_PROVENANCE_SCHEMA,
                "query": _NONEMPTY_STRING,
                "matches": {"type": "array", "items": _EQUATION_MATCH_SCHEMA},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "case_sensitive": {"type": "boolean"},
                "truncated": {"type": "boolean"},
                "warnings": _STRING_ARRAY,
            },
            required=(
                "provenance",
                "query",
                "matches",
                "limit",
                "case_sensitive",
                "truncated",
                "warnings",
            ),
        ),
        effects=_NETWORK_CACHE,
        version=3,
    ),
    _spec(
        "fetch-arxiv-auto",
        _object(
            {
                **_PAPER,
                **_REFRESH,
                "cache_root": {"type": ["string", "null"]},
            },
            required=("paper_id",),
        ),
        service.fetch_arxiv_auto,
        output_schema=_SOURCE_ARTIFACT_SCHEMA,
        effects=_NETWORK_CACHE,
        version=2,
    ),
    _spec(
        "fetch-arxiv-pdf",
        _object(
            {
                **_PAPER,
                **_REFRESH,
                "cache_root": {"type": ["string", "null"]},
            },
            required=("paper_id",),
        ),
        service.fetch_arxiv_pdf,
        output_schema=_SOURCE_ARTIFACT_SCHEMA,
        effects=_NETWORK_CACHE,
    ),
    _spec(
        "import-source",
        _object(
            {
                "path": {"type": "string", "minLength": 1},
                "cache_root": {"type": ["string", "null"]},
                "source_format": {
                    "type": ["string", "null"],
                    "enum": ["html", "markdown", "tex", "pdf", None],
                },
            },
            required=("path",),
        ),
        service.import_source,
        output_schema=_SOURCE_ARTIFACT_SCHEMA,
        effects=frozenset(
            {OperationEffect.CACHE_WRITE, OperationEffect.ARBITRARY_LOCAL_PATH}
        ),
    ),
    _spec(
        "parse-local",
        _object(
            {
                "primary_path": {"type": "string", "minLength": 1},
                "validator_paths": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "cache_root": {"type": ["string", "null"]},
                "primary_format": {
                    "type": ["string", "null"],
                    "enum": ["html", "markdown", "tex", "pdf", None],
                },
                "validator_formats": {
                    "type": "array",
                    "items": {
                        "type": ["string", "null"],
                        "enum": ["html", "markdown", "tex", "pdf", None],
                    },
                },
                "policy": {
                    "type": ["string", "null"],
                    "enum": [
                        "none",
                        "deterministic_only",
                        "visual_all_pages",
                        None,
                    ],
                },
            },
            required=("primary_path",),
        ),
        service.parse_local,
        output_schema=_PARSE_OUTCOME_SCHEMA,
        effects=frozenset(
            {OperationEffect.CACHE_WRITE, OperationEffect.ARBITRARY_LOCAL_PATH}
        ),
        version=2,
    ),
    _spec(
        "extract-keywords",
        _object(
            {
                "source": _NONEMPTY_STRING,
                "project_dir": _NONEMPTY_STRING,
                "approx_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                },
                "cache_root": {"type": ["string", "null"]},
                "refresh": {"type": "boolean", "default": False},
                "llm_provider": _NONEMPTY_STRING,
                "model": {"type": ["string", "null"]},
                "model_tier": {
                    "enum": ["low", "medium", "high", "xhigh"],
                },
                "run_id": {"type": ["string", "null"]},
                "resume_input": {"type": ["object", "null"]},
                "host_authority": {
                    "enum": ["unknown", "restricted", "unrestricted"],
                },
            },
            required=("source", "project_dir"),
        ),
        service.extract_keywords,
        output_schema=_KEYWORD_RESULT_SCHEMA,
        effects=frozenset(
            {
                OperationEffect.NETWORK,
                OperationEffect.CACHE_WRITE,
                OperationEffect.ARBITRARY_LOCAL_PATH,
                OperationEffect.RECURSIVE_LLM,
            }
        ),
    ),
    _spec(
        "cache-list",
        _CACHE_FILTER_INPUT,
        service.list_cache,
        output_schema=_object(
            {
                "as_of": _NONEMPTY_STRING,
                "since_seconds": {"type": ["integer", "null"], "minimum": 1},
                "threshold_at": {"type": ["string", "null"]},
                "entries": {"type": "array", "items": _CACHE_ENTRY_SCHEMA},
                "warnings": _STRING_ARRAY,
            },
            required=(
                "as_of",
                "since_seconds",
                "threshold_at",
                "entries",
                "warnings",
            ),
        ),
        effects=frozenset({OperationEffect.CACHE_ADMIN}),
        version=2,
    ),
    _spec(
        "cache-remove",
        _object(
            {
                "paper_ids": {"type": "array", "items": _NONEMPTY_STRING},
                "entry_ids": {"type": "array", "items": _NONEMPTY_STRING},
                "dry_run": {"type": "boolean", "default": True},
                "cache_root": {"type": ["string", "null"]},
            }
        ),
        service.remove_cache,
        output_schema=_object(
            {
                "dry_run": {"type": "boolean"},
                "selected": {"type": "array", "items": _CACHE_ENTRY_SCHEMA},
                "removed_entry_ids": {
                    "type": "array",
                    "items": _NONEMPTY_STRING,
                },
                "warnings": _STRING_ARRAY,
            },
            required=(
                "dry_run",
                "selected",
                "removed_entry_ids",
                "warnings",
            ),
        ),
        effects=frozenset(
            {OperationEffect.CACHE_ADMIN, OperationEffect.DESTRUCTIVE}
        ),
        version=2,
    ),
    _spec(
        "cache-update",
        _object(
            {
                "paper_ids": {"type": "array", "items": _NONEMPTY_STRING},
                "entry_ids": {"type": "array", "items": _NONEMPTY_STRING},
                "cache_root": {"type": ["string", "null"]},
            }
        ),
        service.update_cache,
        output_schema=_object(
            {
                "records": {
                    "type": "array",
                    "items": _object(
                        {
                            "entry_id": _NONEMPTY_STRING,
                            "component": _NONEMPTY_STRING,
                            "status": {"enum": ["updated", "failed", "skipped"]},
                            "message": _STRING,
                        },
                        required=(
                            "entry_id",
                            "component",
                            "status",
                            "message",
                        ),
                    ),
                },
                "warnings": _STRING_ARRAY,
            },
            required=("records", "warnings"),
        ),
        effects=frozenset(
            {
                OperationEffect.CACHE_ADMIN,
                OperationEffect.NETWORK,
                OperationEffect.CACHE_WRITE,
            }
        ),
        version=2,
    ),
)

OPERATION_REGISTRY: Mapping[str, OperationSpec[Any]] = MappingProxyType(
    {
        key: spec
        for spec in _OPERATIONS
        for key in (spec.operation_id, spec.name)
    }
)


def get_operation(operation: str) -> OperationSpec[Any] | None:
    return OPERATION_REGISTRY.get(operation)


def resolve_operations(
    *,
    excluded_effects: frozenset[OperationEffect] = DEFAULT_EXCLUDED_EFFECTS,
) -> tuple[OperationSpec[Any], ...]:
    """Return the default agent-safe projection of the one authoritative registry."""

    return tuple(
        spec
        for spec in _OPERATIONS
        if not spec.effect_flags.intersection(excluded_effects)
    )


def registry_document(
    *,
    excluded_effects: frozenset[OperationEffect] = DEFAULT_EXCLUDED_EFFECTS,
) -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "operations": [
            spec.document()
            for spec in resolve_operations(excluded_effects=excluded_effects)
        ],
    }


def dispatch_operation(operation: str, parameters: Mapping[str, Any]) -> Any:
    spec = get_operation(operation)
    if spec is None:
        raise OperationRequestError(
            "operation_not_found", f"unknown arc-paper operation: {operation}"
        )
    return spec.invoke(parameters)


__all__ = [
    "DEFAULT_EXCLUDED_EFFECTS",
    "JsonCodec",
    "JsonOutputCodec",
    "OPERATION_REGISTRY",
    "OperationEffect",
    "OperationRequestError",
    "OperationSpec",
    "REGISTRY_SCHEMA_VERSION",
    "dispatch_operation",
    "get_operation",
    "registry_document",
    "resolve_operations",
    "to_json_value",
]
