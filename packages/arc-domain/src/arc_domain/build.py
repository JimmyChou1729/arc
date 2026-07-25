"""Durable orchestration for one complete ARC research-domain build."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Callable, Mapping

from arc_jobs import (
    Failed,
    FailureMode,
    GroupResult,
    JsonValue,
    Paused,
    RunContext,
    RunEngine,
    RunError,
    RunRepository,
    RunSnapshot,
    RunSpec,
    Succeeded,
    StoppedError,
    UnitResult,
    WorkUnit,
    canonical_json_bytes,
)
from arc_llm import (
    JsonOutput,
    LLMStopped,
    LLMCompleted,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMTaskService,
)
from arc_paper import ReferenceInferenceCompleted, ReferenceInferenceService, normalize_paper_id

from ._llm import (
    DomainLLMError,
    awaiting_from_pause,
    execute_routed,
    is_transient_failure,
    outer_resume_input,
    run_error_from_failure,
)
from .catalog import register_domain_run
from .contracts import (
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
    DomainBuildRequest,
    DomainBuildResult,
    DomainBuildWarning,
    decode_domain_build_request,
    encode_domain_build_request,
    encode_domain_build_result,
)
from .fetch import DomainPaperAccess
from .foundation import (
    DEFAULT_FOUNDATION_HEURISTICS,
    FOUNDATION_CANDIDATE_AUDIT_SCHEMA,
    FOUNDATION_SELECTION_SCHEMA,
    FoundationHeuristics,
    apply_reference_inference_result,
    audit_expansion_request,
    build_candidate_records,
    candidate_audit_prompt,
    default_candidate_audit,
    deterministic_foundation_selection,
    enforce_fixed_seed_foundation,
    foundation_selection_prompt,
    normalize_candidate_audit,
    normalize_foundation_selection,
)
from .network import (
    INTENT_RANKING_SCHEMA,
    _add_in_graph_citer_scores,
    _add_reference_edge_scores,
    _build_graph,
    _common_references,
    _enrich_parent_foundations,
    _select_domain_papers,
    deterministic_intent_ranking,
    intent_ranking_prompt,
    merge_citer_pool,
    normalize_intent_ranking,
    strict_window_citer_streams,
)
from .packs import build_domain_packs
from .paths import DomainPaths, domain_id_for
from .render import render_network_html
from .summary import (
    DOMAIN_SUMMARY_SCHEMA,
    normalize_summary_output,
    render_summary_markdown,
    summary_prompt,
)
from .text import deterministic_sample, paper_key


DOMAIN_BUILD_HANDLER = "arc.domain.build.v1"

_FOUNDATION_SELECTION_ARTIFACT = "foundation/selection"
_FOUNDATION_WARNINGS_ARTIFACT = "foundation/warnings"
_GRAPH_ARTIFACT = "network/graph"
_NETWORK_WARNINGS_ARTIFACT = "network/warnings"
_PAPER_PACK_ARTIFACT = "packs/paper-json"
_EVIDENCE_PACK_ARTIFACT = "packs/evidence"
_PACK_WARNINGS_ARTIFACT = "packs/warnings"
_NETWORK_HTML_ARTIFACT = "render/network-html"
_SUMMARY_ARTIFACT = "summary/json"
_SUMMARY_MARKDOWN_ARTIFACT = "summary/markdown"
_SUMMARY_UNAVAILABLE_ARTIFACT = "summary/unavailable"
_RESULT_ARTIFACT = "result"
_FOUNDATION_RECENT_CITER_WITNESS_LIMIT = 50
_FOUNDATION_WITNESS_LIMIT = 60
_MIN_DOMAIN_BUILD_WORKERS = 1
_MAX_DOMAIN_BUILD_WORKERS = 24


class DomainBuildStageError(RuntimeError):
    def __init__(self, code: str, message: str, details: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def validate_domain_build_workers(value: object) -> int:
    """Return a supported operational worker count."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not _MIN_DOMAIN_BUILD_WORKERS <= value <= _MAX_DOMAIN_BUILD_WORKERS
    ):
        raise ValueError("domain build workers must be an integer between 1 and 24")
    return value


class DomainBuildHandler:
    """One replay-safe handler for foundation, graph, packs, and summary."""

    name = DOMAIN_BUILD_HANDLER

    def __init__(
        self,
        request: DomainBuildRequest,
        *,
        paper_access: DomainPaperAccess | None = None,
        task_service: LLMTaskService | None = None,
        reference_service: ReferenceInferenceService | None = None,
        max_workers: int = 8,
    ) -> None:
        max_workers = validate_domain_build_workers(max_workers)
        self.request = request
        self.paper = paper_access or DomainPaperAccess()
        self.task_service = task_service or LLMTaskService()
        self.reference_service = reference_service or ReferenceInferenceService(self.task_service)
        self.max_workers = max_workers

    def semantic_input(self) -> dict[str, JsonValue]:
        return encode_domain_build_request(self.request)

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "domain_build_binding_mismatch",
                    "Handler bindings do not match the durable domain-build request.",
                )
            )
        try:
            resume_input = outer_resume_input(
                context, error_code="domain_resume_input_invalid"
            )
            warnings: list[DomainBuildWarning] = []
            selection_outcome = self._foundation(context, resume_input, warnings)
            if isinstance(selection_outcome, (Paused, Failed)):
                return selection_outcome
            selection, foundation_ref = selection_outcome

            graph_outcome = self._network(
                context, resume_input, selection, warnings
            )
            if isinstance(graph_outcome, (Paused, Failed)):
                return graph_outcome
            graph, graph_ref = graph_outcome

            paper_pack, evidence_pack, paper_pack_ref, evidence_pack_ref = self._packs(
                context, graph, warnings
            )
            summary_outcome = self._summary(
                context,
                resume_input,
                graph,
                evidence_pack,
                selection,
                warnings,
            )
            if isinstance(summary_outcome, (Paused, Failed)):
                return summary_outcome
            summary_ref, summary_markdown_ref = summary_outcome

            html_ref = context.artifacts.find(_NETWORK_HTML_ARTIFACT)
            if html_ref is None:
                html_ref = context.artifacts.publish_bytes(
                    _NETWORK_HTML_ARTIFACT,
                    render_network_html(graph).encode("utf-8"),
                    media_type="text/html; charset=utf-8",
                )
            result = DomainBuildResult(
                domain_id=domain_id_for(self.request.seed_paper, self.request.intent),
                foundation_selection=foundation_ref,
                graph=graph_ref,
                network_html=html_ref,
                paper_json_pack=paper_pack_ref,
                evidence_pack=evidence_pack_ref,
                summary=summary_ref,
                summary_markdown=summary_markdown_ref,
                warnings=tuple(_dedupe_warnings(warnings)),
            )
            result_ref = context.artifacts.find(_RESULT_ARTIFACT)
            if result_ref is None:
                result_ref = context.artifacts.publish_json(
                    _RESULT_ARTIFACT, encode_domain_build_result(result)
                )
            return Succeeded(result_ref)
        except DomainLLMError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except DomainBuildStageError as exc:
            return Failed(RunError(exc.code, str(exc), exc.details))

    def _foundation(
        self,
        context: RunContext,
        resume_input: Any,
        warnings: list[DomainBuildWarning],
    ) -> tuple[dict[str, Any], Any] | Paused | Failed:
        existing = context.artifacts.find(_FOUNDATION_SELECTION_ARTIFACT)
        if existing is not None:
            _extend_stored_warnings(
                context, _FOUNDATION_WARNINGS_ARTIFACT, warnings
            )
            return _read_json(context, existing, "foundation selection"), existing
        warning_start = len(warnings)

        seed_id = self.request.seed_paper
        input_ref = context.artifacts.find("foundation/input")
        if input_ref is None:
            seed_metadata = self.paper.metadata(seed_id)
            newest_citers = self.paper.citers(
                seed_id,
                limit=_FOUNDATION_RECENT_CITER_WITNESS_LIMIT,
                sort="mostrecent",
            )[:_FOUNDATION_RECENT_CITER_WITNESS_LIMIT]
            seed_references = self.paper.references(seed_id)
            sampled_references = _sample_foundation_witness_references(
                seed_references=seed_references,
                newest_citers=newest_citers,
                seed_id=seed_id,
                intent=self.request.intent,
            )
            foundation_input = {
                "seed_metadata": seed_metadata,
                "newest_citers": newest_citers,
                "seed_references": seed_references,
                "sampled_references": sampled_references,
            }
            context.artifacts.publish_json("foundation/input", foundation_input)
        else:
            foundation_input = _read_json(
                context, input_ref, "foundation input"
            )
            seed_metadata = _mapping(
                foundation_input.get("seed_metadata"), "seed metadata"
            )
            newest_citers = _mapping_list(
                foundation_input.get("newest_citers"), "newest citers"
            )
            seed_references = _mapping_list(
                foundation_input.get("seed_references"), "seed references"
            )

        # Candidate evidence has a fixed witness budget.  Keep the complete
        # reference list in foundation/input for provenance, but derive the
        # candidate-facing subset again so resumed legacy artifacts cannot
        # widen the metadata-acquisition scope.
        newest_citers = newest_citers[:_FOUNDATION_RECENT_CITER_WITNESS_LIMIT]
        witness_references = _sample_foundation_witness_references(
            seed_references=seed_references,
            newest_citers=newest_citers,
            seed_id=seed_id,
            intent=self.request.intent,
        )
        citer_ids = _unique_paper_ids(newest_citers)
        refs_by_citer, reference_errors = self._group_values(
            context,
            "foundation-witness-references",
            citer_ids,
            self.paper.references,
            essential=False,
        )
        _append_group_warnings(
            warnings, reference_errors, code="witness_references_unavailable", stage="foundation"
        )
        scan_heuristics = FoundationHeuristics(
            min_citation_count=DEFAULT_FOUNDATION_HEURISTICS.min_citation_count,
            candidate_limit=DEFAULT_FOUNDATION_HEURISTICS.candidate_scan_limit,
            candidate_scan_limit=DEFAULT_FOUNDATION_HEURISTICS.candidate_scan_limit,
        )
        preliminary = build_candidate_records(
            seed_metadata=seed_metadata,
            seed_references=witness_references,
            newest_citers=newest_citers,
            refs_by_citer=refs_by_citer,
            metadata_by_id={},
            intent=self.request.intent,
            heuristics=scan_heuristics,
        )
        candidate_ids = _unique_paper_ids(preliminary)
        metadata_by_id, metadata_errors = self._group_values(
            context,
            "foundation-candidate-metadata",
            candidate_ids,
            self.paper.metadata,
            essential=False,
        )
        _append_group_warnings(
            warnings, metadata_errors, code="candidate_metadata_unavailable", stage="foundation"
        )
        candidates = build_candidate_records(
            seed_metadata=seed_metadata,
            seed_references=witness_references,
            newest_citers=newest_citers,
            refs_by_citer=refs_by_citer,
            metadata_by_id=metadata_by_id,
            intent=self.request.intent,
        )

        audit_request = LLMRequest(
            _task_id("foundation-audit", seed_id, self.request.intent),
            candidate_audit_prompt(
                seed_metadata=seed_metadata,
                candidates=candidates,
                intent=self.request.intent,
                v2_semantics=(
                    self.request.policy.schema_version
                    == DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2
                ),
            ),
            JsonOutput(FOUNDATION_CANDIDATE_AUDIT_SCHEMA, repair="format"),
            self.request.model,
        )
        audit_outcome = execute_routed(
            self.task_service, context, audit_request, resume_input=resume_input
        )
        if isinstance(audit_outcome, LLMCompleted):
            audit = normalize_candidate_audit(_mapping(audit_outcome.value, "candidate audit"))
        elif isinstance(audit_outcome, LLMPaused):
            return Paused(awaiting_from_pause(audit_outcome))
        elif isinstance(audit_outcome, LLMFailed):
            if not is_transient_failure(audit_outcome):
                return Failed(run_error_from_failure(audit_outcome))
            audit = default_candidate_audit()
            warnings.append(
                DomainBuildWarning(
                    "foundation_audit_unavailable",
                    str(audit_outcome.error),
                    "foundation",
                )
            )
        elif isinstance(audit_outcome, LLMStopped):
            raise StoppedError("foundation audit stopped")
        else:
            raise RuntimeError("unknown foundation-audit outcome")

        expansion_request = audit_expansion_request(audit, self.request.intent)
        expansion_report: dict[str, Any] = {"status": "not_requested"}
        if expansion_request is not None:
            reference_outcome = self.reference_service.infer(
                context,
                expansion_request,
                metadata_lookup=self.paper.metadata,
                model=self.request.model,
                resume_input=resume_input,
            )
            if isinstance(reference_outcome, ReferenceInferenceCompleted):
                result_document = reference_outcome.result.to_document()
                expansion_metadata: dict[str, dict[str, Any]] = {}
                for paper_id in result_document.get("paper_ids", []):
                    try:
                        expansion_metadata[paper_id] = self.paper.metadata(paper_id)
                    except Exception as exc:
                        warnings.append(
                            DomainBuildWarning(
                                "reference_inference_metadata_unavailable",
                                str(exc),
                                "foundation",
                                str(paper_id),
                            )
                        )
                candidates, expansion_report = apply_reference_inference_result(
                    candidates,
                    audit,
                    result_document,
                    expansion_metadata,
                    self.request.intent,
                )
            elif isinstance(reference_outcome, LLMPaused):
                return Paused(awaiting_from_pause(reference_outcome))
            elif isinstance(reference_outcome, LLMFailed):
                if not is_transient_failure(reference_outcome):
                    return Failed(run_error_from_failure(reference_outcome))
                warnings.append(
                    DomainBuildWarning(
                        "reference_inference_unavailable",
                        str(reference_outcome.error),
                        "foundation",
                    )
                )
            elif isinstance(reference_outcome, LLMStopped):
                raise StoppedError("reference inference stopped")
            else:
                raise RuntimeError("unknown reference-inference outcome")

        context.artifacts.publish_json(
            "foundation/candidates",
            {
                "candidates": candidates,
                "audit": audit,
                "expansion": expansion_report,
            },
        )
        selection_request = LLMRequest(
            _task_id("foundation-select", seed_id, self.request.intent),
            foundation_selection_prompt(
                seed_metadata=seed_metadata,
                candidates=candidates,
                intent=self.request.intent,
                v2_semantics=(
                    self.request.policy.schema_version
                    == DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2
                ),
                fixed_seed=self.request.policy.foundation_mode == "fixed_seed",
            ),
            JsonOutput(FOUNDATION_SELECTION_SCHEMA, repair="format"),
            self.request.model,
        )
        selection_outcome = execute_routed(
            self.task_service,
            context,
            selection_request,
            resume_input=resume_input,
        )
        if isinstance(selection_outcome, LLMCompleted):
            selection = normalize_foundation_selection(
                _mapping(selection_outcome.value, "foundation selection"),
                candidates,
                intent=self.request.intent,
            )
        elif isinstance(selection_outcome, LLMPaused):
            return Paused(awaiting_from_pause(selection_outcome))
        elif isinstance(selection_outcome, LLMFailed):
            if not is_transient_failure(selection_outcome):
                return Failed(run_error_from_failure(selection_outcome))
            selection = deterministic_foundation_selection(
                candidates, intent=self.request.intent
            )
            warnings.append(
                DomainBuildWarning(
                    "foundation_selection_unavailable",
                    str(selection_outcome.error),
                    "foundation",
                )
            )
        elif isinstance(selection_outcome, LLMStopped):
            raise StoppedError("foundation selection stopped")
        else:
            raise RuntimeError("unknown foundation-selection outcome")
        if self.request.policy.foundation_mode == "fixed_seed":
            selection = enforce_fixed_seed_foundation(
                selection,
                candidates,
                seed_paper_id=seed_id,
                seed_metadata=seed_metadata,
            )
        context.artifacts.publish_json(
            _FOUNDATION_WARNINGS_ARTIFACT,
            [_warning_document(item) for item in warnings[warning_start:]],
        )
        ref = context.artifacts.publish_json(_FOUNDATION_SELECTION_ARTIFACT, selection)
        return selection, ref

    def _network(
        self,
        context: RunContext,
        resume_input: Any,
        selection: dict[str, Any],
        warnings: list[DomainBuildWarning],
    ) -> tuple[dict[str, Any], Any] | Paused | Failed:
        existing = context.artifacts.find(_GRAPH_ARTIFACT)
        if existing is not None:
            _extend_stored_warnings(context, _NETWORK_WARNINGS_ARTIFACT, warnings)
            return _read_json(context, existing, "domain graph"), existing
        warning_start = len(warnings)

        selected_choice = _mapping(
            selection.get("selected_foundation"), "selected foundation"
        )
        foundation_id = normalize_paper_id(str(selected_choice.get("paper_id") or ""))
        if not foundation_id:
            raise DomainBuildStageError(
                "foundation_selection_invalid",
                "Selected foundation does not contain a supported paper ID.",
            )
        policy = self.request.policy
        network_input_ref = context.artifacts.find("network/input")
        strict_window_stats: dict[str, int] | None = None
        if network_input_ref is None:
            foundation = self.paper.metadata(foundation_id)
            foundation["paper_id"] = foundation_id
            foundation["reason"] = str(selected_choice.get("reason") or "")
            most_recent = self.paper.citers(
                foundation_id, limit=policy.citer_pool_limit, sort="mostrecent"
            )
            most_cited = self.paper.citers(
                foundation_id, limit=policy.citer_pool_limit, sort="mostcited"
            )
            if policy.citer_selection_mode == "strict_window":
                most_recent, most_cited, strict_window_stats = strict_window_citer_streams(
                    foundation_id,
                    most_recent=most_recent,
                    most_cited=most_cited,
                    as_of_date=date.fromisoformat(policy.as_of_date),
                    window_days=policy.recent_window_days,
                )
            citer_pool = merge_citer_pool(
                foundation_id,
                most_recent=most_recent,
                most_cited=most_cited,
                limit=policy.citer_pool_limit,
            )
            network_input: dict[str, Any] = {
                "foundation": foundation,
                "citer_pool": citer_pool,
            }
            if strict_window_stats is not None:
                network_input["strict_window"] = strict_window_stats
            context.artifacts.publish_json("network/input", network_input)
        else:
            network_input = _read_json(context, network_input_ref, "network input")
            foundation = _mapping(
                network_input.get("foundation"), "foundation metadata"
            )
            citer_pool = _mapping_list(
                network_input.get("citer_pool"), "citer pool"
            )
            raw_stats = network_input.get("strict_window")
            if isinstance(raw_stats, Mapping):
                strict_window_stats = {
                    key: int(value)
                    for key, value in raw_stats.items()
                    if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
                }

        if strict_window_stats is not None:
            missing_dates = strict_window_stats.get(
                "excluded_missing_first_public_date", 0
            )
            outside_window = strict_window_stats.get("excluded_outside_window", 0)
            if missing_dates or outside_window:
                warnings.append(
                    DomainBuildWarning(
                        "strict_window_citers_excluded",
                        "Excluded "
                        f"{missing_dates} citer(s) without a first-public date and "
                        f"{outside_window} citer(s) outside the requested window.",
                        "network",
                    )
                )
            if strict_window_stats.get("eligible_citers", 0) == 0:
                warnings.append(
                    DomainBuildWarning(
                        "strict_window_no_eligible_citers",
                        "No direct foundation citers were eligible for the requested time window; generated no domain-paper nodes and retained foundation/context nodes only.",
                        "network",
                    )
                )

        if policy.citer_selection_mode == "strict_window" and not citer_pool:
            ranking = deterministic_intent_ranking(
                [],
                intent=self.request.intent,
                reason="No citers were eligible for strict-window ranking.",
            )
        else:
            ranking_request = LLMRequest(
                _task_id("network-rank", foundation_id, self.request.intent),
                intent_ranking_prompt(citer_pool, intent=self.request.intent),
                JsonOutput(INTENT_RANKING_SCHEMA, repair="format"),
                self.request.model,
            )
            ranking_outcome = execute_routed(
                self.task_service, context, ranking_request, resume_input=resume_input
            )
            if isinstance(ranking_outcome, LLMCompleted):
                ranking = normalize_intent_ranking(
                    _mapping(ranking_outcome.value, "intent ranking"),
                    citer_pool=citer_pool,
                )
            elif isinstance(ranking_outcome, LLMPaused):
                return Paused(awaiting_from_pause(ranking_outcome))
            elif isinstance(ranking_outcome, LLMFailed):
                if not is_transient_failure(ranking_outcome):
                    return Failed(run_error_from_failure(ranking_outcome))
                ranking = deterministic_intent_ranking(
                    citer_pool,
                    intent=self.request.intent,
                    reason=str(ranking_outcome.error),
                )
                warnings.append(
                    DomainBuildWarning(
                        "intent_ranking_unavailable",
                        str(ranking_outcome.error),
                        "network",
                    )
                )
            elif isinstance(ranking_outcome, LLMStopped):
                raise StoppedError("intent ranking stopped")
            else:
                raise RuntimeError("unknown intent-ranking outcome")
        context.artifacts.publish_json("network/intent-ranking", ranking)

        parent_choices = _unique_paper_records(
            [
                item
                for item in selection.get("parent_foundations", [])
                if isinstance(item, Mapping)
            ],
            excluded_ids={foundation_id},
        )
        parent_capacity = max(0, policy.graph_node_limit - 1)
        if len(parent_choices) > parent_capacity:
            omitted = len(parent_choices) - parent_capacity
            parent_choices = parent_choices[:parent_capacity]
            warnings.append(
                DomainBuildWarning(
                    "parent_foundations_truncated",
                    f"Omitted {omitted} parent foundation(s) to respect graph_node_limit.",
                    "network",
                )
            )
        parent_ids = _unique_paper_ids(parent_choices)
        parent_id_set = set(parent_ids)
        fixed_count = 1 + len(parent_ids)
        selected = _select_domain_papers(
            citer_pool,
            foundation_id=foundation_id,
            excluded_ids=parent_id_set,
            intent_ranking=ranking,
            intent=self.request.intent,
            selected_count=policy.ranked_paper_limit,
            max_total=max(0, policy.graph_node_limit - fixed_count),
            recent_window_days=policy.recent_window_days,
            as_of_date=date.fromisoformat(policy.as_of_date),
            strict_window=policy.citer_selection_mode == "strict_window",
        )
        selected_ids = _unique_paper_ids(selected)
        refs_by_selected, selected_reference_errors = self._group_values(
            context,
            "network-selected-references",
            selected_ids,
            self.paper.references,
            essential=False,
        )
        _append_group_warnings(
            warnings,
            selected_reference_errors,
            code="domain_references_unavailable",
            stage="network",
        )
        selected = _add_in_graph_citer_scores(
            selected, refs_by_selected=refs_by_selected
        )
        remaining = max(
            0,
            policy.graph_node_limit
            - 1
            - len(parent_ids)
            - len(selected),
        )
        preliminary_common = _common_references(
            foundation_id=foundation_id,
            selected_ids=selected_ids,
            excluded_ids=parent_id_set,
            refs_by_selected=refs_by_selected,
            max_extra=remaining,
            metadata_by_id={},
        )
        common_ids = _unique_paper_ids(preliminary_common)
        common_metadata, common_metadata_errors = self._group_values(
            context,
            "network-common-metadata",
            common_ids,
            self.paper.metadata,
            essential=False,
        )
        _append_group_warnings(
            warnings,
            common_metadata_errors,
            code="common_reference_metadata_unavailable",
            stage="network",
        )
        common = _common_references(
            foundation_id=foundation_id,
            selected_ids=selected_ids,
            excluded_ids=parent_id_set,
            refs_by_selected=refs_by_selected,
            max_extra=remaining,
            metadata_by_id=common_metadata,
        )
        parent_metadata, parent_metadata_errors = self._group_values(
            context,
            "network-parent-metadata",
            parent_ids,
            self.paper.metadata,
            essential=False,
        )
        _append_group_warnings(
            warnings,
            parent_metadata_errors,
            code="parent_foundation_metadata_unavailable",
            stage="network",
        )
        parents = _enrich_parent_foundations(
            parent_choices, metadata_by_id=parent_metadata
        )
        selected = _add_reference_edge_scores(
            selected,
            foundation_id=foundation_id,
            parent_foundations=parents,
            common_references=common,
            refs_by_selected=refs_by_selected,
        )
        created_at = context.repository.inspect(context.run_id).snapshot.created_at
        graph = _build_graph(
            domain_id=domain_id_for(self.request.seed_paper, self.request.intent),
            foundation=foundation,
            parent_foundations=parents,
            selected_papers=selected,
            common_references=common,
            refs_by_selected=refs_by_selected,
            intent=self.request.intent,
            created_at=created_at,
            recent_window_days=policy.recent_window_days,
            as_of_date=date.fromisoformat(policy.as_of_date),
        )
        if len(graph.get("nodes", [])) > policy.graph_node_limit:
            raise DomainBuildStageError(
                "domain_graph_limit_exceeded",
                "Constructed graph exceeds graph_node_limit.",
            )
        context.artifacts.publish_json(
            _NETWORK_WARNINGS_ARTIFACT,
            [_warning_document(item) for item in warnings[warning_start:]],
        )
        ref = context.artifacts.publish_json(_GRAPH_ARTIFACT, graph)
        return graph, ref

    def _packs(
        self,
        context: RunContext,
        graph: dict[str, Any],
        warnings: list[DomainBuildWarning],
    ) -> tuple[dict[str, Any], dict[str, Any], Any, Any]:
        paper_ref = context.artifacts.find(_PAPER_PACK_ARTIFACT)
        evidence_ref = context.artifacts.find(_EVIDENCE_PACK_ARTIFACT)
        if paper_ref is not None and evidence_ref is not None:
            _extend_stored_warnings(context, _PACK_WARNINGS_ARTIFACT, warnings)
            return (
                _read_json(context, paper_ref, "paper pack"),
                _read_json(context, evidence_ref, "evidence pack"),
                paper_ref,
                evidence_ref,
            )
        warning_start = len(warnings)
        node_ids = _unique_paper_ids(
            [node for node in graph.get("nodes", []) if isinstance(node, Mapping)]
        )
        acquired, errors = self._group_values(
            context,
            "packs-acquisition",
            node_ids,
            self.paper.acquire_pack_record,
            essential=False,
        )
        for paper_id, error in errors.items():
            warnings.append(
                DomainBuildWarning(
                    "paper_acquisition_failed",
                    error.message,
                    "paper_acquisition",
                    paper_id,
                )
            )
        for paper_id, record in acquired.items():
            for raw_warning in record.get("warnings", []) if isinstance(record, Mapping) else []:
                if not isinstance(raw_warning, Mapping):
                    continue
                warnings.append(
                    DomainBuildWarning(
                        str(raw_warning.get("code") or "paper_acquisition_warning"),
                        str(raw_warning.get("message") or raw_warning.get("code") or "paper acquisition warning"),
                        str(raw_warning.get("stage") or "paper_acquisition"),
                        str(raw_warning.get("paper_id") or paper_id),
                    )
                )
        packs = build_domain_packs(graph, acquired)
        context.artifacts.publish_json(
            _PACK_WARNINGS_ARTIFACT,
            [_warning_document(item) for item in warnings[warning_start:]],
        )
        if paper_ref is None:
            paper_ref = context.artifacts.publish_json(
                _PAPER_PACK_ARTIFACT, packs.paper_json_pack
            )
        if evidence_ref is None:
            evidence_ref = context.artifacts.publish_json(
                _EVIDENCE_PACK_ARTIFACT, packs.evidence_pack
            )
        return (
            packs.paper_json_pack,
            packs.evidence_pack,
            paper_ref,
            evidence_ref,
        )

    def _summary(
        self,
        context: RunContext,
        resume_input: Any,
        graph: dict[str, Any],
        evidence: dict[str, Any],
        selection: dict[str, Any],
        warnings: list[DomainBuildWarning],
    ) -> tuple[Any | None, Any | None] | Paused | Failed:
        summary_ref = context.artifacts.find(_SUMMARY_ARTIFACT)
        markdown_ref = context.artifacts.find(_SUMMARY_MARKDOWN_ARTIFACT)
        if summary_ref is not None and markdown_ref is not None:
            return summary_ref, markdown_ref
        unavailable = context.artifacts.find(_SUMMARY_UNAVAILABLE_ARTIFACT)
        if unavailable is not None:
            document = _read_json(context, unavailable, "summary warning")
            warnings.append(
                DomainBuildWarning(
                    str(document["code"]),
                    str(document["message"]),
                    "summary",
                )
            )
            return None, None

        request = LLMRequest(
            _task_id(
                "domain-summary-v2",
                domain_id_for(self.request.seed_paper, self.request.intent),
                self.request.intent,
            ),
            summary_prompt(
                graph,
                evidence,
                selection,
                intent=self.request.intent,
            ),
            JsonOutput(DOMAIN_SUMMARY_SCHEMA, repair="format"),
            self.request.model,
        )
        outcome = execute_routed(
            self.task_service, context, request, resume_input=resume_input
        )
        if isinstance(outcome, LLMCompleted):
            try:
                summary = normalize_summary_output(
                    outcome.value,
                    graph=graph,
                    evidence=evidence,
                    selection=selection,
                    intent=self.request.intent,
                )
            except ValueError as exc:
                return Failed(
                    RunError(
                        "domain_summary_invalid",
                        str(exc),
                        {"stage": "summary"},
                    )
                )
            summary_ref = context.artifacts.publish_json(_SUMMARY_ARTIFACT, summary)
            markdown_ref = context.artifacts.publish_bytes(
                _SUMMARY_MARKDOWN_ARTIFACT,
                render_summary_markdown(summary).encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
            )
            return summary_ref, markdown_ref
        if isinstance(outcome, LLMPaused):
            return Paused(awaiting_from_pause(outcome))
        if isinstance(outcome, LLMFailed):
            if not is_transient_failure(outcome):
                return Failed(run_error_from_failure(outcome))
            warning = DomainBuildWarning(
                "domain_summary_unavailable",
                str(outcome.error),
                "summary",
            )
            context.artifacts.publish_json(
                _SUMMARY_UNAVAILABLE_ARTIFACT,
                {"code": warning.code, "message": warning.message},
            )
            warnings.append(warning)
            return None, None
        if isinstance(outcome, LLMStopped):
            raise StoppedError("domain summary stopped")
        raise RuntimeError("unknown domain-summary outcome")

    def _group_values(
        self,
        context: RunContext,
        group_id: str,
        paper_ids: list[str],
        operation: Callable[[str], Any],
        *,
        essential: bool,
    ) -> tuple[dict[str, Any], dict[str, RunError]]:
        by_unit = {_unit_id(paper_id): paper_id for paper_id in paper_ids}
        units = tuple(
            WorkUnit(unit_id, {"paper_id": paper_id})
            for unit_id, paper_id in by_unit.items()
        )

        def worker(unit: WorkUnit):
            paper_id = by_unit[unit.unit_id]
            try:
                return operation(paper_id)
            except StoppedError:
                raise
            except Exception as exc:
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=RunError(
                        "paper_operation_failed",
                        f"{type(exc).__name__}: {exc}",
                        {"paper_id": paper_id},
                    ),
                )

        result = context.run_group(
            group_id,
            units,
            worker,
            max_workers=self.max_workers,
            failure_mode=(
                FailureMode.FAIL_FAST if essential else FailureMode.COLLECT
            ),
        )
        if isinstance(result, Paused):
            raise RuntimeError("paper acquisition group cannot pause")
        assert isinstance(result, GroupResult)
        values: dict[str, Any] = {}
        errors: dict[str, RunError] = {}
        completed = {item.unit_id for item in result.units}
        for item in result.units:
            paper_id = by_unit[item.unit_id]
            if item.status == "succeeded":
                values[paper_id] = item.value
            else:
                errors[paper_id] = item.error or RunError(
                    "paper_operation_failed", "paper operation failed"
                )
        missing = [by_unit[unit.unit_id] for unit in units if unit.unit_id not in completed]
        if essential and (errors or missing):
            first_id = next(iter(errors), missing[0] if missing else "")
            error = errors.get(first_id)
            raise DomainBuildStageError(
                "essential_paper_acquisition_failed",
                error.message if error is not None else f"Paper acquisition did not complete: {first_id}",
                {"paper_id": first_id},
            )
        return values, errors


class DomainBuildRunner:
    """Thin standalone wrapper over :class:`DomainBuildHandler`."""

    def __init__(self, repository: RunRepository) -> None:
        self.repository = repository
        self.engine = RunEngine(repository)

    def execute(
        self,
        request: DomainBuildRequest,
        *,
        run_id: str | None = None,
        paper_access: DomainPaperAccess | None = None,
        task_service: LLMTaskService | None = None,
        reference_service: ReferenceInferenceService | None = None,
        max_workers: int = 8,
    ) -> RunSnapshot:
        handler = DomainBuildHandler(
            request,
            paper_access=paper_access,
            task_service=task_service,
            reference_service=reference_service,
            max_workers=max_workers,
        )
        resolved_run_id = run_id or domain_build_run_id(request)
        spec = RunSpec(resolved_run_id, handler.name, handler.semantic_input())
        self.repository.create(spec)
        register_domain_run(
            self.repository,
            DomainPaths(self.repository.root),
            domain_id=domain_id_for(request.seed_paper, request.intent),
            run_id=resolved_run_id,
        )
        return self.engine.execute(spec, handler)

    def resume(
        self,
        run_id: str,
        *,
        input: Mapping[str, JsonValue] | None = None,
        paper_access: DomainPaperAccess | None = None,
        task_service: LLMTaskService | None = None,
        reference_service: ReferenceInferenceService | None = None,
        max_workers: int = 8,
    ) -> RunSnapshot:
        max_workers = validate_domain_build_workers(max_workers)
        request = decode_domain_build_request(
            self.repository.read_spec(run_id).semantic_input
        )
        handler = DomainBuildHandler(
            request,
            paper_access=paper_access,
            task_service=task_service,
            reference_service=reference_service,
            max_workers=max_workers,
        )
        return self.engine.resume(run_id, handler, input=input)


def domain_build_run_id(request: DomainBuildRequest) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(encode_domain_build_request(request))
    ).hexdigest()
    return f"domain-{digest[:24]}"


def _read_json(context: RunContext, ref: Any, description: str) -> dict[str, Any]:
    if ref.media_type != "application/json":
        raise DomainBuildStageError(
            "domain_artifact_media_type_invalid",
            f"{description} must use application/json.",
        )
    try:
        value = json.loads(context.artifacts.read_bytes(ref).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainBuildStageError(
            "domain_artifact_invalid", f"Cannot decode {description}: {exc}"
        ) from exc
    return _mapping(value, description)


def _mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainBuildStageError(
            "domain_output_invalid", f"{description} must be a JSON object."
        )
    return dict(value)


def _mapping_list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise DomainBuildStageError(
            "domain_artifact_invalid",
            f"{description} must be an array of JSON objects.",
        )
    return [dict(item) for item in value]


def _sample_foundation_witness_references(
    *,
    seed_references: list[dict[str, Any]],
    newest_citers: list[dict[str, Any]],
    seed_id: str,
    intent: str,
) -> list[dict[str, Any]]:
    """Deterministically fill the fixed foundation witness budget with references."""

    return deterministic_sample(
        [item for item in seed_references if paper_key(item)],
        count=max(0, _FOUNDATION_WITNESS_LIMIT - len(newest_citers)),
        seed=f"{seed_id}\n{intent}",
    )


def _unit_id(paper_id: str) -> str:
    digest = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:24]
    return f"paper-{digest}"


def _task_id(prefix: str, identity: str, intent: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes({"identity": identity, "intent": intent})
    ).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _unique_paper_ids(items: list[Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in items:
        paper_id = normalize_paper_id(paper_key(dict(item)))
        if paper_id and paper_id not in values:
            values.append(paper_id)
    return values


def _unique_paper_records(
    items: list[Mapping[str, Any]],
    *,
    excluded_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded = {
        normalize_paper_id(paper_id)
        for paper_id in (excluded_ids or set())
        if normalize_paper_id(paper_id)
    }
    records: list[dict[str, Any]] = []
    for item in items:
        paper_id = normalize_paper_id(paper_key(dict(item)))
        if not paper_id or paper_id in excluded:
            continue
        record = dict(item)
        record["paper_id"] = paper_id
        records.append(record)
        excluded.add(paper_id)
    return records


def _dedupe_warnings(
    warnings: list[DomainBuildWarning],
) -> list[DomainBuildWarning]:
    output: list[DomainBuildWarning] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for warning in warnings:
        key = (warning.code, warning.message, warning.stage, warning.paper_id)
        if key not in seen:
            seen.add(key)
            output.append(warning)
    return output


def _warning_document(warning: DomainBuildWarning) -> dict[str, JsonValue]:
    return {
        "code": warning.code,
        "message": warning.message,
        "stage": warning.stage,
        "paper_id": warning.paper_id,
    }


def _extend_stored_warnings(
    context: RunContext,
    artifact_id: str,
    warnings: list[DomainBuildWarning],
) -> None:
    ref = context.artifacts.find(artifact_id)
    if ref is None:
        raise DomainBuildStageError(
            "domain_stage_incomplete",
            f"Completed stage is missing its warnings artifact: {artifact_id}.",
        )
    document = _read_json_array(context, ref, f"{artifact_id} warnings")
    for item in document:
        value = _mapping(item, f"{artifact_id} warning")
        paper_id = value.get("paper_id")
        warnings.append(
            DomainBuildWarning(
                str(value.get("code") or ""),
                str(value.get("message") or ""),
                str(value.get("stage") or ""),
                None if paper_id is None else str(paper_id),
            )
        )


def _read_json_array(context: RunContext, ref: Any, description: str) -> list[Any]:
    if ref.media_type != "application/json":
        raise DomainBuildStageError(
            "domain_artifact_media_type_invalid",
            f"{description} must use application/json.",
        )
    try:
        value = json.loads(context.artifacts.read_bytes(ref).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainBuildStageError(
            "domain_artifact_invalid", f"Cannot decode {description}: {exc}"
        ) from exc
    if not isinstance(value, list):
        raise DomainBuildStageError(
            "domain_artifact_invalid", f"{description} must be a JSON array."
        )
    return value


def _append_group_warnings(
    warnings: list[DomainBuildWarning],
    errors: Mapping[str, RunError],
    *,
    code: str,
    stage: str,
) -> None:
    for paper_id, error in errors.items():
        warnings.append(DomainBuildWarning(code, error.message, stage, paper_id))


__all__ = [
    "DOMAIN_BUILD_HANDLER",
    "DomainBuildHandler",
    "DomainBuildRunner",
    "domain_build_run_id",
    "validate_domain_build_workers",
]
