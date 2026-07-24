"""Local business validation for model-produced Companion content."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class CompanionContentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_language_result(value: Any) -> dict[str, Any]:
    result = _mapping(value, "language result")
    classification = result.get("classification")
    tag = result.get("language_tag")
    confidence = result.get("confidence")
    if classification not in {"known", "mixed", "unknown"}:
        raise CompanionContentError(
            "language_result_invalid", "language classification is invalid"
        )
    if not isinstance(tag, str) or not tag.strip():
        raise CompanionContentError(
            "language_result_invalid", "language_tag must be non-empty"
        )
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise CompanionContentError(
            "language_result_invalid", "language confidence is invalid"
        )
    return {
        "language_tag": tag.strip(),
        "classification": classification,
        "confidence": float(confidence),
    }


def validate_chapter_plan(
    value: Any,
    *,
    chapter_id: str,
    block_ids: Sequence[str],
) -> dict[str, Any]:
    result = _mapping(value, "chapter plan")
    if result.get("chapter_id") != chapter_id:
        raise CompanionContentError(
            "chapter_plan_invalid", "chapter plan changed chapter_id"
        )
    allowed = set(block_ids)
    units = _mapping_list(result.get("learning_units"), "learning units")
    glossary = _mapping_list(
        result.get("glossary_candidates"), "glossary candidates"
    )
    evidence = _mapping_list(
        result.get("evidence_requests"), "evidence requests"
    )
    _unique(units, "unit_id", "chapter plan learning-unit")
    _unique(evidence, "request_id", "chapter plan evidence-request")
    for item in (*units, *glossary, *evidence):
        _validate_anchors(item.get("anchor_block_ids"), allowed)
    guide = result.get("guide")
    if not isinstance(guide, str) or not guide.strip():
        raise CompanionContentError(
            "chapter_plan_invalid", "chapter guide must be non-empty"
        )
    return {
        "chapter_id": chapter_id,
        "guide": guide.strip(),
        "learning_units": units,
        "glossary_candidates": glossary,
        "evidence_requests": evidence,
    }


def validate_glossary(
    value: Any,
    *,
    document_block_ids: Sequence[str],
    evidence_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], ...]:
    result = _mapping(value, "glossary")
    entries = _mapping_list(result.get("entries"), "glossary entries")
    terms: set[str] = set()
    allowed = set(document_block_ids)
    allowed_evidence = set(evidence_ids)
    for entry in entries:
        term = entry.get("term")
        definition = entry.get("definition")
        preferred = entry.get("preferred_translation")
        if not isinstance(term, str) or not term.strip():
            raise CompanionContentError(
                "glossary_invalid", "glossary term must be non-empty"
            )
        key = term.strip().casefold()
        if key in terms:
            raise CompanionContentError(
                "glossary_invalid", f"duplicate glossary term: {term}"
            )
        terms.add(key)
        if not isinstance(definition, str) or not definition.strip():
            raise CompanionContentError(
                "glossary_invalid", f"glossary definition is empty: {term}"
            )
        if preferred is not None and not isinstance(preferred, str):
            raise CompanionContentError(
                "glossary_invalid", "preferred_translation must be string or null"
            )
        _validate_anchors(entry.get("anchor_block_ids"), allowed)
        citations = entry.get("citations")
        if not isinstance(citations, list) or any(
            not isinstance(citation, str) or citation not in allowed_evidence
            for citation in citations
        ):
            raise CompanionContentError(
                "glossary_citation_invalid",
                "glossary citation does not name frozen evidence",
            )
    return tuple(entries)


def validate_chapter_draft(
    value: Any,
    *,
    plan: Mapping[str, Any],
    block_ids: Sequence[str],
    translation_required: bool,
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    result = _mapping(value, "chapter draft")
    if result.get("chapter_id") != plan.get("chapter_id"):
        raise CompanionContentError(
            "chapter_draft_invalid", "chapter draft changed chapter_id"
        )
    guide = result.get("guide")
    if not isinstance(guide, str) or not guide.strip():
        raise CompanionContentError(
            "chapter_draft_invalid", "chapter guide must be non-empty"
        )
    translations = _mapping_list(result.get("translations"), "translations")
    translation_ids = [item.get("block_id") for item in translations]
    expected = list(block_ids) if translation_required else []
    if translation_ids != expected:
        raise CompanionContentError(
            "translation_coverage_invalid",
            "translation block IDs must exactly match source order",
        )
    if any(
        not isinstance(item.get("text"), str)
        or not item["text"].strip()
        for item in translations
    ):
        raise CompanionContentError(
            "translation_coverage_invalid",
            "translation text must be a non-empty string",
        )
    units = _mapping_list(result.get("learning_units"), "learning units")
    planned = {
        item["unit_id"]: item
        for item in _mapping_list(plan.get("learning_units"), "planned learning units")
    }
    actual_ids = [item.get("unit_id") for item in units]
    if actual_ids != list(planned):
        raise CompanionContentError(
            "learning_unit_coverage_invalid",
            "learning-unit IDs must exactly match the selective plan",
        )
    allowed_evidence = set(evidence_ids)
    for item in units:
        planned_item = planned[str(item["unit_id"])]
        for immutable in ("kind", "title", "anchor_block_ids"):
            if item.get(immutable) != planned_item.get(immutable):
                raise CompanionContentError(
                    "learning_unit_anchor_invalid",
                    f"learning unit changed planned {immutable}",
                )
        citations = item.get("citations")
        if not isinstance(citations, list) or any(
            not isinstance(citation, str) or citation not in allowed_evidence
            for citation in citations
        ):
            raise CompanionContentError(
                "evidence_citation_invalid",
                "learning-unit citation does not name frozen evidence",
            )
        if not isinstance(item.get("content"), str) or not item["content"].strip():
            raise CompanionContentError(
                "learning_unit_content_invalid",
                "learning-unit content must be non-empty",
            )
    return {
        "chapter_id": str(result["chapter_id"]),
        "guide": guide.strip(),
        "translations": translations,
        "learning_units": units,
    }


def apply_safe_review(
    draft: Mapping[str, Any],
    review: Any,
    *,
    allowed_translation_block_ids: set[str],
) -> tuple[dict[str, Any], str]:
    """Apply text-only patches or raise before any source identity can change."""

    result = _mapping(review, "chapter review")
    guide = result.get("guide_replacement")
    if guide is not None and not isinstance(guide, str):
        raise CompanionContentError(
            "review_patch_unsafe", "guide replacement must be string or null"
        )
    translations = [
        dict(item) for item in _mapping_list(draft.get("translations"), "translations")
    ]
    units = [
        dict(item)
        for item in _mapping_list(draft.get("learning_units"), "learning units")
    ]
    _apply_text_patches(
        translations,
        result.get("translation_patches"),
        id_field="block_id",
        text_field="text",
        allowed_ids=allowed_translation_block_ids,
    )
    _apply_text_patches(
        units,
        result.get("learning_unit_patches"),
        id_field="unit_id",
        text_field="content",
    )
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise CompanionContentError(
            "review_patch_unsafe", "review summary must be non-empty"
        )
    return (
        {
            "chapter_id": draft["chapter_id"],
            "guide": guide if guide is not None else draft["guide"],
            "translations": translations,
            "learning_units": units,
        },
        summary.strip(),
    )


def _apply_text_patches(
    values: list[dict[str, Any]],
    patches: Any,
    *,
    id_field: str,
    text_field: str,
    allowed_ids: set[str] | None = None,
) -> None:
    patch_values = _mapping_list(patches, "review patches")
    _unique(patch_values, "id", "review patch")
    by_id = {item[id_field]: item for item in values}
    for patch in patch_values:
        patch_id = patch.get("id")
        replacement = patch.get("replacement")
        if (
            patch_id not in by_id
            or (allowed_ids is not None and patch_id not in allowed_ids)
            or not isinstance(replacement, str)
        ):
            raise CompanionContentError(
                "review_patch_unsafe",
                "review patch targets an unknown ID or invalid replacement",
            )
        by_id[patch_id][text_field] = replacement


def _validate_anchors(value: Any, allowed: set[str]) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or item not in allowed for item in value)
    ):
        raise CompanionContentError(
            "source_anchor_invalid",
            "source anchors must be unique existing block IDs",
        )


def _unique(values: Sequence[Mapping[str, Any]], key: str, label: str) -> None:
    identities = [item.get(key) for item in values]
    if any(not isinstance(item, str) or not item for item in identities):
        raise CompanionContentError(
            f"{label.replace(' ', '_')}_invalid", f"{label} ID is invalid"
        )
    if len(identities) != len(set(identities)):
        raise CompanionContentError(
            f"{label.replace(' ', '_')}_invalid", f"{label} IDs are not unique"
        )


def _mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanionContentError(
            "model_output_invalid", f"{description} must be an object"
        )
    return dict(value)


def _mapping_list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionContentError(
            "model_output_invalid", f"{description} must be an array of objects"
        )
    return [dict(item) for item in value]


__all__ = [
    "CompanionContentError",
    "apply_safe_review",
    "validate_chapter_draft",
    "validate_chapter_plan",
    "validate_glossary",
    "validate_language_result",
]
