from __future__ import annotations

from arc_domain._roles import role_order
from arc_domain.render import render_network_html


def test_role_order_matches_business_artifacts():
    assert role_order("selected_foundation") < role_order("parent_foundation")
    assert role_order("parent_foundation") < role_order("domain_paper")
    assert role_order("domain_paper") < role_order("common_reference")
    assert role_order("unknown") == 99


def test_render_network_html_is_pure_and_orders_domain_before_common_reference():
    graph = {
        "schema_version": "arc.domain_graph.v1",
        "foundation_paper": "arXiv:2401.00001",
        "nodes": [
            {
                "id": "common",
                "paper_id": "arXiv:2301.00001",
                "title": "Common Reference",
                "role": "common_reference",
                "support_count": 10,
                "citation_count": 50,
            },
            {
                "id": "domain",
                "paper_id": "arXiv:2402.00001",
                "title": "Domain Paper",
                "role": "domain_paper",
                "domain_score": 1.0,
                "citation_count": 5,
            },
        ],
        "edges": [],
    }

    rendered = render_network_html(graph)

    assert rendered.startswith("<!doctype html>")
    assert rendered.index("Domain Paper") < rendered.index("Common Reference")
    assert "arc.domain_graph.v1" not in rendered


def test_render_contract_does_not_claim_the_cdn_document_is_self_contained():
    assert "self-contained" not in (render_network_html.__doc__ or "")


def test_render_pins_mathjax_and_uses_canonical_paper_links_everywhere():
    graph = {
        "foundation_paper": "arXiv:2401.00001",
        "nodes": [
            {
                "id": "arxiv",
                "paper_id": "2401.00001v2",
                "title": "ArXiv paper",
                "role": "selected_foundation",
            },
            {
                "id": "doi",
                "paper_id": "DOI:10.1000/ABC",
                "title": "DOI paper",
                "role": "domain_paper",
            },
            {
                "id": "inspire",
                "paper_id": "recid:12345",
                "title": "INSPIRE paper",
                "role": "common_reference",
            },
        ],
        "edges": [],
    }

    rendered = render_network_html(graph)

    assert (
        "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/"
        "es5/tex-chtml.js"
    ) in rendered
    assert "mathjax@3/es5" not in rendered
    for expected in (
        "https://arxiv.org/abs/2401.00001",
        "https://doi.org/10.1000/abc",
        "https://inspirehep.net/literature/12345",
    ):
        assert rendered.count(expected) == 2
    assert "arxivHref" not in rendered
    assert 'href="#"' not in rendered
    assert rendered.count('rel="noopener noreferrer"') == 4


def test_render_shows_one_aggregate_precision_notice_and_node_date_tooltip():
    graph = {
        "foundation_paper": "arXiv:2401.00001",
        "recency": {
            "exact_date_count": 1,
            "reduced_precision_date_count": 2,
            "ambiguous_date_count": 3,
        },
        "nodes": [
            {
                "id": "paper",
                "paper_id": "arXiv:2401.00002",
                "title": "Reduced precision",
                "role": "domain_paper",
                "first_public_date": "2024-01",
                "first_public_date_precision": "month",
                "recency_basis": "published",
            }
        ],
        "edges": [],
    }

    rendered = render_network_html(graph)

    assert (
        "Candidate first-public dates: 1 exact, 2 reduced precision, "
        "3 ambiguous"
    ) in rendered
    assert rendered.count("Date precision notice:") == 1
    assert "First public: 2024-01 (month precision; published)" in rendered
