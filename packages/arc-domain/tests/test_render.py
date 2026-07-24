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
