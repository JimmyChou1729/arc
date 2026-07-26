"""Proposer and reviewer worker construction for ARC calculations."""

from __future__ import annotations

import json
from typing import Any, Mapping

from arc_llm import ModelSelection
from arc_proposer_reviewer import WorkerSpec

from _arc_workflows.calculate_config import (
    CalculateConfig,
    ConfigError,
    _bool_default,
    _dict,
    _read_template,
)
from _arc_workflows.calculate_consensus import (
    _human_gate_pause_statuses_from_mapping,
)
from _arc_workflows.calculate_reviewer_schema import reviewer_output_schema


def proposer_worker(
    config: CalculateConfig,
    proposer_id: str,
    *,
    blind_reference: bool,
) -> WorkerSpec:
    payload = _read_template(
        config.workflow_json_dir / "calculate-proposer.template.json"
    )
    prompt = _dict(payload.get("prompt"), "calculate-proposer.template.prompt")
    template = str(prompt.get("template", "")).replace(
        "{source_policy}", _proposer_source_policy(blind_reference=blind_reference)
    )
    return WorkerSpec(
        worker_id=proposer_id,
        instructions=_worker_instructions(prompt, template),
        output_schema=_dict(
            payload.get("output_schema"),
            "calculate-proposer.template.output_schema",
        ),
        model=_worker_model(config.defaults),
    )


def reviewer_worker(
    config: CalculateConfig,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str],
    *,
    reviewer_reference_claim: Mapping[str, Any] | None = None,
    human_gate: Mapping[str, Any] | None = None,
) -> WorkerSpec:
    payload = _read_template(
        config.workflow_json_dir / "calculate-reviewer.template.json"
    )
    prompt = _dict(payload.get("prompt"), "calculate-reviewer.template.prompt")
    replacements = {
        "{active_proposer_ids}": ", ".join(active_proposer_ids),
        "{reviewer_status_instruction}": _reviewer_status_instruction(
            allow_reference_disagrees=bool(reviewer_reference_claim)
        ),
        "{reference_instruction}": _reviewer_reference_instruction(
            reviewer_reference_claim,
            active_proposer_ids=active_proposer_ids,
        ),
        "{workflow_instruction}": _reviewer_workflow_instruction(human_gate or {}),
    }
    template = str(prompt.get("template", ""))
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return WorkerSpec(
        worker_id="reviewer_001",
        instructions=_worker_instructions(prompt, template),
        output_schema=reviewer_output_schema(
            config,
            active_proposer_ids,
            selectable_proposer_ids,
            allow_reference_disagrees=bool(reviewer_reference_claim),
        ),
        model=_worker_model(config.defaults),
    )


def _proposer_source_policy(*, blind_reference: bool) -> str:
    if blind_reference:
        return (
            "This is a blind-reference check. Do not seek, infer, or use the "
            "reviewer-only reference claim or any source that would disclose it. "
            "Derive the result independently from caller_context, accepted locked "
            "outputs, and your own calculation."
        )
    return (
        "Use available web and ARC tools, including the shared paper cache, when "
        "they can resolve a relevant question. Record actual sources and results, "
        "and do not use any validation-only final formula as a derivation input."
    )


def _reviewer_reference_instruction(
    reviewer_reference_claim: Mapping[str, Any] | None,
    *,
    active_proposer_ids: list[str],
) -> str:
    if not reviewer_reference_claim:
        return ""
    claim_json = json.dumps(
        reviewer_reference_claim,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    active_ids = ", ".join(active_proposer_ids)
    return (
        "Reviewer-only blind reference check is active. Do not reveal the reference "
        "claim to proposers through the public feedback channel. Compare the final "
        f"result from every active proposer ({active_ids}) and "
        "reviewer_reference_claim. When every active blind proposer and the "
        "reference agree, set status=all_agree. When every active blind proposer "
        "agrees with the others but disagrees with the reference claim, set "
        "status=reference_disagrees and set agreed_proposer_ids to the complete "
        "active proposer id set, put the blind proposer result in accepted_result "
        "with reference_claim_status='disagrees', set "
        "agreement_assessment.accepted_by_reviewer_judgment=false, and set one or "
        "more agreement_assessment match fields false according to the mismatch. "
        "Then set workflow_action according to the workflow instruction below. If "
        "blind proposers disagree, do not accept the reference claim merely because "
        "one proposer matches it; set status=unresolved or all_disagree and request "
        f"recalculation.\n\nreviewer_reference_claim:\n{claim_json}"
    )


def _reviewer_status_instruction(*, allow_reference_disagrees: bool) -> str:
    statuses = ["all_agree", "two_agree", "all_disagree", "unresolved"]
    if allow_reference_disagrees:
        statuses.append("reference_disagrees")
    status_text = ", ".join(statuses[:-1]) + f", or {statuses[-1]}"
    return f"set status to {status_text}."


def _reviewer_workflow_instruction(human_gate: Mapping[str, Any]) -> str:
    if not _bool_default(human_gate.get("enabled", False), False):
        return (
            "workflow_action is still required. In normal mode, choose continue "
            "for all_agree and reference_disagrees when the current acceptance "
            "policy applies; for other statuses, choose retry or pause_for_human "
            "with a concise expert_question."
        )
    pause_statuses = ", ".join(
        _human_gate_pause_statuses_from_mapping(human_gate)
    )
    return (
        "Human gate is active. Statuses that trigger a stop: "
        f"{pause_statuses}. When a stop is triggered, workflow_action decides "
        "whether the main agent should ask the human expert or revise project "
        "artifacts. Use pause_for_human with requires_human=true unless all "
        "proposers' assessments and your review agree on the same work-note or "
        "plan revision. Only then use revise_plan or split_step with "
        "requires_human=false."
    )


def _worker_instructions(prompt: Mapping[str, Any], template: str) -> str:
    system = str(prompt.get("system", "")).strip()
    return f"{system}\n\n{template}".strip()


def _worker_model(defaults: Mapping[str, Any]) -> ModelSelection:
    provider = str(defaults.get("provider", "auto") or "auto")
    model_value = defaults.get("model")
    model = None if model_value is None else str(model_value)
    tier = str(defaults.get("model_tier", "high") or "high")
    try:
        return ModelSelection(  # type: ignore[arg-type]
            provider=provider,
            model=model,
            tier=tier,
        )
    except ValueError as exc:
        raise ConfigError(f"defaults model selection: {exc}") from exc


__all__ = ["proposer_worker", "reviewer_worker"]
