"""Validation for selective source-anchored Companion generation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from arc_paper import (
    cached_reference_material_from_document,
    cached_reference_material_to_document,
)
from arc_jobs import canonical_json_bytes

from .rich_text import RichTextError, validate_rich_markdown


class CompanionContentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_chapter_plan(
    value: Any,
    *,
    chapter_id: str,
    block_ids: Sequence[str],
) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "chapter_id",
            "reader_profile",
            "reader_needs",
            "learning_units",
        },
        "chapter plan",
    )
    if result["chapter_id"] != chapter_id:
        raise CompanionContentError(
            "chapter_plan_invalid", "chapter plan changed chapter_id"
        )
    units = _mapping_list(result["learning_units"], "learning units")
    _unique(units, "unit_id", "chapter plan learning-unit")
    allowed_blocks = set(block_ids)
    normalized = [
        _validate_planned_unit(
            item,
            allowed_blocks=allowed_blocks,
        )
        for item in units
    ]
    reader_profile = _validate_reader_profile(result["reader_profile"])
    reader_needs = _validate_reader_needs(
        result["reader_needs"],
        block_ids=block_ids,
        units=normalized,
    )
    return {
        "chapter_id": chapter_id,
        "reader_profile": reader_profile,
        "reader_needs": reader_needs,
        "learning_units": normalized,
    }


def validate_author_identity(
    value: Any,
    *,
    block_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate a conservative publication-author attribution.

    Authorship is accepted only as high-confidence publication identity.  The
    validator deliberately makes uncertain output non-assertive rather than
    trying to rank or repair names.
    """

    result = _exact(
        value,
        {"authors", "confidence", "basis", "anchor_block_ids"},
        "author identity",
    )
    confidence = result["confidence"]
    if confidence not in {"high", "medium", "low"}:
        raise CompanionContentError(
            "author_identity_invalid", "author confidence is invalid"
        )
    authors = _string_ids(result["authors"], "authors")
    anchors = _string_ids(
        result["anchor_block_ids"], "author identity anchors"
    )
    if any(anchor not in set(block_ids) for anchor in anchors):
        raise CompanionContentError(
            "source_anchor_invalid",
            "author identity anchors must be existing block IDs",
        )
    if confidence != "high" and authors:
        raise CompanionContentError(
            "author_identity_uncertain",
            "medium- or low-confidence author identity must be empty",
        )
    if confidence == "high" and not authors:
        raise CompanionContentError(
            "author_identity_invalid",
            "high-confidence author identity requires at least one author",
        )
    if confidence == "high" and not anchors:
        raise CompanionContentError(
            "source_anchor_invalid",
            "high-confidence author identity requires source anchors",
        )
    return {
        "authors": authors,
        "confidence": confidence,
        "basis": _nonempty(result["basis"], "author identity basis"),
        "anchor_block_ids": anchors,
    }


def validate_chapter_guide(
    value: Any,
    *,
    chapter_id: str | None = None,
    block_ids: Sequence[str] = (),
    section_block_ids: Sequence[str] = (),
    plan: Mapping[str, Any] | None = None,
    allow_empty_chapter_guide: bool = False,
) -> dict[str, Any]:
    """Normalize the minimal current proposal into publication-ready units.

    ``plan`` is retained solely for decoding and testing legacy guide
    candidates. Current builds never execute an LLM chapter-plan stage.
    """

    if plan is not None:
        return _validate_legacy_chapter_guide(value, plan=plan)
    if chapter_id is None or not block_ids:
        raise CompanionContentError(
            "chapter_guide_invalid",
            "current chapter guide requires caller-owned chapter and block identity",
        )
    result = _exact(
        value,
        {"chapter_guide", "section_guides", "companions", "references"},
        "chapter Companion proposal",
    )
    chapter_guide = (
        None
        if allow_empty_chapter_guide and result["chapter_guide"] is None
        else _guide_text(result["chapter_guide"], "chapter guide")
    )
    sections = _mapping_list(result["section_guides"], "section guides")
    companions = _mapping_list(result["companions"], "companions")
    references = _minimal_references(result["references"])
    reference_ids, citation_map, unique_references = _published_reference_ids(
        references
    )

    def normalize_markdown(value: Any, description: str) -> tuple[str, list[str]]:
        markdown = _model_prose(value, description)
        for position, reference_id in citation_map.items():
            markdown = markdown.replace(f"[@{position}]", f"[@{reference_id}]")
        if re.search(r"\[@\d+\]", markdown):
            raise CompanionContentError(
                "chapter_reference_coverage_invalid",
                "positional citation refers to a missing reference",
            )
        try:
            citations = validate_rich_markdown(
                markdown,
                allowed_evidence_ids=tuple(reference_ids),
            )
        except RichTextError as exc:
            raise CompanionContentError(
                "learning_markdown_invalid", str(exc)
            ) from exc
        return markdown, list(citations)

    units: list[dict[str, Any]] = []
    if chapter_guide is not None:
        markdown, citations = normalize_markdown(
            chapter_guide["content_markdown"], "chapter-guide Markdown"
        )
        units.append(
            _generated_unit(
                chapter_id,
                kind="chapter",
                index=1,
                title=chapter_guide["title"],
                anchor=block_ids[0],
                placement="chapter",
                markdown=markdown,
                citations=citations,
            )
        )

    seen_sections: set[int] = set()
    for index, raw in enumerate(sections, 1):
        item = _located_guide(
            raw, location_key="section_number", description="section guide"
        )
        location = item["location"]
        if location in seen_sections:
            raise CompanionContentError(
                "chapter_guide_invalid",
                "section guides must use unique section numbers",
            )
        seen_sections.add(location)
        if location > len(section_block_ids):
            raise CompanionContentError(
                "source_anchor_invalid",
                "section guide number is outside the current chapter",
            )
        markdown, citations = normalize_markdown(
            item["content_markdown"], "section-guide Markdown"
        )
        units.append(
            _generated_unit(
                chapter_id,
                kind="section",
                index=index,
                title=item["title"],
                anchor=section_block_ids[location - 1],
                placement="inline",
                markdown=markdown,
                citations=citations,
            )
        )

    for index, raw in enumerate(companions, 1):
        item = _located_guide(
            raw, location_key="after_part", description="companion"
        )
        location = item["location"]
        if location > len(block_ids):
            raise CompanionContentError(
                "source_anchor_invalid",
                "companion part number is outside the current chapter",
            )
        markdown, citations = normalize_markdown(
            item["content_markdown"], "companion Markdown"
        )
        units.append(
            _generated_unit(
                chapter_id,
                kind="companion",
                index=index,
                title=item["title"],
                anchor=block_ids[location - 1],
                placement="inline",
                markdown=markdown,
                citations=citations,
            )
        )

    cited = {
        citation
        for item in units
        for citation in item["citations"]
    }
    if cited != set(reference_ids):
        raise CompanionContentError(
            "chapter_reference_coverage_invalid",
            "chapter references must be exactly the references cited by generated text",
        )
    normalized_references = [
        {
            "reference_id": reference_id,
            "title": item["title"],
            "source": item["source"],
            "dois": _dois(item["source"]),
            "arxiv_ids": _arxiv_ids(item["source"]),
            "cached_document": None,
            "cached_material": None,
        }
        for reference_id, item in zip(
            reference_ids, unique_references, strict=True
        )
    ]
    return {
        "chapter_id": chapter_id,
        "learning_units": units,
        "references": normalized_references,
    }


def _validate_legacy_chapter_guide(
    value: Any,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    result = _exact(
        value,
        {"chapter_id", "learning_units", "references"},
        "chapter learning output",
    )
    if result["chapter_id"] != plan.get("chapter_id"):
        raise CompanionContentError(
            "chapter_guide_invalid",
            "chapter learning output changed chapter_id",
        )
    units = _mapping_list(result["learning_units"], "learning units")
    _unique(units, "unit_id", "chapter guide learning-unit")
    references = _mapping_list(result["references"], "chapter references")
    _unique(references, "reference_id", "chapter reference")
    normalized_references = [
        _validate_reference(item) for item in references
    ]
    reference_ids = {
        item["reference_id"] for item in normalized_references
    }
    allowed_blocks = {
        str(item)
        for item in plan.get("reader_needs", [])
        if isinstance(item, Mapping)
        for item in (item.get("block_id"),)
        if isinstance(item, str)
    }
    if not allowed_blocks:
        allowed_blocks = {
            str(anchor)
            for unit in _mapping_list(
                plan.get("learning_units"), "planned learning units"
            )
            for anchor in unit.get("anchor_block_ids", [])
            if isinstance(anchor, str)
        }
    normalized: list[dict[str, Any]] = []
    for item in units:
        if set(item) != {
            "unit_id",
            "title",
            "anchor_block_ids",
            "placement",
            "purpose",
            "content_markdown",
        }:
            raise CompanionContentError(
                "chapter_guide_invalid",
                "learning unit has invalid fields",
            )
        placement = item["placement"]
        if placement not in {"inline", "chapter"}:
            raise CompanionContentError(
                "chapter_guide_invalid",
                "learning-unit placement is invalid",
            )
        anchors = _validate_anchors(
            item["anchor_block_ids"], allowed_blocks
        )
        content_markdown = _model_prose(
            item.get("content_markdown"),
            "learning-unit Markdown",
        )
        try:
            citations = validate_rich_markdown(
                content_markdown,
                allowed_evidence_ids=tuple(reference_ids),
            )
        except RichTextError as exc:
            raise CompanionContentError(
                "learning_markdown_invalid", str(exc)
            ) from exc
        normalized.append(
            {
                "unit_id": _nonempty(item.get("unit_id"), "learning-unit ID"),
                "title": _nonempty(item.get("title"), "learning-unit title"),
                "anchor_block_ids": anchors,
                "placement": placement,
                "purpose": _nonempty(
                    item.get("purpose"), "learning-unit purpose"
                ),
                "content_markdown": content_markdown,
                "citations": list(citations),
            }
        )
    _validate_final_reader_need_coverage(plan, normalized)
    cited = {
        citation
        for item in normalized
        for citation in item["citations"]
    }
    if cited != reference_ids:
        raise CompanionContentError(
            "chapter_reference_coverage_invalid",
            "chapter references must be exactly the references cited by learning units",
        )
    return {
        "chapter_id": str(result["chapter_id"]),
        "learning_units": normalized,
        "references": normalized_references,
    }


def _guide_text(value: Any, description: str) -> dict[str, str]:
    result = _exact(value, {"title", "content_markdown"}, description)
    return {
        "title": _nonempty(result["title"], f"{description} title"),
        "content_markdown": _model_prose(
            result["content_markdown"], f"{description} Markdown"
        ),
    }


def _located_guide(
    value: Mapping[str, Any],
    *,
    location_key: str,
    description: str,
) -> dict[str, Any]:
    result = _exact(
        value,
        {location_key, "title", "content_markdown"},
        description,
    )
    location = result[location_key]
    if (
        isinstance(location, bool)
        or not isinstance(location, int)
        or location < 1
    ):
        raise CompanionContentError(
            "source_anchor_invalid",
            f"{description} location must be a positive integer",
        )
    return {
        "location": location,
        "title": _nonempty(result["title"], f"{description} title"),
        "content_markdown": result["content_markdown"],
    }


def _minimal_references(value: Any) -> list[dict[str, str]]:
    references = _mapping_list(value, "chapter references")
    return [
        {
            "title": _nonempty(item.get("title"), "reference title"),
            "source": _validate_reference_source(
                _nonempty(item.get("source"), "reference source")
            ),
        }
        if set(item) == {"title", "source"}
        else _raise_invalid_reference()
        for item in references
    ]


def _raise_invalid_reference() -> dict[str, str]:
    raise CompanionContentError(
        "chapter_reference_invalid",
        "proposal references contain only title and source",
    )


def _validate_reference_source(source: str) -> str:
    candidates = re.findall(r"https?://[^\s<>()\]]+", source)
    if not candidates:
        candidates = [source if "://" in source else f"//{source}"]
    for candidate in candidates:
        hostname = (urlparse(candidate.rstrip(".,;")).hostname or "").casefold()
        if (
            hostname.endswith(".wikipedia.org")
            and hostname != "en.wikipedia.org"
        ):
            raise CompanionContentError(
                "chapter_reference_invalid",
                "only the English Wikipedia host is allowed",
            )
    return source


def _published_reference_ids(
    references: Sequence[Mapping[str, str]],
) -> tuple[list[str], dict[int, str], list[Mapping[str, str]]]:
    identities: list[str] = []
    positions: dict[int, str] = {}
    unique_references: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for position, item in enumerate(references, 1):
        reference_id = (
            "reference-"
            + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "title": item["title"],
                        "source": item["source"],
                    }
                )
            ).hexdigest()[:20]
        )
        if reference_id not in seen:
            seen.add(reference_id)
            identities.append(reference_id)
            unique_references.append(item)
        positions[position] = reference_id
    return identities, positions, unique_references


def _generated_unit(
    chapter_id: str,
    *,
    kind: str,
    index: int,
    title: str,
    anchor: str,
    placement: str,
    markdown: str,
    citations: Sequence[str],
) -> dict[str, Any]:
    unit_id = (
        "unit-"
        + hashlib.sha256(
            canonical_json_bytes(
                {
                    "chapter_id": chapter_id,
                    "kind": kind,
                    "index": index,
                }
            )
        ).hexdigest()[:20]
    )
    return {
        "unit_id": unit_id,
        "title": title,
        "anchor_block_ids": [anchor],
        "placement": placement,
        "purpose": kind,
        "content_markdown": markdown,
        "citations": list(citations),
    }


_DOI = re.compile(
    r"(?i)(?:doi:\s*|https?://doi\.org/)(10\.\d{4,9}/\S+)"
)
_ARXIV = re.compile(
    r"(?i)(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)"
    r"([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
)


def _dois(source: str) -> list[str]:
    return list(
        dict.fromkeys(
            match.rstrip(".,;)").casefold()
            for match in _DOI.findall(source)
        )
    )


def _arxiv_ids(source: str) -> list[str]:
    return list(dict.fromkeys(match.casefold() for match in _ARXIV.findall(source)))


def _validate_reference(item: Mapping[str, Any]) -> dict[str, Any]:
    if set(item) != {
        "reference_id",
        "title",
        "source",
        "dois",
        "arxiv_ids",
        "cached_document",
        "cached_material",
    }:
        raise CompanionContentError(
            "chapter_reference_invalid",
            "chapter reference has invalid fields",
        )
    source = _nonempty(item["source"], "reference source")
    parsed_source = source if "://" in source else f"//{source}"
    hostname = (urlparse(parsed_source).hostname or "").casefold()
    if hostname.endswith(".wikipedia.org") and hostname != "en.wikipedia.org":
        raise CompanionContentError(
            "chapter_reference_invalid",
            "only the English Wikipedia host is allowed",
        )
    cached = item["cached_document"]
    if cached is not None and not isinstance(cached, Mapping):
        raise CompanionContentError(
            "chapter_reference_invalid",
            "cached_document must be an object or null",
        )
    if cached is not None and set(cached) != {
        "source_format",
        "source_sha256",
        "source_size",
        "media_type",
        "parser_contract",
        "parsed_document_sha256",
    }:
        raise CompanionContentError(
            "chapter_reference_invalid",
            "cached_document has invalid fields",
        )
    cached_material = item["cached_material"]
    if cached_material is not None:
        try:
            cached_material = cached_reference_material_to_document(
                cached_reference_material_from_document(cached_material)
            )
        except (TypeError, ValueError) as exc:
            raise CompanionContentError(
                "chapter_reference_invalid",
                f"cached_material is invalid: {exc}",
            ) from exc
    return {
        "reference_id": _nonempty(item["reference_id"], "reference ID"),
        "title": _nonempty(item["title"], "reference title"),
        "source": source,
        "dois": _string_ids(item["dois"], "reference DOIs"),
        "arxiv_ids": _string_ids(
            item["arxiv_ids"], "reference arXiv identifiers"
        ),
        "cached_document": dict(cached) if cached is not None else None,
        "cached_material": cached_material,
    }


def _validate_final_reader_need_coverage(
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
) -> None:
    covered = {
        anchor
        for unit in units
        for anchor in unit["anchor_block_ids"]
    }
    for need in _mapping_list(plan.get("reader_needs"), "reader needs"):
        if (
            need.get("needs_companion") is True
            and need.get("block_id") not in covered
        ):
            raise CompanionContentError(
                "reader_need_coverage_invalid",
                "final learning units do not cover every required reader need",
            )


def _validate_planned_unit(
    item: Mapping[str, Any],
    *,
    allowed_blocks: set[str],
) -> dict[str, Any]:
    expected = {
        "unit_id",
        "anchor_block_ids",
        "placement",
        "purpose",
    }
    if set(item) != expected:
        raise CompanionContentError(
            "chapter_plan_invalid",
            "planned learning unit has invalid fields",
        )
    placement = item["placement"]
    if placement not in {"inline", "chapter"}:
        raise CompanionContentError(
            "chapter_plan_invalid", "learning-unit placement is invalid"
        )
    return {
        "unit_id": _nonempty(item["unit_id"], "learning-unit ID"),
        "anchor_block_ids": _validate_anchors(
            item["anchor_block_ids"], allowed_blocks
        ),
        "placement": placement,
        "purpose": _nonempty(item["purpose"], "learning-unit purpose"),
    }


def _validate_reader_profile(value: Any) -> dict[str, str]:
    profile = _exact(
        value,
        {"source_type", "assumed_background", "basis"},
        "reader profile",
    )
    source_type = profile["source_type"]
    if source_type not in {
        "user_specified",
        "popular_or_directional",
        "research_paper",
        "textbook",
        "other",
    }:
        raise CompanionContentError(
            "chapter_plan_invalid", "reader profile source_type is invalid"
        )
    return {
        "source_type": str(source_type),
        "assumed_background": _nonempty(
            profile["assumed_background"],
            "reader assumed background",
        ),
        "basis": _nonempty(profile["basis"], "reader profile basis"),
    }


def _validate_reader_needs(
    value: Any,
    *,
    block_ids: Sequence[str],
    units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    needs = _mapping_list(value, "reader needs")
    actual_ids = [item.get("block_id") for item in needs]
    if actual_ids != list(block_ids):
        raise CompanionContentError(
            "reader_needs_invalid",
            "reader needs must cover every chapter block exactly once in source order",
        )
    units_by_id = {str(item["unit_id"]): item for item in units}
    normalized: list[dict[str, Any]] = []
    for need in needs:
        if set(need) != {
            "block_id",
            "needs_companion",
            "reason",
            "learning_unit_ids",
        }:
            raise CompanionContentError(
                "reader_needs_invalid", "reader need has invalid fields"
            )
        required = need["needs_companion"]
        if not isinstance(required, bool):
            raise CompanionContentError(
                "reader_needs_invalid",
                "reader need needs_companion must be a boolean",
            )
        unit_ids = _string_ids(
            need["learning_unit_ids"], "reader need learning units"
        )
        if not required and unit_ids:
            raise CompanionContentError(
                "reader_needs_invalid",
                "a self-contained block cannot map to a learning unit",
            )
        block_id = str(need["block_id"])
        for unit_id in unit_ids:
            unit = units_by_id.get(unit_id)
            if unit is None:
                raise CompanionContentError(
                    "reader_needs_invalid",
                    "reader need cites an unknown learning unit",
                )
            if block_id not in unit["anchor_block_ids"]:
                raise CompanionContentError(
                    "reader_needs_invalid",
                    "reader need learning units must anchor the covered block",
                )
        normalized.append(
            {
                "block_id": block_id,
                "needs_companion": required,
                "reason": _nonempty(
                    need["reason"], "reader need reason"
                ),
                "learning_unit_ids": unit_ids,
            }
        )
    return normalized


def _exact(
    value: Any, fields: set[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CompanionContentError(
            "model_output_invalid", f"{description} has invalid fields"
        )
    return dict(value)


def _mapping_list(
    value: Any, description: str
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionContentError(
            "model_output_invalid",
            f"{description} must be an array of objects",
        )
    return [dict(item) for item in value]


def _nonempty(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompanionContentError(
            "model_output_invalid", f"{description} must be non-empty"
        )
    return value.strip()


def _model_prose(value: Any, description: str) -> str:
    """Decode model-escaped line breaks in fields contracted as plain prose."""

    text = _nonempty(value, description).replace(r"\r\n", "\n")
    return re.sub(r"(?<!\\)\\n", "\n", text).strip()


def _unique(
    values: Sequence[Mapping[str, Any]], key: str, description: str
) -> None:
    identities = [item.get(key) for item in values]
    if (
        any(
            not isinstance(item, str) or not item
            for item in identities
        )
        or len(identities) != len(set(identities))
    ):
        raise CompanionContentError(
            "model_output_invalid", f"{description} IDs are invalid"
        )


def _validate_anchors(
    value: Any, allowed: set[str]
) -> list[str]:
    anchors = _string_ids(
        value, "source anchors", allow_empty=False
    )
    if any(item not in allowed for item in anchors):
        raise CompanionContentError(
            "source_anchor_invalid",
            "source anchors must be unique existing block IDs",
        )
    return anchors


def _string_ids(
    value: Any,
    description: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise CompanionContentError(
            "model_output_invalid",
            f"{description} must be unique non-empty strings",
        )
    return [item.strip() for item in value]


__all__ = [
    "CompanionContentError",
    "validate_author_identity",
    "validate_chapter_guide",
    "validate_chapter_plan",
]
