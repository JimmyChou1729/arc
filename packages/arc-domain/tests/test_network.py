from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from arc_domain import network
from arc_domain.text import paper_key


def _paper(
    paper_id: str,
    *,
    title: str | None = None,
    citations: int = 1,
    **extra,
) -> dict:
    return {
        "paper_id": paper_id,
        "title": title or paper_id,
        "citation_count": citations,
        "identifiers": {"paper_id": paper_id},
        **extra,
    }


def test_merge_citer_pool_balances_fixed_inspire_rankings():
    recent = [
        _paper("arXiv:2401.00001", title="Recent A"),
        _paper("arXiv:2401.00002", title="Recent B"),
        _paper("arXiv:2401.00003", title="Recent C"),
    ]
    cited = [
        _paper("arXiv:2401.00002", title="Recent B", citations=200),
        _paper("arXiv:2001.00001", title="Cited D", citations=500),
        _paper("arXiv:2001.00002", title="Cited E", citations=400),
    ]

    merged = network.merge_citer_pool(
        "arXiv:2301.00001",
        most_recent=recent,
        most_cited=cited,
        limit=3,
    )

    assert [item["paper_id"] for item in merged] == [
        "arXiv:2401.00001",
        "arXiv:2401.00002",
        "arXiv:2001.00001",
    ]
    assert merged[1]["citer_sources"] == ["mostrecent", "mostcited"]
    assert merged[1]["mostrecent_rank"] == 2
    assert merged[1]["mostcited_rank"] == 1


@pytest.mark.parametrize(
    ("recent_published", "cited_published"),
    [
        ("2020-03-04", "2026-07-20"),
        ("2026-07-20", "2020-03-04"),
    ],
)
def test_merge_citer_pool_preserves_earliest_same_field_across_stream_order(
    recent_published: str,
    cited_published: str,
):
    paper_id = "arXiv:2003.00001"

    merged = network.merge_citer_pool(
        "arXiv:1901.00001",
        most_recent=[_paper(paper_id, published=recent_published)],
        most_cited=[_paper(paper_id, published=cited_published)],
        limit=1,
    )

    assert merged[0]["published"] == "2020-03-04"
    evidence = network._first_public_date_evidence(merged[0])
    assert evidence is not None
    assert (evidence.lower, evidence.basis) == (
        date(2020, 3, 1),
        "arxiv_id_month",
    )


def test_merge_citer_pool_preserves_earliest_date_and_basis_across_fields():
    paper_id = "doi:10.1000/date-conflict"
    merged = network.merge_citer_pool(
        "arXiv:1901.00001",
        most_recent=[_paper(paper_id, published="2026-07-20")],
        most_cited=[_paper(paper_id, preprint_date="2020-03-04")],
        limit=1,
    )

    selected = network._select_domain_papers(
        merged,
        foundation_id="arXiv:1901.00001",
        intent_ranking={"ranked_paper_ids": []},
        intent="",
        selected_count=1,
        max_total=1,
        as_of_date=date(2026, 7, 24),
        strict_window=True,
    )

    evidence = network._first_public_date_evidence(merged[0])
    assert evidence is not None
    assert (evidence.lower, evidence.basis) == (
        date(2020, 3, 4),
        "preprint_date",
    )
    assert selected[0]["first_public_date"] == "2020-03-04"
    assert selected[0]["recency_basis"] == "preprint_date"


@pytest.mark.parametrize("limit", [0, -1, True])
def test_merge_citer_pool_requires_positive_limit(limit):
    with pytest.raises(ValueError, match="positive"):
        network.merge_citer_pool(
            "arXiv:2301.00001",
            most_recent=[],
            most_cited=[],
            limit=limit,
        )


def test_intent_ranking_filters_unknown_and_has_lexical_fallback():
    pool = [
        _paper("arXiv:2401.00001", title="Scattering amplitudes"),
        _paper("arXiv:2401.00002", title="Inflation"),
    ]
    normalized = network.normalize_intent_ranking(
        {
            "ranked_paper_ids": [
                "2401.00001",
                "arXiv:9999.99999",
                "2401.00001",
            ],
            "reasoning": "match",
        },
        citer_pool=pool,
    )
    fallback = network.deterministic_intent_ranking(
        pool,
        intent="amplitude scattering",
    )

    assert normalized["ranked_paper_ids"] == ["arXiv:2401.00001"]
    assert fallback["ranked_paper_ids"] == ["arXiv:2401.00001"]
    assert fallback["method"] == "deterministic_fallback"
    assert "Candidate papers" in network.intent_ranking_prompt(
        pool,
        intent="amplitudes",
    )


def test_title_only_reference_is_not_a_graph_identity():
    assert paper_key({"title": "A title is not a stable identifier"}) == ""
    assert paper_key({"identifiers": {"arxiv": "2201.00001"}}) == (
        "arXiv:2201.00001"
    )
    assert paper_key({"identifiers": {"inspire_recid": "12345"}}) == (
        "inspire:12345"
    )


def test_common_references_ignore_title_only_entries():
    refs_by_selected = {
        "arXiv:2401.00001": [
            {"title": "Title Only"},
            {"paper_id": "arXiv:2201.00001", "title": "Stable"},
        ],
        "arXiv:2401.00002": [
            {"title": "Title Only"},
            {"identifiers": {"arxiv": "2201.00001"}, "title": "Stable"},
        ],
    }
    common = network._common_references(
        foundation_id="arXiv:2301.00001",
        selected_ids=["arXiv:2401.00001", "arXiv:2401.00002"],
        refs_by_selected=refs_by_selected,
        max_extra=10,
        metadata_by_id={
            "arXiv:2201.00001": {
                "paper_id": "arXiv:2201.00001",
                "title": "Stable",
                "citation_count": 42,
            }
        },
    )

    assert [item["paper_id"] for item in common] == ["arXiv:2201.00001"]


def test_selection_adds_recent_arxiv_papers_within_total_cap():
    selected = network._select_domain_papers(
        [
            _paper(
                "arXiv:2001.00001",
                citations=10_000,
                year=2020,
                published="2020-01-01",
            ),
            _paper(
                "arXiv:2607.00001",
                citations=0,
                year=2026,
                published="2026-07-24",
            ),
        ],
        foundation_id="arXiv:1901.00001",
        intent_ranking={"ranked_paper_ids": []},
        intent="",
        selected_count=1,
        max_total=2,
        as_of_date=date(2026, 7, 24),
    )

    assert [item["paper_id"] for item in selected] == [
        "arXiv:2001.00001",
        "arXiv:2607.00001",
    ]
    assert selected[1]["recent_arxiv"] is True


def test_recent_window_uses_first_public_date_and_arxiv_month_fallback():
    as_of = datetime(2026, 7, 20, tzinfo=timezone.utc)
    old_revised = _paper(
        "arXiv:2001.00001",
        published="2020-01-01",
        updated="2026-07-20",
    )
    boundary = _paper("arXiv:2507.00001", published="2025-07-20")

    assert not network._is_recent_arxiv_paper(
        old_revised,
        now=as_of,
        window_days=365,
    )
    assert network._is_recent_arxiv_paper(
        boundary,
        now=as_of,
        window_days=365,
    )
    evidence = network._first_public_date_evidence(old_revised)
    assert evidence is not None
    assert (evidence.lower, evidence.basis) == (
        date(2020, 1, 1),
        "published",
    )
    fallback = network._first_public_date_evidence(
        _paper("arXiv:2507.12345", updated="2026-07-20")
    )
    assert fallback is not None
    assert (fallback.lower, fallback.basis) == (
        date(2025, 7, 1),
        "arxiv_id_month",
    )


def test_first_public_date_uses_earliest_valid_field_not_field_priority():
    record = _paper(
        "doi:10.1000/field-order",
        published="2026-07-20",
        preprint_date="2020-03-04",
        earliest_date="2019-02-03",
        created="2021-04-05",
        updated="2018-01-01",
    )

    evidence = network._first_public_date_evidence(record)
    assert evidence is not None
    assert (evidence.lower, evidence.basis) == (
        date(2019, 2, 3),
        "earliest_date",
    )

    record["published"] = "2019-02-03"
    evidence = network._first_public_date_evidence(record)
    assert evidence is not None
    assert (evidence.lower, evidence.basis) == (
        date(2019, 2, 3),
        "published",
    )


def test_graph_v2_keeps_domain_identity_and_resolved_recency_date():
    graph = network._build_graph(
        domain_id="domain-a",
        foundation=_paper("arXiv:2301.00001"),
        parent_foundations=[],
        selected_papers=[],
        common_references=[],
        refs_by_selected={},
        intent="amplitudes",
        created_at="2026-07-24T12:00:00+00:00",
        recent_window_days=365,
        as_of_date=date(2026, 7, 24),
        recency_stats=network.recency_candidate_stats(
            [], as_of_date=date(2026, 7, 24), window_days=365
        ),
    )

    assert graph["schema_version"] == "arc.domain_graph.v2"
    assert graph["domain_id"] == "domain-a"
    assert graph["created_at"] == "2026-07-24T12:00:00+00:00"
    assert graph["recency"]["end_date"] == "2026-07-24"


@pytest.mark.parametrize(
    "value",
    [
        "2025junk",
        "2025-01bad",
        "2025-01-02 trailing",
        "02025",
        "2025-13",
        "2025-02-30",
    ],
)
def test_public_date_parser_rejects_malformed_or_prefixed_values(value: str):
    assert network._parse_date_evidence(value, basis="published") is None


def test_date_evidence_preserves_precision_and_uses_earliest_cross_field_lower_bound():
    year = network._parse_date_evidence("2024", basis="published")
    month = network._parse_date_evidence("2024-02", basis="published")
    day = network._parse_date_evidence("2024-02-29", basis="preprint_date")

    assert year is not None
    assert (year.value, year.precision, year.lower, year.upper) == (
        "2024",
        "year",
        date(2024, 1, 1),
        date(2024, 12, 31),
    )
    assert month is not None
    assert (month.lower, month.upper) == (
        date(2024, 2, 1),
        date(2024, 2, 29),
    )
    evidence = network._first_public_date_evidence(
        {
            "paper_id": "doi:10.1000/precision",
            "published": "2024",
            "preprint_date": "2024-02-29",
        }
    )
    assert evidence == year


def test_strict_window_uses_bounds_and_excludes_partial_overlap():
    inside_year = _paper("doi:10.1000/year-inside", published="2026")
    ambiguous_year = _paper("doi:10.1000/year-edge", published="2025")
    ambiguous_month = _paper("doi:10.1000/month-edge", published="2025-12")
    exact_boundary = _paper(
        "doi:10.1000/day-edge", published="2025-12-31"
    )

    recent, cited, stats = network.strict_window_citer_streams(
        "arXiv:2001.00001",
        most_recent=[
            inside_year,
            ambiguous_year,
            ambiguous_month,
            exact_boundary,
        ],
        most_cited=[],
        as_of_date=date(2026, 12, 31),
        window_days=365,
    )

    assert [item["paper_id"] for item in recent] == [
        inside_year["paper_id"],
        exact_boundary["paper_id"],
    ]
    assert cited == []
    assert stats == {
        "unique_citers": 4,
        "eligible_citers": 2,
        "exact_date_citers": 1,
        "reduced_precision_date_citers": 3,
        "excluded_missing_first_public_date": 0,
        "excluded_ambiguous_first_public_date": 2,
        "excluded_outside_window": 0,
    }


def test_merge_overlapping_same_field_prefers_higher_precision():
    merged = network.merge_citer_pool(
        "arXiv:1901.00001",
        most_recent=[
            _paper("doi:10.1000/merged-precision", published="2024")
        ],
        most_cited=[
            _paper(
                "doi:10.1000/merged-precision",
                published="2024-03-04",
            )
        ],
        limit=1,
    )

    assert merged[0]["published"] == "2024-03-04"
