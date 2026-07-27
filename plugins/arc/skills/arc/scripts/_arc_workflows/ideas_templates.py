"""Materialize ARC idea plans and their proposer-reviewer batch."""

from __future__ import annotations

from arc_proposer_reviewer import BatchFailurePolicy, BatchRequest
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION

from _arc_workflows.ideas_config import IdeasConfig
from _arc_workflows.ideas_context import (
    caller_context,
    caller_context_warnings,
    general_exploration_profiles,
    workspace_input_paths,
)
from _arc_workflows.ideas_models import IdeaPlan
from _arc_workflows.ideas_template_io import read_json
from _arc_workflows.ideas_worker_templates import idea_loop_spec


def materialize_ideas(config: IdeasConfig) -> list[IdeaPlan]:
    ideas: list[IdeaPlan] = []
    general_profiles = (
        general_exploration_profiles(config)
        if not config.exploration_profiles
        else config.exploration_profiles
    )
    for variant in config.variants:
        for idea_index in range(1, config.loops_per_variant + 1):
            idea_id = f"{variant.variant_id}/idea_{idea_index:03d}"
            context, workspace_input_paths = caller_context(
                config,
                variant=variant,
                idea_id=idea_id,
                idea_index=idea_index,
                general_profiles=general_profiles,
            )
            ideas.append(
                IdeaPlan(
                    idea_id=idea_id,
                    variant_id=variant.variant_id,
                    idea_index=idea_index,
                    loop_id=f"{variant.variant_id}_idea_{idea_index:03d}",
                    variant=variant,
                    caller_context=context,
                    workspace_input_paths=workspace_input_paths,
                )
            )
    return ideas


def batch_request(config: IdeasConfig, ideas: list[IdeaPlan]) -> BatchRequest:
    return BatchRequest(
        schema_version=BATCH_SCHEMA_VERSION,
        batch_id=config.run_id,
        loops=tuple(idea_loop_spec(idea) for idea in ideas),
        failure_policy=BatchFailurePolicy.COLLECT,
    )


__all__ = [
    "batch_request",
    "caller_context_warnings",
    "materialize_ideas",
    "read_json",
    "workspace_input_paths",
]
