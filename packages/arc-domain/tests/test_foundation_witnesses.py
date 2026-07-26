from __future__ import annotations

import json
from pathlib import Path

from arc_domain.build import DomainBuildRunner
from arc_domain.contracts import DomainBuildPolicy, DomainBuildRequest
from arc_jobs import ImmutableArtifactStore, RunRepository, RunStatus
from arc_llm import (
    LLMCompleted,
)


SEED = "arXiv:2401.00001"


def _paper(paper_id: str, *, citations: int = 20) -> dict[str, object]:
    return {
        "paper_id": paper_id,
        "title": f"Paper {paper_id}",
        "abstract": "foundation methods",
        "authors": ["A. Author"],
        "year": 2024,
        "citation_count": citations,
        "identifiers": {"paper_id": paper_id},
    }


class WitnessPaperAccess:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.citers_for_seed = [
            _paper(f"arXiv:2501.{index:05d}") for index in range(1, 56)
        ]
        self.seed_references = [
            _paper(f"arXiv:2201.{index:05d}", citations=100)
            for index in range(1, 101)
        ]

    def metadata(self, paper_id: str) -> dict[str, object]:
        self.calls.append(("metadata", paper_id))
        if paper_id == SEED:
            return _paper(SEED, citations=10)
        return _paper(paper_id, citations=100)

    def references(self, paper_id: str) -> list[dict[str, object]]:
        self.calls.append(("references", paper_id))
        if paper_id == SEED:
            return [dict(reference) for reference in self.seed_references]
        return []

    def citers(
        self, paper_id: str, *, limit: int, sort: str
    ) -> list[dict[str, object]]:
        self.calls.append(("citers", paper_id))
        if paper_id == SEED and limit == 50 and sort == "mostrecent":
            return [dict(citer) for citer in self.citers_for_seed]
        return []

    def acquire_pack_record(self, paper_id: str) -> dict[str, object]:
        self.calls.append(("pack", paper_id))
        return {
            "metadata": self.metadata(paper_id),
            "references": [],
            "toc": [],
            "conclusion": None,
            "warnings": [],
        }


class SufficientCandidateTasks:
    def execute_or_resume(self, context, request, **kwargs):
        del kwargs
        if request.task_id.startswith("foundation-audit-"):
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_foundation_candidate_audit.v1",
                    "candidate_set_sufficient": True,
                    "confidence": "complete",
                    "search_queries": [],
                    "citation_directions": [],
                    "reasoning": "sufficient",
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("foundation-select-"):
            choice = {"paper_id": SEED, "title": "Seed", "reason": "selected"}
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_foundation_selection.v1",
                    "selected_foundation": choice,
                    "best_reference_paper": choice,
                    "parent_foundations": [],
                    "rejected_candidates": [],
                    "reasoning": "selected seed",
                    "warnings": [],
                },
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("network-rank-"):
            return LLMCompleted(
                {"ranked_paper_ids": [], "reasoning": "no domain papers"},
                None,
                None,
                None,
                None,
            )
        if request.task_id.startswith("domain-summary-"):
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
            return LLMCompleted(
                {
                    "schema_version": "arc.domain_summary.v5",
                    "domain_title": "Foundation methods",
                    "brief_introduction": "A compact introduction.",
                    "task_focus": {
                        "user_intent": "foundation methods",
                        "research_scope": "The supplied papers.",
                        "priority_rules": ["Satisfy the user intent first."],
                    },
                    "foundation_paper": dict(
                        selection["selected_foundation"]
                    ),
                    "best_reference_paper": dict(
                        selection["best_reference_paper"]
                    ),
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


def _read_artifact(
    repository: RunRepository, run_id: str, artifact_name: str
) -> dict[str, object]:
    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    reference = store.find(artifact_name)
    assert reference is not None
    return json.loads(store.read_bytes(reference).decode("utf-8"))


def test_foundation_candidates_use_only_the_bounded_witness_subset(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path / "runs")
    paper = WitnessPaperAccess()
    request = DomainBuildRequest(
        SEED,
        "foundation methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            citer_pool_limit=1,
            ranked_paper_limit=1,
            graph_node_limit=2,
        ),
    )

    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=paper,
        task_service=SufficientCandidateTasks(),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    foundation_input = _read_artifact(repository, snapshot.run_id, "foundation/input")
    candidates_document = _read_artifact(
        repository, snapshot.run_id, "foundation/candidates"
    )
    newest_citers = foundation_input["newest_citers"]
    seed_references = foundation_input["seed_references"]
    sampled_references = foundation_input["sampled_references"]
    assert isinstance(newest_citers, list)
    assert isinstance(seed_references, list)
    assert isinstance(sampled_references, list)
    assert len(newest_citers) == 50
    assert len(seed_references) == 100
    assert len(sampled_references) == 10

    sampled_ids = {item["paper_id"] for item in sampled_references}
    unsampled_ids = {
        item["paper_id"] for item in seed_references if item["paper_id"] not in sampled_ids
    }
    candidate_ids = {
        item["paper_id"] for item in candidates_document["candidates"]
    }
    metadata_ids = {paper_id for operation, paper_id in paper.calls if operation == "metadata"}
    witness_reference_ids = {
        paper_id
        for operation, paper_id in paper.calls
        if operation == "references" and paper_id != SEED
    }

    assert candidate_ids <= {SEED, *sampled_ids}
    assert not candidate_ids & unsampled_ids
    assert not metadata_ids & unsampled_ids
    assert witness_reference_ids == {
        citer["paper_id"] for citer in newest_citers
    }


def test_fixed_seed_anchors_to_seed_metadata_when_candidate_scan_omits_seed(
    tmp_path: Path,
) -> None:
    class ScanExcludesSeedPaperAccess(WitnessPaperAccess):
        def __init__(self) -> None:
            super().__init__()
            self.seed_references = []

        def references(self, paper_id: str) -> list[dict[str, object]]:
            self.calls.append(("references", paper_id))
            if paper_id == SEED:
                return []
            if paper_id.startswith("arXiv:2501."):
                index = paper_id.rsplit(".", 1)[-1]
                return [_paper(f"arXiv:2101.{index}", citations=500)]
            return []

    repository = RunRepository(tmp_path / "runs")
    request = DomainBuildRequest(
        SEED,
        "foundation methods",
        DomainBuildPolicy(
            as_of_date="2026-07-24",
            citer_pool_limit=2,
            ranked_paper_limit=1,
            graph_node_limit=3,
            schema_version="arc.domain_build_policy.v2",
            foundation_mode="fixed_seed",
            citer_selection_mode="representative_plus_recent",
        ),
    )
    snapshot = DomainBuildRunner(repository).execute(
        request,
        paper_access=ScanExcludesSeedPaperAccess(),
        task_service=SufficientCandidateTasks(),
        reference_service=NoReferenceInference(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    candidates = _read_artifact(repository, snapshot.run_id, "foundation/candidates")
    selection = _read_artifact(repository, snapshot.run_id, "foundation/selection")
    assert SEED not in {candidate["paper_id"] for candidate in candidates["candidates"]}
    assert selection["selected_foundation"]["paper_id"] == SEED
    assert "fixed_seed_recovered_from_seed_metadata" in selection["warnings"]
