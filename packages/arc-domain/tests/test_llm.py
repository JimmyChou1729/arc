from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc_domain import _llm
from arc_jobs import ResumeReason
from arc_llm import (
    DeliveryState,
    FailureCategory,
    LLMFailed,
    LLMPaused,
    ModelSelection,
    ProviderFailure,
    ResumeAction,
    ResumeInput,
    resume_input_to_document,
)


class FakeTaskService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute_or_resume(self, context, request, **kwargs):
        self.calls.append({"context": context, "request": request, **kwargs})
        return "outcome"


def _failure(category: FailureCategory) -> LLMFailed:
    return LLMFailed(
        ProviderFailure(
            "provider failed",
            category=category,
            delivery=DeliveryState.NOT_DELIVERED,
        )
    )


def test_execute_routed_only_passes_matching_resume_input(monkeypatch):
    service = FakeTaskService()
    context = object()
    request = object()
    resume_input = object()

    monkeypatch.setattr(_llm, "resume_input_matches", lambda _request, _resume: True)
    assert _llm.execute_routed(service, context, request, resume_input=resume_input) == "outcome"
    assert service.calls == [{"context": context, "request": request, "input": resume_input}]

    service.calls.clear()
    monkeypatch.setattr(_llm, "resume_input_matches", lambda _request, _resume: False)
    _llm.execute_routed(service, context, request, resume_input=resume_input)
    assert service.calls == [{"context": context, "request": request}]


def test_outer_resume_input_decodes_or_uses_caller_error_code():
    resume_input = ResumeInput(resume_key="resume-key", action=ResumeAction.CONTINUE)
    context = SimpleNamespace(resume_input=resume_input_to_document(resume_input))
    assert _llm.outer_resume_input(context, error_code="domain_resume_invalid") == resume_input
    assert _llm.outer_resume_input(SimpleNamespace(resume_input=None), error_code="unused") is None

    with pytest.raises(_llm.DomainLLMError, match="Invalid LLM resume input") as raised:
        _llm.outer_resume_input(SimpleNamespace(resume_input={}), error_code="domain_resume_invalid")
    assert raised.value.code == "domain_resume_invalid"


def test_model_document_and_pause_failure_conversions():
    assert _llm.model_document(ModelSelection(provider="codex", model="gpt-5.6")) == {
        "provider": "codex",
        "model": "gpt-5.6",
        "tier": "medium",
    }
    pause = LLMPaused(
        reason=ResumeReason.INTERACTION_REQUIRED,
        resume_key="resume-key",
        input_required=True,
        response_contract="arc.response.v1",
        details={"reason": "choose"},
    )
    awaiting = _llm.awaiting_from_pause(pause)
    assert awaiting.resume_key == "resume-key"
    assert awaiting.input_required is True
    assert awaiting.details == {"reason": "choose"}

    run_error = _llm.run_error_from_failure(_failure(FailureCategory.TRANSPORT))
    assert run_error.code == "provider_transport"
    assert run_error.message == "provider failed"


def test_only_transport_and_timeout_failures_are_transient():
    assert _llm.is_transient_failure(_failure(FailureCategory.TRANSPORT)) is True
    assert _llm.is_transient_failure(_failure(FailureCategory.TIMEOUT)) is True
    assert _llm.is_transient_failure(_failure(FailureCategory.RATE_LIMIT)) is False
