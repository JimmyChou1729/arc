"""Small public-facade adapters for durable domain LLM calls."""

from __future__ import annotations

from typing import Any

from arc_jobs import Awaiting, JsonValue, RunContext, RunError
from arc_llm import (
    FailureCategory,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMTaskOutcome,
    LLMTaskService,
    ModelSelection,
    ProviderFailure,
    ResumeInput,
    decode_resume_input,
    resume_input_matches,
)


class DomainLLMError(RuntimeError):
    """A stable domain error suitable for conversion to a durable run error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def execute_routed(
    service: LLMTaskService,
    context: RunContext,
    request: LLMRequest,
    *,
    resume_input: ResumeInput | None,
) -> LLMTaskOutcome:
    """Resume only a pause that belongs to this exact LLM request."""
    if resume_input is not None and resume_input_matches(request, resume_input):
        return service.execute_or_resume(context, request, input=resume_input)
    return service.execute_or_resume(context, request)


def outer_resume_input(
    context: RunContext,
    *,
    error_code: str,
) -> ResumeInput | None:
    """Decode the outer run's input, retaining the caller's domain error code."""
    if context.resume_input is None:
        return None
    try:
        return decode_resume_input(context.resume_input)
    except Exception as exc:
        raise DomainLLMError(error_code, f"Invalid LLM resume input: {exc}") from exc


def model_document(value: ModelSelection) -> dict[str, JsonValue]:
    """Encode the complete model requirement in a durable JSON document."""
    return {
        "provider": value.provider,
        "model": value.model,
        "tier": value.tier,
    }


def awaiting_from_pause(outcome: LLMPaused) -> Awaiting:
    return Awaiting(
        outcome.reason,
        outcome.resume_key,
        outcome.input_required,
        outcome.request_ref,
        outcome.response_contract,
        outcome.details,
    )


def run_error_from_failure(outcome: LLMFailed) -> RunError:
    return RunError(
        outcome.error.code.value,
        str(outcome.error),
        outcome.error.details,
    )


def is_transient_failure(outcome: LLMFailed) -> bool:
    """Only exhausted provider transport and timeout failures are degradable."""
    error = outcome.error
    return isinstance(error, ProviderFailure) and error.category in {
        FailureCategory.TRANSPORT,
        FailureCategory.TIMEOUT,
    }


__all__ = [
    "DomainLLMError",
    "awaiting_from_pause",
    "execute_routed",
    "is_transient_failure",
    "model_document",
    "outer_resume_input",
    "run_error_from_failure",
]
