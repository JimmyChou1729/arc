"""Reviewer-schema specialization for one ARC calculation attempt."""

from __future__ import annotations

from typing import Any

from _arc_workflows.calculate_config import (
    CALCULATOR_IDS,
    CalculateConfig,
    _read_template,
)


def reviewer_output_schema(
    config: CalculateConfig,
    active_proposer_ids: list[str],
) -> dict[str, Any]:
    """Bind the closed referee payload schema to the two current calculators."""

    if tuple(active_proposer_ids) != CALCULATOR_IDS:
        raise ValueError(
            "calculate attempts require exactly proposer_001 and proposer_002"
        )
    schema = _read_template(
        config.workflow_json_dir / "calculate-reviewer-output.schema.json"
    )
    properties = schema["properties"]
    assessment = properties["calculator_assessments"]["items"]["properties"]
    assessment["proposer_id"]["enum"] = list(active_proposer_ids)

    trusted = properties["trusted_results"]["items"]["properties"]
    trusted["supporting_proposer_ids"]["items"]["enum"] = list(
        active_proposer_ids
    )
    trusted["selected_proposer_id"]["enum"] = list(active_proposer_ids)

    related_ids = properties["remarks"]["items"]["properties"][
        "related_proposer_ids"
    ]
    related_ids["items"]["enum"] = list(active_proposer_ids)
    related_ids["maxItems"] = len(active_proposer_ids)
    return schema


__all__ = ["reviewer_output_schema"]
