from __future__ import annotations

from collections import deque
import json
from pathlib import Path

import pytest

from arc_domain.build import (
    DomainBuildHandler,
    DomainBuildRunner,
    _task_id,
    domain_build_run_id,
)
from arc_domain.contracts import (
    DomainBuildPolicy,
    DomainBuildRequest,
    decode_domain_build_result,
)
from arc_domain.package_view import DomainPackageValidationError
from arc_domain.paths import domain_id_for
from arc_jobs import (
    ImmutableArtifactStore,
    ResumeReason,
    RunContext,
    RunRepository,
    RunSpec,
    RunStatus,
)
from arc_llm import (
    FailureCategory,
    HostAuthority,
    IsolationMode,
    JsonOutput,
    LLMStopped,
    LLMCompleted,
    LLMExecutionOptions,
    LLMFailed,
    LLMRequest,
    LLMTaskService,
    ProviderCapabilities,
    ProviderDiagnostic,
    ProviderExecution,
    ProviderFailure,
    ProviderRegistry,
    ProviderTerminalKind,
    StructuredOutputMode,
    UsageAvailability,
)
from arc_llm.output import CandidateMaterial
from arc_paper import ReferenceInferenceCompleted, ReferenceInferenceResult


SEED = "arXiv:2401.00001"
FOUNDATION = "arXiv:2001.00001"
DOMAIN_PAPER = "arXiv:2501.00001"
INFERRED_FOUNDATION = "arXiv:1901.00001"


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


def _request() -> DomainBuildRequest:
    return DomainBuildRequest(
        SEED,
        "recent methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            recent_window_days=365,
            citer_pool_limit=10,
            ranked_paper_limit=1,
            graph_node_limit=5,
        ),
    )


def _request_with_modes(
    *, fixed_seed: bool = False, strict_window: bool = False
) -> DomainBuildRequest:
    return DomainBuildRequest(
        SEED,
        "recent methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            recent_window_days=730,
            citer_pool_limit=10,
            ranked_paper_limit=1,
            graph_node_limit=5,
            foundation_mode="fixed_seed" if fixed_seed else "infer_from_seed",
            citer_selection_mode=(
                "strict_window" if strict_window else "representative_plus_recent"
            ),
        ),
    )


def _failure(
    category: FailureCategory,
    *,
    details: dict | None = None,
) -> LLMFailed:
    return LLMFailed(
        ProviderFailure(
            "fake provider failure",
            category=category,
            details=details,
        )
    )


def _completed(value: dict) -> LLMCompleted:
    return LLMCompleted(value, None, None, None, None)


def _audit(*, expand: bool = False) -> dict:
    return {
        "schema_version": "arc.domain_foundation_candidate_audit.v1",
        "candidate_set_sufficient": not expand,
        "confidence": "complete",
        "search_queries": (
            [
                {
                    "query": "canonical scope foundation",
                    "reason": "candidate gap",
                    "confidence": "complete",
                }
            ]
            if expand
            else []
        ),
        "citation_directions": [],
        "reasoning": "fake audit",
        "warnings": [],
    }


def _selection(paper_id: str) -> dict:
    choice = {
        "paper_id": paper_id,
        "title": "Selected foundation",
        "reason": "fake selection",
    }
    return {
        "schema_version": "arc.domain_foundation_selection.v1",
        "selected_foundation": choice,
        "best_reference_paper": choice,
        "parent_foundations": [],
        "rejected_candidates": [],
        "reasoning": "fake selection",
        "warnings": [],
    }


def _summary_payload(*, user_intent: str) -> dict:
    choice = {
        "paper_id": FOUNDATION,
        "title": "Foundation",
        "reason": "fake selection",
    }
    return {
        "schema_version": "arc.domain_summary.v5",
        "domain_title": "Example domain",
        "brief_introduction": "A compact introduction.",
        "task_focus": {
            "user_intent": user_intent,
            "research_scope": "The supplied papers.",
            "priority_rules": ["Satisfy the user intent first."],
        },
        "foundation_paper": choice,
        "best_reference_paper": choice,
        "methodology": [],
        "mathematical_opportunities": {"well_defined_problems": []},
        "known_solved_cases": [],
        "open_axes_for_new_work": [],
        "warnings": [],
    }


def _request_stage(task_id: str) -> str:
    for prefix, stage in (
        ("foundation-audit-", "audit"),
        ("foundation-select-", "selection"),
        ("network-rank-", "ranking"),
        ("domain-summary-v3-", "summary"),
    ):
        if task_id.startswith(prefix):
            return stage
    raise AssertionError(f"unexpected task {task_id}")


class FakePaperAccess:
    def __init__(self) -> None:
        self.metadata_by_id = {
            SEED: _metadata(SEED, title="Seed", year=2024, citations=10),
            FOUNDATION: _metadata(
                FOUNDATION, title="Foundation", year=2020, citations=500
            ),
            DOMAIN_PAPER: _metadata(
                DOMAIN_PAPER, title="Recent method", year=2025, citations=20
            ),
            INFERRED_FOUNDATION: _metadata(
                INFERRED_FOUNDATION,
                title="Verified inferred foundation",
                year=2019,
                citations=900,
            ),
        }

    def metadata(self, paper_id: str) -> dict:
        return dict(self.metadata_by_id[paper_id])

    def references(self, paper_id: str) -> list[dict]:
        if paper_id == SEED:
            return [dict(self.metadata_by_id[FOUNDATION])]
        return []

    def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict]:
        del limit, sort
        if paper_id in {SEED, FOUNDATION, INFERRED_FOUNDATION}:
            return [dict(self.metadata_by_id[DOMAIN_PAPER])]
        return []

    def acquire_pack_record(self, paper_id: str) -> dict:
        return {
            "metadata": self.metadata(paper_id),
            "references": self.references(paper_id),
            "toc": [],
            "conclusion": None,
            "warnings": [],
        }


class DomainTaskService:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        expand_audit: bool = False,
        selected_foundation: str = FOUNDATION,
        stopped_stage: str | None = None,
        summary_value: dict | None = None,
        summary_retry_value: dict | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.expand_audit = expand_audit
        self.selected_foundation = selected_foundation
        self.stopped_stage = stopped_stage
        self.summary_value = summary_value
        self.summary_retry_value = summary_retry_value
        self.requests = []

    def execute_or_resume(self, context, request, **kwargs):
        del kwargs
        self.requests.append(request)
        stage = _request_stage(request.task_id)

        if stage == self.stopped_stage:
            return LLMStopped()
        if stage == self.fail_stage:
            return _failure(FailureCategory.TRANSPORT)
        if stage == "audit":
            return _completed(_audit(expand=self.expand_audit))
        if stage == "selection":
            return _completed(_selection(self.selected_foundation))
        if stage == "ranking":
            return _completed(
                {
                    "schema_version": "arc.domain_intent_ranking.v1",
                    "ranked_paper_ids": [DOMAIN_PAPER],
                    "reasoning": "fake ranking",
                }
            )
        if stage == "summary":
            payload = (
                self.summary_retry_value
                if (
                    "semantic-retry" in request.task_id
                    and self.summary_retry_value is not None
                )
                else self.summary_value
                if self.summary_value is not None
                else _summary_payload(user_intent=_request().intent)
            )
            if self.summary_value is None:
                selection_input = next(
                    item
                    for item in request.inputs
                    if item.input_id == "foundation-selection"
                )
                selection = json.loads(
                    context.artifacts.read_source(
                        selection_input.source
                    ).content.decode("utf-8")
                )
                choice = dict(selection["selected_foundation"])
                payload["foundation_paper"] = choice
                payload["best_reference_paper"] = dict(
                    selection["best_reference_paper"]
                )
            return _completed(payload)
        raise AssertionError(f"unhandled stage {stage}")


class TransientSummaryOnceService(DomainTaskService):
    def __init__(self) -> None:
        super().__init__()
        self.summary_attempts = 0

    def execute_or_resume(self, context, request, **kwargs):
        if _request_stage(request.task_id) == "summary":
            self.summary_attempts += 1
            if self.summary_attempts == 1:
                self.requests.append(request)
                return _failure(
                    FailureCategory.TIMEOUT,
                    details={
                        "provider_failure": {
                            "category": "timeout",
                            "diagnostic_artifact_id": (
                                "diagnostics/provider-failure"
                            ),
                        }
                    },
                )
        return super().execute_or_resume(context, request, **kwargs)


class ScriptedDomainProvider:
    name = "codex"
    compatibility_version = "domain-upgrade-test-v1"

    def __init__(self, values: list[dict]) -> None:
        self.values = deque(values)
        self.start_calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_resume=False,
            structured_output=StructuredOutputMode.NATIVE,
            usage=UsageAvailability.UNAVAILABLE,
            config_isolation=IsolationMode.ISOLATED,
            tool_isolation=IsolationMode.ISOLATED,
            cooperative_stop=True,
            provider_persistence=False,
        )

    def doctor(self) -> ProviderDiagnostic:
        return ProviderDiagnostic(self.name, True, "fake-codex")

    def start(self, request, observer, stop) -> ProviderExecution:
        del request, stop
        self.start_calls += 1
        if not self.values:
            raise AssertionError("fake provider script exhausted")
        return ProviderExecution(
            ProviderTerminalKind.COMPLETED,
            (CandidateMaterial(value=self.values.popleft(), terminal=True),),
        )

    def resume(self, handle, request, observer, stop) -> ProviderExecution:
        del handle
        return self.start(request, observer, stop)


class ForbiddenReferenceService:
    def infer(self, *args, **kwargs):
        raise AssertionError("candidate expansion should not run")


class CompletedReferenceService:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def infer(self, context, text: str, **kwargs):
        del context, kwargs
        self.requests.append(text)
        return ReferenceInferenceCompleted(
            ReferenceInferenceResult(
                request_digest="0" * 64,
                paper_ids=(INFERRED_FOUNDATION,),
                focus_scope="one_domain",
                warnings=(),
                verified_references=(
                    {
                        "paper_id": INFERRED_FOUNDATION,
                        "evidence_urls": ["https://example.test/foundation"],
                        "reasoning": "verified foundation",
                    },
                ),
                rejected_candidates=(),
                provenance=None,
            )
        )


class ManyCompletedReferencesService:
    def __init__(self, paper_ids: tuple[str, ...]) -> None:
        self.paper_ids = paper_ids

    def infer(self, context, text: str, **kwargs):
        del context, text, kwargs
        return ReferenceInferenceCompleted(
            ReferenceInferenceResult(
                request_digest="1" * 64,
                paper_ids=self.paper_ids,
                focus_scope="more_than_two_domains",
                warnings=(),
                verified_references=tuple(
                    {
                        "paper_id": paper_id,
                        "evidence_urls": [
                            f"https://example.test/{index}"
                        ],
                        "reasoning": "verified foundation",
                    }
                    for index, paper_id in enumerate(
                        self.paper_ids, start=1
                    )
                ),
                rejected_candidates=(),
                provenance=None,
            )
        )


class FailingReferenceService:
    def infer(self, *args, **kwargs):
        return _failure(FailureCategory.TRANSPORT)


def _result(repository: RunRepository, snapshot):
    assert snapshot.result_ref is not None
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    return decode_domain_build_result(
        json.loads(store.read_bytes(snapshot.result_ref).decode("utf-8"))
    ), store


def _warning_codes(repository: RunRepository, snapshot) -> set[str]:
    result, _ = _result(repository, snapshot)
    return {warning.code for warning in result.warnings}


def test_all_domain_llm_requests_use_formatter_json_repair(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs")
    service = DomainTaskService()

    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=service,
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert {_request_stage(request.task_id) for request in service.requests} == {
        "audit",
        "selection",
        "ranking",
        "summary",
    }
    assert all(isinstance(request.output, JsonOutput) for request in service.requests)
    assert {request.output.repair for request in service.requests} == {"format"}


def test_successful_summary_binds_request_intent_and_replays_without_llm(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    request = _request()
    model_payload = _summary_payload(user_intent="model-altered intent")
    service = DomainTaskService(summary_value=model_payload)
    live_events = []

    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=FakePaperAccess(),
        task_service=service,
        reference_service=ForbiddenReferenceService(),
        event_sink=live_events.append,
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert live_events
    summary_request = next(
        item for item in service.requests if item.task_id.startswith("domain-summary-v3-")
    )
    task_identity = domain_id_for(request.seed_paper, request.intent)
    assert summary_request.task_id == _task_id(
        "domain-summary-v3", task_identity, request.intent
    )
    assert summary_request.task_id != _task_id(
        "domain-summary", task_identity, request.intent
    )
    assert request.intent in summary_request.prompt
    assert "foundation_selection.intent" not in summary_request.prompt
    assert [item.input_id for item in summary_request.inputs] == [
        "domain-graph",
        "foundation-selection",
        "evidence-pack",
        "paper-pack",
    ]
    result, store = _result(repository, snapshot)
    assert result.summary is not None
    assert result.summary_markdown is not None
    published_summary = json.loads(store.read_bytes(result.summary).decode("utf-8"))
    published_markdown = store.read_bytes(result.summary_markdown).decode("utf-8")
    assert published_summary["task_focus"]["user_intent"] == request.intent
    assert f"- User intent: {request.intent}" in published_markdown
    assert model_payload["task_focus"]["user_intent"] == "model-altered intent"

    replay_service = DomainTaskService(
        summary_value=_summary_payload(user_intent="must not be used")
    )
    replayed_events = []
    replayed = DomainBuildRunner(repository).execute(
        request,
        paper_access=FakePaperAccess(),
        task_service=replay_service,
        reference_service=ForbiddenReferenceService(),
        event_sink=replayed_events.append,
    )

    assert replayed.status is RunStatus.SUCCEEDED
    assert replayed_events == []
    assert replay_service.requests == []
    replayed_result, _ = _result(repository, replayed)
    assert replayed_result.summary == result.summary
    assert replayed_result.summary_markdown == result.summary_markdown


def test_transient_summary_failure_pauses_then_retries_without_marker(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    runner = DomainBuildRunner(repository)
    service = TransientSummaryOnceService()
    initial_events = []

    paused = runner.execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=service,
        reference_service=ForbiddenReferenceService(),
        event_sink=initial_events.append,
    )

    assert paused.status is RunStatus.PAUSED
    assert initial_events
    assert paused.awaiting is not None
    assert paused.awaiting.reason is ResumeReason.EXTERNAL_CONDITION
    assert paused.awaiting.input_required is False
    assert paused.awaiting.details["llm_error"] == {
        "code": "provider_timeout",
        "details": {
            "provider_failure": {
                "category": "timeout",
                "diagnostic_artifact_id": (
                    "diagnostics/provider-failure"
                ),
            }
        },
    }
    store = ImmutableArtifactStore(
        repository.run_directory(paused.run_id), repository_root=repository.root
    )
    assert store.find("summary/unavailable") is None
    assert store.find("summary/json") is None

    resumed_events = []
    completed = runner.resume(
        paused.run_id,
        paper_access=FakePaperAccess(),
        task_service=service,
        reference_service=ForbiddenReferenceService(),
        event_sink=resumed_events.append,
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert resumed_events
    result, store = _result(repository, completed)
    assert result.summary is not None
    assert result.summary_markdown is not None
    assert store.find("summary/unavailable") is None
    assert service.summary_attempts == 2


def test_real_task_service_ignores_unrelated_summary_state_and_replays_parent(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    request = _request()
    adapter = ScriptedDomainProvider(
        [
            {"stale": True},
            _audit(),
            _selection(FOUNDATION),
            {
                "ranked_paper_ids": [DOMAIN_PAPER],
                "reasoning": "fake ranking",
            },
            _summary_payload(user_intent=request.intent),
        ]
    )
    registry = ProviderRegistry()
    registry.register("codex", lambda: adapter)
    task_service = LLMTaskService(registry=registry)
    handler = DomainBuildHandler(
        request,
        paper_access=FakePaperAccess(),
        task_service=task_service,
        reference_service=ForbiddenReferenceService(),
        llm=LLMExecutionOptions(host_authority=HostAuthority.UNRESTRICTED),
    )
    run_id = domain_build_run_id(request)
    snapshot = repository.create(
        RunSpec(run_id, handler.name, handler.semantic_input())
    )
    context = RunContext(repository, snapshot, resume_input=None)
    task_identity = domain_id_for(request.seed_paper, request.intent)
    unrelated_task_id = _task_id(
        "domain-summary", task_identity, request.intent
    )
    unrelated = task_service.execute(
        context,
        LLMRequest(
            unrelated_task_id,
            "Return the stale marker.",
            JsonOutput(
                {
                    "type": "object",
                    "properties": {"stale": {"type": "boolean"}},
                    "required": ["stale"],
                    "additionalProperties": False,
                }
            ),
            request.model,
        ),
        options=LLMExecutionOptions(host_authority=HostAuthority.UNRESTRICTED),
    )
    assert isinstance(unrelated, LLMCompleted)

    completed = DomainBuildRunner(repository).execute(
        request,
        paper_access=FakePaperAccess(),
        task_service=task_service,
        reference_service=ForbiddenReferenceService(),
        llm=LLMExecutionOptions(host_authority=HostAuthority.UNRESTRICTED),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert adapter.start_calls == 5
    result, _ = _result(repository, completed)
    assert result.summary is not None

    replayed = DomainBuildRunner(repository).execute(
        request,
        paper_access=FakePaperAccess(),
        task_service=task_service,
        reference_service=ForbiddenReferenceService(),
    )

    assert replayed.status is RunStatus.SUCCEEDED
    assert replayed.result_ref == completed.result_ref
    assert adapter.start_calls == 5


def test_invalid_summary_provenance_retries_once_then_pauses_with_candidate(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    payload = _summary_payload(user_intent=_request().intent)
    payload["methodology"] = [
        {
            "claim": "Unsupported method attribution.",
            "papers": ["doi:10.9999/not-in-domain"],
        }
    ]

    tasks = DomainTaskService(summary_value=payload)
    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=tasks,
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.PAUSED
    assert snapshot.awaiting is not None
    assert (
        snapshot.awaiting.details["code"]
        == "domain_summary_semantic_invalid_after_retry"
    )
    assert snapshot.awaiting.details["stage"] == "summary"
    candidate_path = Path(
        str(snapshot.awaiting.details["candidate_path"])
    )
    assert candidate_path.is_file()
    summary_requests = [
        request.task_id
        for request in tasks.requests
        if _request_stage(request.task_id) == "summary"
    ]
    assert len(summary_requests) == 2
    assert "semantic-retry" in summary_requests[1]
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    assert snapshot.awaiting.request_ref is not None
    diagnostic = json.loads(
        store.read_bytes(snapshot.awaiting.request_ref)
    )
    assert "$.methodology[0].papers[0]" in diagnostic["retry_error"]
    assert diagnostic["initial_candidate_artifact_id"]
    assert store.find("summary/json") is None
    assert store.find("summary/markdown") is None


def test_invalid_summary_provenance_fresh_retry_can_complete(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    invalid = _summary_payload(user_intent=_request().intent)
    invalid["methodology"] = [
        {
            "claim": "Unsupported method attribution.",
            "papers": ["doi:10.9999/not-in-domain"],
        }
    ]
    tasks = DomainTaskService(
        summary_value=invalid,
        summary_retry_value=_summary_payload(
            user_intent=_request().intent
        ),
    )

    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=tasks,
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    summary_requests = [
        request
        for request in tasks.requests
        if _request_stage(request.task_id) == "summary"
    ]
    assert len(summary_requests) == 2
    assert "semantic-retry" in summary_requests[1].task_id
    assert "Validation feedback:" in summary_requests[1].prompt
    candidate_path = (
        repository.run_directory(snapshot.run_id)
        / "working"
        / "candidates"
        / "domain"
        / "summary.json"
    )
    assert json.loads(candidate_path.read_text())["methodology"] == []


def test_edited_summary_candidate_recovers_without_another_provider_call(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    invalid = _summary_payload(user_intent=_request().intent)
    invalid["methodology"] = [
        {
            "claim": "Unsupported method attribution.",
            "papers": ["doi:10.9999/not-in-domain"],
        }
    ]
    tasks = DomainTaskService(summary_value=invalid)
    runner = DomainBuildRunner(repository)
    paused = runner.execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=tasks,
        reference_service=ForbiddenReferenceService(),
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    candidate_path = Path(
        str(paused.awaiting.details["candidate_path"])
    )
    request_count = len(tasks.requests)
    candidate_path.write_text(
        json.dumps(_summary_payload(user_intent=_request().intent)),
        encoding="utf-8",
    )

    recovered = runner.resume(
        paused.run_id,
        paper_access=FakePaperAccess(),
        task_service=tasks,
        reference_service=ForbiddenReferenceService(),
    )

    assert recovered.status is RunStatus.SUCCEEDED
    assert recovered.recovery_epoch == 0
    assert len(tasks.requests) == request_count


def test_deleted_summary_candidate_is_regenerated_only_on_explicit_resume(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    invalid = _summary_payload(user_intent=_request().intent)
    invalid["methodology"] = [
        {
            "claim": "Unsupported method attribution.",
            "papers": ["doi:10.9999/not-in-domain"],
        }
    ]
    tasks = DomainTaskService(summary_value=invalid)
    runner = DomainBuildRunner(repository)
    paused = runner.execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=tasks,
        reference_service=ForbiddenReferenceService(),
    )
    assert paused.awaiting is not None
    candidate_path = Path(
        str(paused.awaiting.details["candidate_path"])
    )
    request_count = len(tasks.requests)
    candidate_path.unlink()
    assert len(tasks.requests) == request_count

    retried = runner.resume(
        paused.run_id,
        paper_access=FakePaperAccess(),
        task_service=tasks,
        reference_service=ForbiddenReferenceService(),
    )

    assert retried.status is RunStatus.PAUSED
    assert retried.recovery_epoch == 0
    assert len(tasks.requests) == request_count + 2


def test_build_validates_the_package_view_before_publishing_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from arc_domain import build as build_module

    def reject_package(*_args, **_kwargs):
        raise DomainPackageValidationError("simulated package coverage failure")

    monkeypatch.setattr(
        build_module,
        "decode_domain_package",
        reject_package,
    )
    repository = RunRepository(tmp_path / "runs")

    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=DomainTaskService(
            summary_value=_summary_payload(user_intent=_request().intent)
        ),
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.PAUSED
    assert snapshot.awaiting is not None
    assert (
        snapshot.awaiting.details["code"]
        == "domain_summary_semantic_invalid_after_retry"
    )
    assert Path(
        str(snapshot.awaiting.details["candidate_path"])
    ).is_file()
    assert snapshot.awaiting.request_ref is not None
    store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id),
        repository_root=repository.root,
    )
    diagnostic = json.loads(
        store.read_bytes(snapshot.awaiting.request_ref)
    )
    assert (
        diagnostic["retry_error"]
        == "simulated package coverage failure"
    )
    assert store.find("summary/json") is None
    assert store.find("summary/markdown") is None


def test_verified_reference_inference_candidate_is_available_to_selection(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    task_service = DomainTaskService(
        expand_audit=True,
        selected_foundation=INFERRED_FOUNDATION,
    )
    reference_service = CompletedReferenceService()

    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=task_service,
        reference_service=reference_service,
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert reference_service.requests
    selection_request = next(
        request
        for request in task_service.requests
        if request.task_id.startswith("foundation-select-")
    )
    assert INFERRED_FOUNDATION in selection_request.prompt
    result, store = _result(repository, snapshot)
    selection = json.loads(
        store.read_bytes(result.foundation_selection).decode("utf-8")
    )
    assert selection["selected_foundation"]["paper_id"] == INFERRED_FOUNDATION


def test_domain_expansion_keeps_only_two_verified_reference_ids(
    tmp_path: Path,
) -> None:
    extra_a = "arXiv:1801.00001"
    extra_b = "arXiv:1701.00001"

    class ExpandedPaperAccess(FakePaperAccess):
        def __init__(self) -> None:
            super().__init__()
            self.metadata_by_id[extra_a] = _metadata(
                extra_a,
                title="Second verified foundation",
                year=2018,
                citations=700,
            )
            self.metadata_by_id[extra_b] = _metadata(
                extra_b,
                title="Third verified foundation",
                year=2017,
                citations=800,
            )

    repository = RunRepository(tmp_path / "runs")
    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=ExpandedPaperAccess(),
        task_service=DomainTaskService(
            expand_audit=True,
            selected_foundation=INFERRED_FOUNDATION,
        ),
        reference_service=ManyCompletedReferencesService(
            (INFERRED_FOUNDATION, extra_a, extra_b)
        ),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    result, store = _result(repository, snapshot)
    candidates_ref = store.find("foundation/candidates")
    assert candidates_ref is not None
    candidates = json.loads(
        store.read_bytes(candidates_ref).decode("utf-8")
    )
    expansion = candidates["expansion"]
    assert expansion["added_papers"] == [
        INFERRED_FOUNDATION,
        extra_a,
    ]
    assert extra_b not in {
        item["paper_id"] for item in candidates["candidates"]
    }
    assert any(
        item.startswith(
            "domain_foundation_expansion_verified_ids_truncated:"
        )
        for item in expansion["warnings"]
    )
    assert "foundation_expansion_truncated" in {
        warning.code for warning in result.warnings
    }


def test_fixed_seed_repairs_an_llm_attempt_to_move_the_foundation(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    snapshot = DomainBuildRunner(repository).execute(
        _request_with_modes(fixed_seed=True),
        paper_access=FakePaperAccess(),
        task_service=DomainTaskService(selected_foundation=FOUNDATION),
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    result, store = _result(repository, snapshot)
    selection = json.loads(store.read_bytes(result.foundation_selection).decode("utf-8"))
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    assert selection["selected_foundation"]["paper_id"] == SEED
    assert any(item.startswith("fixed_seed_foundation_enforced:") for item in selection["warnings"])
    assert graph["foundation_paper"] == SEED


def test_fixed_seed_normalizes_model_parents_against_authoritative_seed(
    tmp_path: Path,
) -> None:
    parent_id = "arXiv:2301.00002"

    class ParentPaperAccess(FakePaperAccess):
        def __init__(self) -> None:
            super().__init__()
            self.metadata_by_id[parent_id] = _metadata(
                parent_id,
                title="Valid parent",
                year=2023,
                citations=400,
            )

        def references(self, paper_id: str) -> list[dict]:
            if paper_id == SEED:
                return [
                    dict(self.metadata_by_id[FOUNDATION]),
                    dict(self.metadata_by_id[parent_id]),
                ]
            return super().references(paper_id)

    class WrongFoundationSelection(DomainTaskService):
        def execute_or_resume(self, context, request, **kwargs):
            if _request_stage(request.task_id) == "selection":
                self.requests.append(request)
                wrong = {
                    "paper_id": FOUNDATION,
                    "title": "Wrong selected foundation",
                    "reason": "model moved the anchor",
                }
                return _completed(
                    {
                        "schema_version": (
                            "arc.domain_foundation_selection.v1"
                        ),
                        "selected_foundation": wrong,
                        "best_reference_paper": wrong,
                        "parent_foundations": [
                            {
                                "paper_id": parent_id,
                                "title": "Valid parent",
                                "reason": "earlier than authoritative seed",
                            }
                        ],
                        "rejected_candidates": [],
                        "reasoning": "fixture",
                        "warnings": [],
                    }
                )
            return super().execute_or_resume(
                context, request, **kwargs
            )

    repository = RunRepository(tmp_path / "runs")
    snapshot = DomainBuildRunner(repository).execute(
        _request_with_modes(fixed_seed=True),
        paper_access=ParentPaperAccess(),
        task_service=WrongFoundationSelection(),
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    result, store = _result(repository, snapshot)
    selection = json.loads(
        store.read_bytes(result.foundation_selection).decode("utf-8")
    )
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    assert selection["selected_foundation"]["paper_id"] == SEED
    assert [
        item["paper_id"] for item in selection["parent_foundations"]
    ] == [parent_id]
    assert {
        item["paper_id"]: item["role"] for item in graph["nodes"]
    }[parent_id] == "parent_foundation"


def test_strict_window_excludes_undated_and_old_citers_before_ranking(
    tmp_path: Path,
) -> None:
    old = "arXiv:2001.00002"
    missing = "doi:10.1000/undated"

    class StrictPaper(FakePaperAccess):
        def __init__(self) -> None:
            super().__init__()
            self.metadata_by_id[DOMAIN_PAPER]["published"] = "2025-01-15"
            self.metadata_by_id[old] = _metadata(old, title="Old", year=2020, citations=10)
            self.metadata_by_id[old]["published"] = "2020-01-15"
            self.metadata_by_id[missing] = _metadata(missing, title="Undated", year=2025, citations=10)
            self.metadata_by_id[missing]["identifiers"] = {"doi": "10.1000/undated"}

        def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict]:
            del limit, sort
            if paper_id == FOUNDATION:
                return [
                    dict(self.metadata_by_id[old]),
                    dict(self.metadata_by_id[DOMAIN_PAPER]),
                    dict(self.metadata_by_id[missing]),
                ]
            return [dict(self.metadata_by_id[DOMAIN_PAPER])]

    repository = RunRepository(tmp_path / "runs")
    snapshot = DomainBuildRunner(repository).execute(
        _request_with_modes(strict_window=True),
        paper_access=StrictPaper(),
        task_service=DomainTaskService(selected_foundation=FOUNDATION),
        reference_service=ForbiddenReferenceService(),
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    result, store = _result(repository, snapshot)
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    assert {
        node["paper_id"] for node in graph["nodes"] if node["role"] == "domain_paper"
    } == {DOMAIN_PAPER}
    assert "strict_window_citers_excluded" in _warning_codes(repository, snapshot)
    network_input_ref = store.find("network/input")
    assert network_input_ref is not None
    network_input = json.loads(store.read_bytes(network_input_ref).decode("utf-8"))
    assert network_input["recency_stats"] == {
        "unique_citers": 3,
        "eligible_citers": 2,
        "exact_date_citers": 0,
        "reduced_precision_date_citers": 3,
        "excluded_missing_first_public_date": 0,
        "excluded_ambiguous_first_public_date": 0,
        "excluded_outside_window": 1,
    }


def test_strict_window_with_no_eligible_citers_skips_llm_ranking(tmp_path: Path) -> None:
    old = "doi:10.1000/old-citer"

    class NoEligiblePaper(FakePaperAccess):
        def __init__(self) -> None:
            super().__init__()
            self.metadata_by_id[old] = _metadata(old, title="Old", year=2020, citations=10)
            self.metadata_by_id[old].update(
                {"published": "2020-01-15", "identifiers": {"doi": "10.1000/old-citer"}}
            )

        def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict]:
            del limit, sort
            if paper_id == FOUNDATION:
                return [dict(self.metadata_by_id[old])]
            return [dict(self.metadata_by_id[DOMAIN_PAPER])]

    repository = RunRepository(tmp_path / "runs")
    service = DomainTaskService(selected_foundation=FOUNDATION)
    snapshot = DomainBuildRunner(repository).execute(
        _request_with_modes(strict_window=True),
        paper_access=NoEligiblePaper(),
        task_service=service,
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert not any(
        request.task_id.startswith("network-rank-") for request in service.requests
    )
    assert "strict_window_no_eligible_citers" in _warning_codes(repository, snapshot)
    result, store = _result(repository, snapshot)
    graph = json.loads(store.read_bytes(result.graph).decode("utf-8"))
    assert not [node for node in graph["nodes"] if node["role"] == "domain_paper"]


@pytest.mark.parametrize(
    ("failure", "reference_service", "expected_warning"),
    [
        ("audit", ForbiddenReferenceService(), "foundation_audit_unavailable"),
        ("reference", FailingReferenceService(), "reference_inference_unavailable"),
        ("selection", ForbiddenReferenceService(), "foundation_selection_unavailable"),
    ],
)
def test_transient_foundation_failures_use_stage_specific_fallbacks(
    tmp_path: Path,
    failure: str,
    reference_service,
    expected_warning: str,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    task_service = DomainTaskService(
        fail_stage="selection" if failure == "selection" else None,
        expand_audit=failure == "reference",
    )
    if failure == "audit":
        task_service.fail_stage = "audit"

    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=task_service,
        reference_service=reference_service,
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert expected_warning in _warning_codes(repository, snapshot)


def test_llm_stop_pauses_the_outer_domain_run(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs")
    runner = DomainBuildRunner(repository)

    snapshot = runner.execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=DomainTaskService(stopped_stage="audit"),
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.PAUSED
    assert snapshot.awaiting is not None
    assert snapshot.awaiting.reason.value == "execution_stopped"

    resumed_service = DomainTaskService()
    resumed = runner.resume(
        snapshot.run_id,
        paper_access=FakePaperAccess(),
        task_service=resumed_service,
        reference_service=ForbiddenReferenceService(),
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert resumed.run_id == snapshot.run_id
    assert resumed.attempt == snapshot.attempt + 1
    assert resumed_service.requests
