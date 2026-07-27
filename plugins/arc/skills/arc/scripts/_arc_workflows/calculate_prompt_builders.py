"""Proposer and reviewer worker construction for ARC calculations."""

from __future__ import annotations

import json
from typing import Any, Mapping

from arc_llm import ModelSelection
from arc_proposer_reviewer import WorkerSpec

from _arc_workflows.calculate_config import (
    CalculateConfig,
    ConfigError,
    _dict,
    _read_template,
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
    *,
    reviewer_reference_claim: Mapping[str, Any] | None = None,
) -> WorkerSpec:
    payload = _read_template(
        config.workflow_json_dir / "calculate-reviewer.template.json"
    )
    prompt = _dict(payload.get("prompt"), "calculate-reviewer.template.prompt")
    replacements = {
        "{active_proposer_ids}": ", ".join(active_proposer_ids),
        "{reference_instruction}": _reviewer_reference_instruction(
            reviewer_reference_claim,
            active_proposer_ids=active_proposer_ids,
        ),
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
        ),
        model=_worker_model(config.defaults),
    )


def _proposer_source_policy(*, blind_reference: bool) -> str:
    if blind_reference:
        return (
            "This is a blind-reference check. Do not seek, infer, or use the "
            "reviewer-only reference claim or any source that would disclose it. "
            "Derive the result independently from caller_context and your own "
            "calculation."
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
        "claim to calculators through the public feedback channel. Compare the "
        f"results from both blind calculators ({active_ids}) with the reference "
        "only after assessing both calculations independently. A reference/source "
        "match does not make a result trusted, and a mismatch does not automatically "
        "make a jointly supported result untrusted. Record every mismatch or "
        "ambiguity as an explicitly untrusted remark and choose workflow_action "
        "from your scientific judgment. Never copy the reference formula into "
        f"calculator feedback.\n\nreviewer_reference_claim:\n{claim_json}"
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
