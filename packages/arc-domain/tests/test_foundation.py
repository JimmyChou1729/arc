from __future__ import annotations

import ast
from pathlib import Path

from jsonschema import validate

from arc_domain import foundation


def _paper(paper_id: str, *, title: str, year: int, citations: int, abstract: str = "") -> dict[str, object]:
    return {
        "paper_id": paper_id,
        "title": title,
        "year": year,
        "citation_count": citations,
        "abstract": abstract,
        "authors": ["A. Author"],
    }


def test_candidate_threshold_is_a_priority_not_an_exclusion_rule() -> None:
    low = _paper("arXiv:2401.00001", title="Low", year=2024, citations=4)
    high = _paper("arXiv:2301.00001", title="High", year=2023, citations=120)
    candidates = foundation.build_candidate_records(
        seed_metadata=low,
        seed_references=[high],
        newest_citers=[],
        refs_by_citer={},
        metadata_by_id={low["paper_id"]: low, high["paper_id"]: high},
        intent="",
    )

    assert {candidate["paper_id"] for candidate in candidates} == {low["paper_id"], high["paper_id"]}
    assert "low_citation_foundation_priority" in next(
        candidate for candidate in candidates if candidate["paper_id"] == low["paper_id"]
    )["warnings"]
    selected = foundation.deterministic_foundation_selection(candidates, intent="")
    assert selected["selected_foundation"]["paper_id"] == high["paper_id"]


def test_current_prompt_uses_the_configurable_soft_citation_band() -> None:
    broad = _paper("arXiv:2301.00001", title="Broad", year=2023, citations=501)
    candidates = foundation.build_candidate_records(
        seed_metadata=broad,
        seed_references=[],
        newest_citers=[],
        refs_by_citer={},
        metadata_by_id={broad["paper_id"]: broad},
        intent="",
        heuristics=foundation.FoundationHeuristics(
            min_citation_count=50,
            max_citation_count=500,
        ),
    )
    assert "high_citation_parent_domain_risk" in candidates[0]["warnings"]
    prompt = foundation.foundation_selection_prompt(
        seed_metadata=broad,
        candidates=candidates,
        intent="",
        min_citation_count=50,
        max_citation_count=500,
        fixed_seed=True,
    )
    assert "50–500" in prompt
    assert "Fixed-seed mode" in prompt
    assert (
        "Seed paper, even when it is absent from the bounded candidate set"
        in prompt
    )
    assert (
        "best_reference_paper and parent_foundations only from the supplied candidates"
        in prompt
    )
    assert (
        "Choose selected_foundation and best_reference_paper only from the supplied candidates"
        not in prompt
    )


def test_current_high_citation_boundary_remains_inclusive() -> None:
    boundary = _paper("arXiv:2301.00001", title="Boundary", year=2023, citations=1000)
    candidates = foundation.build_candidate_records(
        seed_metadata=boundary,
        seed_references=[],
        newest_citers=[],
        refs_by_citer={},
        metadata_by_id={boundary["paper_id"]: boundary},
        intent="",
    )
    assert "high_citation_parent_domain_risk" in candidates[0]["warnings"]


def test_deterministic_selection_uses_normalized_id_ascending_independent_of_input_order() -> None:
    first = _paper("arXiv:2301.00001", title="First", year=2023, citations=300)
    second = _paper("arXiv:2301.00002", title="Second", year=2023, citations=300)
    for candidate in (first, second):
        candidate["witness_citation_overlap"] = 2
        candidate["intent_overlap"] = 0.5

    forward = foundation.deterministic_foundation_selection(
        [second, first], intent="same scope"
    )
    reversed_order = foundation.deterministic_foundation_selection(
        [first, second], intent="same scope"
    )

    assert forward["selected_foundation"]["paper_id"] == first["paper_id"]
    assert reversed_order["selected_foundation"]["paper_id"] == first["paper_id"]


def test_audit_expansion_requires_every_gate() -> None:
    incomplete = foundation.normalize_candidate_audit(
        {
            "candidate_set_sufficient": False,
            "confidence": "complete",
            "search_queries": [{"query": "arXiv:2101.00001", "reason": "", "confidence": "complete"}],
        }
    )
    assert foundation.audit_expansion_request(incomplete, "intent") is None

    high_confidence = foundation.normalize_candidate_audit(
        {
            "candidate_set_sufficient": False,
            "confidence": "high",
            "search_queries": [{"query": "canonical inflation foundation", "reason": "", "confidence": "complete"}],
        }
    )
    assert "canonical inflation foundation" in foundation.audit_expansion_request(
        high_confidence, "intent"
    )

    eligible = foundation.normalize_candidate_audit(
        {
            "candidate_set_sufficient": False,
            "confidence": "complete",
            "search_queries": [{"query": "canonical inflation foundation", "reason": "scope gap", "confidence": "complete"}],
        }
    )
    assert "canonical inflation foundation" in foundation.audit_expansion_request(eligible, "intent")


def test_expansion_adds_only_verified_ids_present_in_metadata() -> None:
    existing = _paper("arXiv:2301.00001", title="Existing", year=2023, citations=300)
    verified = _paper("arXiv:2101.00001", title="Verified", year=2021, citations=500)
    unverified = _paper("arXiv:2201.00001", title="Unverified", year=2022, citations=400)
    audit = {
        "candidate_set_sufficient": False,
        "confidence": "complete",
        "search_queries": [{"query": "missing canonical scope", "reason": "gap", "confidence": "complete"}],
    }
    expanded, report = foundation.apply_reference_inference_result(
        [existing],
        audit,
        {
            "paper_ids": [verified["paper_id"], unverified["paper_id"]],
            "verified_references": [
                {
                    "paper_id": verified["paper_id"],
                    "evidence_urls": ["https://example.test/verified"],
                    "reasoning": "verified",
                }
            ],
            "warnings": [],
            "focus_scope": "one_domain",
        },
        {verified["paper_id"]: verified, unverified["paper_id"]: unverified},
        "scope",
    )

    assert [candidate["paper_id"] for candidate in expanded] == [existing["paper_id"], verified["paper_id"]]
    assert expanded[-1]["llm_added"] is True
    assert report["added_papers"] == [verified["paper_id"]]


def test_expansion_keeps_the_verified_id_when_metadata_uses_an_alias() -> None:
    verified_id = "arXiv:2101.00001"
    metadata = _paper("INSPIRE:123", title="Verified", year=2021, citations=500)
    expanded, report = foundation.apply_reference_inference_result(
        [],
        {
            "candidate_set_sufficient": False,
            "confidence": "complete",
            "search_queries": [
                {"query": "missing canonical scope", "reason": "gap", "confidence": "complete"}
            ],
        },
        {
            "paper_ids": [verified_id],
            "verified_references": [
                {"paper_id": verified_id, "evidence_urls": [], "reasoning": "verified"}
            ],
            "warnings": [],
            "focus_scope": "one_domain",
        },
        {verified_id: metadata},
        "scope",
    )

    assert expanded[0]["paper_id"] == verified_id
    assert report["added_papers"] == [verified_id]


def test_selection_unknown_ids_repair_to_known_candidates() -> None:
    candidate = _paper("arXiv:2301.00001", title="Known", year=2023, citations=300)
    selection = foundation.normalize_foundation_selection(
        {
            "selected_foundation": {"paper_id": "arXiv:2201.00001", "title": "Unknown", "reason": "bad"},
            "best_reference_paper": {"paper_id": "arXiv:2101.00001", "title": "Unknown", "reason": "bad"},
        },
        [candidate],
    )

    assert selection["selected_foundation"]["paper_id"] == candidate["paper_id"]
    assert selection["best_reference_paper"]["paper_id"] == candidate["paper_id"]
    validate(selection, foundation.FOUNDATION_SELECTION_SCHEMA)


def test_selection_rejects_later_parent_foundation() -> None:
    selected = _paper("arXiv:2301.00001", title="Selected", year=2023, citations=300)
    earlier = _paper("arXiv:2201.00001", title="Earlier", year=2022, citations=1200)
    later = _paper("arXiv:2401.00001", title="Later", year=2024, citations=1200)
    selection = foundation.normalize_foundation_selection(
        {
            "selected_foundation": {"paper_id": selected["paper_id"], "title": "", "reason": "selected"},
            "best_reference_paper": {"paper_id": selected["paper_id"], "title": "", "reason": "read"},
            "parent_foundations": [
                {"paper_id": later["paper_id"], "title": "", "reason": "later"},
                {"paper_id": earlier["paper_id"], "title": "", "reason": "earlier"},
            ],
        },
        [selected, earlier, later],
    )

    assert [choice["paper_id"] for choice in selection["parent_foundations"]] == [earlier["paper_id"]]
    assert [choice["paper_id"] for choice in selection["rejected_candidates"]] == [later["paper_id"]]


def test_fixed_seed_validates_parents_against_authoritative_seed_metadata() -> None:
    seed_id = "arXiv:2401.00001"
    stale_seed = _paper(
        seed_id, title="Stale seed", year=2020, citations=10
    )
    authoritative_seed = _paper(
        seed_id, title="Seed", year=2024, citations=10
    )
    parent = _paper(
        "arXiv:2301.00001",
        title="Parent",
        year=2023,
        citations=500,
    )

    result = foundation.enforce_fixed_seed_foundation(
        {
            "selected_foundation": {
                "paper_id": seed_id,
                "title": "",
                "reason": "",
            },
            "best_reference_paper": {
                "paper_id": seed_id,
                "title": "",
                "reason": "",
            },
            "parent_foundations": [
                {
                    "paper_id": parent["paper_id"],
                    "title": "",
                    "reason": "explicit parent",
                }
            ],
            "rejected_candidates": [],
            "warnings": [],
        },
        [stale_seed, parent],
        seed_paper_id=seed_id,
        seed_metadata=authoritative_seed,
    )

    assert result["selected_foundation"]["title"] == "Seed"
    assert [
        item["paper_id"] for item in result["parent_foundations"]
    ] == [parent["paper_id"]]


def test_fixed_seed_does_not_promote_best_reference_to_parent() -> None:
    seed = _paper(
        "arXiv:2401.00001",
        title="Seed",
        year=2024,
        citations=10,
    )
    reference = _paper(
        "arXiv:2201.00001",
        title="Reading reference",
        year=2022,
        citations=500,
    )

    result = foundation.enforce_fixed_seed_foundation(
        {
            "selected_foundation": {
                "paper_id": seed["paper_id"],
                "title": "",
                "reason": "",
            },
            "best_reference_paper": {
                "paper_id": reference["paper_id"],
                "title": "",
                "reason": "best exposition",
            },
            "parent_foundations": [],
            "rejected_candidates": [],
            "warnings": [],
        },
        [seed, reference],
        seed_paper_id=str(seed["paper_id"]),
        seed_metadata=seed,
    )

    assert result["best_reference_paper"]["paper_id"] == (
        reference["paper_id"]
    )
    assert result["parent_foundations"] == []


def test_foundation_core_has_no_io_or_private_cross_package_imports() -> None:
    path = Path(foundation.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert "arc_paper" in imported
    assert not any(name.startswith("arc_paper.") for name in imported)
    assert not any(name in {"ac_llm", "arc_domain.paper", "arc_domain.cache"} for name in imported)
    assert not any(name in {"os", "pathlib", "subprocess", "threading"} for name in imported)
