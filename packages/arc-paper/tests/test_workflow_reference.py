from __future__ import annotations

from pathlib import Path

from arc_jobs import Failed, RunContext, RunRepository, RunSpec
from arc_llm import LLMCompleted, ModelSelection, ProviderUsage

from arc_paper.workflows.reference import (
    ReferenceInferenceCompleted,
    ReferenceInferenceHandler,
    ReferenceInferenceRunner,
    ReferenceInferenceService,
)


class FakeReferenceLLM:
    def __init__(self) -> None:
        self.requests = []

    def execute_or_resume(self, context, request, *, input=None, options=None):
        self.requests.append(request)
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
        repository, snapshot, resume_input=None, execution_slice=None
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
    assert fake.requests[0].capabilities.internet is True
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
        repository, snapshot, resume_input=None, execution_slice=None
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
        resume_input={"not": "an arc.llm resume input"},
        execution_slice=None,
    )

    outcome = handler.execute(context)

    assert isinstance(outcome, Failed)
    assert outcome.error.code == "reference_resume_input_invalid"
