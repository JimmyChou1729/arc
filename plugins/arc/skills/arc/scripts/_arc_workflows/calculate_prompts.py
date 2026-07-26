"""Prompt, worker, and materialized batch construction for ARC calculations."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

from arc_llm import ModelSelection
from arc_proposer_reviewer import (
    BatchFailurePolicy,
    BatchRequest,
    LoopSpec,
    ProposerFailurePolicy,
    WorkerSpec,
)
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION

from _arc_workflows.calculate_config import (
    CalculateConfig,
    CalculateStep,
    ConfigError,
    _bool_default,
    _dict,
    _integrity_reference,
    _read_template,
)
from _arc_workflows.calculate_consensus import (
    _human_gate_pause_statuses_from_mapping,
)


CALLER_ALLOWED_CONTEXT_OMIT_KEYS = {
    "cache_path",
    "path",
    "reviewer_reference_claim",
    "source_path",
    "source_commands",
    "shell_commands",
    "mcp_call_instructions",
    "cli_invocations",
}


def _attempt_batch_request(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    attempt_number: int,
    active_proposer_ids: list[str],
    locked_outputs: dict[str, Any],
    retry_feedback: list[dict[str, Any]],
    accepted_step_outputs: Mapping[str, Any],
) -> BatchRequest:
    """Build one deterministic, independent public proposer-reviewer batch."""

    attempt_id = _attempt_id(step.step_id, attempt_number)
    selectable_proposer_ids = list(
        dict.fromkeys([*active_proposer_ids, *[proposer_id for proposer_id in locked_outputs]])
    )
    caller_context = _caller_context(
        config,
        step,
        attempt_number=attempt_number,
        active_proposer_ids=active_proposer_ids,
        locked_outputs=locked_outputs,
        retry_feedback=retry_feedback,
        accepted_step_outputs=accepted_step_outputs,
    )
    return BatchRequest(
        schema_version=BATCH_SCHEMA_VERSION,
        batch_id=_batch_run_id(config.run_id, attempt_id),
        loops=(
            LoopSpec(
                loop_id=attempt_id,
                context=caller_context,
                proposers=tuple(
                    _proposer_worker(
                        config,
                        proposer_id,
                        runtime=_proposer_runtime(config, step),
                    )
                    for proposer_id in active_proposer_ids
                ),
                reviewer=_reviewer_worker(
                    config,
                    active_proposer_ids,
                    selectable_proposer_ids,
                    reviewer_reference_claim=step.reviewer_reference_claim,
                    human_gate=config.human_gate,
                ),
                max_rounds=1,
                allow_early_stop=False,
                on_proposer_failure=ProposerFailurePolicy.FAIL_LOOP,
            ),
        ),
        failure_policy=BatchFailurePolicy.COLLECT,
    )


def _caller_context(
    config: CalculateConfig,
    step: CalculateStep,
    *,
    attempt_number: int,
    active_proposer_ids: list[str],
    locked_outputs: dict[str, Any],
    retry_feedback: list[dict[str, Any]],
    accepted_step_outputs: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "step_kind": step.kind,
        "step_prompt": step.prompt,
        "allowed_context": _sanitize_caller_allowed_context(step.allowed_context),
        "attempt_number": attempt_number,
        "active_proposer_ids": active_proposer_ids,
        "locked_outputs": copy.deepcopy(locked_outputs),
        "retry_feedback": copy.deepcopy(retry_feedback),
        "accepted_prior_step_outputs": copy.deepcopy(dict(accepted_step_outputs)),
        "max_recalculations": config.max_recalculations,
        "integrity_reference": _integrity_reference(config.defaults.get("integrity_reference_path")),
        "consensus_instruction": "Work only on this calculation step. Respect accepted_prior_step_outputs and locked_outputs as already accepted unless explicitly asked to check them.",
    }


def _proposer_worker(
    config: CalculateConfig, proposer_id: str, *, runtime: Mapping[str, Any]
) -> WorkerSpec:
    payload = _read_template(config.workflow_json_dir / "calculate-proposer.template.json")
    prompt = _dict(payload.get("prompt"), "calculate-proposer.template.prompt")
    template = str(prompt.get("template", "")).replace(
        "{source_policy}", _proposer_source_policy(runtime)
    )
    return WorkerSpec(
        worker_id=proposer_id,
        instructions=_worker_instructions(prompt, template),
        output_schema=_dict(payload.get("output_schema"), "calculate-proposer.template.output_schema"),
        model=_worker_model(config.defaults),
    )


def _reviewer_worker(
    config: CalculateConfig,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str],
    *,
    reviewer_reference_claim: Mapping[str, Any] | None = None,
    human_gate: Mapping[str, Any] | None = None,
) -> WorkerSpec:
    payload = _read_template(config.workflow_json_dir / "calculate-reviewer.template.json")
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
    runtime = {
        "allow_internet": False if reviewer_reference_claim else _bool_default(
            config.defaults.get("reviewer_allow_internet", False), False
        ),
        "inherit_host_tools": False,
        "evidence_access": "none" if reviewer_reference_claim else "context_only",
    }
    return WorkerSpec(
        worker_id="reviewer_001",
        instructions=_worker_instructions(prompt, template),
        output_schema=_reviewer_output_schema(
            config,
            active_proposer_ids,
            selectable_proposer_ids,
            allow_reference_disagrees=bool(reviewer_reference_claim),
        ),
        model=_worker_model(config.defaults),
    )


def _proposer_runtime(config: CalculateConfig, step: CalculateStep) -> dict[str, Any]:
    if step.reviewer_reference_claim:
        runtime = {
            "allow_internet": False,
            "codex_sandbox": "read-only",
            "evidence_access": "none",
            "inherit_host_tools": False,
        }
    else:
        runtime = {
            "allow_internet": True,
            "codex_sandbox": "read-only",
            "evidence_access": "context_only",
            "inherit_host_tools": False,
        }
    runtime.update(_dict(config.defaults.get("proposer_runtime", {}), "defaults.proposer_runtime"))
    runtime.update(step.proposer_runtime)
    runtime["evidence_access"] = str(runtime.get("evidence_access", "context_only"))
    runtime["inherit_host_tools"] = False
    if step.reviewer_reference_claim:
        # Blind validation is a hard isolation boundary; ordinary runtime
        # overrides cannot expose paper or inherited host tools here.
        runtime["evidence_access"] = "none"
        runtime["inherit_host_tools"] = False
        runtime["allow_internet"] = False
    return runtime


def _proposer_source_policy(runtime: Mapping[str, Any]) -> str:
    allow_internet = _bool_default(runtime.get("allow_internet", False), False)
    evidence_access = str(runtime.get("evidence_access", "context_only"))
    if evidence_access == "none" and not allow_internet:
        return (
            "Do not use internet search. Do not invoke ARC CLIs, shell commands, or MCP tools. "
            "Do not read paper source sections, arXiv pages, INSPIRE pages, "
            "cached paper text, or any external source. Use only the supplied "
            "caller_context, accepted locked_outputs, and your own local algebra. "
            "Do not use validation-only final formulas as derivation inputs."
        )
    parts = []
    parts.append(
        "Use only evidence present in caller_context and any explicitly supplied public operation result; "
        "do not invoke ARC CLIs, shell commands, MCP tools, or nested LLM commands."
    )
    if allow_internet:
        parts.append("Internet search is allowed only for source discovery or uncached paper access.")
    else:
        parts.append("Do not use internet search.")
    parts.append("Cite any supplied public paper-operation result or internet source you use.")
    parts.append("Do not use validation-only final formulas as derivation inputs.")
    return " ".join(parts)


def _reviewer_reference_instruction(
    reviewer_reference_claim: Mapping[str, Any] | None,
    *,
    active_proposer_ids: list[str],
) -> str:
    if not reviewer_reference_claim:
        return ""
    claim_json = json.dumps(reviewer_reference_claim, indent=2, ensure_ascii=False, sort_keys=True)
    active_ids = ", ".join(active_proposer_ids)
    return (
        "Reviewer-only blind reference check is active. Do not reveal the reference claim "
        "to proposers through the public feedback channel. Compare the final result from "
        f"every active proposer ({active_ids}) and reviewer_reference_claim. "
        "When every active blind proposer and the reference agree, set status=all_agree. "
        "When every active blind proposer agrees with the others but disagrees with the "
        "reference claim, set status=reference_disagrees and set agreed_proposer_ids "
        "to the complete active proposer id set, "
        "put the blind proposer result in accepted_result with reference_claim_status='disagrees', "
        "set agreement_assessment.accepted_by_reviewer_judgment=false, and set "
        "one or more agreement_assessment match fields false according to the mismatch. "
        "Then set workflow_action according to the workflow instruction below. "
        "If blind proposers disagree, do not accept the reference claim merely because one proposer matches it; "
        "set status=unresolved or all_disagree and request recalculation.\n\n"
        f"reviewer_reference_claim:\n{claim_json}"
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
            "workflow_action is still required. In normal mode, choose continue for "
            "all_agree and reference_disagrees when the current acceptance policy applies; for other "
            "statuses, choose retry or pause_for_human with a concise expert_question."
        )
    pause_statuses = ", ".join(_human_gate_pause_statuses_from_mapping(human_gate))
    return (
        "Human gate is active. Statuses that trigger a stop: "
        f"{pause_statuses}. When a stop is triggered, workflow_action decides whether "
        "the main agent should ask the human expert or revise project artifacts. Use "
        "pause_for_human with requires_human=true unless all proposers' assessments and "
        "your review agree on the same work-note or plan revision. Only then use "
        "revise_plan or split_step with requires_human=false."
    )


def _reviewer_output_schema(
    config: CalculateConfig,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str] | None = None,
    *,
    allow_reference_disagrees: bool = False,
) -> dict[str, Any]:
    if selectable_proposer_ids is None:
        selectable_proposer_ids = active_proposer_ids
    schema = _read_template(config.workflow_json_dir / "calculate-reviewer-output.schema.json")
    status_values = ["all_agree", "two_agree", "all_disagree", "unresolved"]
    if allow_reference_disagrees:
        status_values.append("reference_disagrees")
    consensus_properties = schema["properties"]["consensus"]["properties"]
    consensus_properties["status"]["enum"] = status_values
    for field in ["agreed_proposer_ids", "likely_wrong_proposer_ids", "recalculate_proposer_ids"]:
        consensus_properties[field]["items"]["type"] = "string"
        consensus_properties[field]["items"]["enum"] = active_proposer_ids
        consensus_properties[field]["uniqueItems"] = True
    consensus_properties["best_written_proposer_id"]["anyOf"] = [
        {"type": "string", "enum": selectable_proposer_ids},
        {"type": "null"},
    ]
    accepted_properties = consensus_properties["accepted_result"]["properties"]
    for field in ["selected_proposer_id", "source_proposer_id"]:
        accepted_properties[field]["enum"] = selectable_proposer_ids
    exact_active_agreement = {
        "type": "array",
        "items": {"type": "string", "enum": active_proposer_ids},
        "uniqueItems": True,
        "minItems": len(active_proposer_ids),
        "maxItems": len(active_proposer_ids),
    }
    conditionals = [
        {
            "if": {"properties": {"status": {"const": "all_agree"}}},
            "then": {
                "properties": {
                    "accepted_result": {
                        "type": "object",
                        "properties": {
                            "reference_claim_status": {
                                "const": (
                                    "agrees"
                                    if allow_reference_disagrees
                                    else "not_applicable"
                                )
                            }
                        },
                    },
                    "agreed_proposer_ids": exact_active_agreement,
                }
            },
        },
        {
            "if": {
                "properties": {
                    "status": {
                        "enum": ["two_agree", "all_disagree", "unresolved"]
                    }
                }
            },
            "then": {"properties": {"accepted_result": {"type": "null"}}},
        },
        {
            "if": {
                "properties": {
                    "workflow_action": {
                        "properties": {
                            "action": {
                                "enum": ["revise_plan", "split_step"]
                            },
                            "requires_human": {"const": False},
                        },
                        "required": ["action", "requires_human"],
                    }
                }
            },
            "then": {
                "properties": {
                    "workflow_action": {
                        "properties": {
                            "proposed_revision": {
                                "type": "string",
                                "minLength": 1,
                                "pattern": "\\S",
                            }
                        }
                    }
                }
            },
        },
    ]
    if allow_reference_disagrees:
        conditionals.append(
            {
                "if": {
                    "properties": {
                        "status": {"const": "reference_disagrees"}
                    }
                },
                "then": {
                    "properties": {
                        "accepted_result": {
                            "type": "object",
                            "properties": {
                                "reference_claim_status": {
                                    "const": "disagrees"
                                }
                            },
                        },
                        "agreed_proposer_ids": exact_active_agreement,
                    }
                },
            }
        )
    schema["properties"]["consensus"]["allOf"] = conditionals
    return schema


def _sanitize_caller_allowed_context(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_caller_allowed_context(item)
            for key, item in value.items()
            if str(key) not in CALLER_ALLOWED_CONTEXT_OMIT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_caller_allowed_context(item) for item in value]
    return copy.deepcopy(value)


def _worker_instructions(prompt: Mapping[str, Any], template: str) -> str:
    system = str(prompt.get("system", "")).strip()
    return f"{system}\n\n{template}".strip()


def _worker_model(defaults: Mapping[str, Any]) -> ModelSelection:
    provider = str(defaults.get("provider", "auto") or "auto")
    model_value = defaults.get("model")
    model = None if model_value is None else str(model_value)
    tier = str(defaults.get("model_tier", "high") or "high")
    try:
        return ModelSelection(provider=provider, model=model, tier=tier)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ConfigError(f"defaults model selection: {exc}") from exc


def _attempt_id(step_id: str, attempt_number: int) -> str:
    return _bounded_id(f"{step_id}_attempt_{attempt_number:03d}")


def _batch_run_id(config_run_id: str, attempt_id: str) -> str:
    return _bounded_id(f"calculate_{config_run_id}_{attempt_id}")


def _bounded_id(candidate: str) -> str:
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
    return f"{candidate[:115]}-{digest}"


__all__ = [
    "_attempt_batch_request",
    "_attempt_id",
    "_batch_run_id",
    "_caller_context",
    "_proposer_runtime",
    "_proposer_worker",
    "_reviewer_output_schema",
    "_reviewer_worker",
]
