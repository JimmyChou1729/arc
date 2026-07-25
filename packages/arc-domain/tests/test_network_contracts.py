from __future__ import annotations

from datetime import date

import pytest

from arc_domain import network
from arc_domain.render import _node_rank_key


FOUNDATION = "arXiv:2001.00001"
PAPER_A = "arXiv:2501.00001"
PAPER_B = "arXiv:2501.00002"
COMMON_A = "arXiv:1901.00001"
COMMON_B = "arXiv:1901.00002"


def _paper(
    paper_id: str,
    *,
    title: str | None = None,
    year: int = 2025,
    citations: int = 0,
    **extra,
) -> dict:
    return {
        "paper_id": paper_id,
        "title": title or paper_id,
        "year": year,
        "citation_count": citations,
        "identifiers": {"paper_id": paper_id},
        **extra,
    }


def test_zero_domain_score_never_falls_back_to_common_reference_support() -> None:
    low_citation_high_support = {
        "id": "high-support",
        "role": "domain_paper",
        "domain_score": 0.0,
        "support_count": 100,
        "citation_count": 1,
    }
    high_citation_no_support = {
        "id": "high-citation",
        "role": "domain_paper",
        "domain_score": 0.0,
        "support_count": 0,
        "citation_count": 2,
    }

    assert [
        node["id"]
        for node in sorted(
            [low_citation_high_support, high_citation_no_support],
            key=_node_rank_key,
        )
    ] == ["high-citation", "high-support"]


def test_common_reference_capacity_honors_zero_and_finite_limits() -> None:
    refs = {
        PAPER_A: [_paper(COMMON_A), _paper(COMMON_B)],
        PAPER_B: [_paper(COMMON_A), _paper(COMMON_B)],
        "arXiv:2501.00003": [_paper(COMMON_A)],
    }

    none = network._common_references(
        foundation_id=FOUNDATION,
        selected_ids=[PAPER_A, PAPER_B],
        refs_by_selected=refs,
        max_extra=0,
        metadata_by_id={},
    )
    one = network._common_references(
        foundation_id=FOUNDATION,
        selected_ids=[PAPER_A, PAPER_B],
        refs_by_selected=refs,
        max_extra=1,
        metadata_by_id={},
    )

    assert none == []
    assert [item["paper_id"] for item in one] == [COMMON_A]
    assert one[0]["support_count"] == 3


def test_recent_candidates_only_fill_the_remaining_graph_capacity() -> None:
    selected = network._select_domain_papers(
        [
            _paper(PAPER_A, title="highly cited", year=2020, citations=10_000),
            _paper(PAPER_B, title="another cited", year=2021, citations=9_000),
            _paper("arXiv:2607.00001", year=2026, citations=0),
            _paper("arXiv:2607.00002", year=2026, citations=0),
            _paper("arXiv:2607.00003", year=2026, citations=0),
        ],
        foundation_id=FOUNDATION,
        intent_ranking={"ranked_paper_ids": []},
        intent="",
        selected_count=2,
        max_total=3,
        as_of_date=date(2026, 7, 24),
    )

    assert len(selected) == 3
    assert {item["paper_id"] for item in selected[:2]} == {PAPER_A, PAPER_B}
    assert sum(bool(item["recent_arxiv"]) for item in selected) == 1


def test_domain_selection_excludes_fixed_roles_and_refills_after_duplicates() -> None:
    selected = network._select_domain_papers(
        [
            _paper(COMMON_A, year=2020, citations=100_000),
            _paper(PAPER_A, year=2025, citations=20),
            _paper(
                "https://arxiv.org/abs/2501.00001v2",
                year=2025,
                citations=10,
            ),
            _paper(PAPER_B, year=2025, citations=5),
        ],
        foundation_id=FOUNDATION,
        excluded_ids={COMMON_A},
        intent_ranking={
            "ranked_paper_ids": [COMMON_A, PAPER_A, PAPER_B],
        },
        intent="",
        selected_count=2,
        max_total=2,
        as_of_date=date(2026, 7, 24),
        strict_window=True,
    )

    assert [item["paper_id"] for item in selected] == [PAPER_A, PAPER_B]


def test_common_references_exclude_parent_roles_and_refill_capacity() -> None:
    refs = {
        PAPER_A: [_paper(COMMON_A), _paper(COMMON_B)],
        PAPER_B: [_paper(COMMON_A), _paper(COMMON_B)],
    }

    common = network._common_references(
        foundation_id=FOUNDATION,
        selected_ids=[PAPER_A, PAPER_B],
        excluded_ids={COMMON_B},
        refs_by_selected=refs,
        max_extra=1,
        metadata_by_id={},
    )

    assert [item["paper_id"] for item in common] == [COMMON_A]


def test_common_references_refill_after_metadata_resolves_to_an_existing_role() -> None:
    refs = {
        PAPER_A: [_paper(COMMON_A), _paper(COMMON_B)],
        PAPER_B: [_paper(COMMON_A), _paper(COMMON_B)],
    }

    common = network._common_references(
        foundation_id=FOUNDATION,
        selected_ids=[PAPER_A, PAPER_B],
        refs_by_selected=refs,
        max_extra=1,
        metadata_by_id={
            # COMMON_B ranks first on the deterministic ID tie-break, but its
            # authoritative metadata resolves to an already-selected paper.
            COMMON_B: _paper(PAPER_A),
        },
    )

    assert [item["paper_id"] for item in common] == [COMMON_A]


def test_strict_window_filters_unique_citers_before_merge_and_accepts_dated_non_arxiv() -> None:
    old = _paper("arXiv:2001.00002", year=2020, citations=1000, published="2020-01-02")
    boundary = _paper("arXiv:2407.00001", year=2024, citations=1, published="2024-07-24")
    doi = _paper(
        "doi:10.1000/recent",
        year=2026,
        citations=2,
        published="2026-07-20",
        identifiers={"doi": "10.1000/recent"},
    )
    missing = _paper(
        "doi:10.1000/undated",
        year=2025,
        citations=3,
        identifiers={"doi": "10.1000/undated"},
    )
    revised_old = _paper(
        "arXiv:2002.00001",
        year=2020,
        citations=4,
        published="2020-02-01",
        updated="2026-07-20",
    )
    recent, cited, stats = network.strict_window_citer_streams(
        FOUNDATION,
        most_recent=[old, boundary, doi, missing, revised_old],
        most_cited=[old, doi],
        as_of_date=date(2026, 7, 24),
        window_days=730,
    )

    assert [item["paper_id"] for item in recent] == [boundary["paper_id"], doi["paper_id"]]
    assert [item["paper_id"] for item in cited] == [doi["paper_id"]]
    assert stats == {
        "unique_citers": 5,
        "eligible_citers": 2,
        "excluded_missing_first_public_date": 1,
        "excluded_outside_window": 2,
    }
    merged = network.merge_citer_pool(
        FOUNDATION, most_recent=recent, most_cited=cited, limit=1
    )
    assert merged[0]["paper_id"] == boundary["paper_id"]


def test_strict_window_uses_earliest_field_and_cross_stream_date_at_boundary() -> None:
    boundary = _paper(
        "doi:10.1000/boundary",
        published="2024-07-24",
        identifiers={"doi": "10.1000/boundary"},
    )
    earlier_field = _paper(
        "doi:10.1000/earlier-field",
        published="2026-07-20",
        preprint_date="2024-07-23",
        identifiers={"doi": "10.1000/earlier-field"},
    )
    cross_stream_recent = _paper(
        "doi:10.1000/cross-stream",
        published="2026-07-20",
        identifiers={"doi": "10.1000/cross-stream"},
    )
    cross_stream_old = _paper(
        "doi:10.1000/cross-stream",
        published="2024-07-23",
        identifiers={"doi": "10.1000/cross-stream"},
    )

    recent, cited, stats = network.strict_window_citer_streams(
        FOUNDATION,
        most_recent=[boundary, earlier_field, cross_stream_recent],
        most_cited=[cross_stream_old, boundary],
        as_of_date=date(2026, 7, 24),
        window_days=730,
    )

    assert [item["paper_id"] for item in recent] == [boundary["paper_id"]]
    assert [item["paper_id"] for item in cited] == [boundary["paper_id"]]
    assert stats == {
        "unique_citers": 3,
        "eligible_citers": 1,
        "excluded_missing_first_public_date": 0,
        "excluded_outside_window": 2,
    }


def test_network_scores_recompute_across_initial_citer_and_reference_stages() -> None:
    initial = network._select_domain_papers(
        [
            _paper(PAPER_A, title="alpha", year=2025),
            _paper(PAPER_B, title="beta", year=2024),
        ],
        foundation_id=FOUNDATION,
        intent_ranking={"ranked_paper_ids": []},
        intent="",
        selected_count=2,
        max_total=2,
        as_of_date=date(2026, 7, 24),
    )
    initial_by_id = {item["paper_id"]: item for item in initial}
    assert {
        paper_id: (item["in_graph_citer_count"], item["reference_edge_count"])
        for paper_id, item in initial_by_id.items()
    } == {PAPER_A: (0, 0), PAPER_B: (0, 0)}

    refs = {
        PAPER_A: [_paper(FOUNDATION), _paper(COMMON_A), _paper(COMMON_A)],
        PAPER_B: [_paper(PAPER_A), _paper(COMMON_A)],
    }
    after_citers = network._add_in_graph_citer_scores(
        initial,
        refs_by_selected=refs,
    )
    citer_by_id = {item["paper_id"]: item for item in after_citers}
    assert citer_by_id[PAPER_A]["in_graph_citer_count"] == 1
    assert citer_by_id[PAPER_A]["in_graph_citer_score"] == 1.0
    assert citer_by_id[PAPER_B]["in_graph_citer_count"] == 0
    assert citer_by_id[PAPER_A]["domain_score"] == pytest.approx(
        initial_by_id[PAPER_A]["domain_score"] + network.GRAPH_CITER_WEIGHT
    )

    common = network._common_references(
        foundation_id=FOUNDATION,
        selected_ids=[PAPER_A, PAPER_B],
        refs_by_selected=refs,
        max_extra=1,
        metadata_by_id={},
    )
    final = network._add_reference_edge_scores(
        after_citers,
        foundation_id=FOUNDATION,
        parent_foundations=[],
        common_references=common,
        refs_by_selected=refs,
    )
    final_by_id = {item["paper_id"]: item for item in final}
    assert [item["paper_id"] for item in common] == [COMMON_A]
    assert final_by_id[PAPER_A]["reference_edge_count"] == 2
    assert final_by_id[PAPER_B]["reference_edge_count"] == 2
    for paper_id, item in final_by_id.items():
        expected = network._domain_score(
            citation_per_year=item["citation_per_year"],
            recency=item["recency"],
            intent_overlap=item["intent_overlap"],
            intent_boost=item["intent_boost"],
            in_graph_citer_score=item["in_graph_citer_score"],
            reference_edge_count=item["reference_edge_count"],
        )
        assert item["domain_score"] == pytest.approx(round(expected, 4))


def test_graph_role_precedence_is_defensive_and_globally_unique() -> None:
    selected = _paper(
        PAPER_A,
        published="2025-01-01",
        recent_arxiv=True,
        recency_basis="published",
    )
    duplicate_selected = _paper(
        "https://arxiv.org/abs/2501.00001v3",
        published="2020-01-01",
        recent_arxiv=False,
        recency_basis="published",
    )

    graph = network._build_graph(
        domain_id="domain-role-precedence",
        foundation=_paper(FOUNDATION),
        parent_foundations=[
            _paper("https://arxiv.org/abs/2001.00001v2"),
            _paper(COMMON_A),
        ],
        selected_papers=[
            _paper(COMMON_A),
            selected,
            duplicate_selected,
        ],
        common_references=[
            _paper(PAPER_A),
            _paper(COMMON_B),
            _paper(COMMON_B),
        ],
        refs_by_selected={},
        intent="role precedence",
        created_at="2026-07-24T00:00:00+00:00",
        recent_window_days=365,
        as_of_date=date(2026, 7, 24),
    )

    roles = {node["paper_id"]: node["role"] for node in graph["nodes"]}
    assert roles == {
        FOUNDATION: "selected_foundation",
        COMMON_A: "parent_foundation",
        PAPER_A: "domain_paper",
        COMMON_B: "common_reference",
    }
    assert len(graph["nodes"]) == len(roles)
    assert graph["recency"]["included_count"] == 1
    assert graph["recency"]["excluded_count"] == 0
    assert graph["recency"]["recency_basis"] == {"published": 1}
