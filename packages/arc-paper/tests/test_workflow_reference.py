from __future__ import annotations

from pathlib import Path

from ac_jobs import Failed, RunContext, RunRepository, RunSpec
from ac_llm import LLMCompleted, ModelSelection, ProviderUsage

from arc_paper.workflows.reference import (
    ReferenceInferenceCompleted,
    ReferenceInferenceHandler,
    ReferenceInferenceRunner,
    ReferenceInferenceService,
)


class FakeReferenceLLM:
    def __init__(self) -> None:
        self.requests = []
        self.options = []

    def execute_or_resume(self, context, request, *, input=None, options=None):
        self.requests.append(request)
        self.options.append(options)
        return LLMCompleted(
            {
                "focus_scope": "one_domain",
                "candidates": [
                    {
                        "domain": "quantum gravity",
                        "paper_id": "arXiv:0911.3380",
                        "title": "Candidate",
                        "evidence_urls": ["https://arxiv.org/abs/0911.3380"],
                        "reasoning": "Found in a primary index.",
                    }
                ],
                "warnings": [],
            },
            "claude",
            "fake-reference-model",
            None,
            ProviderUsage(4, 3),
        )


def test_reference_inference_uses_parent_context_and_verifies_candidates(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = repository.create(
        RunSpec("reference-parent", "test.parent.v1", {"case": "reference"})
    )
    context = RunContext(
        repository, snapshot, resume_input=None
    )
    fake = FakeReferenceLLM()
    service = ReferenceInferenceService(fake)

    outcome = service.infer(
        context,
        "Find a foundational reference.",
        metadata_lookup=lambda paper_id: {
            "paper_id": paper_id,
            "arxiv_id": "0911.3380",
            "title": "Verified title",
        },
        model=ModelSelection("auto", tier="medium"),
    )

    assert isinstance(outcome, ReferenceInferenceCompleted)
    assert outcome.result.paper_ids == ("arXiv:0911.3380",)
    assert outcome.result.verified_references[0]["verified_title"] == "Verified title"
    assert outcome.result.provenance.provider == "claude"
    assert fake.options[0].internet is True
    assert fake.requests[0].model.provider == "auto"


def test_reference_standalone_wrapper_uses_same_handler(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path)
    fake = FakeReferenceLLM()
    snapshot = ReferenceInferenceRunner(repository).execute(
        "reference-run",
        "Find a foundational reference.",
        metadata_lookup=lambda paper_id: {
            "paper_id": paper_id,
            "arxiv_id": "0911.3380",
            "title": "Verified title",
        },
        service=ReferenceInferenceService(fake),
    )

    assert snapshot.status.value == "succeeded"
    assert snapshot.result_ref is not None


def test_explicit_reference_identifier_bypasses_llm(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path)
    snapshot = repository.create(
        RunSpec("explicit-parent", "test.parent.v1", {"case": "explicit"})
    )
    context = RunContext(
        repository, snapshot, resume_input=None
    )
    fake = FakeReferenceLLM()

    outcome = ReferenceInferenceService(fake).infer(
        context,
        "Please use arXiv:0911.3380 as the reference.",
        metadata_lookup=lambda paper_id: {
            "paper_id": paper_id,
            "arxiv_id": "0911.3380",
            "title": "Verified title",
        },
    )

    assert isinstance(outcome, ReferenceInferenceCompleted)
    assert outcome.result.paper_ids == ("arXiv:0911.3380",)
    assert outcome.result.provenance is None
    assert fake.requests == []


def test_all_explicit_reference_identifiers_are_verified_in_input_order(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    snapshot = repository.create(
        RunSpec("explicit-many", "test.parent.v1", {"case": "explicit-many"})
    )
    context = RunContext(
        repository, snapshot, resume_input=None
    )
    looked_up: list[str] = []

    def metadata_lookup(paper_id: str):
        looked_up.append(paper_id)
        if paper_id == "arXiv:2203.00003":
            raise RuntimeError("not cached")
        return {"paper_id": paper_id, "title": f"Verified {paper_id}"}

    outcome = ReferenceInferenceService(FakeReferenceLLM()).infer(
        context,
        (
            "Compare arXiv:0911.3380, arXiv:2101.00001, "
            "arXiv:2203.00003, and arXiv:2304.00004 with "
            "arXiv:2101.00001."
        ),
        metadata_lookup=metadata_lookup,
    )

    assert isinstance(outcome, ReferenceInferenceCompleted)
    assert looked_up == [
        "arXiv:0911.3380",
        "arXiv:2101.00001",
        "arXiv:2203.00003",
        "arXiv:2304.00004",
    ]
    assert outcome.result.paper_ids == (
        "arXiv:0911.3380",
        "arXiv:2101.00001",
        "arXiv:2304.00004",
    )
    assert outcome.result.focus_scope == "more_than_two_domains"
    assert outcome.result.rejected_candidates[0]["paper_id"] == "arXiv:2203.00003"


def test_inferred_reference_verifies_one_candidate_per_distinct_domain(
    tmp_path: Path,
) -> None:
    class ManyDomainLLM:
        def execute_or_resume(self, context, request, *, input=None, options=None):
            domains = ("gravity", "cosmology", "amplitudes", "gravity")
            return LLMCompleted(
                {
                    "focus_scope": "more_than_two_domains",
                    "candidates": [
                        {
                            "domain": domain,
                            "paper_id": f"arXiv:2401.0000{index}",
                            "title": domain,
                            "evidence_urls": [
                                f"https://arxiv.org/abs/2401.0000{index}"
                            ],
                            "reasoning": "Primary-index evidence.",
                        }
                        for index, domain in enumerate(domains, start=1)
                    ],
                    "warnings": [],
                },
                "claude",
                "fake-reference-model",
                None,
                ProviderUsage(4, 3),
            )

    repository = RunRepository(tmp_path)
    snapshot = repository.create(
        RunSpec("reference-many", "test.parent.v1", {"case": "reference-many"})
    )
    context = RunContext(
        repository, snapshot, resume_input=None
    )
    outcome = ReferenceInferenceService(ManyDomainLLM()).infer(
        context,
        "Compare gravity, cosmology, and amplitudes.",
        metadata_lookup=lambda paper_id: {
            "paper_id": paper_id,
            "title": f"Verified {paper_id}",
        },
    )

    assert isinstance(outcome, ReferenceInferenceCompleted)
    assert outcome.result.paper_ids == (
        "arXiv:2401.00001",
        "arXiv:2401.00002",
        "arXiv:2401.00003",
    )
    assert outcome.result.focus_scope == "more_than_two_domains"
    assert outcome.result.rejected_candidates == (
        {
            "paper_id": "arXiv:2401.00004",
            "error": "duplicate_domain_candidate",
        },
    )


def test_reference_resume_decode_uses_reference_error_code(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path)
    handler = ReferenceInferenceHandler(
        "Find a foundational reference.",
        metadata_lookup=lambda paper_id: {"paper_id": paper_id},
        service=ReferenceInferenceService(FakeReferenceLLM()),
    )
    snapshot = repository.create(
        RunSpec("reference-resume", handler.name, handler.semantic_input())
    )
    context = RunContext(
        repository,
        snapshot,
        resume_input={"not": "an ac.llm resume input"},
    )

    outcome = handler.execute(context)

    assert isinstance(outcome, Failed)
    assert outcome.error.code == "reference_resume_input_invalid"
