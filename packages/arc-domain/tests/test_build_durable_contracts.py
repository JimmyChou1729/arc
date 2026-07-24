from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_domain.build import DomainBuildRunner
from arc_domain.contracts import (
    DomainBuildPolicy,
    DomainBuildRequest,
    decode_domain_build_result,
)
from arc_jobs import ImmutableArtifactStore, RunRepository, RunStatus
from arc_llm import (
    DeliveryState,
    FailureCategory,
    JsonOutput,
    LLMCancelled,
    LLMCompleted,
    LLMFailed,
    ProviderFailure,
)
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


def _failure(category: FailureCategory) -> LLMFailed:
    return LLMFailed(
        ProviderFailure(
            "fake provider failure",
            category=category,
            delivery=DeliveryState.NOT_DELIVERED,
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


def _request_stage(task_id: str) -> str:
    for prefix, stage in (
        ("foundation-audit-", "audit"),
        ("foundation-select-", "selection"),
        ("network-rank-", "ranking"),
        ("domain-summary-", "summary"),
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
        cancelled_stage: str | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.expand_audit = expand_audit
        self.selected_foundation = selected_foundation
        self.cancelled_stage = cancelled_stage
        self.requests = []

    def execute_or_resume(self, context, request, **kwargs):
        del context, kwargs
        self.requests.append(request)
        stage = _request_stage(request.task_id)

        if stage == self.cancelled_stage:
            return LLMCancelled()
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
            return _failure(FailureCategory.TIMEOUT)
        raise AssertionError(f"unhandled stage {stage}")


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


def test_all_domain_llm_requests_use_local_json_repair(tmp_path: Path) -> None:
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
    assert {request.output.repair for request in service.requests} == {"local"}


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


def test_llm_cancellation_cancels_the_outer_domain_run(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs")

    snapshot = DomainBuildRunner(repository).execute(
        _request(),
        paper_access=FakePaperAccess(),
        task_service=DomainTaskService(cancelled_stage="audit"),
        reference_service=ForbiddenReferenceService(),
    )

    assert snapshot.status is RunStatus.CANCELLED
