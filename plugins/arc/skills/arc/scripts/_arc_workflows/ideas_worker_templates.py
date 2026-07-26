"""Worker and loop template materialization for ARC ideas."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from arc_llm import ModelSelection
from arc_proposer_reviewer import (
    LoopSpec,
    ProposerFailurePolicy,
    WorkerSpec,
)

from _arc_workflows.ideas_config import ConfigError, VariantConfig
from _arc_workflows.ideas_marking import load_marking_scheme, marks_schema
from _arc_workflows.ideas_models import IdeaPlan
from _arc_workflows.ideas_template_io import (
    merged_worker_payload,
    positive_template_int,
    read_json,
    required_text,
)


def idea_loop_spec(idea: IdeaPlan) -> LoopSpec:
    template = read_json(idea.variant.loop_template)
    max_rounds = positive_template_int(template.get("max_rounds"), "max_rounds")
    early_stop = template.get("early_stop", {})
    if not isinstance(early_stop, Mapping):
        raise ConfigError(
            f"{idea.variant.loop_template}.early_stop must be an object"
        )
    enabled = early_stop.get("enabled", False)
    if type(enabled) is not bool:
        raise ConfigError(
            f"{idea.variant.loop_template}.early_stop.enabled must be a boolean"
        )
    return LoopSpec(
        loop_id=idea.loop_id,
        context=idea.caller_context,
        proposers=(
            worker_spec(
                merged_worker_payload(
                    read_json(idea.variant.proposer_template),
                    idea.variant.proposer_overrides,
                ),
                source=idea.variant.proposer_template,
            ),
        ),
        reviewer=worker_spec(
            reviewer_worker_payload(idea.variant),
            source=idea.variant.reviewer_template,
        ),
        max_rounds=max_rounds,
        allow_early_stop=enabled,
        on_proposer_failure=ProposerFailurePolicy.FAIL_LOOP,
    )


def worker_spec(
    payload: Mapping[str, Any],
    *,
    source: Path,
) -> WorkerSpec:
    worker_id = required_text(payload, "id", source)
    prompt = payload.get("prompt")
    if not isinstance(prompt, Mapping):
        raise ConfigError(f"{source}.prompt must be an object")
    system = required_text(prompt, "system", source)
    template = required_text(prompt, "template", source)
    output_schema = payload.get("output_schema")
    if not isinstance(output_schema, Mapping):
        raise ConfigError(f"{source}.output_schema must be an object")
    tier = str(payload.get("model_tier", "medium") or "medium").strip().lower()
    provider = str(payload.get("provider", "auto") or "auto").strip()
    model_value = payload.get("model")
    model = None if model_value is None else str(model_value).strip()
    if model == "":
        model = None
    try:
        selection = ModelSelection(
            provider=provider,
            model=model,
            tier=tier,  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise ConfigError(f"{source}.model configuration is invalid: {exc}") from exc
    return WorkerSpec(
        worker_id=worker_id,
        instructions=f"{system}\n\n{template}",
        output_schema=copy.deepcopy(dict(output_schema)),
        model=selection,
    )


def reviewer_worker_payload(variant: VariantConfig) -> dict[str, Any]:
    payload = read_json(variant.reviewer_template)
    payload["output_schema"] = reviewer_payload_schema(variant)
    return payload


def reviewer_payload_schema(variant: VariantConfig) -> dict[str, Any]:
    schema = read_json(variant.reviewer_output_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or "marks" not in properties:
        raise ConfigError(
            f"{variant.reviewer_output_schema} must be a direct reviewer payload schema"
        )
    schema["properties"]["marks"] = marks_schema(
        load_marking_scheme(variant.marking_scheme)
    )
    return schema
