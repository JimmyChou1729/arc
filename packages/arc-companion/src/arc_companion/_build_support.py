"""Shared deterministic helpers for the current Companion build handler."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from arc_jobs import (
    Awaiting,
    JsonValue,
    Paused,
    ResumeReason,
    RunContext,
    canonical_json_bytes,
)

from .contracts import (
    AcceptedBook,
    AcceptedChapter,
    CompanionContentCodec,
    EvidenceSource,
)
from .generation_validation import CompanionContentError


_EVIDENCE_ARTIFACT = "planning/evidence"
_EVIDENCE_REQUEST_ARTIFACT = "planning/evidence-request"
_EVIDENCE_INTERACTION_SCHEMA = "arc.companion.evidence_response.v2"
_SUPERVISION_SCHEMA = "arc.companion.review_supervision.v1"


def collect_evidence(
    context: RunContext,
    request_plan: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], ...]] | Paused:
    """Freeze a multi-candidate research log and its selected evidence."""

    requests = mapping_list(
        request_plan.get("requests"), "literature requests"
    )
    request_ids = [str(item["request_id"]) for item in requests]
    if len(set(request_ids)) != len(request_ids):
        raise CompanionContentError(
            "evidence_request_invalid",
            "evidence request IDs must be unique across the book",
        )
    existing = context.artifacts.find(_EVIDENCE_ARTIFACT)
    if existing is not None:
        document = read_json(context, existing, "frozen evidence")
        if set(document) != {
            "schema_version",
            "research_log",
            "selected_evidence",
        } or document.get("schema_version") != _EVIDENCE_INTERACTION_SCHEMA:
            raise CompanionContentError(
                "evidence_response_invalid",
                "frozen evidence has invalid fields",
            )
        collection = _validate_evidence_responses(
            document.get("research_log"), requests=requests
        )
        if list(collection["selected_evidence"]) != document.get(
            "selected_evidence"
        ):
            raise CompanionContentError(
                "evidence_response_invalid",
                "frozen selected evidence does not match the research log",
            )
        return collection
    digest = hashlib.sha256(canonical_json_bytes(requests)).hexdigest()
    resume_key = f"evidence-{digest[:24]}"
    value = context.resume_input
    if value is not None and value.get("resume_key") == resume_key:
        if set(value) != {"schema_version", "resume_key", "responses"}:
            raise CompanionContentError(
                "evidence_response_invalid",
                "evidence response has invalid fields",
            )
        if value.get("schema_version") != _EVIDENCE_INTERACTION_SCHEMA:
            raise CompanionContentError(
                "evidence_response_invalid",
                "evidence response schema is unsupported",
            )
        collection = _validate_evidence_responses(
            value.get("responses"), requests=requests
        )
        context.artifacts.publish_json(
            _EVIDENCE_ARTIFACT,
            {
                "schema_version": _EVIDENCE_INTERACTION_SCHEMA,
                "research_log": list(collection["research_log"]),
                "selected_evidence": list(
                    collection["selected_evidence"]
                ),
            },
        )
        return collection

    request_ref = context.artifacts.find(_EVIDENCE_REQUEST_ARTIFACT)
    if request_ref is None:
        request_ref = context.artifacts.publish_json(
            _EVIDENCE_REQUEST_ARTIFACT,
            {
                "schema_version": "arc.companion.evidence_request.v2",
                "resume_key": resume_key,
                "response_schema": _evidence_response_schema(request_ids),
                "minimum_candidate_count": 20,
                "candidate_categories": [
                    "source_named",
                    "important_prior_history",
                    "later_core_debate",
                ],
                "requests": [
                    {
                        "request_id": item["request_id"],
                        "kind": item["kind"],
                        "query": item["query"],
                        "purpose": item["purpose"],
                        "anchors": list(item["anchor_block_ids"]),
                    }
                    for item in requests
                ],
            },
        )
    return Paused(
        Awaiting(
            ResumeReason.INTERACTION_REQUIRED,
            resume_key,
            True,
            request_ref,
            _EVIDENCE_INTERACTION_SCHEMA,
            {"request_count": len(requests)},
        )
    )


def review_supervision(
    context: RunContext,
    chapter_id: str,
    draft: Mapping[str, Any],
    review: Any,
    error: CompanionContentError,
) -> dict[str, Any] | Paused:
    """Pause for the sole safe response to an invalid guide-review patch."""

    digest = hashlib.sha256(
        canonical_json_bytes({"chapter_id": chapter_id, "review": review})
    ).hexdigest()[:24]
    resume_key = f"review-{digest}"
    value = context.resume_input
    if value is not None and value.get("resume_key") == resume_key:
        if set(value) != {"schema_version", "resume_key", "action"}:
            raise CompanionContentError(
                "review_supervision_invalid",
                "review supervision response has invalid fields",
            )
        if (
            value.get("schema_version") != _SUPERVISION_SCHEMA
            or value.get("action") != "discard_review"
        ):
            raise CompanionContentError(
                "review_supervision_invalid",
                "only discard_review is supported for an unsafe patch",
            )
        return dict(draft)
    request_ref = context.artifacts.find(
        f"chapters/{chapter_id}/review-supervision"
    )
    if request_ref is None:
        request_ref = context.artifacts.publish_json(
            f"chapters/{chapter_id}/review-supervision",
            {
                "schema_version": _SUPERVISION_SCHEMA,
                "resume_key": resume_key,
                "reason": error.code,
                "message": str(error),
                "allowed_actions": ["discard_review"],
            },
        )
    return Paused(
        Awaiting(
            ResumeReason.SUPERVISION_REQUIRED,
            resume_key,
            True,
            request_ref,
            _SUPERVISION_SCHEMA,
            {"chapter_id": chapter_id, "code": error.code},
        )
    )


def task_id(prefix: str, semantic: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(dict(semantic))
    ).hexdigest()
    return f"{prefix}-{digest[:24]}"


def planned_source_documents(
    plan: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep only source blocks explicitly anchored by selective chapter work."""

    anchored: set[str] = set()
    for item in mapping_list(
        plan.get("learning_units"), "learning units"
    ):
        block_ids = item.get("anchor_block_ids")
        if not isinstance(block_ids, list):
            raise CompanionContentError(
                "chapter_plan_invalid",
                "learning_units anchor_block_ids must be an array",
            )
        anchored.update(str(block_id) for block_id in block_ids)
    return [block for block in blocks if block.get("block_id") in anchored]


def bibliography_contracts(
    evidence: Sequence[Mapping[str, Any]],
    *,
    cited_ids: Sequence[str] | None = None,
) -> tuple[EvidenceSource, ...]:
    allowed = (
        set(cited_ids) if cited_ids is not None else None
    )
    return tuple(
        EvidenceSource(
            evidence_id=str(item["evidence_id"]),
            title=str(item["title"]),
            source=str(item["source"]),
        )
        for item in evidence
        if allowed is None or str(item["evidence_id"]) in allowed
    )


def evidence_digest(evidence: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(list(evidence))
    ).hexdigest()


def accepted_chapter_document(
    chapter: AcceptedChapter,
) -> dict[str, Any]:
    placeholder = AcceptedBook(
        document_digest="0" * 64,
        title="chapter",
        source_language="und",
        target_language="und",
        translation_mode=(
            "enabled" if chapter.translations else "skipped"
        ),
        chapters=(chapter,),
        bibliography=(),
    )
    return CompanionContentCodec.to_document(placeholder)["chapters"][0]


def ref_document(ref: Any) -> dict[str, JsonValue]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": {
            "algorithm": ref.digest.algorithm,
            "value": ref.digest.value,
            "size_bytes": ref.digest.size_bytes,
        },
        "media_type": ref.media_type,
        "relative_path": ref.relative_path,
    }


def read_json(
    context: RunContext, ref: Any, description: str
) -> dict[str, Any]:
    try:
        value = json.loads(
            context.artifacts.read_bytes(ref).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"cannot decode {description}: {exc}",
        ) from exc
    return mapping(value, description)


def mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"{description} must be an object",
        )
    return dict(value)


def mapping_list(
    value: Any, description: str
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionContentError(
            "companion_artifact_invalid",
            f"{description} must be an array of objects",
        )
    return [dict(item) for item in value]


def _validate_evidence_responses(
    value: Any,
    *,
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    responses = mapping_list(value, "evidence responses")
    expected_ids = [str(item["request_id"]) for item in requests]
    by_request: dict[str, dict[str, Any]] = {}
    evidence_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    expected_fields = {
        "request_id",
        "candidates",
        "selected_evidence_ids",
        "selection_rationale",
    }
    for response in responses:
        if set(response) != expected_fields:
            raise CompanionContentError(
                "evidence_response_invalid",
                "each evidence response must contain exactly request_id, "
                "candidates, selected_evidence_ids, and selection_rationale",
            )
        request_id = response["request_id"]
        rationale = response["selection_rationale"]
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise CompanionContentError(
                "evidence_response_invalid",
                "evidence response identity and rationale must be non-empty strings",
            )
        if request_id in by_request:
            raise CompanionContentError(
                "evidence_response_invalid",
                f"duplicate evidence response for request {request_id}",
            )
        candidates = mapping_list(
            response["candidates"], "evidence candidates"
        )
        candidate_by_id: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            if set(candidate) != {
                "evidence_id",
                "title",
                "content",
                "source",
            } or any(
                not isinstance(candidate[field], str)
                or not str(candidate[field]).strip()
                for field in (
                    "evidence_id",
                    "title",
                    "content",
                    "source",
                )
            ):
                raise CompanionContentError(
                    "evidence_response_invalid",
                    "evidence candidates require non-empty evidence_id, "
                    "title, content, and source",
                )
            evidence_id = str(candidate["evidence_id"]).strip()
            if evidence_id in evidence_ids:
                raise CompanionContentError(
                    "evidence_response_invalid",
                    f"duplicate evidence ID {evidence_id}",
                )
            normalized = {
                "evidence_id": evidence_id,
                "title": str(candidate["title"]).strip(),
                "content": str(candidate["content"]).strip(),
                "source": str(candidate["source"]).strip(),
            }
            candidate_by_id[evidence_id] = normalized
            evidence_ids.add(evidence_id)
        selected_ids = response["selected_evidence_ids"]
        if (
            not isinstance(selected_ids, list)
            or any(
                not isinstance(item, str)
                or item not in candidate_by_id
                for item in selected_ids
            )
            or len(selected_ids) != len(set(selected_ids))
        ):
            raise CompanionContentError(
                "evidence_response_invalid",
                "selected evidence IDs must be unique candidates from the same request",
            )
        normalized_response = {
            "request_id": request_id,
            "candidates": list(candidate_by_id.values()),
            "selected_evidence_ids": list(selected_ids),
            "selection_rationale": rationale.strip(),
        }
        by_request[request_id] = normalized_response
        selected.extend(
            {
                "request_id": request_id,
                **candidate_by_id[evidence_id],
            }
            for evidence_id in selected_ids
        )
    if (
        set(by_request) != set(expected_ids)
        or len(by_request) != len(expected_ids)
    ):
        raise CompanionContentError(
            "evidence_response_invalid",
            "evidence responses must exactly cover every planned request ID",
        )
    if requests and len(evidence_ids) < 20:
        raise CompanionContentError(
            "evidence_response_invalid",
            "the research log must inspect at least 20 distinct candidates",
        )
    return {
        "research_log": tuple(
            by_request[request_id] for request_id in expected_ids
        ),
        "selected_evidence": tuple(selected),
    }


def _evidence_response_schema(
    request_ids: Sequence[str],
) -> dict[str, Any]:
    nonempty = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "properties": {
            "schema_version": {
                "const": _EVIDENCE_INTERACTION_SCHEMA,
            },
            "resume_key": nonempty,
            "responses": {
                "type": "array",
                "minItems": len(request_ids),
                "maxItems": len(request_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "enum": list(request_ids),
                        },
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "evidence_id": nonempty,
                                    "title": nonempty,
                                    "content": nonempty,
                                    "source": nonempty,
                                },
                                "required": [
                                    "evidence_id",
                                    "title",
                                    "content",
                                    "source",
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "selected_evidence_ids": {
                            "type": "array",
                            "items": nonempty,
                            "uniqueItems": True,
                        },
                        "selection_rationale": nonempty,
                    },
                    "required": [
                        "request_id",
                        "candidates",
                        "selected_evidence_ids",
                        "selection_rationale",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["schema_version", "resume_key", "responses"],
        "additionalProperties": False,
    }


__all__ = [
    "accepted_chapter_document",
    "bibliography_contracts",
    "collect_evidence",
    "evidence_digest",
    "mapping",
    "mapping_list",
    "planned_source_documents",
    "read_json",
    "ref_document",
    "review_supervision",
    "task_id",
]
