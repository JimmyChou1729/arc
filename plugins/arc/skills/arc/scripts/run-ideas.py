from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from arc_domain.summary import mathematical_opportunities_validation_error
from arc_jobs import RunEngine, RunRepository, RunSnapshot, RunSpec
from arc_llm import CapabilityPolicy, LLMTaskService, ModelSelection
from arc_proposer_reviewer import (
    BatchFailurePolicy,
    BatchInspection,
    BatchProjectionIntegrityError,
    BatchRequest,
    BatchTrace,
    ExecutionOptions,
    LoopSpec,
    ProposerFailurePolicy,
    ProposerReviewerHandler,
    ProposerReviewerService,
    WorkerSpec,
    inspect_batch,
    read_batch_round,
    read_batch_trace,
)
from arc_proposer_reviewer.models import BATCH_SCHEMA_VERSION
from arc_proposer_reviewer.protocol import encode_batch_request

from _arc_workflows.evidence import (
    ArcPaperEvidenceResolver,
    EVIDENCE_OPERATION_NAMES,
    evidence_operation_contracts,
)
from _arc_workflows.ideas_config import ConfigError, IdeasConfig, VariantConfig, load_ideas_config
from _arc_workflows.ideas_marking import (
    load_marking_scheme,
    marking_scheme_for_context,
    marks_schema,
    normalized_marks,
)
from _arc_workflows.workflow_io import (
    NonObjectJsonError,
    read_json_object,
    require_strict_int,
)


MODEL_TIER_RANKS = {"low": 1, "medium": 2, "high": 3, "xhigh": 4}
DEFAULT_CROSS_DOMAIN_PROFILES = [
    {
        "profile_id": "forward_transfer",
        "mission": (
            "Treat the first domain card as the source and choose the strongest distinct target from the remaining "
            "cards. Transfer one concrete, mature source method, mechanism, formal structure, or constraint."
        ),
    },
    {
        "profile_id": "reverse_transfer",
        "mission": (
            "Treat the first domain card as the target and choose the strongest distinct source from the remaining "
            "cards. Find a reverse transfer that creates a substantive new target result."
        ),
    },
    {
        "profile_id": "method_transfer",
        "mission": (
            "Compare both directions and choose the strongest method or formalism transfer. State the exact "
            "translation dictionary and the target calculation it newly enables."
        ),
    },
    {
        "profile_id": "observable_or_constraint_transfer",
        "mission": (
            "Compare both directions and transfer an observable, consistency condition, validation strategy, "
            "or constraint that yields a new discriminating target-domain result."
        ),
    },
    {
        "profile_id": "high_upside_wildcard",
        "mission": (
            "Pursue the highest-upside feasible bridge, including a challenge to a standard target assumption. "
            "Require explicit compatibility checks, a bounded first calculation, and a kill criterion."
        ),
    },
]
ARC_PAPER_EVIDENCE_OPERATIONS = [
    {"operation": operation}
    for operation in EVIDENCE_OPERATION_NAMES
]


class BatchExecutor(Protocol):
    """Typed seam for durable batch execution in workflow tests and hosts."""

    def __call__(
        self,
        repository: RunRepository,
        spec: RunSpec,
        handler: ProposerReviewerHandler,
    ) -> RunSnapshot: ...


@dataclass(frozen=True)
class IdeaPlan:
    idea_id: str
    variant_id: str
    idea_index: int
    loop_id: str
    variant: VariantConfig
    caller_context: dict[str, Any]


def run_ideas(
    config: IdeasConfig | Mapping[str, Any],
    *,
    executor: BatchExecutor | None = None,
    llm_service: LLMTaskService | None = None,
    evidence_resolver: ArcPaperEvidenceResolver | None = None,
    base_env: Mapping[str, str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Materialize and run one typed proposer-reviewer ideas batch.

    The workflow owns caller context and template translation only.  Durable
    lifecycle and committed output observation come exclusively from the public
    proposer-reviewer and arc-jobs APIs.
    """

    ideas_config = config if isinstance(config, IdeasConfig) else load_ideas_config(config)
    ideas = _materialize_ideas(ideas_config)
    request = _batch_request(ideas_config, ideas)
    max_concurrent = _max_concurrent_loops(len(ideas))
    warnings = [
        _concurrency_warning(
            ideas_config,
            len(ideas),
            max_concurrent=max_concurrent,
            request=request,
        ),
        *ideas_config.routing_warnings,
        *_model_tier_warnings(request),
        *_caller_context_warnings(ideas),
    ]
    repository = RunRepository(ideas_config.run_dir)

    if dry_run:
        return _dry_run_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
        )

    if stop_check is not None and stop_check():
        return _not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="stopped",
        )

    resolver = evidence_resolver or ArcPaperEvidenceResolver()
    handler = ProposerReviewerHandler(
        ProposerReviewerService(llm_service or LLMTaskService()),
        options=ExecutionOptions(
            max_concurrent_loops=max_concurrent,
            max_concurrent_workers=1,
            interaction_resolver=resolver,
        ),
    )
    spec = RunSpec(
        ideas_config.run_id,
        handler.name,
        encode_batch_request(request),
    )
    effective_progress = _combined_progress_callback(
        progress_callback,
        _progress_sidechannel_callback(base_env),
    )
    _emit_progress(
        effective_progress,
        {"event": "ideas_batch_started", "run_id": ideas_config.run_id},
    )
    try:
        snapshot = (executor or _execute_batch)(repository, spec, handler)
    except Exception as exc:
        warnings.append(f"ideas_batch_execution_failed: {type(exc).__name__}")
        return _not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="failed",
        )

    try:
        inspection = inspect_batch(repository, snapshot.run_id)
    except Exception as exc:
        warnings.append(f"ideas_batch_inspection_failed: {type(exc).__name__}")
        return _not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status=snapshot.status.value,
        )

    try:
        trace = read_batch_trace(repository, snapshot.run_id)
    except BatchProjectionIntegrityError:
        # Inspection remains useful for lifecycle reporting.  Ranking and scores
        # fail closed rather than inferring output from files or activity.
        trace = None
        warnings.append("committed_trace_unavailable: committed artifacts could not be verified")
    _emit_progress(
        effective_progress,
        {
            "event": "ideas_batch_finished",
            "run_id": ideas_config.run_id,
            "status": inspection.run_lifecycle,
        },
    )
    return _observed_result(
        ideas_config,
        repository=repository,
        request=request,
        ideas=ideas,
        warnings=warnings,
        max_concurrent=max_concurrent,
        inspection=inspection,
        trace=trace,
        evidence_resolver=resolver,
    )


def _execute_batch(
    repository: RunRepository,
    spec: RunSpec,
    handler: ProposerReviewerHandler,
) -> RunSnapshot:
    return RunEngine(repository).execute(spec, handler)


def _batch_request(config: IdeasConfig, ideas: list[IdeaPlan]) -> BatchRequest:
    return BatchRequest(
        schema_version=BATCH_SCHEMA_VERSION,
        batch_id=config.run_id,
        loops=tuple(_idea_loop_spec(idea) for idea in ideas),
        failure_policy=BatchFailurePolicy.COLLECT,
    )


def _idea_loop_spec(idea: IdeaPlan) -> LoopSpec:
    template = _read_json(idea.variant.loop_template)
    max_rounds = _positive_template_int(template.get("max_rounds"), "max_rounds")
    early_stop = template.get("early_stop", {})
    if not isinstance(early_stop, Mapping):
        raise ConfigError(f"{idea.variant.loop_template}.early_stop must be an object")
    enabled = early_stop.get("enabled", False)
    if type(enabled) is not bool:
        raise ConfigError(f"{idea.variant.loop_template}.early_stop.enabled must be a boolean")
    evidence_enabled = idea.variant.context_policy.attach_arc_paper_tool_notes
    return LoopSpec(
        loop_id=idea.loop_id,
        context=idea.caller_context,
        proposers=(
            _worker_spec(
                _merged_worker_payload(
                    _read_json(idea.variant.proposer_template),
                    idea.variant.proposer_overrides,
                ),
                source=idea.variant.proposer_template,
                evidence_enabled=evidence_enabled,
            ),
        ),
        reviewer=_worker_spec(
            _reviewer_worker_payload(idea.variant),
            source=idea.variant.reviewer_template,
            evidence_enabled=evidence_enabled,
        ),
        max_rounds=max_rounds,
        allow_early_stop=enabled,
        on_proposer_failure=ProposerFailurePolicy.FAIL_LOOP,
    )


def _worker_spec(
    payload: Mapping[str, Any],
    *,
    source: Path,
    evidence_enabled: bool,
) -> WorkerSpec:
    worker_id = _required_text(payload, "id", source)
    prompt = payload.get("prompt")
    if not isinstance(prompt, Mapping):
        raise ConfigError(f"{source}.prompt must be an object")
    system = _required_text(prompt, "system", source)
    template = _required_text(prompt, "template", source)
    output_schema = payload.get("output_schema")
    if not isinstance(output_schema, Mapping):
        raise ConfigError(f"{source}.output_schema must be an object")
    runtime = payload.get("runtime", {})
    if not isinstance(runtime, Mapping):
        raise ConfigError(f"{source}.runtime must be an object")
    internet = runtime.get("allow_internet", False)
    if type(internet) is not bool:
        raise ConfigError(f"{source}.runtime.allow_internet must be a boolean")
    tier = str(payload.get("model_tier", "medium") or "medium").strip().lower()
    provider = str(payload.get("provider", "auto") or "auto").strip()
    model_value = payload.get("model")
    model = None if model_value is None else str(model_value).strip()
    if model == "":
        model = None
    try:
        selection = ModelSelection(provider=provider, model=model, tier=tier)  # type: ignore[arg-type]
    except Exception as exc:
        raise ConfigError(f"{source}.model configuration is invalid: {exc}") from exc
    return WorkerSpec(
        worker_id=worker_id,
        instructions=f"{system}\n\n{template}",
        output_schema=copy.deepcopy(dict(output_schema)),
        model=selection,
        capabilities=CapabilityPolicy(
            internet=internet,
            inherit_host_config=False,
            allowed_tools=(),
        ),
        interaction_operations=evidence_operation_contracts() if evidence_enabled else {},
        max_interaction_turns=2,
    )


def _reviewer_worker_payload(variant: VariantConfig) -> dict[str, Any]:
    payload = _read_json(variant.reviewer_template)
    payload["output_schema"] = _reviewer_payload_schema(variant)
    return payload


def _reviewer_payload_schema(variant: VariantConfig) -> dict[str, Any]:
    """Materialize the direct WorkerSpec reviewer payload contract."""
    schema = _read_json(variant.reviewer_output_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or "marks" not in properties:
        raise ConfigError(f"{variant.reviewer_output_schema} must be a direct reviewer payload schema")
    schema["properties"]["marks"] = marks_schema(load_marking_scheme(variant.marking_scheme))
    return schema


def _materialize_ideas(config: IdeasConfig) -> list[IdeaPlan]:
    ideas: list[IdeaPlan] = []
    for variant in config.variants:
        for idea_index in range(1, config.loops_per_variant + 1):
            idea_id = f"{variant.variant_id}/idea_{idea_index:03d}"
            ideas.append(
                IdeaPlan(
                    idea_id=idea_id,
                    variant_id=variant.variant_id,
                    idea_index=idea_index,
                    loop_id=f"{variant.variant_id}_idea_{idea_index:03d}",
                    variant=variant,
                    caller_context=_caller_context(
                        config,
                        variant=variant,
                        idea_id=idea_id,
                        idea_index=idea_index,
                    ),
                )
            )
    return ideas


def _caller_context(
    config: IdeasConfig,
    *,
    variant: VariantConfig,
    idea_id: str,
    idea_index: int,
) -> dict[str, Any]:
    loop_template = _read_json(variant.loop_template)
    caller_context = copy.deepcopy(loop_template.get("caller_context", {}))
    if not isinstance(caller_context, dict):
        raise ConfigError(f"{variant.loop_template}.caller_context must be an object")
    caller_context = _replace_placeholders(caller_context, {"<user_intent>": config.user_intent})
    caller_context["user_intent"] = config.user_intent
    caller_context["variant_id"] = variant.variant_id
    caller_context["idea_id"] = idea_id
    caller_context["marking_scheme"] = marking_scheme_for_context(load_marking_scheme(variant.marking_scheme))
    if variant.research_scope == "cross_domain":
        caller_context["generation_mode"] = "cross_domain"
        domain_cards = _domain_cards(config)
        caller_context["domain_cards"] = domain_cards
        legacy_domain_ids = [
            str(card.get("field_id", ""))
            for card in domain_cards
            if not card.get("summary_capabilities", {}).get("mathematical_opportunities")
        ]
        if legacy_domain_ids:
            caller_context.setdefault("warnings", []).append(
                "legacy_domain_summary_without_mathematical_opportunities: "
                + ", ".join(legacy_domain_ids)
            )
        caller_context["exploration_profile"] = _cross_domain_profile(config, idea_index=idea_index)
    if variant.context_policy.attach_domain_markdown:
        markdown_files = _domain_markdown_files(config.project_dir / "domain")
        if markdown_files:
            caller_context["domain_markdown_files"] = markdown_files
        else:
            if variant.context_policy.require_domain_markdown:
                raise ConfigError(f"{variant.variant_id} requires domain markdown under {config.project_dir / 'domain'}")
            caller_context.pop("domain_markdown_files", None)
            caller_context.setdefault("warnings", []).append(
                "domain_markdown_unavailable: Domain markdown was unavailable; continuing with user intent and ARC paper/tool context only."
            )
    else:
        caller_context.pop("domain_markdown_files", None)
    if not variant.context_policy.attach_arc_paper_tool_notes:
        caller_context.pop("arc_paper_tool_notes", None)
        caller_context.pop("controller_evidence_operations", None)
    else:
        caller_context["controller_evidence_operations"] = copy.deepcopy(ARC_PAPER_EVIDENCE_OPERATIONS)
    return caller_context


def _observed_result(
    config: IdeasConfig,
    *,
    repository: RunRepository,
    request: BatchRequest,
    ideas: list[IdeaPlan],
    warnings: list[str],
    max_concurrent: int,
    inspection: BatchInspection,
    trace: BatchTrace | None,
    evidence_resolver: ArcPaperEvidenceResolver,
) -> dict[str, Any]:
    try:
        round_score_table = _round_score_table(
            ideas,
            repository=repository,
            run_id=inspection.run_id,
            trace=trace,
        )
    except BatchProjectionIntegrityError:
        # A concurrent state change can invalidate an earlier trace vector.
        # Do not infer scores from activity or private layout in that case.
        warnings.append("committed_round_unavailable: committed artifacts could not be verified")
        round_score_table = _round_score_table(
            ideas,
            repository=None,
            run_id=None,
            trace=None,
        )
    loop_by_id = {loop.loop_id: loop for loop in inspection.loops}
    committed_rounds = {
        loop.loop_id: len(loop.rounds)
        for loop in trace.loops
    } if trace is not None else {}
    loops = [
        {
            "idea_id": idea.idea_id,
            "variant_id": idea.variant_id,
            "idea_index": idea.idea_index,
            "loop_id": idea.loop_id,
            "lifecycle": loop_by_id[idea.loop_id].lifecycle,
            "phase": loop_by_id[idea.loop_id].phase,
            "current_round": loop_by_id[idea.loop_id].current_round,
            "rounds_completed": loop_by_id[idea.loop_id].rounds_completed,
            "committed_rounds": committed_rounds.get(idea.loop_id, 0),
            "integrity_error": loop_by_id[idea.loop_id].integrity_error,
        }
        for idea in ideas
    ]
    reviewer_call_count = sum(committed_rounds.values())
    return {
        "schema_version": "arc.workflow.ideas.result.v1",
        "status": inspection.run_lifecycle,
        "run_id": config.run_id,
        # This is the RunRepository root consumed by rank-ideas, not the
        # repository's private per-run directory layout.
        "run_root": str(repository.root),
        "research_scope": config.research_scope,
        "domain_manifest_path": str(config.domain_manifest_path),
        "warnings": warnings,
        "proposal_count": len(ideas),
        "reviewer_call_count": reviewer_call_count,
        "loop_reviewer_call_count": reviewer_call_count,
        "max_concurrent_loops": max_concurrent,
        "max_concurrent_proposal_calls": max_concurrent,
        "batch_request_artifact_id": "proposer-reviewer/request",
        "batch": {
            "batch_id": request.batch_id,
            "run_revision": inspection.run_revision,
            "loop_revisions": dict(inspection.loop_revisions),
            "trace_verified": trace is not None,
        },
        "evidence": {
            "request_limit": evidence_resolver.request_limit,
            "request_count": evidence_resolver.request_count,
            "records": copy.deepcopy(evidence_resolver.records),
        },
        "loops": loops,
        "round_score_table": round_score_table,
    }


def _dry_run_result(
    config: IdeasConfig,
    *,
    request: BatchRequest,
    ideas: list[IdeaPlan],
    warnings: list[str],
    max_concurrent: int,
) -> dict[str, Any]:
    return {
        "schema_version": "arc.workflow.ideas.result.v1",
        "status": "dry_run",
        "run_id": config.run_id,
        "run_root": str(config.run_dir.resolve()),
        "research_scope": config.research_scope,
        "domain_manifest_path": str(config.domain_manifest_path),
        "warnings": warnings,
        "proposal_count": len(ideas),
        "reviewer_call_count": sum(loop.max_rounds for loop in request.loops),
        "loop_reviewer_call_count": sum(loop.max_rounds for loop in request.loops),
        "max_concurrent_loops": max_concurrent,
        "max_concurrent_proposal_calls": max_concurrent,
        "batch_request": encode_batch_request(request),
        "loops": [
            {
                "idea_id": idea.idea_id,
                "variant_id": idea.variant_id,
                "idea_index": idea.idea_index,
                "loop_id": idea.loop_id,
                "lifecycle": "validated",
                "phase": "not_started",
                "current_round": 1,
                "rounds_completed": 0,
                "committed_rounds": 0,
                "integrity_error": None,
            }
            for idea in ideas
        ],
        "round_score_table": _round_score_table(ideas, repository=None, run_id=None, trace=None),
    }


def _not_started_result(
    config: IdeasConfig,
    *,
    request: BatchRequest,
    ideas: list[IdeaPlan],
    warnings: list[str],
    max_concurrent: int,
    status: str,
) -> dict[str, Any]:
    result = _dry_run_result(
        config,
        request=request,
        ideas=ideas,
        warnings=warnings,
        max_concurrent=max_concurrent,
    )
    result["status"] = status
    result.pop("batch_request", None)
    result["reviewer_call_count"] = 0
    result["loop_reviewer_call_count"] = 0
    return result


def _round_score_table(
    ideas: list[IdeaPlan],
    *,
    repository: RunRepository | None,
    run_id: str | None,
    trace: BatchTrace | None,
) -> dict[str, Any]:
    by_loop = {} if trace is None else {loop.loop_id: loop for loop in trace.loops}
    rows = [
        _round_score_row(
            idea,
            repository=repository,
            run_id=run_id,
            committed_round_count=len(by_loop.get(idea.loop_id).rounds) if idea.loop_id in by_loop else 0,
        )
        for idea in ideas
    ]
    max_round = max(
        (max((int(key) for key in row["total_scores_by_round"]), default=0) for row in rows),
        default=0,
    )
    columns = [
        "Idea",
        "Group",
        "Final Title",
        *[f"R{round_number}" for round_number in range(1, max_round + 1)],
        f"Δ R1→R{max_round}" if max_round else "Δ",
        "Best",
    ]
    return {
        "schema_version": "arc.workflow.ideas.round_score_table.v1",
        "source": "committed_trace",
        "columns": columns,
        "rows": rows,
        "markdown": _round_score_markdown(columns, rows, max_round=max_round),
    }


def _round_score_row(
    idea: IdeaPlan,
    *,
    repository: RunRepository | None,
    run_id: str | None,
    committed_round_count: int,
) -> dict[str, Any]:
    scheme = load_marking_scheme(idea.variant.marking_scheme)
    rounds: dict[int, dict[str, Any]] = {}
    titles: dict[int, str] = {}
    if repository is not None and run_id is not None:
        for round_number in range(1, committed_round_count + 1):
            committed = read_batch_round(repository, run_id, idea.loop_id, round_number)
            title = _proposal_title(committed.proposals)
            if title:
                titles[round_number] = title
            marks = _review_marks(committed.review)
            if marks is not None:
                rounds[round_number] = normalized_marks(marks, scheme)
    total_scores = {
        round_number: marks["total_score"]
        for round_number, marks in rounds.items()
        if isinstance(marks.get("total_score"), (int, float))
    }
    first_round = min(total_scores, default=None)
    last_round = max(total_scores, default=None)
    delta_total = (
        total_scores[last_round] - total_scores[first_round]
        if first_round is not None and last_round is not None
        else None
    )
    return {
        "idea_id": idea.idea_id,
        "variant_id": idea.variant_id,
        "group": idea.variant_id,
        "loop_id": idea.loop_id,
        "final_title": titles[max(titles)] if titles else "",
        "rounds": [
            {"round": round_number, "marks": rounds[round_number]}
            for round_number in sorted(rounds)
        ],
        "total_scores_by_round": {str(key): value for key, value in sorted(total_scores.items())},
        "delta_total": delta_total,
        "best_total": max(total_scores.values(), default=None),
    }


def _proposal_title(proposals: Mapping[str, Any]) -> str:
    for proposal in proposals.values():
        if isinstance(proposal, Mapping):
            title = proposal.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return ""


def _review_marks(review: Any) -> Mapping[str, Any] | None:
    if not isinstance(review, Mapping):
        return None
    payload = review.get("payload")
    if not isinstance(payload, Mapping):
        return None
    marks = payload.get("marks")
    return marks if isinstance(marks, Mapping) else None


def _round_score_markdown(columns: list[str], rows: list[dict[str, Any]], *, max_round: int) -> str:
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---:" if column.startswith("R") or column in {"Best"} or column.startswith("Δ") else "---" for column in columns) + "|",
    ]
    for row in rows:
        total_scores = {int(key): value for key, value in row["total_scores_by_round"].items()}
        values = [
            row["loop_id"],
            row["group"],
            str(row.get("final_title", "")).replace("|", "/"),
            *[_format_score(total_scores.get(round_number)) for round_number in range(1, max_round + 1)],
            _format_delta(row.get("delta_total")),
            _format_score(row.get("best_total")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _format_score(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else ""


def _format_delta(value: Any) -> str:
    return f"{value:+g}" if isinstance(value, (int, float)) else ""


def _caller_context_warnings(ideas: list[IdeaPlan]) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for idea in ideas:
        for warning in idea.caller_context.get("warnings", []):
            text = str(warning)
            if text not in seen:
                seen.add(text)
                warnings.append(text)
    return warnings


def _model_tier_warnings(request: BatchRequest) -> list[str]:
    problems: list[str] = []
    for loop in request.loops:
        reviewer_rank = MODEL_TIER_RANKS.get(loop.reviewer.model.tier)
        if reviewer_rank is None:
            continue
        for proposer in loop.proposers:
            proposer_rank = MODEL_TIER_RANKS.get(proposer.model.tier)
            if proposer_rank is not None and proposer_rank > reviewer_rank:
                problems.append(
                    f"{loop.loop_id}: {proposer.worker_id}={proposer.model.tier} > "
                    f"{loop.reviewer.worker_id}={loop.reviewer.model.tier}"
                )
    if not problems:
        return []
    return [
        "WARNING: REVIEWER MODEL TIER BELOW PROPOSER. "
        "Reviewer feedback may be less useful when the reviewer is configured with a lower model tier than the proposer. "
        "Affected assignments: " + "; ".join(problems)
    ]


def _max_concurrent_loops(proposal_count: int) -> int:
    raw = os.environ.get("ARC_IDEAS_MAX_CONCURRENT_LOOPS", "12")
    try:
        configured = int(raw)
    except ValueError as exc:
        raise ConfigError("ARC_IDEAS_MAX_CONCURRENT_LOOPS must be a positive integer") from exc
    if configured <= 0:
        raise ConfigError("ARC_IDEAS_MAX_CONCURRENT_LOOPS must be a positive integer")
    return min(proposal_count, configured)


def _concurrency_warning(
    config: IdeasConfig,
    proposal_count: int,
    *,
    max_concurrent: int,
    request: BatchRequest,
) -> str:
    round_counts = sorted({loop.max_rounds for loop in request.loops})
    round_text = (
        f"{round_counts[0]} reviewer reports per loop"
        if len(round_counts) == 1
        else f"reviewer report counts {round_counts}"
    )
    return (
        "WARNING: Running "
        f"{len(config.variants)} variants x {config.loops_per_variant} proposer-reviewer loops "
        f"with {round_text} and loop concurrency capped at {max_concurrent} ({proposal_count} loops). "
        "The typed batch stores durable artifacts through arc-jobs."
    )


def _cross_domain_profile(config: IdeasConfig, *, idea_index: int) -> dict[str, str]:
    profiles = config.exploration_profiles or DEFAULT_CROSS_DOMAIN_PROFILES
    try:
        return copy.deepcopy(profiles[idea_index - 1])
    except IndexError as exc:
        raise ConfigError(f"No cross-domain exploration profile is configured for idea {idea_index}") from exc


def _domain_cards(config: IdeasConfig) -> list[dict[str, Any]]:
    manifest = config.domain_manifest
    if not isinstance(manifest, Mapping):
        raise ConfigError("cross-domain ideas require a domain manifest")
    groups = manifest.get("field_groups")
    packages = manifest.get("domain_packages")
    if not isinstance(groups, list) or not isinstance(packages, list):
        raise ConfigError(f"{config.domain_manifest_path}.field_groups must be an array")
    by_id = {str(item.get("domain_package_id", "")): item for item in packages if isinstance(item, Mapping)}
    cards: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            raise ConfigError(f"{config.domain_manifest_path}.field_groups[{index}] must be an object")
        field_id = str(group.get("field_id", "")).strip()
        field_card = group.get("field_card")
        if not field_id or not isinstance(field_card, Mapping):
            raise ConfigError(f"{config.domain_manifest_path}.field_groups[{index}] requires field_id and field_card")
        versions: list[str] = []
        opportunities: list[Any] = []
        for package_index, package_id in enumerate(group.get("domain_package_ids", [])):
            package = by_id.get(str(package_id))
            if not isinstance(package, Mapping):
                raise ConfigError(f"field {field_id!r} references unknown package {package_id!r}")
            summary_path = _domain_summary_path(config, entry=package, index=package_index)
            summary = _read_json(summary_path)
            version = str(summary.get("schema_version", "")).strip()
            if version not in {"arc.domain_summary.v4", "arc.domain_summary.v5"}:
                raise ConfigError(f"{summary_path}.schema_version must be arc.domain_summary.v4 or arc.domain_summary.v5")
            legacy_domain_id = str(summary.get("domain_id", "")).strip()
            if version == "arc.domain_summary.v5" and "domain_id" in summary:
                raise ConfigError(
                    f"{summary_path} arc.domain_summary.v5 must not contain domain_id"
                )
            if (
                version == "arc.domain_summary.v4"
                and legacy_domain_id
                and legacy_domain_id != str(package_id)
            ):
                raise ConfigError(
                    f"package {package_id!r} points to legacy summary for "
                    f"another package: {summary_path}"
                )
            versions.append(version)
            if version == "arc.domain_summary.v5":
                raw = summary.get("mathematical_opportunities")
                validation_error = mathematical_opportunities_validation_error(raw)
                if validation_error is not None:
                    raise ConfigError(f"{summary_path}.mathematical_opportunities is invalid for v5: {validation_error}")
                opportunities.extend(copy.deepcopy(raw.get("well_defined_problems", [])))
        supports = bool(versions) and all(item == "arc.domain_summary.v5" for item in versions)
        card = copy.deepcopy(dict(field_card))
        card.update(
            {
                "field_id": field_id,
                "domain_package_ids": list(group.get("domain_package_ids", [])),
                "summary_capabilities": {"mathematical_opportunities": supports},
                "mathematical_opportunities": {"well_defined_problems": opportunities},
            }
        )
        cards.append(card)
    if len(cards) < 2:
        raise ConfigError("cross-domain ideas require at least two distinct field cards")
    return cards


def _domain_summary_path(config: IdeasConfig, *, entry: Mapping[str, Any], index: int) -> Path:
    raw = str(
        entry.get("summary_json_path")
        or entry.get("domain_summary_path")
        or entry.get("summary_path")
        or ""
    ).strip()
    if not raw:
        raise ConfigError(f"{config.domain_manifest_path}.domains[{index}] requires summary_json_path")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        path = candidate
    else:
        project_relative = config.project_dir / candidate
        manifest_relative = config.domain_manifest_path.parent / candidate
        path = project_relative if project_relative.is_file() else manifest_relative
    if not path.is_file():
        raise ConfigError(f"domain summary does not exist: {path}")
    return path.resolve()


def _domain_markdown_files(domain_dir: Path) -> list[dict[str, str]]:
    if not domain_dir.exists():
        return []
    return [
        {
            "path": str(path.relative_to(domain_dir.parent)),
            "content": path.read_text(encoding="utf-8", errors="replace"),
        }
        for path in sorted(domain_dir.rglob("*.md"))
        if path.is_file()
    ]


def _merged_worker_payload(template: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    return _deep_merge(template, overrides)


def _deep_merge(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _replace_placeholders(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for old, new in replacements.items():
            result = result.replace(old, new)
        return result
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, replacements) for key, item in value.items()}
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read JSON file {path}: {exc}") from exc
    except NonObjectJsonError as exc:
        raise ConfigError(f"JSON file must contain an object: {path}") from exc


def _read_config_file(path: str) -> dict[str, Any]:
    return _read_json(Path(path))


def _required_text(payload: Mapping[str, Any], key: str, source: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source}.{key} must be a non-empty string")
    return value.strip()


def _positive_template_int(value: Any, key: str) -> int:
    return require_strict_int(
        value,
        key,
        minimum=1,
        requirement="a positive integer",
        error_type=ConfigError,
    )


def _progress_sidechannel_callback(
    base_env: Mapping[str, str] | None,
) -> Callable[[dict[str, Any]], None] | None:
    environment = base_env if base_env is not None else os.environ
    raw = str(environment.get("ARC_JOB_PROGRESS_FILE", "")).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    lock = threading.Lock()

    def append_progress(event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

    return append_progress


def _combined_progress_callback(
    first: Callable[[dict[str, Any]], None] | None,
    second: Callable[[dict[str, Any]], None] | None,
) -> Callable[[dict[str, Any]], None] | None:
    callbacks = tuple(item for item in (first, second) if item is not None)
    if not callbacks:
        return None

    def emit(event: dict[str, Any]) -> None:
        for callback in callbacks:
            callback(dict(event))

    return emit


def _emit_progress(callback: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if callback is not None:
        callback(event)


def _foreground_progress_callback() -> Callable[[dict[str, Any]], None] | None:
    if str(os.environ.get("ARC_JOB_PROGRESS_FILE", "")).strip():
        return None

    def emit(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str), file=sys.stderr, flush=True)

    return emit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARC ideas workflow helper")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stop_event = threading.Event()
    installed_handlers: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            installed_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass
    try:
        result = run_ideas(
            _read_config_file(args.config),
            dry_run=args.dry_run,
            progress_callback=_foreground_progress_callback(),
            stop_check=stop_event.is_set,
        )
    finally:
        for signum, handler in installed_handlers.items():
            signal.signal(signum, handler)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for warning in result.get("warnings", []):
            print(warning)
        print(result["status"])
        table = result.get("round_score_table", {}).get("markdown")
        if table:
            print(table)
    return 1 if result.get("status") in {"failed", "stopped", "paused"} else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
