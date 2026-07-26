"""Shared deterministic helpers for the current Companion build handler."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from arc_jobs import (
    JsonValue,
    Paused,
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
_EVIDENCE_RESEARCH_SCHEMA = "arc.companion.evidence_research.v1"
_LEGACY_EVIDENCE_INTERACTION_SCHEMA = "arc.companion.evidence_response.v2"
_SUPERVISION_SCHEMA = "arc.companion.review_supervision.v1"


def frozen_evidence(
    context: RunContext,
    request_plan: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], ...]] | None:
    """Load and validate a current or legacy frozen evidence artifact."""

    existing = context.artifacts.find(_EVIDENCE_ARTIFACT)
    if existing is None:
        return None
    requests = mapping_list(
        request_plan.get("requests"), "literature requests"
    )
    request_ids = [str(item["request_id"]) for item in requests]
    if len(set(request_ids)) != len(request_ids):
        raise CompanionContentError(
            "evidence_request_invalid",
            "evidence request IDs must be unique across the book",
        )
    document = read_json(context, existing, "frozen evidence")
    if set(document) != {
        "schema_version",
        "research_log",
        "selected_evidence",
    } or document.get("schema_version") not in {
        _EVIDENCE_RESEARCH_SCHEMA,
        _LEGACY_EVIDENCE_INTERACTION_SCHEMA,
    }:
        raise CompanionContentError(
            "evidence_response_invalid",
            "frozen evidence has invalid fields",
        )
    collection = validate_evidence_research(
        {"responses": document.get("research_log")},
        requests=requests,
    )
    if list(collection["selected_evidence"]) != document.get(
        "selected_evidence"
    ):
        raise CompanionContentError(
            "evidence_response_invalid",
            "frozen selected evidence does not match the research log",
        )
    return collection


def freeze_evidence(
    context: RunContext,
    request_plan: Mapping[str, Any],
    value: Any,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Validate model research and publish the immutable selected subset."""

    requests = mapping_list(
        request_plan.get("requests"), "literature requests"
    )
    collection = validate_evidence_research(value, requests=requests)
    context.artifacts.publish_json(
        _EVIDENCE_ARTIFACT,
        {
            "schema_version": _EVIDENCE_RESEARCH_SCHEMA,
            "research_log": list(collection["research_log"]),
            "selected_evidence": list(collection["selected_evidence"]),
        },
    )
    return collection


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
    by_id = {
        str(item["evidence_id"]): item
        for item in evidence
    }
    ordered_ids = (
        tuple(by_id)
        if cited_ids is None
        else tuple(dict.fromkeys(str(item) for item in cited_ids))
    )
    return tuple(
        EvidenceSource(
            evidence_id=evidence_id,
            title=str(by_id[evidence_id]["title"]),
            source=str(by_id[evidence_id]["source"]),
        )
        for evidence_id in ordered_ids
        if evidence_id in by_id
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


def validate_evidence_research(
    value: Any,
    *,
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    if not isinstance(value, Mapping) or set(value) != {"responses"}:
        raise CompanionContentError(
            "evidence_response_invalid",
            "evidence research must contain exactly responses",
        )
    responses = mapping_list(value.get("responses"), "evidence responses")
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
            _validate_wikipedia_source(normalized["source"])
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


def _validate_wikipedia_source(source: str) -> None:
    hostname = urlparse(source).hostname
    if hostname is None:
        return
    hostname = hostname.rstrip(".").lower()
    if (
        hostname == "wikipedia.org"
        or hostname.endswith(".wikipedia.org")
    ) and hostname != "en.wikipedia.org":
        raise CompanionContentError(
            "evidence_response_invalid",
            "Wikipedia evidence must use en.wikipedia.org",
        )


__all__ = [
    "accepted_chapter_document",
    "bibliography_contracts",
    "evidence_digest",
    "freeze_evidence",
    "frozen_evidence",
    "mapping",
    "mapping_list",
    "planned_source_documents",
    "read_json",
    "ref_document",
    "review_supervision",
    "task_id",
    "validate_evidence_research",
]
