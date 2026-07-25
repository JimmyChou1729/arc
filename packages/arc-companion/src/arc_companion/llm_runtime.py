"""Public-facade adapters for Companion LLM tasks."""

from __future__ import annotations

from arc_jobs import Awaiting, RunContext, RunError, StoppedError
from arc_llm import (
    LLMStopped,
    LLMExecutionOptions,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMTaskOutcome,
    LLMTaskService,
    ResumeInput,
    decode_resume_input,
    resume_input_matches,
)


class CompanionLLMError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def outer_resume_input(context: RunContext) -> ResumeInput | None:
    if context.resume_input is None:
        return None
    try:
        return decode_resume_input(context.resume_input)
    except Exception as exc:
        if context.resume_input.get("schema_version") == "arc.llm.resume_input.v2":
            raise CompanionLLMError(
                "companion_llm_resume_input_invalid",
                "Malformed arc-llm resume input.",
            ) from exc
        # A Companion supervision response is intentionally not an LLM resume
        # input. The owning chapter worker will decode it instead.
        return None


def execute_task(
    service: LLMTaskService,
    context: RunContext,
    request: LLMRequest,
    *,
    resume_input: ResumeInput | None,
    options: LLMExecutionOptions,
) -> LLMTaskOutcome:
    if resume_input is not None and resume_input_matches(request, resume_input):
        return service.execute_or_resume(
            context, request, input=resume_input, options=options
        )
    return service.execute_or_resume(context, request, options=options)


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


def ensure_not_stopped(outcome: LLMTaskOutcome, description: str) -> None:
    if isinstance(outcome, LLMStopped):
        raise StoppedError(f"{description} stopped")


__all__ = [
    "CompanionLLMError",
    "awaiting_from_pause",
    "ensure_not_stopped",
    "execute_task",
    "outer_resume_input",
    "run_error_from_failure",
]
