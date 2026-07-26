"""Reviewer-schema specialization for one ARC calculation attempt."""

from __future__ import annotations

from typing import Any

from _arc_workflows.calculate_config import CalculateConfig, _read_template


def reviewer_output_schema(
    config: CalculateConfig,
    active_proposer_ids: list[str],
    selectable_proposer_ids: list[str] | None = None,
    *,
    allow_reference_disagrees: bool = False,
) -> dict[str, Any]:
    if selectable_proposer_ids is None:
        selectable_proposer_ids = active_proposer_ids
    schema = _read_template(
        config.workflow_json_dir / "calculate-reviewer-output.schema.json"
    )
    status_values = ["all_agree", "two_agree", "all_disagree", "unresolved"]
    if allow_reference_disagrees:
        status_values.append("reference_disagrees")
    consensus_properties = schema["properties"]["consensus"]["properties"]
    consensus_properties["status"]["enum"] = status_values
    for field in [
        "agreed_proposer_ids",
        "likely_wrong_proposer_ids",
        "recalculate_proposer_ids",
    ]:
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
                            "action": {"enum": ["revise_plan", "split_step"]},
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
                                "reference_claim_status": {"const": "disagrees"}
                            },
                        },
                        "agreed_proposer_ids": exact_active_agreement,
                    }
                },
            }
        )
    schema["properties"]["consensus"]["allOf"] = conditionals
    return schema


__all__ = ["reviewer_output_schema"]
