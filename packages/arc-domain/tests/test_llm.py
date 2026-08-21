from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc_domain import _llm
from ac_jobs import ResumeReason
from ac_llm import (
    FailureCategory,
    LLMFailed,
    LLMPaused,
    ModelSelection,
    ProviderFailure,
    ResumeAction,
    ResumeInput,
    resume_input_to_document,
)


def _failure(category: FailureCategory) -> LLMFailed:
    return LLMFailed(
        ProviderFailure(
            "provider failed",
            category=category,
        )
    )


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
