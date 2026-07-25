from __future__ import annotations

import re
from copy import deepcopy

import pytest

from arc_domain import summary


PAPER_ID = "arXiv:2401.00001"
REFERENCE_ID = "arXiv:2401.00002"


def _context() -> tuple[dict, dict, dict]:
    return (
        {
            "foundation_paper": PAPER_ID,
            "nodes": [
                {"paper_id": PAPER_ID, "role": "foundation", "title": "Foundation"},
                {"paper_id": REFERENCE_ID, "role": "domain_paper", "title": "Reference"},
            ],
            "edges": [],
        },
        {"papers": [{"paper_id": REFERENCE_ID, "title": "Reference"}], "warnings": []},
        {
            "selected_foundation": {"paper_id": PAPER_ID, "title": "Foundation", "reason": "anchor"},
            "best_reference_paper": {"paper_id": REFERENCE_ID, "title": "Reference", "reason": "entry point"},
        },
    )


def _payload() -> dict:
    return {
        "schema_version": "arc.domain_summary.v5",
        "domain_title": "Example domain",
        "brief_introduction": "A compact introduction.",
        "task_focus": {
            "user_intent": "Find a controlled calculation.",
            "research_scope": "The supplied domain papers.",
            "priority_rules": ["Satisfy the user intent first."],
        },
        "foundation_paper": {"paper_id": PAPER_ID, "title": "Foundation", "reason": "anchor"},
        "best_reference_paper": {"paper_id": REFERENCE_ID, "title": "Reference", "reason": "entry point"},
        "methodology": [{"claim": "Use the controlled limit.", "papers": [REFERENCE_ID]}],
        "mathematical_opportunities": {
            "well_defined_problems": [
                {
                    "problem": "Evaluate the first coefficient.",
                    "importance": "It distinguishes two mechanisms.",
                    "mathematical_object": "A first expansion coefficient.",
                    "assumptions_and_regime": ["controlled limit"],
                    "success_criterion": "The coefficient agrees with the Ward identity.",
                    "available_systematic_methods": [
                        {
                            "method": "Recursion.",
                            "origin": "in_domain",
                            "source_area": "The supplied papers.",
                            "required_adaptation": "Specialize the boundary data.",
                            "applicability_conditions": ["controlled limit"],
                            "validation_checks": ["Ward identity"],
                        }
                    ],
                    "bounded_first_calculation": "Compute the leading coefficient.",
                    "feasibility": {
                        "ready_inputs": ["boundary data"],
                        "blocking_unknowns": ["normalization"],
                        "kill_criterion": "Stop if the identity is violated.",
                    },
                    "target_domain_papers": [REFERENCE_ID],
                    "evidence_status": "source_explicit",
                }
            ]
        },
        "known_solved_cases": [],
        "open_axes_for_new_work": [],
        "warnings": [],
    }


def _payload_with_all_paper_paths() -> dict:
    payload = _payload()
    payload["known_solved_cases"] = [
        {
            "solved_case": "A benchmark limit.",
            "why_it_is_solved": "The coefficient is known.",
            "transferable_form": "Use the same normalization check.",
            "forbidden_reuse": "Do not claim the full problem is solved.",
            "valid_new_axes": ["Change the boundary condition."],
            "papers": [REFERENCE_ID],
        }
    ]
    payload["open_axes_for_new_work"] = [
        {
            "axis": "Alternative boundary data.",
            "guidance": "Keep the controlled limit.",
            "example_variations": ["A second boundary condition."],
            "papers": [REFERENCE_ID],
        }
    ]
    return payload


def test_normalize_summary_output_returns_only_closed_v5_payload():
    graph, evidence, selection = _context()
    payload = _payload()

    normalized = summary.normalize_summary_output(
        payload,
        graph=graph,
        evidence=evidence,
        selection=selection,
        intent="Find a controlled calculation.",
    )

    assert normalized == payload
    assert normalized is not payload

    malformed = deepcopy(payload)
    malformed["unexpected"] = True
    with pytest.raises(ValueError, match="domain_summary_schema_invalid"):
        summary.normalize_summary_output(
            malformed,
            graph=graph,
            evidence=evidence,
            selection=selection,
            intent="Find a controlled calculation.",
        )


def test_normalize_summary_output_binds_a_copy_to_authoritative_intent():
    graph, evidence, selection = _context()
    payload = _payload()
    payload["task_focus"]["user_intent"] = "model-altered intent"

    normalized = summary.normalize_summary_output(
        payload,
        graph=graph,
        evidence=evidence,
        selection=selection,
        intent="用户的原始研究意图",
    )

    assert normalized["task_focus"]["user_intent"] == "用户的原始研究意图"
    assert payload["task_focus"]["user_intent"] == "model-altered intent"


def test_normalize_summary_output_preserves_empty_authoritative_intent():
    graph, evidence, selection = _context()
    payload = _payload()

    normalized = summary.normalize_summary_output(
        payload,
        graph=graph,
        evidence=evidence,
        selection=selection,
        intent="",
    )

    assert normalized["task_focus"]["user_intent"] == ""
    assert payload["task_focus"]["user_intent"] == "Find a controlled calculation."


def test_normalize_summary_output_accepts_equivalent_ids_across_all_paper_paths():
    graph, evidence, selection = _context()
    graph["nodes"][1]["identifiers"] = {
        "arxiv": "2401.00002v3",
        "doi": "10.1234/REFERENCE",
        "inspire_recid": "12345",
    }
    selection["best_reference_paper"]["paper_id"] = "inspire:12345"
    payload = _payload_with_all_paper_paths()
    payload["foundation_paper"]["paper_id"] = (
        "https://arxiv.org/abs/2401.00001v4"
    )
    payload["best_reference_paper"]["paper_id"] = (
        "https://doi.org/10.1234/REFERENCE"
    )
    payload["methodology"][0]["papers"] = ["doi:10.1234/reference"]
    payload["mathematical_opportunities"]["well_defined_problems"][0][
        "target_domain_papers"
    ] = ["recid:12345"]
    payload["known_solved_cases"][0]["papers"] = ["arXiv:2401.00002v8"]
    payload["open_axes_for_new_work"][0]["papers"] = [
        "https://doi.org/10.1234/reference"
    ]

    normalized = summary.normalize_summary_output(
        payload,
        graph=graph,
        evidence=evidence,
        selection=selection,
        intent="Find a controlled calculation.",
    )

    assert normalized == payload


@pytest.mark.parametrize(
    ("container_path", "expected_path"),
    [
        (("methodology", 0, "papers"), "$.methodology[0].papers[0]"),
        (
            (
                "mathematical_opportunities",
                "well_defined_problems",
                0,
                "target_domain_papers",
            ),
            "$.mathematical_opportunities.well_defined_problems"
            "[0].target_domain_papers[0]",
        ),
        (
            ("known_solved_cases", 0, "papers"),
            "$.known_solved_cases[0].papers[0]",
        ),
        (
            ("open_axes_for_new_work", 0, "papers"),
            "$.open_axes_for_new_work[0].papers[0]",
        ),
    ],
)
def test_normalize_summary_output_rejects_unknown_ids_at_every_list_path(
    container_path: tuple[object, ...],
    expected_path: str,
):
    graph, evidence, selection = _context()
    payload = _payload_with_all_paper_paths()
    container = payload
    for key in container_path:
        container = container[key]
    container[0] = "arXiv:9999.99999"

    with pytest.raises(
        ValueError,
        match=(
            "domain_summary_provenance_invalid: "
            + re.escape(expected_path)
        ),
    ):
        summary.normalize_summary_output(
            payload,
            graph=graph,
            evidence=evidence,
            selection=selection,
            intent="Find a controlled calculation.",
        )


@pytest.mark.parametrize(
    ("summary_key", "wrong_id", "expected_selection"),
    [
        ("foundation_paper", REFERENCE_ID, "selected_foundation"),
        ("best_reference_paper", PAPER_ID, "best_reference_paper"),
    ],
)
def test_normalize_summary_output_requires_authoritative_selected_papers(
    summary_key: str,
    wrong_id: str,
    expected_selection: str,
):
    graph, evidence, selection = _context()
    payload = _payload()
    payload[summary_key]["paper_id"] = wrong_id

    with pytest.raises(
        ValueError,
        match=(
            "domain_summary_provenance_invalid: "
            + re.escape(f"$.{summary_key}.paper_id")
            + f".*{expected_selection}"
        ),
    ):
        summary.normalize_summary_output(
            payload,
            graph=graph,
            evidence=evidence,
            selection=selection,
            intent="Find a controlled calculation.",
        )


@pytest.mark.parametrize("summary_key", ["foundation_paper", "best_reference_paper"])
def test_normalize_summary_output_requires_authoritative_selected_titles(
    summary_key: str,
):
    graph, evidence, selection = _context()
    payload = _payload()
    payload[summary_key]["title"] = "Model-invented title"

    with pytest.raises(
        ValueError,
        match=(
            "domain_summary_provenance_invalid: "
            + re.escape(f"$.{summary_key}.title")
        ),
    ):
        summary.normalize_summary_output(
            payload,
            graph=graph,
            evidence=evidence,
            selection=selection,
            intent="Find a controlled calculation.",
        )


def test_summary_prompt_compacts_large_evidence():
    graph, evidence, selection = _context()
    repeated = "source sentence " * 10_000
    evidence["papers"] = [
        {
            "paper_id": f"arXiv:2401.{index:05d}",
            "role": "domain_paper",
            "title": f"Paper {index}",
            "abstract": repeated,
            "conclusion": {"text": repeated},
            "warnings": [repeated],
        }
        for index in range(200)
    ]
    graph["nodes"] = [
        {"paper_id": item["paper_id"], "role": item["role"], "title": item["title"]}
        for item in evidence["papers"]
    ]

    prompt = summary.summary_prompt(
        graph,
        evidence,
        selection,
        intent="用户的原始研究意图",
    )

    assert len(prompt) <= summary.SUMMARY_PROMPT_CHAR_LIMIT
    assert "[truncated]" in prompt
    assert '"paper_detail_limit": 150' in prompt
    assert '"omitted_paper_count": 50' in prompt
    assert '"user_intent": "用户的原始研究意图"' in prompt
    assert "foundation_selection.intent" not in prompt


def test_summary_prompt_fallback_compaction_preserves_authoritative_intent(
    monkeypatch: pytest.MonkeyPatch,
):
    graph, evidence, selection = _context()
    repeated = "source sentence " * 10_000
    evidence["papers"] = [
        {
            "paper_id": f"arXiv:2401.{index:05d}",
            "role": "domain_paper",
            "title": f"Paper {index}",
            "abstract": repeated,
            "conclusion": {"text": repeated},
            "warnings": [repeated],
        }
        for index in range(200)
    ]
    graph["nodes"] = [
        {"paper_id": item["paper_id"], "role": item["role"], "title": item["title"]}
        for item in evidence["papers"]
    ]
    monkeypatch.setattr(summary, "SUMMARY_PROMPT_CHAR_LIMIT", 300_000)

    prompt = summary.summary_prompt(
        graph,
        evidence,
        selection,
        intent="用户的原始研究意图",
    )

    assert len(prompt) <= 300_000
    assert '"paper_detail_limit": 80' in prompt
    assert '"omitted_paper_count": 120' in prompt
    assert '"user_intent": "用户的原始研究意图"' in prompt


def test_render_summary_markdown_renders_opportunity_without_warnings():
    markdown = summary.render_summary_markdown(_payload())

    assert markdown.startswith("# Example domain\n")
    assert "## Mathematical Opportunities" in markdown
    assert "Evaluate the first coefficient." in markdown
    assert "Recursion. (in domain)" in markdown
    assert "## Warnings" not in markdown
