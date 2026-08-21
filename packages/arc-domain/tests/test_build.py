from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from arc_domain.build import (
    DOMAIN_BUILD_HANDLER,
    DOMAIN_BUILD_SEMANTIC_SCHEMA_VERSION,
    DOMAIN_NETWORK_RENDER_RECIPE,
    DomainBuildHandler,
    DomainBuildRunner,
    domain_build_run_id,
    validate_domain_build_workers,
)
from arc_domain.contracts import (
    DomainBuildPolicy,
    DomainBuildRequest,
    decode_domain_build_result,
    encode_domain_build_request,
)
from ac_jobs import (
    EventWriter,
    ImmutableArtifactStore,
    ResumeReason,
    RunRepository,
    RunSpec,
    RunStatus,
)
from ac_llm import (
    FailureCategory,
    LLMCompleted,
    LLMFailed,
    LLMPaused,
    ProviderFailure,
)


SEED = "arXiv:2401.00001"
FOUNDATION = "arXiv:2001.00001"
DOMAIN_PAPER = "arXiv:2501.00001"
EXTRA_PARENT = "arXiv:1901.00001"
SECOND_CITER = "arXiv:2502.00002"


def _metadata(paper_id: str, *, title: str, year: int, citations: int) -> dict:
    return {
        "paper_id": paper_id,
        "title": title,
        "abstract": f"{title} methods",
        "authors": ["A. Author"],
        "year": year,
        "citation_count": citations,
        "identifiers": {"paper_id": paper_id},
    }


class FakePaperAccess:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.metadata_by_id = {
            SEED: _metadata(SEED, title="Seed", year=2024, citations=10),
            FOUNDATION: _metadata(
                FOUNDATION, title="Foundation", year=2020, citations=500
            ),
            DOMAIN_PAPER: _metadata(
                DOMAIN_PAPER, title="Recent method", year=2025, citations=20
            ),
        }
        self.references_by_id = {
            SEED: [self.metadata_by_id[FOUNDATION]],
            DOMAIN_PAPER: [],
        }

    def metadata(self, paper_id: str) -> dict:
        self.calls.append(("metadata", paper_id))
        return dict(self.metadata_by_id[paper_id])

    def references(self, paper_id: str) -> list[dict]:
        self.calls.append(("references", paper_id))
        return [dict(item) for item in self.references_by_id.get(paper_id, [])]

    def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict]:
        self.calls.append(("citers", paper_id, limit, sort))
        if paper_id == SEED:
            return [dict(self.metadata_by_id[DOMAIN_PAPER])]
        if paper_id == FOUNDATION:
            return [dict(self.metadata_by_id[DOMAIN_PAPER])]
        return []

    def acquire_pack_record(self, paper_id: str) -> dict:
        self.calls.append(("pack", paper_id))
        return {
            "metadata": dict(self.metadata_by_id[paper_id]),
            "references": self.references(paper_id),
            "toc": [],
            "conclusion": None,
            "warnings": [
                {
                    "code": "conclusion_section_unavailable",
                    "message": "No conclusion section.",
                    "stage": "paper_acquisition",
                    "paper_id": paper_id,
                }
            ],
        }


class DisjointCiterPaperAccess(FakePaperAccess):
    def __init__(self) -> None:
        super().__init__()
        self.metadata_by_id[DOMAIN_PAPER]["published"] = "2025-01-15"
        self.metadata_by_id[SECOND_CITER] = _metadata(
            SECOND_CITER,
            title="Independent recent method",
            year=2025,
            citations=5,
        )
        self.metadata_by_id[SECOND_CITER]["published"] = "2025-02-15"

    def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict]:
        self.calls.append(("citers", paper_id, limit, sort))
        if paper_id == SEED:
            return [dict(self.metadata_by_id[DOMAIN_PAPER])]
        if paper_id == FOUNDATION:
            selected = DOMAIN_PAPER if sort == "mostrecent" else SECOND_CITER
            return [dict(self.metadata_by_id[selected])]
        return []


class FakeTaskService:
    def __init__(self, *, pause_network_once: bool = False) -> None:
        self.pause_network_once = pause_network_once
        self.task_ids: list[str] = []

    def execute_or_resume(self, context, request, **kwargs):
        del context, kwargs
        self.task_ids.append(request.task_id)
        if request.task_id.startswith("foundation-audit-"):
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_foundation_candidate_audit.v1",
                    "candidate_set_sufficient": True,
                    "confidence": "complete",
                    "search_queries": [],
                    "citation_directions": [],
                    "reasoning": "candidate set is sufficient",
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("foundation-select-"):
            choice = {
                "paper_id": FOUNDATION,
                "title": "Foundation",
                "reason": "same-scope foundation",
            }
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_foundation_selection.v1",
                    "selected_foundation": choice,
                    "best_reference_paper": choice,
                    "parent_foundations": [],
                    "rejected_candidates": [],
                    "reasoning": "selected from supplied candidates",
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("network-rank-"):
            if self.pause_network_once:
                self.pause_network_once = False
                return LLMPaused(
                    ResumeReason.EXTERNAL_CONDITION,
                    "network-resume",
                    input_required=False,
                )
            return LLMCompleted(
                {
                    "ranked_paper_ids": [DOMAIN_PAPER],
                    "reasoning": "matches intent",
                },
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("domain-summary-"):
            choice = {
                "paper_id": FOUNDATION,
                "title": "Foundation",
                "reason": "same-scope foundation",
            }
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_summary.v5",
                    "domain_title": "Recent methods",
                    "brief_introduction": "A compact introduction.",
                    "task_focus": {
                        "user_intent": "recent methods",
                        "research_scope": "The supplied papers.",
                        "priority_rules": ["Satisfy the user intent first."],
                    },
                    "foundation_paper": choice,
                    "best_reference_paper": choice,
                    "methodology": [],
                    "mathematical_opportunities": {
                        "well_defined_problems": []
                    },
                    "known_solved_cases": [],
                    "open_axes_for_new_work": [],
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        raise AssertionError(f"unexpected task: {request.task_id}")


class NoReferenceInference:
    def infer(self, *args, **kwargs):
        raise AssertionError("candidate expansion should not run")


class NetworkFailureTaskService(FakeTaskService):
    def __init__(self, category: FailureCategory) -> None:
        super().__init__()
        self.category = category

    def execute_or_resume(self, context, request, **kwargs):
        if request.task_id.startswith("network-rank-"):
            self.task_ids.append(request.task_id)
            return LLMFailed(
                ProviderFailure(
                    "network ranking failed",
                    category=self.category,
                )
            )
        return super().execute_or_resume(context, request, **kwargs)


class FoundationSelectionPauseService(FakeTaskService):
    def __init__(self) -> None:
        super().__init__()
        self.pause_selection_once = True

    def execute_or_resume(self, context, request, **kwargs):
        if (
            request.task_id.startswith("foundation-select-")
            and self.pause_selection_once
        ):
            self.pause_selection_once = False
            self.task_ids.append(request.task_id)
            return LLMPaused(
                ResumeReason.EXTERNAL_CONDITION,
                "foundation-selection-resume",
                input_required=False,
            )
        return super().execute_or_resume(context, request, **kwargs)


class SummaryPauseService(FakeTaskService):
    def __init__(self) -> None:
        super().__init__()
        self.pause_summary_once = True

    def execute_or_resume(self, context, request, **kwargs):
        if request.task_id.startswith("domain-summary-") and self.pause_summary_once:
            self.pause_summary_once = False
            self.task_ids.append(request.task_id)
            return LLMPaused(
                ResumeReason.EXTERNAL_CONDITION,
                "summary-resume",
                input_required=False,
            )
        return super().execute_or_resume(context, request, **kwargs)


class ParentPaperAccess(FakePaperAccess):
    def __init__(self) -> None:
        super().__init__()
        self.metadata_by_id[EXTRA_PARENT] = _metadata(
            EXTRA_PARENT, title="Older parent", year=2019, citations=900
        )
        self.references_by_id[SEED] = [
            self.metadata_by_id[FOUNDATION],
            self.metadata_by_id[EXTRA_PARENT],
        ]


class ParentSelectionTaskService(FakeTaskService):
    def execute_or_resume(self, context, request, **kwargs):
        if request.task_id.startswith("domain-summary-"):
            choice = {
                "paper_id": SEED,
                "title": "Seed",
                "reason": "same-scope foundation",
            }
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_summary.v5",
                    "domain_title": "Recent methods",
                    "brief_introduction": "A compact introduction.",
                    "task_focus": {
                        "user_intent": "recent methods",
                        "research_scope": "The supplied papers.",
                        "priority_rules": ["Satisfy the user intent first."],
                    },
                    "foundation_paper": choice,
                    "best_reference_paper": choice,
                    "methodology": [],
                    "mathematical_opportunities": {
                        "well_defined_problems": []
                    },
                    "known_solved_cases": [],
                    "open_axes_for_new_work": [],
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("foundation-select-"):
            self.task_ids.append(request.task_id)
            selected = {
                "paper_id": SEED,
                "title": "Seed",
                "reason": "same-scope foundation",
            }
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_foundation_selection.v1",
                    "selected_foundation": selected,
                    "best_reference_paper": selected,
                    "parent_foundations": [
                        {
                            "paper_id": FOUNDATION,
                            "title": "Foundation",
                            "reason": "parent",
                        },
                        {
                            "paper_id": EXTRA_PARENT,
                            "title": "Older parent",
                            "reason": "parent",
                        },
                    ],
                    "rejected_candidates": [],
                    "reasoning": "choose seed with two parents",
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        return super().execute_or_resume(context, request, **kwargs)


class OverlappingParentPaperAccess(ParentPaperAccess):
    def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict]:
        self.calls.append(("citers", paper_id, limit, sort))
        if paper_id == SEED:
            return [
                dict(self.metadata_by_id[FOUNDATION]),
                dict(self.metadata_by_id[DOMAIN_PAPER]),
            ]
        return []


class OverlappingParentTaskService(ParentSelectionTaskService):
    def execute_or_resume(self, context, request, **kwargs):
        if request.task_id.startswith("network-rank-"):
            self.task_ids.append(request.task_id)
            return LLMCompleted(
                {
                    "ranked_paper_ids": [FOUNDATION, DOMAIN_PAPER],
                    "reasoning": "parent appears first in the citer ranking",
                },
                None,
                None,
                None,
                None,
            )
        return super().execute_or_resume(context, request, **kwargs)


def _request() -> DomainBuildRequest:
    return DomainBuildRequest(
        SEED,
        "recent methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            recent_window_days=365,
            citer_pool_limit=10,
            ranked_paper_limit=2,
            graph_node_limit=5,
        ),
    )


def test_worker_count_is_bounded_operational_policy_not_content_identity() -> None:
    request = _request()
    paper_access = FakePaperAccess()
    task_service = FakeTaskService()
    reference_service = NoReferenceInference()

    single_worker = DomainBuildHandler(
        request,
        paper_access=paper_access,
        task_service=task_service,
        reference_service=reference_service,
        max_workers=1,
    )
    maximum_workers = DomainBuildHandler(
        request,
        paper_access=paper_access,
        task_service=task_service,
        reference_service=reference_service,
        max_workers=24,
    )

    assert single_worker.max_workers == 1
    assert maximum_workers.max_workers == 24
    assert single_worker.semantic_input() == maximum_workers.semantic_input()
    assert "workers" not in single_worker.semantic_input()
    assert {
        domain_build_run_id(handler.request)
        for handler in (single_worker, maximum_workers)
    } == {domain_build_run_id(request)}


def test_domain_build_semantic_input_is_closed_and_unwrapped_resume_is_rejected(
    tmp_path: Path,
) -> None:
    request = _request()
    handler = DomainBuildHandler(
        request,
        paper_access=FakePaperAccess(),
        task_service=FakeTaskService(),
        reference_service=NoReferenceInference(),
    )

    assert handler.semantic_input() == {
        "schema_version": DOMAIN_BUILD_SEMANTIC_SCHEMA_VERSION,
        "request": encode_domain_build_request(request),
        "network_render_recipe": DOMAIN_NETWORK_RENDER_RECIPE,
    }
    assert handler.name == DOMAIN_BUILD_HANDLER == "arc.domain.build.v2"

    repository = RunRepository(tmp_path / "runs")
    repository.create(
        RunSpec(
            "unwrapped-request",
            handler.name,
            encode_domain_build_request(request),
        )
    )
    with pytest.raises(
        ValueError,
        match="must contain exactly schema_version",
    ):
        DomainBuildRunner(repository).resume("unwrapped-request")

    repository.create(
        RunSpec(
            "v1-request",
            handler.name,
            {
                "schema_version": "arc.domain_build_semantic.v1",
                "request": encode_domain_build_request(request),
                "network_render_recipe": "arc.domain.network_html.v1",
            },
        )
    )
    with pytest.raises(
        ValueError,
        match="schema_version must be arc.domain_build_semantic.v2",
    ):
        DomainBuildRunner(repository).resume("v1-request")


@pytest.mark.parametrize("value", [True, False, -1, 0, 25, 1.5, "8", None])
def test_worker_count_validator_rejects_non_integer_or_out_of_range_values(
    value: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="domain build workers must be an integer between 1 and 24",
    ):
        validate_domain_build_workers(value)


@pytest.mark.parametrize("operation", ["execute", "resume"])
def test_runner_rejects_invalid_workers_before_durable_run_access(
    operation: str,
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    runner = DomainBuildRunner(repository)
    run_id = "invalid-workers" if operation == "execute" else "missing-run"

    with pytest.raises(
        ValueError,
        match="domain build workers must be an integer between 1 and 24",
    ):
        if operation == "execute":
            runner.execute(_request(), run_id=run_id, max_workers=25)
        else:
            runner.resume(run_id, max_workers=25)

    assert not repository.run_directory(run_id).exists()


def _decode_result(repository: RunRepository, run_id: str):
    snapshot = repository.inspect(run_id).snapshot
    assert snapshot.result_ref is not None
    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    return decode_domain_build_result(
        json.loads(store.read_bytes(snapshot.result_ref).decode("utf-8"))
    )


def test_complete_build_is_durable_bounded_and_requires_summary(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    paper = FakePaperAccess()
    task_service = FakeTaskService()
    request = _request()

    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
        max_workers=2,
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert snapshot.run_id == domain_build_run_id(request)
    assert "workers" not in repository.read_spec(snapshot.run_id).semantic_input
    result = _decode_result(repository, snapshot.run_id)
    assert result.summary is not None
    assert result.summary_markdown is not None
    assert {warning.code for warning in result.warnings} == {
        "conclusion_section_unavailable",
    }
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    assert graph["schema_version"] == "arc.domain_graph.v2"
    assert len(graph["nodes"]) <= request.policy.graph_node_limit
    assert {node["role"] for node in graph["nodes"]} == {
        "selected_foundation",
        "domain_paper",
    }
    assert ("citers", FOUNDATION, 10, "mostrecent") in paper.calls
    assert ("citers", FOUNDATION, 10, "mostcited") in paper.calls
    events = EventWriter(
        repository.run_directory(snapshot.run_id) / "events.jsonl",
        run_id=snapshot.run_id,
    ).tail()
    stages = {
        event["data"]["stage"]
        for event in events
        if event.get("event") == "domain_operation_started"
    }
    assert stages == {
        "foundation",
        "network",
        "paper_acquisition",
        "summary",
        "render",
        "finalize",
    }


def test_representative_recency_stats_cover_union_before_pool_limit(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    base = _request()
    request = replace(
        base,
        policy=replace(
            base.policy,
            citer_pool_limit=1,
            ranked_paper_limit=1,
        ),
    )

    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=DisjointCiterPaperAccess(),
        task_service=FakeTaskService(),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id),
        repository_root=repository.root,
    )
    network_input_ref = store.find("network/input")
    assert network_input_ref is not None
    network_input = json.loads(
        store.read_bytes(network_input_ref).decode("utf-8")
    )
    assert len(network_input["citer_pool"]) == 1
    assert network_input["recency_stats"]["unique_citers"] == 2
    assert network_input["recency_stats"]["exact_date_citers"] == 0
    assert network_input["recency_stats"]["reduced_precision_date_citers"] == 2


def test_paused_network_replays_completed_foundation_and_resumes_same_run(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    paper = FakePaperAccess()
    task_service = FakeTaskService(pause_network_once=True)
    runner = DomainBuildRunner(repository)
    request = _request()

    paused = runner.execute(
        request,
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
    )
    assert paused.status is RunStatus.PAUSED
    foundation_calls = list(task_service.task_ids)

    resumed = runner.resume(
        paused.run_id,
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert sum(
        task_id.startswith("foundation-audit-") for task_id in task_service.task_ids
    ) == 1
    assert sum(
        task_id.startswith("foundation-select-") for task_id in task_service.task_ids
    ) == 1
    assert foundation_calls[-1].startswith("network-rank-")
    assert paper.calls.count(("citers", FOUNDATION, 10, "mostrecent")) == 1
    assert paper.calls.count(("citers", FOUNDATION, 10, "mostcited")) == 1
    assert _decode_result(repository, resumed.run_id).graph is not None


def test_network_transport_failure_uses_deterministic_ranking(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=NetworkFailureTaskService(FailureCategory.TRANSPORT),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    result = _decode_result(repository, snapshot.run_id)
    assert "intent_ranking_unavailable" in {
        warning.code for warning in result.warnings
    }


def test_foundation_group_units_replay_after_selection_pause(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    paper = FakePaperAccess()
    task_service = FoundationSelectionPauseService()
    runner = DomainBuildRunner(repository)

    paused = runner.execute(
        _request(),
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
    )
    assert paused.status is RunStatus.PAUSED
    assert paper.calls.count(("references", DOMAIN_PAPER)) == 1

    resumed = runner.resume(
        paused.run_id,
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
    )

    assert resumed.status is RunStatus.SUCCEEDED
    # One foundation-witness call, one network call, and one pack call. The
    # completed foundation group itself was replayed instead of executing a
    # fourth call.
    assert paper.calls.count(("references", DOMAIN_PAPER)) == 3
    assert paper.calls.count(("references", SEED)) == 1


def test_pack_warnings_replay_after_summary_pause(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    runner = DomainBuildRunner(repository)
    paper = FakePaperAccess()
    task_service = SummaryPauseService()

    paused = runner.execute(
        _request(),
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
    )
    assert paused.status is RunStatus.PAUSED

    resumed = runner.resume(
        paused.run_id,
        paper_access=paper,
        task_service=task_service,
        reference_service=NoReferenceInference(),
    )

    assert resumed.status is RunStatus.SUCCEEDED
    codes = [warning.code for warning in _decode_result(repository, resumed.run_id).warnings]
    assert codes.count("conclusion_section_unavailable") == 2
    assert "domain_summary_unavailable" not in codes


def test_parent_foundations_are_truncated_to_the_graph_capacity(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    request = DomainBuildRequest(
        SEED,
        "recent methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            citer_pool_limit=10,
            ranked_paper_limit=1,
            graph_node_limit=2,
        ),
    )
    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=ParentPaperAccess(),
        task_service=ParentSelectionTaskService(),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    result = _decode_result(repository, snapshot.run_id)
    assert "parent_foundations_truncated" in {
        warning.code for warning in result.warnings
    }
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    assert len(graph["nodes"]) == 2


def test_parent_overlap_is_excluded_before_domain_selection_and_capacity_fill(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    request = DomainBuildRequest(
        SEED,
        "recent methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            citer_pool_limit=10,
            ranked_paper_limit=1,
            graph_node_limit=4,
        ),
    )

    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=OverlappingParentPaperAccess(),
        task_service=OverlappingParentTaskService(),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    result = _decode_result(repository, snapshot.run_id)
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    roles = {node["paper_id"]: node["role"] for node in graph["nodes"]}
    assert roles == {
        SEED: "selected_foundation",
        FOUNDATION: "parent_foundation",
        EXTRA_PARENT: "parent_foundation",
        DOMAIN_PAPER: "domain_paper",
    }
    assert len(graph["nodes"]) == request.policy.graph_node_limit


def test_network_schema_failure_fails_the_outer_run(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs-root")
    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=NetworkFailureTaskService(FailureCategory.SCHEMA),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "invalid_schema"
