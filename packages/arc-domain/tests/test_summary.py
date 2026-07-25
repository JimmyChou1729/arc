from __future__ import annotations

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


def test_normalize_summary_output_rejects_unknown_opportunity_target():
    graph, evidence, selection = _context()
    payload = _payload()
    payload["mathematical_opportunities"]["well_defined_problems"][0]["target_domain_papers"] = [
        "arXiv:9999.99999"
    ]

    with pytest.raises(ValueError, match="domain_summary_unknown_target_domain_papers"):
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
