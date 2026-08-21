"""Small public-facade adapters for durable domain LLM calls."""

from __future__ import annotations

from ac_jobs import JsonValue, RunContext
from ac_llm import (
    FailureCategory,
    LLMFailed,
    LLMRequest,
    ModelSelection,
    ProviderFailure,
    ResumeInput,
    awaiting_from_pause,
    decode_resume_input,
    execute_or_resume_matching,
    run_error_from_failure,
    semantic_retry_request as _shared_semantic_retry_request,
)


class DomainLLMError(RuntimeError):
    """A stable domain error suitable for conversion to a durable run error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def semantic_retry_request(
    request: LLMRequest,
    *,
    validator_contract: str,
    feedback: str,
) -> LLMRequest:
    """Create one deterministic fresh task after package semantic validation."""

    return _shared_semantic_retry_request(
        request,
        identity_schema_version="arc.domain.semantic_output_retry.v1",
        validator_contract=validator_contract,
        feedback=feedback,
    )


execute_routed = execute_or_resume_matching


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
    "semantic_retry_request",
]
