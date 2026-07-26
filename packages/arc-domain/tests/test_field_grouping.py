from __future__ import annotations

import pytest

from arc_domain import (
    FieldGroupingConstraintError,
    FieldGroupingError,
    build_field_groups,
    normalize_field_grouping_pairs,
)


def _package(package_id: str) -> dict[str, object]:
    suffix = package_id.rsplit("-", 1)[-1]
    return {
        "domain_package_id": package_id,
        "seed_paper": f"seed:{suffix}",
        "title": f"Domain {suffix}",
        "overview": f"Overview {suffix}",
        "task_focus": {"goal": suffix},
        "methodology": [f"method-{suffix}"],
        "known_solved_cases": [],
        "open_axes_for_new_work": [],
        "mathematical_opportunities": {
            "well_defined_problems": []
        },
        "summary_schema_version": "arc.domain_summary.v5",
        "summary_json_path": f"{suffix}_domain_summary.json",
        "summary_markdown_path": f"{suffix}_domain_summary.md",
        "paper_json_pack_path": f"{suffix}_paper_json_pack.json",
        "paper_ids": [f"paper:{suffix}"],
        "citation_edges": [],
    }


def _pair(
    left: str,
    right: str,
    classification: str,
    confidence: float,
) -> dict[str, object]:
    return {
        "package_a": left,
        "package_b": right,
        "classification": classification,
        "confidence": confidence,
        "reason": f"{left}-{right}",
        "evidence": {"semantic": "fixture"},
    }


def test_normalize_pairs_requires_every_pair_and_orders_endpoints() -> None:
    packages = [
        _package("domain-c"),
        _package("domain-a"),
        _package("domain-b"),
    ]
    pairs = [
        _pair("domain-c", "domain-b", "distinct_field", 0.9),
        _pair("domain-b", "domain-a", "same_field", 0.8),
        _pair("domain-c", "domain-a", "distinct_field", 0.9),
    ]

    normalized = normalize_field_grouping_pairs(
        {"pairs": pairs}, packages
    )

    assert [
        (item["package_a"], item["package_b"])
        for item in normalized
    ] == [
        ("domain-a", "domain-b"),
        ("domain-a", "domain-c"),
        ("domain-b", "domain-c"),
    ]
    assert normalized[0]["confidence"] == 0.8

    with pytest.raises(
        FieldGroupingError,
        match="classify every package pair",
    ):
        normalize_field_grouping_pairs(
            {"pairs": pairs[:-1]}, packages
        )


def test_normalize_pairs_reports_typed_malformed_input() -> None:
    packages = [_package("domain-a"), _package("domain-b")]

    with pytest.raises(
        FieldGroupingError,
        match="requires evidence",
    ):
        normalize_field_grouping_pairs(
            {
                "pairs": [
                    {
                        **_pair(
                            "domain-a",
                            "domain-b",
                            "same_field",
                            0.9,
                        ),
                        "evidence": [],
                    }
                ]
            },
            packages,
        )


@pytest.mark.parametrize(
    "packages, message",
    [
        ([], "non-empty list"),
        ([{}], "domain_package_id"),
        (
            [_package("domain-a"), _package("domain-a")],
            "must be unique",
        ),
    ],
)
def test_package_identity_errors_are_typed(
    packages: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(FieldGroupingError, match=message):
        normalize_field_grouping_pairs(None, packages)
    with pytest.raises(FieldGroupingError, match=message):
        build_field_groups(
            packages,
            [],
            intent="",
            force_single=False,
        )


def test_transitive_merge_across_hard_split_is_typed_conflict() -> None:
    packages = [
        _package("domain-a"),
        _package("domain-b"),
        _package("domain-c"),
    ]
    pairs = [
        _pair("domain-a", "domain-b", "same_field", 0.9),
        _pair("domain-b", "domain-c", "uncertain", 0.7),
        _pair("domain-a", "domain-c", "distinct_field", 0.95),
    ]

    with pytest.raises(
        FieldGroupingConstraintError,
        match="contradictory/non-transitive",
    ):
        normalize_field_grouping_pairs(
            {"pairs": pairs}, packages
        )


def test_field_group_construction_is_stable_and_handles_sparse_cards() -> None:
    packages = [
        _package("domain-b"),
        {
            **_package("domain-a"),
            "mathematical_opportunities": None,
        },
        _package("domain-c"),
    ]
    payload = {
        "pairs": [
            _pair(
                "domain-b", "domain-c", "distinct_field", 0.88
            ),
            _pair(
                "domain-a", "domain-c", "distinct_field", 0.94
            ),
            _pair(
                "domain-a", "domain-b", "same_field", 0.91
            ),
        ]
    }
    pairs = normalize_field_grouping_pairs(payload, packages)

    first = build_field_groups(
        packages,
        pairs,
        intent="bridge",
        force_single=False,
    )
    second = build_field_groups(
        list(reversed(packages)),
        pairs,
        intent="bridge",
        force_single=False,
    )

    assert first == second
    assert [item["domain_package_ids"] for item in first] == [
        ["domain-a", "domain-b"],
        ["domain-c"],
    ]
    assert first[0]["confidence"] == 0.91
    assert first[0]["field_card"]["mathematical_opportunities"] == {
        "well_defined_problems": []
    }


def test_force_single_preserves_conservative_fallback_contract() -> None:
    packages = [_package("domain-b"), _package("domain-a")]

    groups = build_field_groups(
        packages,
        [],
        intent="bridge",
        force_single=True,
    )

    assert len(groups) == 1
    assert groups[0]["domain_package_ids"] == [
        "domain-a",
        "domain-b",
    ]
    assert groups[0]["confidence"] == 0.0
    assert groups[0]["reason"].startswith(
        "Conservative fallback merged all packages"
    )
