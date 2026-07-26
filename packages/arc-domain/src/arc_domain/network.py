from __future__ import annotations

import re
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from arc_paper import arxiv_path_id, normalize_paper_id

from .text import citation_per_year, log_score, paper_key, token_overlap_score


INTENT_RANKING_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "arc.domain-intent-ranking-v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["ranked_paper_ids", "reasoning"],
    "properties": {
        "ranked_paper_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "reasoning": {"type": "string"},
    },
}
CITATION_RATE_WEIGHT = 0.1
RECENCY_WEIGHT = 0.5
INTENT_OVERLAP_WEIGHT = 1.0
GRAPH_CITER_WEIGHT = 2.0
REFERENCE_EDGE_WEIGHT = 0.5
RECENT_ARXIV_WINDOW_DAYS = 365
MAX_GRAPH_PAPER_COUNT = 90
_PUBLIC_DATE_FIELDS = ("published", "preprint_date", "earliest_date", "created")
_DATE_PRECISION_ORDER = {"year": 1, "month": 2, "day": 3}


@dataclass(frozen=True)
class _DateEvidence:
    """One public-date claim without inventing unavailable precision."""

    value: str
    precision: str
    lower: date
    upper: date
    basis: str


def strict_window_citer_streams(
    foundation_id: str,
    *,
    most_recent: list[dict[str, Any]],
    most_cited: list[dict[str, Any]],
    as_of_date: date,
    window_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return first-public-date eligible citer streams before merge/capping.

    The provider may return the same paper in both rankings with slightly
    different metadata.  Classify each normalized paper ID once after merging
    its available metadata, then project that decision back to both ranking
    streams.  This keeps the merge order while ensuring neither stream can
    consume capacity with an out-of-window or undated citer.
    """

    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError("window_days must be a positive integer")
    normalized_foundation = normalize_paper_id(foundation_id)
    records: dict[str, dict[str, Any]] = {}
    stream_ids: dict[str, list[str]] = {"mostrecent": [], "mostcited": []}
    for source, stream in (("mostrecent", most_recent), ("mostcited", most_cited)):
        seen_in_stream: set[str] = set()
        for item in stream:
            if not isinstance(item, dict):
                continue
            paper_id = normalize_paper_id(paper_key(item))
            if not paper_id or paper_id == normalized_foundation:
                continue
            if paper_id not in seen_in_stream:
                stream_ids[source].append(paper_id)
                seen_in_stream.add(paper_id)
            candidate = dict(item)
            candidate["paper_id"] = paper_id
            existing = records.get(paper_id)
            if existing is None:
                records[paper_id] = candidate
            else:
                _merge_citer_metadata(existing, candidate)

    start = as_of_date - timedelta(days=window_days)
    eligible: set[str] = set()
    stats = _empty_recency_stats(len(records))
    for paper_id, record in records.items():
        evidence = _first_public_date_evidence(record)
        if evidence is None:
            stats["excluded_missing_first_public_date"] += 1
            continue
        _apply_date_evidence(record, evidence)
        if evidence.precision == "day":
            stats["exact_date_citers"] += 1
        else:
            stats["reduced_precision_date_citers"] += 1
        membership = _window_membership(
            evidence, start=start, end=as_of_date
        )
        if membership == "inside":
            eligible.add(paper_id)
        elif membership == "outside":
            stats["excluded_outside_window"] += 1
        else:
            stats["excluded_ambiguous_first_public_date"] += 1
    stats["eligible_citers"] = len(eligible)
    return (
        [dict(records[paper_id]) for paper_id in stream_ids["mostrecent"] if paper_id in eligible],
        [dict(records[paper_id]) for paper_id in stream_ids["mostcited"] if paper_id in eligible],
        stats,
    )


def first_public_date(record: dict[str, Any]) -> tuple[date | None, str | None]:
    """Return a compatible lower-bound date and basis without considering updates.

    Internal window decisions use bounded date evidence instead of this
    compatibility projection, so a year or month is never treated as an exact
    first day.
    """

    evidence = _first_public_date_evidence(record)
    if evidence is None:
        return None, None
    return evidence.lower, evidence.basis


def recency_candidate_stats(
    records: list[dict[str, Any]],
    *,
    as_of_date: date,
    window_days: int,
) -> dict[str, int]:
    """Summarize the complete deduplicated candidate pool by date evidence."""

    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError("window_days must be a positive integer")
    start = as_of_date - timedelta(days=window_days)
    stats = _empty_recency_stats(len(records))
    for record in records:
        evidence = _first_public_date_evidence(record)
        if evidence is None:
            stats["excluded_missing_first_public_date"] += 1
            continue
        if evidence.precision == "day":
            stats["exact_date_citers"] += 1
        else:
            stats["reduced_precision_date_citers"] += 1
        membership = _window_membership(evidence, start=start, end=as_of_date)
        if membership == "inside":
            stats["eligible_citers"] += 1
        elif membership == "outside":
            stats["excluded_outside_window"] += 1
        else:
            stats["excluded_ambiguous_first_public_date"] += 1
    return stats


def merge_citer_pool(
    foundation_id: str,
    *,
    most_recent: list[dict[str, Any]],
    most_cited: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Interleave fixed INSPIRE rankings into one bounded deduplicated pool."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    foundation_id = normalize_paper_id(foundation_id)
    merged: dict[str, dict[str, Any]] = {}
    source_ids: dict[str, list[str]] = {"mostrecent": [], "mostcited": []}
    for source, items in (
        ("mostrecent", most_recent),
        ("mostcited", most_cited),
    ):
        seen_in_source: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            paper_id = normalize_paper_id(paper_key(item))
            if not paper_id or paper_id == foundation_id:
                continue
            if paper_id not in seen_in_source:
                source_ids[source].append(paper_id)
                seen_in_source.add(paper_id)
            record = dict(item)
            record["paper_id"] = paper_id
            record.setdefault("citer_sources", [])
            if source not in record["citer_sources"]:
                record["citer_sources"].append(source)
            record[f"{source}_rank"] = index + 1
            if paper_id in merged:
                existing = merged[paper_id]
                _merge_citer_metadata(
                    existing,
                    record,
                    excluded_fields={"citer_sources"},
                )
                for label in record["citer_sources"]:
                    if label not in existing["citer_sources"]:
                        existing["citer_sources"].append(label)
            else:
                merged[paper_id] = record

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_source_len = max((len(ids) for ids in source_ids.values()), default=0)
    for index in range(max_source_len):
        for source in ("mostrecent", "mostcited"):
            ids = source_ids[source]
            if index >= len(ids):
                continue
            paper_id = ids[index]
            if paper_id in seen:
                continue
            ordered.append(merged[paper_id])
            seen.add(paper_id)
            if len(ordered) >= limit:
                return ordered
    return ordered


def intent_ranking_prompt(
    citer_pool: list[dict[str, Any]],
    *,
    intent: str,
) -> str:
    """Build the strict intent-ranking prompt without invoking an LLM."""

    shortlist = sorted(
        citer_pool,
        key=lambda item: (
            token_overlap_score(f"{item.get('title', '')} {item.get('abstract', '')}", intent),
            int(item.get("citation_count") or 0),
        ),
        reverse=True,
    )[:120]
    compact = [
        {
            "paper_id": item.get("paper_id"),
            "title": item.get("title", ""),
            "abstract": str(item.get("abstract") or "")[:800],
            "year": item.get("year"),
            "citation_count": item.get("citation_count", 0),
        }
        for item in shortlist
    ]
    return "\n\n".join(
        [
            "Rank up to 10 papers whose titles and abstracts best match the user's research intent.",
            "Return only IDs from the supplied list. Prefer scientifically specific matches over generic review papers.",
            f"User intent:\n{intent}",
            f"Candidate papers:\n{compact}",
            "Return JSON only.",
        ]
    )


def normalize_intent_ranking(
    payload: dict[str, Any],
    *,
    citer_pool: list[dict[str, Any]],
    method: str = "llm",
) -> dict[str, Any]:
    """Filter a strict LLM ranking to IDs present in the acquired pool."""

    raw_ids = payload.get("ranked_paper_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("ranked_paper_ids must be an array")
    valid = {str(item.get("paper_id") or "") for item in citer_pool}
    ranked: list[str] = []
    for item in raw_ids:
        if not isinstance(item, str):
            raise ValueError("ranked_paper_ids entries must be strings")
        paper_id = normalize_paper_id(item)
        if paper_id in valid and paper_id not in ranked:
            ranked.append(paper_id)
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str):
        raise ValueError("reasoning must be a string")
    return {
        "schema_version": "arc.domain_intent_ranking.v1",
        "ranked_paper_ids": ranked[:10],
        "reasoning": reasoning,
        "method": method,
    }


def deterministic_intent_ranking(
    citer_pool: list[dict[str, Any]],
    *,
    intent: str,
    reason: str = "LLM intent ranking unavailable",
) -> dict[str, Any]:
    """Return the stable lexical fallback for intent ranking."""

    if not intent.strip():
        ranked: list[str] = []
        reason = "No research intent supplied."
    else:
        shortlist = sorted(
            citer_pool,
            key=lambda item: (
                token_overlap_score(
                    f"{item.get('title', '')} {item.get('abstract', '')}",
                    intent,
                ),
                int(item.get("citation_count") or 0),
            ),
            reverse=True,
        )
        ranked = [
            str(item["paper_id"])
            for item in shortlist
            if token_overlap_score(
                f"{item.get('title', '')} {item.get('abstract', '')}",
                intent,
            )
            > 0
        ][:10]
    return {
        "schema_version": "arc.domain_intent_ranking.v1",
        "ranked_paper_ids": ranked,
        "reasoning": reason,
        "method": "deterministic_fallback",
    }


def _select_domain_papers(
    citer_pool: list[dict[str, Any]],
    *,
    foundation_id: str,
    excluded_ids: set[str] | None = None,
    intent_ranking: dict[str, Any],
    intent: str,
    selected_count: int,
    max_total: int = MAX_GRAPH_PAPER_COUNT,
    recent_window_days: int = RECENT_ARXIV_WINDOW_DAYS,
    as_of_date: date | None = None,
    strict_window: bool = False,
) -> list[dict[str, Any]]:
    current = as_of_date or datetime.now(timezone.utc).date()
    now = datetime.combine(current, datetime.min.time(), tzinfo=timezone.utc)
    current_year = now.year
    excluded = {
        normalize_paper_id(paper_id)
        for paper_id in (excluded_ids or set())
        if normalize_paper_id(paper_id)
    }
    normalized_foundation = normalize_paper_id(foundation_id)
    if normalized_foundation:
        excluded.add(normalized_foundation)
    intent_rank = {
        normalize_paper_id(paper_id): index
        for index, paper_id in enumerate(intent_ranking.get("ranked_paper_ids") or [], start=1)
        if normalize_paper_id(paper_id)
    }
    scored = []
    seen_paper_ids: set[str] = set()
    for item in citer_pool:
        paper_id = normalize_paper_id(item.get("paper_id") or paper_key(item))
        if not paper_id or paper_id in excluded or paper_id in seen_paper_ids:
            continue
        seen_paper_ids.add(paper_id)
        record = dict(item)
        record["paper_id"] = paper_id
        cpy = citation_per_year(record, current_year)
        age = max(1, current_year - int(record.get("year") or current_year) + 1)
        recency = 1.0 / age
        intent_overlap = token_overlap_score(f"{record.get('title', '')} {record.get('abstract', '')}", intent)
        intent_boost = 0.0
        if paper_id in intent_rank:
            intent_boost = 2.0 - 0.12 * (intent_rank[paper_id] - 1)
        score = _domain_score(
            citation_per_year=cpy,
            recency=recency,
            intent_overlap=intent_overlap,
            intent_boost=intent_boost,
            in_graph_citer_score=0.0,
            reference_edge_count=0,
        )
        record["domain_score"] = round(score, 4)
        record["citation_per_year"] = round(cpy, 4)
        record["citation_rate_score"] = round(CITATION_RATE_WEIGHT * log_score(cpy), 4)
        record["recency"] = round(recency, 4)
        record["recency_score"] = round(RECENCY_WEIGHT * recency, 4)
        record["intent_overlap"] = round(intent_overlap, 4)
        record["intent_overlap_score"] = round(INTENT_OVERLAP_WEIGHT * intent_overlap, 4)
        record["intent_boost"] = round(intent_boost, 4)
        record["in_graph_citer_count"] = 0
        record["in_graph_citer_score"] = 0.0
        record["reference_edge_count"] = 0
        record["reference_edge_score"] = 0.0
        evidence = _first_public_date_evidence(record)
        _apply_date_evidence(record, evidence)
        if strict_window:
            record["recent_arxiv"] = (
                evidence is not None
                and _window_membership(
                    evidence,
                    start=current - timedelta(days=recent_window_days),
                    end=current,
                )
                == "inside"
            )
        else:
            record["recent_arxiv"] = _is_recent_arxiv_paper(
                record, now=now, window_days=recent_window_days
            )
        record["selection_reason"] = _selection_reason(record, paper_id in intent_rank)
        scored.append(record)
    scored.sort(
        key=lambda item: (item["domain_score"], item.get("citation_count") or 0, item.get("year") or 0),
        reverse=True,
    )
    max_total = max(0, max_total)
    selected = scored[: min(selected_count, max_total)]
    selected_ids = {item["paper_id"] for item in selected}
    recent = [
        item
        for item in scored[selected_count:]
        if item.get("recent_arxiv") and item["paper_id"] not in selected_ids
    ][: max(0, max_total - len(selected))]
    return selected if strict_window else [*selected, *recent]


def _parent_foundation_ids(parent_foundations: list[dict[str, Any]]) -> set[str]:
    return {
        normalize_paper_id(paper_key(item))
        for item in parent_foundations
        if isinstance(item, dict) and paper_key(item)
    }


def _add_in_graph_citer_scores(
    selected_papers: list[dict[str, Any]],
    *,
    refs_by_selected: dict[str, Any],
) -> list[dict[str, Any]]:
    selected_ids = [normalize_paper_id(item.get("paper_id") or paper_key(item)) for item in selected_papers]
    selected_set = set(selected_ids)
    counts = Counter()
    for source_id, refs in refs_by_selected.items():
        source_id = normalize_paper_id(source_id)
        if source_id not in selected_set or not isinstance(refs, list):
            continue
        seen = set()
        for ref in refs:
            target_id = normalize_paper_id(paper_key(ref))
            if not target_id or target_id == source_id or target_id not in selected_set or target_id in seen:
                continue
            seen.add(target_id)
            counts[target_id] += 1

    max_count = max(counts.values(), default=0)
    scored = []
    for item in selected_papers:
        record = dict(item)
        paper_id = normalize_paper_id(record.get("paper_id") or paper_key(record))
        count = int(counts.get(paper_id, 0))
        normalized = count / max_count if max_count else 0.0
        record["in_graph_citer_count"] = count
        record["in_graph_citer_score"] = round(normalized, 4)
        score = _domain_score(
            citation_per_year=float(record.get("citation_per_year") or 0),
            recency=float(record.get("recency") or 0),
            intent_overlap=float(record.get("intent_overlap") or 0),
            intent_boost=float(record.get("intent_boost") or 0),
            in_graph_citer_score=normalized,
            reference_edge_count=int(record.get("reference_edge_count") or 0),
        )
        record["domain_score"] = round(score, 4)
        record["selection_reason"] = _selection_reason(record, bool(record.get("intent_boost")))
        scored.append(record)
    scored.sort(
        key=lambda item: (
            item["domain_score"],
            item.get("in_graph_citer_count") or 0,
            item.get("citation_count") or 0,
            item.get("year") or 0,
        ),
        reverse=True,
    )
    return scored


def _add_reference_edge_scores(
    selected_papers: list[dict[str, Any]],
    *,
    foundation_id: str,
    parent_foundations: list[dict[str, Any]],
    common_references: list[dict[str, Any]],
    refs_by_selected: dict[str, Any],
) -> list[dict[str, Any]]:
    selected_ids = [normalize_paper_id(item.get("paper_id") or paper_key(item)) for item in selected_papers]
    node_ids = set(selected_ids)
    if foundation_id:
        node_ids.add(normalize_paper_id(foundation_id))
    for item in parent_foundations:
        parent_id = normalize_paper_id(item.get("paper_id") or paper_key(item))
        if parent_id:
            node_ids.add(parent_id)
    for item in common_references:
        common_id = normalize_paper_id(item.get("paper_id") or paper_key(item))
        if common_id:
            node_ids.add(common_id)

    counts: Counter[str] = Counter()
    for source_id, refs in refs_by_selected.items():
        source_id = normalize_paper_id(source_id)
        if source_id not in node_ids or not isinstance(refs, list):
            continue
        seen = set()
        for ref in refs:
            target_id = normalize_paper_id(paper_key(ref))
            if not target_id or target_id == source_id or target_id not in node_ids or target_id in seen:
                continue
            seen.add(target_id)
            counts[source_id] += 1

    scored = []
    for item in selected_papers:
        record = dict(item)
        paper_id = normalize_paper_id(record.get("paper_id") or paper_key(record))
        count = int(counts.get(paper_id, 0))
        record["reference_edge_count"] = count
        record["reference_edge_score"] = round(REFERENCE_EDGE_WEIGHT * count, 4)
        score = _domain_score(
            citation_per_year=float(record.get("citation_per_year") or 0),
            recency=float(record.get("recency") or 0),
            intent_overlap=float(record.get("intent_overlap") or 0),
            intent_boost=float(record.get("intent_boost") or 0),
            in_graph_citer_score=float(record.get("in_graph_citer_score") or 0),
            reference_edge_count=count,
        )
        record["domain_score"] = round(score, 4)
        record["selection_reason"] = _selection_reason(record, bool(record.get("intent_boost")))
        scored.append(record)
    scored.sort(
        key=lambda item: (
            item["domain_score"],
            item.get("reference_edge_count") or 0,
            item.get("in_graph_citer_count") or 0,
            item.get("citation_count") or 0,
            item.get("year") or 0,
        ),
        reverse=True,
    )
    return scored


def _domain_score(
    *,
    citation_per_year: float,
    recency: float,
    intent_overlap: float,
    intent_boost: float,
    in_graph_citer_score: float,
    reference_edge_count: int,
) -> float:
    return (
        CITATION_RATE_WEIGHT * log_score(citation_per_year)
        + RECENCY_WEIGHT * recency
        + INTENT_OVERLAP_WEIGHT * intent_overlap
        + intent_boost
        + GRAPH_CITER_WEIGHT * in_graph_citer_score
        + REFERENCE_EDGE_WEIGHT * reference_edge_count
    )


def _common_references(
    *,
    foundation_id: str,
    selected_ids: list[str],
    excluded_ids: set[str] | None = None,
    refs_by_selected: dict[str, Any],
    max_extra: int,
    metadata_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    capacity = max(0, max_extra)
    if capacity == 0:
        return []
    excluded = {
        normalize_paper_id(item)
        for item in [*selected_ids, *(excluded_ids or set()), foundation_id]
        if normalize_paper_id(item)
    }
    counts = Counter()
    support: dict[str, list[str]] = defaultdict(list)
    embedded: dict[str, dict[str, Any]] = {}
    for source_id, refs in refs_by_selected.items():
        if not isinstance(refs, list):
            continue
        seen = set()
        for ref in refs:
            ref_id = normalize_paper_id(paper_key(ref))
            if not ref_id or ref_id in excluded or ref_id in seen:
                continue
            seen.add(ref_id)
            counts[ref_id] += 1
            support[ref_id].append(source_id)
            embedded.setdefault(ref_id, ref)
    top_ids = [
        ref_id
        for ref_id, count in sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
        if count >= 2
    ]
    common = []
    for ref_id in top_ids:
        meta = metadata_by_id.get(ref_id)
        if not isinstance(meta, dict) or meta.get("error"):
            meta = embedded.get(ref_id, {})
        paper_id = normalize_paper_id(meta.get("paper_id") or ref_id)
        if not paper_id or paper_id in excluded:
            continue
        common.append(
            {
                "paper_id": paper_id,
                "title": meta.get("title") or embedded.get(ref_id, {}).get("title", ""),
                "abstract": meta.get("abstract", ""),
                "authors": meta.get("authors", []),
                "year": meta.get("year"),
                "citation_count": int(meta.get("citation_count") or 0),
                "support_count": int(counts[ref_id]),
                "supported_by": support.get(ref_id, [])[:50],
                "identifiers": meta.get("identifiers") or {},
            }
        )
        excluded.add(paper_id)
        if len(common) >= capacity:
            break
    return common


def _enrich_parent_foundations(
    parent_foundations: list[dict[str, Any]],
    *,
    metadata_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    enriched = []
    for item in parent_foundations:
        parent_id = normalize_paper_id(paper_key(item))
        meta = metadata_by_id.get(parent_id)
        if not isinstance(meta, dict) or meta.get("error"):
            meta = {}
        record = dict(meta)
        record["paper_id"] = normalize_paper_id(record.get("paper_id") or parent_id)
        record["title"] = record.get("title") or item.get("title", "")
        record["reason"] = item.get("reason", "")
        record["selection_reason"] = item.get("reason", "")
        for key in ("abstract", "authors", "year", "citation_count", "identifiers"):
            if key not in record and key in item:
                record[key] = item[key]
        enriched.append(record)
    return enriched


def _build_graph(
    *,
    domain_id: str,
    foundation: dict[str, Any],
    parent_foundations: list[dict[str, Any]],
    selected_papers: list[dict[str, Any]],
    common_references: list[dict[str, Any]],
    refs_by_selected: dict[str, Any],
    intent: str,
    created_at: str,
    recent_window_days: int = RECENT_ARXIV_WINDOW_DAYS,
    as_of_date: date | None = None,
    candidate_recency_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    foundation_id = normalize_paper_id(foundation.get("paper_id") or "")
    if foundation_id:
        nodes[foundation_id] = _node(foundation, role="selected_foundation")
    for item in parent_foundations:
        parent_id = normalize_paper_id(item.get("paper_id") or item.get("upi") or "")
        if parent_id and parent_id not in nodes:
            nodes[parent_id] = _node(item, role="parent_foundation")
    accepted_selected: list[dict[str, Any]] = []
    for item in selected_papers:
        paper_id = normalize_paper_id(item.get("paper_id") or item.get("upi") or "")
        if paper_id and paper_id not in nodes:
            nodes[paper_id] = _node(item, role="domain_paper")
            accepted_selected.append(item)
    for item in common_references:
        paper_id = normalize_paper_id(item.get("paper_id") or item.get("upi") or "")
        if paper_id and paper_id not in nodes:
            nodes[paper_id] = _node(item, role="common_reference")

    edges = []
    seen_edges = set()
    node_ids = set(nodes)
    for source_id, refs in refs_by_selected.items():
        source_id = normalize_paper_id(source_id)
        if source_id not in node_ids:
            continue
        if foundation_id and source_id != foundation_id:
            _add_edge(edges, seen_edges, source_id, foundation_id, relation="cites_foundation")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            target_id = normalize_paper_id(paper_key(ref))
            if target_id in node_ids and target_id != source_id:
                _add_edge(edges, seen_edges, source_id, target_id, relation="cites")
    current = as_of_date or datetime.now(timezone.utc).date()
    included = sum(bool(item.get("recent_arxiv")) for item in accepted_selected)
    precision = Counter(
        str(item.get("first_public_date_precision") or "missing")
        for item in accepted_selected
    )
    candidate_stats = dict(candidate_recency_stats or {})
    candidate_exact = int(
        candidate_stats.get("exact_date_citers", precision.get("day", 0))
    )
    candidate_reduced = int(
        candidate_stats.get(
            "reduced_precision_date_citers",
            precision.get("month", 0) + precision.get("year", 0),
        )
    )
    candidate_ambiguous = int(
        candidate_stats.get("excluded_ambiguous_first_public_date", 0)
    )
    recency: dict[str, Any] = {
        "window_days": recent_window_days,
        "start_date": (current - timedelta(days=recent_window_days)).isoformat(),
        "end_date": current.isoformat(),
        "recency_basis": dict(sorted(Counter(str(item.get("recency_basis") or "unavailable") for item in accepted_selected).items())),
        "first_public_date_precision": dict(sorted(precision.items())),
        "selected_exact_date_count": precision.get("day", 0),
        "selected_reduced_precision_date_count": (
            precision.get("month", 0) + precision.get("year", 0)
        ),
        "exact_date_count": candidate_exact,
        "reduced_precision_date_count": candidate_reduced,
        "ambiguous_date_count": candidate_ambiguous,
        "included_count": included,
        "excluded_count": len(accepted_selected) - included,
        "candidate_pool": candidate_stats,
    }
    return {
        "schema_version": "arc.domain_graph.v1",
        "domain_id": domain_id,
        "intent": intent,
        "foundation_paper": foundation_id,
        "nodes": list(nodes.values()),
        "edges": edges,
        "created_at": created_at,
        "recency": recency,
    }


def _node(paper_record: dict[str, Any], *, role: str) -> dict[str, Any]:
    paper_id = normalize_paper_id(paper_record.get("paper_id") or paper_record.get("upi") or "")
    evidence = _first_public_date_evidence(paper_record)
    node = {
        "id": paper_id,
        "paper_id": paper_id,
        "role": role,
        "title": paper_record.get("title", ""),
        "abstract": paper_record.get("abstract", ""),
        "authors": paper_record.get("authors", []),
        "year": paper_record.get("year"),
        "citation_count": int(paper_record.get("citation_count") or paper_record.get("cited_by_count") or 0),
        "citation_per_year": paper_record.get("citation_per_year"),
        "domain_score": paper_record.get("domain_score"),
        "citation_rate_score": paper_record.get("citation_rate_score"),
        "recency": paper_record.get("recency"),
        "recency_score": paper_record.get("recency_score"),
        "intent_overlap": paper_record.get("intent_overlap"),
        "intent_overlap_score": paper_record.get("intent_overlap_score"),
        "intent_boost": paper_record.get("intent_boost"),
        "in_graph_citer_count": paper_record.get("in_graph_citer_count"),
        "in_graph_citer_score": paper_record.get("in_graph_citer_score"),
        "reference_edge_count": paper_record.get("reference_edge_count"),
        "reference_edge_score": paper_record.get("reference_edge_score"),
        "selection_reason": paper_record.get("selection_reason") or paper_record.get("reason", ""),
        "support_count": paper_record.get("support_count"),
        "identifiers": paper_record.get("identifiers") or {},
        "first_public_date": (
            paper_record.get("first_public_date")
            or (evidence.value if evidence is not None else None)
        ),
        "first_public_date_precision": (
            paper_record.get("first_public_date_precision")
            or (evidence.precision if evidence is not None else None)
        ),
        "recency_basis": (
            paper_record.get("recency_basis")
            or (evidence.basis if evidence is not None else None)
        ),
    }
    for field in (
        "source_role",
        "llm_added",
        "llm_recommended",
        "llm_addition_reason",
        "llm_reference_query",
        "llm_verified_evidence_urls",
        "llm_reference_inference",
    ):
        if field in paper_record:
            node[field] = paper_record[field]
    return node


def _copy_selection_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    if reason := source.get("reason"):
        target["reason"] = reason
    for field in (
        "source_role",
        "llm_added",
        "llm_recommended",
        "llm_addition_reason",
        "llm_reference_query",
        "llm_verified_evidence_urls",
        "llm_reference_inference",
    ):
        if field in source:
            target[field] = source[field]


def _add_edge(edges: list[dict[str, Any]], seen: set[tuple[str, str, str]], source: str, target: str, *, relation: str) -> None:
    key = (source, target, relation)
    if key in seen:
        return
    seen.add(key)
    edges.append({"source": source, "target": target, "relation": relation})


def _selection_reason(record: dict[str, Any], intent_ranked: bool) -> str:
    parts = []
    if intent_ranked:
        parts.append("LLM intent-ranked")
    if record.get("recent_arxiv"):
        parts.append("recent arXiv")
    if record.get("citation_per_year", 0) > 0:
        parts.append("citation-per-year")
    if record.get("in_graph_citer_count", 0) > 0:
        parts.append("cited-within-graph")
    if record.get("reference_edge_count", 0) > 0:
        parts.append("reference-connected")
    if record.get("year"):
        parts.append("recency")
    return ", ".join(parts) or "representative foundation citer"


def _is_recent_arxiv_paper(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
    window_days: int = RECENT_ARXIV_WINDOW_DAYS,
) -> bool:
    if not _has_arxiv_id(record):
        return False
    evidence = _first_public_date_evidence(record)
    if evidence is None:
        return False
    current = (now or datetime.now(timezone.utc)).date()
    return (
        _window_membership(
            evidence,
            start=current - timedelta(days=window_days),
            end=current,
        )
        != "outside"
    )


def _has_arxiv_id(record: dict[str, Any]) -> bool:
    identifiers = record.get("identifiers") or {}
    values = (
        record.get("paper_id"),
        record.get("arxiv_id"),
        record.get("arxiv"),
        identifiers.get("paper_id"),
        identifiers.get("arxiv_id"),
        identifiers.get("arxiv"),
    )
    return any(arxiv_path_id(str(value or "")) for value in values)


def _paper_date(record: dict[str, Any]) -> date | None:
    return _paper_date_with_basis(record)[0]


def _paper_date_with_basis(record: dict[str, Any]) -> tuple[date | None, str | None]:
    evidence = _paper_date_evidence(record)
    if evidence is None:
        return None, None
    return evidence.lower, evidence.basis


def _paper_date_evidence(record: dict[str, Any]) -> _DateEvidence | None:
    selected: _DateEvidence | None = None
    for priority, key in enumerate(_PUBLIC_DATE_FIELDS):
        value = str(record.get(key) or "").strip()
        candidate = _parse_date_evidence(value, basis=key)
        if candidate is not None:
            selected = _prefer_date_evidence(
                selected,
                candidate,
                left_priority=(
                    _PUBLIC_DATE_FIELDS.index(selected.basis)
                    if selected is not None and selected.basis in _PUBLIC_DATE_FIELDS
                    else len(_PUBLIC_DATE_FIELDS)
                ),
                right_priority=priority,
            )
    return selected


def _first_public_date_evidence(
    record: dict[str, Any],
) -> _DateEvidence | None:
    evidence = _paper_date_evidence(record)
    if evidence is not None:
        return evidence
    return _arxiv_month_evidence(record)


def _merge_citer_metadata(
    target: dict[str, Any],
    incoming: dict[str, Any],
    *,
    excluded_fields: set[str] | None = None,
) -> None:
    """Merge non-empty metadata while retaining each field's earliest date."""

    excluded = excluded_fields or set()
    for key, value in incoming.items():
        if key in excluded or value in (None, "", [], {}):
            continue
        if key in _PUBLIC_DATE_FIELDS:
            current = _parse_date_evidence(
                str(target.get(key) or "").strip(), basis=key
            )
            incoming_evidence = _parse_date_evidence(
                str(value).strip(), basis=key
            )
            preferred = _prefer_date_evidence(
                current,
                incoming_evidence,
                left_priority=0,
                right_priority=0,
            )
            if preferred is current:
                continue
        target[key] = value


def _parse_date(value: str) -> date | None:
    evidence = _parse_date_evidence(value, basis="unknown")
    return evidence.lower if evidence is not None else None


def _parse_date_evidence(
    value: str,
    *,
    basis: str,
) -> _DateEvidence | None:
    if not value:
        return None
    day_match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?",
        value,
    )
    month_match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    year_match = re.fullmatch(r"(\d{4})", value)
    try:
        if day_match is not None:
            exact = date(
                int(day_match.group(1)),
                int(day_match.group(2)),
                int(day_match.group(3)),
            )
            return _DateEvidence(
                exact.isoformat(), "day", exact, exact, basis
            )
        if month_match is not None:
            year = int(month_match.group(1))
            month = int(month_match.group(2))
            lower = date(year, month, 1)
            upper = date(year, month, monthrange(year, month)[1])
            return _DateEvidence(
                f"{year:04d}-{month:02d}", "month", lower, upper, basis
            )
        if year_match is not None:
            year = int(year_match.group(1))
            return _DateEvidence(
                f"{year:04d}",
                "year",
                date(year, 1, 1),
                date(year, 12, 31),
                basis,
            )
    except ValueError:
        return None
    return None


def _prefer_date_evidence(
    left: _DateEvidence | None,
    right: _DateEvidence | None,
    *,
    left_priority: int,
    right_priority: int,
) -> _DateEvidence | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.upper < right.lower:
        return left
    if right.upper < left.lower:
        return right
    left_precision = _DATE_PRECISION_ORDER[left.precision]
    right_precision = _DATE_PRECISION_ORDER[right.precision]
    if left_precision != right_precision:
        return left if left_precision > right_precision else right
    if left_priority != right_priority:
        return left if left_priority < right_priority else right
    return left if (left.lower, left.value) <= (right.lower, right.value) else right


def _window_membership(
    evidence: _DateEvidence,
    *,
    start: date,
    end: date,
) -> str:
    if evidence.lower >= start and evidence.upper <= end:
        return "inside"
    if evidence.upper < start or evidence.lower > end:
        return "outside"
    return "ambiguous"


def _apply_date_evidence(
    record: dict[str, Any],
    evidence: _DateEvidence | None,
) -> None:
    record["first_public_date"] = (
        evidence.value if evidence is not None else None
    )
    record["first_public_date_precision"] = (
        evidence.precision if evidence is not None else None
    )
    record["recency_basis"] = evidence.basis if evidence is not None else None


def _empty_recency_stats(unique_citers: int) -> dict[str, int]:
    return {
        "unique_citers": unique_citers,
        "eligible_citers": 0,
        "exact_date_citers": 0,
        "reduced_precision_date_citers": 0,
        "excluded_missing_first_public_date": 0,
        "excluded_ambiguous_first_public_date": 0,
        "excluded_outside_window": 0,
    }


def _arxiv_month_date(record: dict[str, Any]) -> date | None:
    evidence = _arxiv_month_evidence(record)
    return evidence.lower if evidence is not None else None


def _arxiv_month_evidence(
    record: dict[str, Any],
) -> _DateEvidence | None:
    paper_id = normalize_paper_id(record.get("paper_id") or paper_key(record))
    arxiv_id = arxiv_path_id(paper_id)
    match = re.fullmatch(r"(\d{2})(\d{2})\.\d{4,5}", arxiv_id)
    if not match:
        return None
    year = 2000 + int(match.group(1))
    month = int(match.group(2))
    try:
        return _DateEvidence(
            f"{year:04d}-{month:02d}",
            "month",
            date(year, month, 1),
            date(year, month, monthrange(year, month)[1]),
            "arxiv_id_month",
        )
    except ValueError:
        return None
