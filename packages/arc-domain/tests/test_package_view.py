from __future__ import annotations

from copy import deepcopy

import pytest

from arc_domain import (
    DomainPackageValidationError,
    decode_domain_package,
    decode_domain_paper_pack,
    decode_domain_summary,
)


FOUNDATION = "arXiv:2301.00001"
REFERENCE = "arXiv:2401.00002"
REFERENCE_DOI = "doi:10.1234/reference"


def _summary(*, schema_version: str = "arc.domain_summary.v5") -> dict:
    value = {
        "schema_version": schema_version,
        "domain_title": "Example domain",
        "brief_introduction": "A compact introduction.",
        "task_focus": {
            "user_intent": "Find a controlled calculation.",
            "research_scope": "The supplied domain papers.",
            "priority_rules": ["Satisfy the user intent first."],
        },
        "foundation_paper": {
            "paper_id": "https://arxiv.org/abs/2301.00001v3",
            "title": "Foundation",
            "reason": "Anchor",
        },
        "best_reference_paper": {
            "paper_id": REFERENCE_DOI,
            "title": "Reference",
            "reason": "Entry point",
        },
        "methodology": [
            {"claim": "Use the controlled limit.", "papers": [REFERENCE_DOI]}
        ],
        "mathematical_opportunities": {
            "well_defined_problems": [
                {
                    "problem": "Evaluate the first coefficient.",
                    "importance": "It distinguishes two mechanisms.",
                    "mathematical_object": "A first expansion coefficient.",
                    "assumptions_and_regime": ["controlled limit"],
                    "success_criterion": "It agrees with the Ward identity.",
                    "available_systematic_methods": [],
                    "bounded_first_calculation": "Compute the leading term.",
                    "feasibility": {
                        "ready_inputs": ["boundary data"],
                        "blocking_unknowns": [],
                        "kill_criterion": "Stop if the identity fails.",
                    },
                    "target_domain_papers": [REFERENCE_DOI],
                    "evidence_status": "source_explicit",
                }
            ]
        },
        "known_solved_cases": [
            {
                "solved_case": "A benchmark limit.",
                "why_it_is_solved": "The coefficient is known.",
                "transferable_form": "Reuse the normalization check.",
                "forbidden_reuse": "Do not claim the full problem is new.",
                "valid_new_axes": ["Change the boundary condition."],
                "papers": [REFERENCE_DOI],
            }
        ],
        "open_axes_for_new_work": [
            {
                "axis": "Alternative boundary data.",
                "guidance": "Keep the controlled limit.",
                "example_variations": ["A second boundary condition."],
                "papers": [REFERENCE_DOI],
            }
        ],
        "warnings": [],
    }
    return value


def _paper(
    paper_id: str,
    role: str,
    *,
    metadata: dict | None = None,
    references: list[dict] | None = None,
) -> dict:
    return {
        "paper_id": paper_id,
        "role": role,
        "metadata": metadata or {},
        "references": references or [],
        "toc": [],
        "warnings": [],
    }


def _paper_pack() -> dict:
    papers = [
        _paper(
            FOUNDATION,
            "selected_foundation",
            references=[{"paper_id": REFERENCE}],
        ),
        _paper(
            REFERENCE,
            "domain_paper",
            metadata={"identifiers": {"doi": "10.1234/REFERENCE"}},
        ),
    ]
    return {
        "schema_version": "arc.domain_paper_json_pack.v1",
        "domain_id": "domain-a",
        "foundation_paper": FOUNDATION,
        "paper_count": len(papers),
        "papers": papers,
        "warnings": [],
        "created_at": "2026-07-25T00:00:00+00:00",
    }


def test_domain_package_view_validates_identity_aliases_and_coverage() -> None:
    view = decode_domain_package(
        _summary(),
        _paper_pack(),
        expected_domain_id="domain-a",
    )

    assert view.domain_id == "domain-a"
    assert view.summary.schema_version == "arc.domain_summary.v5"
    assert view.summary.title == "Example domain"
    assert view.summary.overview == "A compact introduction."
    assert view.summary.referenced_paper_ids == (FOUNDATION, REFERENCE_DOI)
    assert view.paper_pack.foundation_paper_id == FOUNDATION
    assert view.paper_pack.paper_ids == (FOUNDATION, REFERENCE)
    assert view.paper_pack.citation_edges == ((FOUNDATION, REFERENCE),)
    assert view.paper_pack.covers("https://doi.org/10.1234/reference")
    assert view.paper_pack.equivalent(REFERENCE, REFERENCE_DOI)


def test_v4_summary_is_not_accepted() -> None:
    summary = _summary(schema_version="arc.domain_summary.v4")
    with pytest.raises(
        DomainPackageValidationError,
        match="summary.schema_version must be arc.domain_summary.v5",
    ):
        decode_domain_package(summary, _paper_pack())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda summary: summary.update({"unexpected": True}),
        lambda summary: summary.pop("task_focus"),
        lambda summary: summary.update({"schema_version": "arc.domain_summary.v99"}),
        lambda summary: summary.update({"domain_id": "domain-a"}),
    ],
)
def test_v5_summary_decoder_enforces_the_existing_closed_schema(mutation) -> None:
    summary = _summary()
    mutation(summary)

    with pytest.raises(DomainPackageValidationError):
        decode_domain_summary(summary)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda pack: pack.update({"schema_version": "arc.domain_paper_json_pack.v99"}),
            "schema_version must be",
        ),
        (
            lambda pack: pack.update({"unexpected": True}),
            "closed shape",
        ),
        (
            lambda pack: pack.update({"paper_count": True}),
            "non-negative integer",
        ),
        (
            lambda pack: pack.update({"paper_count": 1}),
            "does not match",
        ),
        (
            lambda pack: pack["papers"][0].update({"unexpected": True}),
            "closed shape",
        ),
        (
            lambda pack: pack["papers"][0].update({"references": ["not-an-object"]}),
            r"references\[0\] must be an object",
        ),
        (
            lambda pack: pack.update({"foundation_paper": "arXiv:9999.99999"}),
            "exactly one packed paper",
        ),
    ],
)
def test_paper_pack_decoder_enforces_closed_shape_and_internal_counts(
    mutation,
    message: str,
) -> None:
    pack = _paper_pack()
    mutation(pack)

    with pytest.raises(DomainPackageValidationError, match=message):
        decode_domain_paper_pack(pack)


def test_paper_pack_decoder_rejects_duplicate_alias_identities() -> None:
    pack = _paper_pack()
    pack["papers"].append(
        _paper(
            "inspire:12345",
            "common_reference",
            metadata={"identifiers": {"doi": "10.1234/reference"}},
        )
    )
    pack["paper_count"] += 1

    with pytest.raises(
        DomainPackageValidationError,
        match="duplicates an existing paper identity",
    ):
        decode_domain_paper_pack(pack)


@pytest.mark.parametrize(
    ("container_path", "expected_path"),
    [
        (("methodology", 0, "papers"), "summary.methodology[0].papers[0]"),
        (
            (
                "mathematical_opportunities",
                "well_defined_problems",
                0,
                "target_domain_papers",
            ),
            "summary.mathematical_opportunities.well_defined_problems"
            "[0].target_domain_papers[0]",
        ),
        (
            ("known_solved_cases", 0, "papers"),
            "summary.known_solved_cases[0].papers[0]",
        ),
        (
            ("open_axes_for_new_work", 0, "papers"),
            "summary.open_axes_for_new_work[0].papers[0]",
        ),
    ],
)
def test_domain_package_rejects_summary_references_absent_from_pack(
    container_path: tuple[object, ...],
    expected_path: str,
) -> None:
    summary = _summary()
    container = summary
    for key in container_path:
        container = container[key]
    container[0] = "arXiv:9999.99999"

    with pytest.raises(
        DomainPackageValidationError,
        match=expected_path.replace("[", r"\[").replace("]", r"\]"),
    ):
        decode_domain_package(summary, _paper_pack())


def test_domain_package_rejects_wrong_foundation_and_expected_domain() -> None:
    summary = _summary()
    summary["foundation_paper"]["paper_id"] = REFERENCE
    with pytest.raises(
        DomainPackageValidationError,
        match="summary.foundation_paper does not match",
    ):
        decode_domain_package(summary, _paper_pack())

    with pytest.raises(
        DomainPackageValidationError,
        match="does not match expected domain ID",
    ):
        decode_domain_package(
            _summary(),
            _paper_pack(),
            expected_domain_id="other-domain",
        )


def test_decoders_do_not_mutate_input_documents() -> None:
    summary = _summary()
    pack = _paper_pack()
    original_summary = deepcopy(summary)
    original_pack = deepcopy(pack)

    decode_domain_package(summary, pack)

    assert summary == original_summary
    assert pack == original_pack
