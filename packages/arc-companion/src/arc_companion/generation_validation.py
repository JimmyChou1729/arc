"""Validation for selective source-anchored Companion generation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from arc_jobs import canonical_json_bytes

from .rich_text import RichTextError, validate_rich_markdown


class CompanionContentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    chapter_anchor_block_id: str | None = None,
    section_block_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Normalize the minimal proposal into publication-ready units."""
    if chapter_id is None or not block_ids:
        raise CompanionContentError(
            "chapter_guide_invalid",
            "current chapter guide requires caller-owned chapter and block identity",
        )
    if (
        chapter_anchor_block_id is None
        or chapter_anchor_block_id not in block_ids
    ):
        raise CompanionContentError(
            "source_anchor_invalid",
            "chapter guide requires a program-owned display anchor in the chapter",
        )
    result = _exact(
        value,
        {"chapter_guide", "section_guides", "companions", "references"},
        "chapter Companion proposal",
    )
    chapter_guide = (
        None
        if result["chapter_guide"] is None
        else _guide_text(result["chapter_guide"], "chapter guide")
    )
    sections = _mapping_list(result["section_guides"], "section guides")
    companions = _mapping_list(result["companions"], "companions")
    references = _minimal_references(result["references"])
    reference_ids, citation_map, unique_references = _published_reference_ids(
        references
    )

    def normalize_markdown(value: Any, description: str) -> tuple[str, list[str]]:
        markdown = _without_leading_heading(
            _model_prose(value, description),
            description=description,
        )
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
        return markdown, list(dict.fromkeys(citations))

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
                anchor=chapter_anchor_block_id,
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


def validate_chapter_guide_review_audit(
    review: Any,
    *,
    proposal: Mapping[str, Any],
    part_count: int,
    section_count: int,
    program_companions: Sequence[Mapping[str, Any]] = (),
    program_section_guides: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Require model-authored final content to use reviewer-inspected locations."""

    if not isinstance(review, Mapping):
        raise CompanionContentError(
            "chapter_guide_review_audit_invalid",
            "chapter guide requires a completed source review",
        )
    payload = _exact(
        review.get("payload"),
        {
            "checked_complete_chapter",
            "checked_part_numbers",
            "checked_section_numbers",
        },
        "chapter guide review audit",
    )
    if payload["checked_complete_chapter"] is not True:
        raise CompanionContentError(
            "chapter_guide_review_audit_invalid",
            "reviewer must inspect the complete current chapter",
        )
    checked_parts = _positive_integer_ids(
        payload["checked_part_numbers"],
        maximum=part_count,
        description="reviewed part numbers",
    )
    checked_sections = _positive_integer_ids(
        payload["checked_section_numbers"],
        maximum=section_count,
        description="reviewed section numbers",
    )
    companions = _mapping_list(
        proposal.get("companions"), "companions"
    )
    sections = _mapping_list(
        proposal.get("section_guides"), "section guides"
    )
    required_parts = {
        item.get("after_part")
        for item in companions
        if item not in program_companions
        if isinstance(item.get("after_part"), int)
        and not isinstance(item.get("after_part"), bool)
    }
    required_sections = {
        item.get("section_number")
        for item in sections
        if item not in program_section_guides
        if isinstance(item.get("section_number"), int)
        and not isinstance(item.get("section_number"), bool)
    }
    if not required_parts.issubset(set(checked_parts)):
        raise CompanionContentError(
            "chapter_guide_review_audit_invalid",
            "final companions use parts the reviewer did not inspect",
        )
    if not required_sections.issubset(set(checked_sections)):
        raise CompanionContentError(
            "chapter_guide_review_audit_invalid",
            "final section guides use sections the reviewer did not inspect",
        )
    return {
        "checked_complete_chapter": True,
        "checked_part_numbers": checked_parts,
        "checked_section_numbers": checked_sections,
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


def _without_leading_heading(text: str, *, description: str) -> str:
    """Keep the structured title as the sole leading unit heading."""

    lines = text.splitlines()
    first = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first is None:
        raise CompanionContentError(
            "model_output_invalid", f"{description} must be non-empty"
        )
    remove_through: int | None = None
    if re.fullmatch(r"\s{0,3}#{1,6}\s+\S.*", lines[first]):
        remove_through = first
    elif (
        first + 1 < len(lines)
        and lines[first].strip()
        and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", lines[first + 1])
    ):
        remove_through = first + 1
    if remove_through is None:
        return text
    body = "\n".join(lines[remove_through + 1 :]).strip()
    if not body:
        raise CompanionContentError(
            "learning_markdown_invalid",
            f"{description} contains only a duplicate heading",
        )
    return body


def _positive_integer_ids(
    value: Any,
    *,
    maximum: int,
    description: str,
) -> list[int]:
    if (
        not isinstance(value, list)
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 1
            or item > maximum
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise CompanionContentError(
            "chapter_guide_review_audit_invalid",
            f"{description} must be unique locations in the current chapter",
        )
    return sorted(value)


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
    "validate_chapter_guide_review_audit",
]
