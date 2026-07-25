"""Pure, deterministic foundation-candidate and selection logic.

Acquisition, durable orchestration, and LLM execution deliberately live outside
this module.  The domain runner supplies already acquired paper documents and
applies the prompt/result boundaries defined here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from arc_paper import ReferenceInferenceResult, extract_paper_ids, normalize_paper_id

from .text import normalize_authors, paper_key, token_overlap_score


@dataclass(frozen=True)
class FoundationHeuristics:
    """Configurable, deterministic foundation-selection heuristics.

    The citation threshold is a prioritization rule rather than an exclusion
    rule: a lower-citation paper can still win if the supplied evidence offers
    no stronger same-scope foundation.
    """

    min_citation_count: int = 100
    candidate_limit: int = 10
    candidate_scan_limit: int = 20
    max_citation_count: int = 1000

    def __post_init__(self) -> None:
        if self.min_citation_count < 0:
            raise ValueError("min_citation_count must be non-negative")
        if self.max_citation_count < self.min_citation_count:
            raise ValueError("max_citation_count must be at least min_citation_count")
        if self.candidate_limit < 1 or self.candidate_scan_limit < 1:
            raise ValueError("candidate limits must be positive")


DEFAULT_FOUNDATION_HEURISTICS = FoundationHeuristics()
MIN_FOUNDATION_CITATION_COUNT = DEFAULT_FOUNDATION_HEURISTICS.min_citation_count
MAX_FOUNDATION_CITATION_COUNT = DEFAULT_FOUNDATION_HEURISTICS.max_citation_count
MAX_AUDIT_SEARCH_QUERIES = 3
LLM_CANDIDATE_SOURCE_ROLE = "llm_added_foundation_candidate"
LLM_SELECTION_MARK_FIELDS = (
    "source_role",
    "llm_added",
    "llm_recommended",
    "llm_addition_reason",
    "llm_reference_query",
    "llm_verified_evidence_urls",
    "llm_reference_inference",
)


FOUNDATION_SELECTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "arc.domain-foundation-selection-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "selected_foundation",
        "best_reference_paper",
        "parent_foundations",
        "rejected_candidates",
        "reasoning",
        "warnings",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": "arc.domain_foundation_selection.v1"},
        "selected_foundation": {"$ref": "#/$defs/paper_choice"},
        "best_reference_paper": {"$ref": "#/$defs/paper_choice"},
        "parent_foundations": {"type": "array", "items": {"$ref": "#/$defs/paper_choice"}},
        "rejected_candidates": {"type": "array", "items": {"$ref": "#/$defs/paper_choice"}},
        "reasoning": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "$defs": {
        "paper_choice": {
            "type": "object",
            "additionalProperties": False,
            "required": ["paper_id", "title", "reason"],
            "properties": {
                "paper_id": {"type": "string"},
                "title": {"type": "string"},
                "reason": {"type": "string"},
            },
        }
    },
}


FOUNDATION_CANDIDATE_AUDIT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "arc.domain-foundation-candidate-audit-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "candidate_set_sufficient",
        "confidence",
        "search_queries",
        "citation_directions",
        "reasoning",
        "warnings",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": "arc.domain_foundation_candidate_audit.v1"},
        "candidate_set_sufficient": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["complete", "high", "medium", "low"]},
        "search_queries": {
            "type": "array",
            "maxItems": MAX_AUDIT_SEARCH_QUERIES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query", "reason", "confidence"],
                "properties": {
                    "query": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["complete", "high", "medium", "low"]},
                },
            },
        },
        "citation_directions": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "reasoning": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


def build_candidate_records(
    *,
    seed_metadata: Mapping[str, Any],
    seed_references: Sequence[Mapping[str, Any]],
    newest_citers: Sequence[Mapping[str, Any]],
    refs_by_citer: Mapping[str, Sequence[Mapping[str, Any]]],
    metadata_by_id: Mapping[str, Mapping[str, Any]],
    intent: str,
    heuristics: FoundationHeuristics = DEFAULT_FOUNDATION_HEURISTICS,
) -> list[dict[str, Any]]:
    """Build the bounded candidate set from already acquired paper data.

    References shared by the newest citers are the main evidence.  The seed
    itself and direct seed references are retained as eligible candidates, even
    when no citer reference lists mention them.
    """

    seed_id = _paper_id(seed_metadata)
    citer_ids = [_paper_id(citer) for citer in newest_citers]
    known_citer_ids = [paper_id for paper_id in citer_ids if paper_id]
    refs_by_normalized_citer = {
        _normalized_id(citer_id): refs
        for citer_id, refs in refs_by_citer.items()
        if _normalized_id(citer_id) and _is_paper_sequence(refs)
    }

    overlap: Counter[str] = Counter()
    support: dict[str, list[str]] = defaultdict(list)
    embedded: dict[str, Mapping[str, Any]] = {}
    for citer_id in known_citer_ids:
        references = refs_by_normalized_citer.get(citer_id, ())
        seen_in_citer: set[str] = set()
        for reference in references:
            reference_id = _paper_id(reference)
            if not reference_id or reference_id in seen_in_citer:
                continue
            seen_in_citer.add(reference_id)
            overlap[reference_id] += 1
            support[reference_id].append(citer_id)
            embedded.setdefault(reference_id, reference)

    if seed_id:
        overlap.setdefault(seed_id, 0)
        embedded.setdefault(seed_id, seed_metadata)
    seed_reference_ids: set[str] = set()
    for reference in seed_references:
        reference_id = _paper_id(reference)
        if not reference_id:
            continue
        seed_reference_ids.add(reference_id)
        overlap.setdefault(reference_id, 0)
        embedded.setdefault(reference_id, reference)

    normalized_metadata = {
        _normalized_id(paper_id): document
        for paper_id, document in metadata_by_id.items()
        if _normalized_id(paper_id) and _usable_metadata(document)
    }
    candidate_ids = sorted(
        overlap,
        key=lambda paper_id: (
            -overlap[paper_id],
            -_citation_count(normalized_metadata.get(paper_id) or embedded.get(paper_id, {})),
            paper_id,
        ),
    )[: max(0, heuristics.candidate_scan_limit)]

    records: list[dict[str, Any]] = []
    for rank, candidate_id in enumerate(candidate_ids, start=1):
        metadata = normalized_metadata.get(candidate_id) or embedded.get(candidate_id, {})
        source_role = (
            "seed"
            if candidate_id == seed_id
            else "seed_reference"
            if candidate_id in seed_reference_ids
            else "common_reference"
        )
        records.append(
            _metadata_candidate_record(
                candidate_id=candidate_id,
                metadata=metadata,
                fallback=embedded.get(candidate_id, {}),
                rank=rank,
                intent=intent,
                source_role=source_role,
                witness_citation_overlap=int(overlap[candidate_id]),
                supported_by=support.get(candidate_id, []),
                min_citation_count=heuristics.min_citation_count,
                max_citation_count=heuristics.max_citation_count,
            )
        )
    return records[: max(0, heuristics.candidate_limit)]


def candidate_audit_prompt(
    *,
    seed_metadata: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    intent: str,
    min_citation_count: int = MIN_FOUNDATION_CITATION_COUNT,
    max_citation_count: int = MAX_FOUNDATION_CITATION_COUNT,
) -> str:
    """Return the fixed prompt contract for an external candidate audit."""

    lines = [
            "You audit a theoretical-physics foundation-paper candidate set before selection.",
            "Decide whether the supplied candidates are sufficient for choosing the same-scope foundation paper.",
            "Propose a search_queries entry only when you are completely sure a likely foundational or canonical same-scope paper is missing.",
            "A search query must be plain search terms: do not include a paper ID, title quotation, URL, or a paper invented from memory.",
            "Citation directions are optional hints such as references/citers to inspect; they are not selected papers.",
            f"Low-citation heuristic: fewer than {min_citation_count} citations normally means low priority as selected foundation unless no better-supported same-scope foundation is available.",
            f"User intent:\n{intent or '(none)'}",
            f"Seed paper:\n{dict(seed_metadata)}",
            f"Candidate papers:\n{[dict(candidate) for candidate in candidates]}",
            "Return JSON only.",
    ]
    lines.insert(
        5,
        "Citation counts are a soft scope prior: papers below "
        f"{min_citation_count} citations may indicate a too-shallow field, while "
        f"papers above {max_citation_count} may be broader parent domains. "
        "Evidence for a canonical same-scope origin may override either signal.",
    )
    return "\n\n".join(lines)


def default_candidate_audit() -> dict[str, Any]:
    """The conservative audit used when an external audit is unavailable."""

    return {
        "schema_version": "arc.domain_foundation_candidate_audit.v1",
        "candidate_set_sufficient": True,
        "confidence": "low",
        "search_queries": [],
        "citation_directions": [],
        "reasoning": "No reliable audit expansion; use deterministic candidates.",
        "warnings": [],
    }


def normalize_candidate_audit(audit: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a closed, schema-shaped audit document without trusting extras."""

    source = audit if isinstance(audit, Mapping) else {}
    warnings = _string_list(source.get("warnings"))
    if not isinstance(audit, Mapping):
        warnings.append("candidate_audit_not_object")

    raw_sufficient = source.get("candidate_set_sufficient")
    if isinstance(raw_sufficient, bool):
        sufficient = raw_sufficient
    else:
        sufficient = True
        if raw_sufficient is not None:
            warnings.append("candidate_audit_invalid_candidate_set_sufficient")

    confidence = _confidence(source.get("confidence"))
    if source.get("confidence") is not None and confidence == "low" and str(source.get("confidence")).strip().lower() != "low":
        warnings.append("candidate_audit_invalid_confidence")

    search_queries: list[dict[str, str]] = []
    raw_queries = source.get("search_queries")
    if raw_queries is not None and not isinstance(raw_queries, list):
        warnings.append("candidate_audit_invalid_search_queries")
    elif isinstance(raw_queries, list):
        for raw_query in raw_queries:
            if len(search_queries) >= MAX_AUDIT_SEARCH_QUERIES:
                warnings.append("candidate_audit_search_queries_truncated")
                break
            if not isinstance(raw_query, Mapping):
                warnings.append("candidate_audit_invalid_search_query")
                continue
            query = _text(raw_query.get("query"))
            if not query:
                warnings.append("candidate_audit_empty_search_query")
                continue
            query_confidence = _confidence(raw_query.get("confidence"))
            if raw_query.get("confidence") is not None and query_confidence == "low" and str(raw_query.get("confidence")).strip().lower() != "low":
                warnings.append(f"candidate_audit_invalid_query_confidence:{query}")
            search_queries.append(
                {
                    "query": query,
                    "reason": _text(raw_query.get("reason")),
                    "confidence": query_confidence,
                }
            )

    raw_directions = source.get("citation_directions")
    citation_directions = (
        [_text(item) for item in raw_directions if _text(item)][:5]
        if isinstance(raw_directions, list)
        else []
    )
    if raw_directions is not None and not isinstance(raw_directions, list):
        warnings.append("candidate_audit_invalid_citation_directions")

    return {
        "schema_version": "arc.domain_foundation_candidate_audit.v1",
        "candidate_set_sufficient": sufficient,
        "confidence": confidence,
        "search_queries": search_queries,
        "citation_directions": citation_directions,
        "reasoning": _text(source.get("reasoning")),
        "warnings": _dedupe_strings(warnings),
    }


def audit_expansion_request(audit: Mapping[str, Any], intent: str) -> str | None:
    """Build one verifier request, or return ``None`` when expansion is unsafe.

    Expansion is intentionally gated on all three conditions: an explicitly
    insufficient candidate set, complete audit confidence, and at least one
    complete search hint that contains no paper identifier.
    """

    normalized = normalize_candidate_audit(audit)
    if normalized["candidate_set_sufficient"] is not False:
        return None
    if normalized["confidence"] != "complete":
        return None
    hints = [
        item
        for item in normalized["search_queries"]
        if item["confidence"] == "complete" and not extract_paper_ids(item["query"])
    ]
    if not hints:
        return None

    lines = [
        "Find and verify up to two missing foundational or canonical same-scope papers.",
        "Use the following independent, identifier-free search hints:",
    ]
    lines.extend(f"- {query}" for query in dict.fromkeys(item["query"] for item in hints))
    reasons = list(
        dict.fromkeys(
            item["reason"]
            for item in hints
            if item["reason"] and not extract_paper_ids(item["reason"])
        )
    )
    if reasons:
        lines.append("Audit reasons:")
        lines.extend(f"- {reason}" for reason in reasons)
    if intent and not extract_paper_ids(intent):
        lines.append(f"User intent: {intent}")
    return "\n".join(lines)


def apply_reference_inference_result(
    candidates: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
    result_document: Mapping[str, Any] | ReferenceInferenceResult,
    metadata_by_id: Mapping[str, Mapping[str, Any]],
    intent: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply only verifier-confirmed reference results to the candidate set."""

    expanded = [dict(candidate) for candidate in candidates]
    request = audit_expansion_request(audit, intent)
    report: dict[str, Any] = {
        "schema_version": "arc.domain_foundation_candidate_expansion.v1",
        "initial_candidate_count": len(candidates),
        "expanded_candidate_count": len(expanded),
        "added_candidate_count": 0,
        "added_papers": [],
        "request": request,
        "warnings": [],
    }
    if request is None:
        report["status"] = "skipped_audit_gate"
        return expanded, report

    document = _reference_document(result_document)
    if document is None:
        report["status"] = "invalid_reference_inference_result"
        report["warnings"].append("reference_inference_result_not_document")
        return expanded, report

    verified_by_id = _verified_references_by_id(document)
    result_ids = _normalized_ids(document.get("paper_ids"))
    eligible_ids = [paper_id for paper_id in result_ids if paper_id in verified_by_id]
    report["warnings"].extend(_string_list(document.get("warnings")))
    report["focus_scope"] = _text(document.get("focus_scope"))
    if not eligible_ids:
        report["status"] = "no_verified_papers"
        report["warnings"].append("reference_inference_returned_no_verified_ids")
        report["warnings"] = _dedupe_strings(report["warnings"])
        return expanded, report

    normalized_metadata = {
        _normalized_id(paper_id): metadata
        for paper_id, metadata in metadata_by_id.items()
        if _normalized_id(paper_id) and _usable_metadata(metadata)
    }
    candidate_by_id = {
        _paper_id(candidate): candidate
        for candidate in expanded
        if _paper_id(candidate)
    }
    for paper_id in eligible_ids:
        verified = verified_by_id[paper_id]
        if paper_id in candidate_by_id:
            _mark_existing_llm_recommended(candidate_by_id[paper_id], request=request, verified=verified)
            continue
        metadata = normalized_metadata.get(paper_id)
        if not metadata:
            report["warnings"].append(f"reference_inference_metadata_missing:{paper_id}")
            continue
        record = _metadata_candidate_record(
            candidate_id=paper_id,
            metadata=metadata,
            fallback={},
            rank=len(expanded) + 1,
            intent=intent,
            source_role=LLM_CANDIDATE_SOURCE_ROLE,
            witness_citation_overlap=0,
            supported_by=[],
            min_citation_count=MIN_FOUNDATION_CITATION_COUNT,
            max_citation_count=MAX_FOUNDATION_CITATION_COUNT,
        )
        record.update(
            {
                "llm_added": True,
                "llm_addition_reason": _text(verified.get("reasoning")),
                "llm_reference_query": request,
                "llm_reference_inference": {
                    "focus_scope": _text(document.get("focus_scope")),
                    "warnings": _string_list(document.get("warnings")),
                },
            }
        )
        evidence_urls = _string_list(verified.get("evidence_urls"))
        if evidence_urls:
            record["llm_verified_evidence_urls"] = evidence_urls
        expanded.append(record)
        candidate_by_id[paper_id] = record
        report["added_papers"].append(paper_id)

    report["expanded_candidate_count"] = len(expanded)
    report["added_candidate_count"] = len(report["added_papers"])
    report["status"] = "added" if report["added_papers"] else "no_new_verified_papers"
    report["warnings"] = _dedupe_strings(report["warnings"])
    return expanded, report


def foundation_selection_prompt(
    *,
    seed_metadata: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    intent: str,
    min_citation_count: int = MIN_FOUNDATION_CITATION_COUNT,
    max_citation_count: int = MAX_FOUNDATION_CITATION_COUNT,
    fixed_seed: bool = False,
) -> str:
    """Return the fixed prompt contract for external foundation selection."""

    lines = [
            "You are selecting the foundation paper for a theoretical-physics research domain.",
            "Choose selected_foundation and best_reference_paper only from the supplied candidates. They may be the same paper.",
            "The selected foundation is the same-scope paper that best defines the field represented by the seed and its citers.",
            "The best reference is the most useful paper to read before proposing or calculating for the user's intended methodology.",
            "A parent foundation must be earlier than, or from the same year as, selected_foundation; a later paper cannot be a parent.",
            f"Candidates with fewer than {min_citation_count} citations should normally have low priority as selected foundation unless no better-supported same-scope foundation is supplied.",
            f"User intent:\n{intent or '(none)'}",
            f"Seed paper:\n{dict(seed_metadata)}",
            f"Candidate papers:\n{[dict(candidate) for candidate in candidates]}",
            "Return JSON only.",
    ]
    lines.insert(
        6,
        f"The {min_citation_count}–{max_citation_count} citation band is a soft scope prior: below it may be "
        "too shallow and at or above its upper end may be an over-broad parent domain. Do not "
        "override direct canonical-origin evidence solely because of this prior.",
    )
    if fixed_seed:
        lines[1] = (
            "Choose best_reference_paper and parent_foundations only from the "
            "supplied candidates."
        )
        lines.insert(
            2,
            "Fixed-seed mode is active: selected_foundation must be the separate "
            "Seed paper, even when it is absent from the bounded candidate set. "
            "Use the candidate fields for better reading references or earlier "
            "parents, not to replace the seed.",
        )
    return "\n\n".join(lines)


def deterministic_foundation_selection(
    candidates: Sequence[Mapping[str, Any]],
    *,
    intent: str,
    min_citation_count: int = MIN_FOUNDATION_CITATION_COUNT,
) -> dict[str, Any]:
    """Select a foundation deterministically when a selection result is absent."""

    normalized_candidates = [dict(candidate) for candidate in candidates if _paper_id(candidate)]
    if not normalized_candidates:
        selected = {"paper_id": "", "title": "", "reason": "no candidates were available"}
    else:
        best = sorted(
            normalized_candidates,
            key=lambda item: (
                -int(_citation_count(item) >= min_citation_count),
                -_integer(item.get("witness_citation_overlap")),
                -_float(item.get("intent_overlap")),
                -_citation_count(item),
                _paper_id(item),
            ),
        )[0]
        selected = _choice(
            best,
            "highest deterministic combination of citation threshold, witness overlap, intent overlap, and citation count",
        )
    best_reference = _deterministic_best_reference(normalized_candidates, selected)
    selected_year = _candidate_year(next((item for item in normalized_candidates if _paper_id(item) == selected["paper_id"]), {}))
    parents = [
        _choice(candidate, "high-citation candidate kept as a possible broader parent foundation")
        for candidate in normalized_candidates
        if _paper_id(candidate) != selected["paper_id"]
        and "high_citation_parent_domain_risk" in _string_list(candidate.get("warnings"))
        and _is_valid_parent_year(_candidate_year(candidate), selected_year)
    ]
    return {
        "schema_version": "arc.domain_foundation_selection.v1",
        "selected_foundation": selected,
        "best_reference_paper": best_reference,
        "parent_foundations": parents[:5],
        "rejected_candidates": [],
        "reasoning": f"Deterministic fallback selection. User intent: {intent or '(none)'}.",
        "warnings": ["deterministic_foundation_selection"],
    }


def normalize_foundation_selection(
    selection: Mapping[str, Any] | None,
    candidates: Sequence[Mapping[str, Any]],
    *,
    intent: str = "",
    min_citation_count: int = MIN_FOUNDATION_CITATION_COUNT,
) -> dict[str, Any]:
    """Repair an external selection to known candidates and valid parent years."""

    source = selection if isinstance(selection, Mapping) else {}
    candidate_by_id = {
        _paper_id(candidate): dict(candidate)
        for candidate in candidates
        if _paper_id(candidate)
    }
    fallback = deterministic_foundation_selection(
        list(candidate_by_id.values()), intent=intent, min_citation_count=min_citation_count
    )
    warnings = _string_list(source.get("warnings"))
    if not isinstance(selection, Mapping):
        warnings.append("foundation_selection_not_object")

    requested_selected = _choice_id(source.get("selected_foundation"))
    if requested_selected in candidate_by_id:
        selected = _known_choice(source.get("selected_foundation"), candidate_by_id[requested_selected])
    else:
        selected = dict(fallback["selected_foundation"])
        if requested_selected:
            selected["reason"] = "LLM selected an unknown id; repaired via deterministic fallback ranking"
            warnings.append(f"llm_selected_unknown_id:{requested_selected}")

    requested_reference = _choice_id(source.get("best_reference_paper"))
    if requested_reference in candidate_by_id:
        best_reference = _known_choice(source.get("best_reference_paper"), candidate_by_id[requested_reference])
    else:
        selected_candidate = candidate_by_id.get(selected["paper_id"], {})
        best_reference = _choice(
            selected_candidate or selected,
            "Best-reference selection was unknown; repaired to the selected foundation",
        )
        if requested_reference:
            warnings.append(f"llm_best_reference_unknown_id:{requested_reference}")

    selected_year = _candidate_year(candidate_by_id.get(selected["paper_id"], {}))
    parents, rejected, parent_warnings = _normalize_parent_foundations(
        source.get("parent_foundations"),
        selected_id=selected["paper_id"],
        selected_year=selected_year,
        candidate_by_id=candidate_by_id,
    )
    warnings.extend(parent_warnings)
    rejected.extend(_normalize_rejected(source.get("rejected_candidates"), candidate_by_id))
    return {
        "schema_version": "arc.domain_foundation_selection.v1",
        "selected_foundation": selected,
        "best_reference_paper": best_reference,
        "parent_foundations": parents,
        "rejected_candidates": _dedupe_choices(rejected),
        "reasoning": _text(source.get("reasoning")) or _text(fallback["reasoning"]),
        "warnings": _dedupe_strings(warnings),
    }


def enforce_fixed_seed_foundation(
    selection: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    seed_paper_id: str,
    seed_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Repair a selection so fixed-seed mode cannot move the network anchor.

    The audit and selection stages still contribute the best reading reference
    and earlier contextual parents.  Only the selected foundation is fixed.
    """

    normalized_seed = _normalized_id(seed_paper_id)
    candidate_by_id = {
        _paper_id(candidate): dict(candidate)
        for candidate in candidates
        if _paper_id(candidate)
    }
    seed = candidate_by_id.get(normalized_seed)
    result = dict(selection)
    warnings = _string_list(result.get("warnings"))
    if seed is None:
        # Candidate scanning deliberately prioritizes high-witness papers and
        # can omit the seed.  Fixed-seed mode keeps independently acquired
        # seed metadata as an anchor even in that bounded candidate case.
        seed = dict(seed_metadata)
        seed["paper_id"] = normalized_seed
        warnings.append("fixed_seed_recovered_from_seed_metadata")

    previous_id = _choice_id(result.get("selected_foundation"))
    result["selected_foundation"] = _choice(
        seed,
        "Fixed-seed mode keeps the requested seed as the network foundation.",
    )
    if previous_id != normalized_seed:
        warnings.append(f"fixed_seed_foundation_enforced:{normalized_seed}")

    seed_year = _candidate_year(seed)
    parents: list[dict[str, Any]] = []
    rejected = [
        dict(item)
        for item in result.get("rejected_candidates", [])
        if isinstance(item, Mapping)
    ]
    for choice in result.get("parent_foundations", []):
        parent_id = _choice_id(choice)
        parent = candidate_by_id.get(parent_id)
        if (
            parent is None
            or parent_id == normalized_seed
            or not _is_valid_parent_year(_candidate_year(parent), seed_year)
        ):
            if parent is not None:
                rejected.append(
                    _choice(
                        parent,
                        "Rejected because it is not an earlier parent of the fixed seed.",
                    )
                )
            warnings.append(f"fixed_seed_parent_rejected:{parent_id or 'unknown'}")
            continue
        parents.append(_known_choice(choice, parent))
    result["parent_foundations"] = _dedupe_choices(parents)
    result["rejected_candidates"] = _dedupe_choices(rejected)
    result["warnings"] = _dedupe_strings(warnings)
    return result


def _metadata_candidate_record(
    *,
    candidate_id: str,
    metadata: Mapping[str, Any],
    fallback: Mapping[str, Any],
    rank: int,
    intent: str,
    source_role: str,
    witness_citation_overlap: int,
    supported_by: Sequence[str],
    min_citation_count: int,
    max_citation_count: int,
) -> dict[str, Any]:
    title = _text(metadata.get("title")) or _text(fallback.get("title"))
    abstract = _text(metadata.get("abstract")) or _text(fallback.get("abstract"))
    citation_count = _citation_count(metadata) or _citation_count(fallback)
    paper_id = _normalized_id(candidate_id)
    record = {
        "paper_id": paper_id,
        "rank": rank,
        "title": title,
        "abstract": abstract,
        "authors": list(metadata.get("authors") or fallback.get("authors") or []),
        "authors_short": normalize_authors(metadata.get("authors") or fallback.get("authors") or []),
        "year": metadata.get("year") or fallback.get("year"),
        "citation_count": citation_count,
        "witness_citation_overlap": witness_citation_overlap,
        "supported_by": list(dict.fromkeys(supported_by))[:50],
        "intent_overlap": round(token_overlap_score(f"{title} {abstract}", intent), 4),
        "identifiers": dict(metadata.get("identifiers") or fallback.get("identifiers") or {}),
        "warnings": [],
        "source_role": source_role,
    }
    if citation_count < min_citation_count:
        record["warnings"].append("low_citation_foundation_priority")
    if citation_count >= max_citation_count:
        record["warnings"].append("high_citation_parent_domain_risk")
    return record


def _reference_document(
    result: Mapping[str, Any] | ReferenceInferenceResult,
) -> Mapping[str, Any] | None:
    if isinstance(result, ReferenceInferenceResult):
        return result.to_document()
    return result if isinstance(result, Mapping) else None


def _verified_references_by_id(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    verified: dict[str, Mapping[str, Any]] = {}
    raw = result.get("verified_references")
    if not isinstance(raw, list):
        return verified
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        paper_id = _normalized_id(item.get("paper_id") or item.get("input_paper_id"))
        if paper_id:
            verified[paper_id] = item
    return verified


def _mark_existing_llm_recommended(
    record: dict[str, Any],
    *,
    request: str,
    verified: Mapping[str, Any],
) -> None:
    record["llm_recommended"] = True
    record.setdefault("llm_reference_query", request)
    record.setdefault("llm_addition_reason", _text(verified.get("reasoning")))
    evidence_urls = _string_list(verified.get("evidence_urls"))
    if evidence_urls:
        record.setdefault("llm_verified_evidence_urls", evidence_urls)


def _deterministic_best_reference(
    candidates: Sequence[Mapping[str, Any]], selected: Mapping[str, Any]
) -> dict[str, Any]:
    if not candidates:
        return {
            "paper_id": _text(selected.get("paper_id")),
            "title": _text(selected.get("title")),
            "reason": "no separate candidates were available",
        }
    best = sorted(
        candidates,
        key=lambda item: (
            -_float(item.get("intent_overlap")),
            -(_candidate_year(item) or 0),
            -_citation_count(item),
            -_integer(item.get("witness_citation_overlap")),
            _paper_id(item),
        ),
    )[0]
    return _choice(
        best,
        "highest deterministic combination of intent overlap, recency, citation count, and witness support for a readable methodology reference",
    )


def _normalize_parent_foundations(
    raw_parents: Any,
    *,
    selected_id: str,
    selected_year: int | None,
    candidate_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    parents: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    warnings: list[str] = []
    if not isinstance(raw_parents, list):
        if raw_parents is not None:
            warnings.append("foundation_selection_invalid_parent_foundations")
        return parents, rejected, warnings
    seen: set[str] = set()
    for raw_parent in raw_parents:
        parent_id = _choice_id(raw_parent)
        if not parent_id or parent_id in seen:
            continue
        seen.add(parent_id)
        candidate = candidate_by_id.get(parent_id)
        if candidate is None:
            warnings.append(f"llm_parent_unknown_id:{parent_id}")
            continue
        if parent_id == selected_id:
            warnings.append(f"llm_parent_is_selected_foundation:{parent_id}")
            continue
        parent_year = _candidate_year(candidate)
        if not _is_valid_parent_year(parent_year, selected_year):
            rejected.append(
                _choice(
                    candidate,
                    f"Cannot be a parent foundation because it is from {parent_year}, later than the selected foundation year {selected_year}.",
                )
            )
            continue
        parents.append(_known_choice(raw_parent, candidate))
    return parents, rejected, warnings


def _normalize_rejected(raw_rejected: Any, candidate_by_id: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(raw_rejected, list):
        return []
    output: list[dict[str, str]] = []
    for raw in raw_rejected:
        paper_id = _choice_id(raw)
        candidate = candidate_by_id.get(paper_id)
        if candidate is not None:
            output.append(_known_choice(raw, candidate))
    return output


def _known_choice(raw: Any, candidate: Mapping[str, Any]) -> dict[str, str]:
    reason = _text(raw.get("reason")) if isinstance(raw, Mapping) else ""
    return _choice(candidate, reason or "selected from the supplied candidate set")


def _choice(candidate: Mapping[str, Any], reason: str) -> dict[str, str]:
    return {
        "paper_id": _paper_id(candidate),
        "title": _text(candidate.get("title")),
        "reason": reason,
    }


def _choice_id(value: Any) -> str:
    return _paper_id(value) if isinstance(value, Mapping) else ""


def _paper_id(document: Mapping[str, Any]) -> str:
    return _normalized_id(paper_key(dict(document)))


def _normalized_id(value: Any) -> str:
    return normalize_paper_id(_text(value))


def _normalized_ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(_normalized_id(item) for item in value if _normalized_id(item)))


def _citation_count(document: Mapping[str, Any]) -> int:
    return max(0, _integer(document.get("citation_count") or document.get("cited_by_count")))


def _candidate_year(candidate: Mapping[str, Any]) -> int | None:
    year = _integer(candidate.get("year"))
    return year if year > 0 else None


def _is_valid_parent_year(parent_year: int | None, selected_year: int | None) -> bool:
    return parent_year is None or selected_year is None or parent_year <= selected_year


def _confidence(value: Any) -> str:
    confidence = _text(value).lower()
    return confidence if confidence in {"complete", "high", "medium", "low"} else "low"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(item) for item in value if _text(item)]


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _dedupe_choices(values: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        paper_id = _text(value.get("paper_id"))
        if paper_id and paper_id not in seen:
            output.append(dict(value))
            seen.add(paper_id)
    return output


def _is_paper_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, Mapping) for item in value)


def _usable_metadata(value: Any) -> bool:
    return isinstance(value, Mapping) and not value.get("error")
