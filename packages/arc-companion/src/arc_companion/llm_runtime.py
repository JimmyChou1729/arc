"""Public-facade adapters for Companion LLM tasks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from arc_jobs import (
    Awaiting,
    Paused,
    ResumeReason,
    RunContext,
    StoppedError,
)
from arc_llm import (
    LLMCompleted,
    LLMExecutionOptions,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMStopped,
    LLMTaskOutcome,
    LLMTaskService,
    RESUME_SCHEMA_VERSION,
    ResumeInput,
    awaiting_from_pause,
    decode_resume_input,
    execute_or_resume_matching,
    run_error_from_failure,
)

from .generation_validation import CompanionContentError


class CompanionLLMError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


T = TypeVar("T")
_NO_FALLBACK = object()


@dataclass(frozen=True)
class SemanticTaskCompleted(Generic[T]):
    """A model value accepted by Companion's deterministic semantics."""

    value: T
    candidate_paths: tuple[Path, ...]
    validation_warning: CompanionContentError | None = None


def outer_resume_input(context: RunContext) -> ResumeInput | None:
    if context.resume_input is None:
        return None
    try:
        return decode_resume_input(context.resume_input)
    except Exception as exc:
        if context.resume_input.get("schema_version") == RESUME_SCHEMA_VERSION:
            raise CompanionLLMError(
                "companion_llm_resume_input_invalid",
                "Malformed arc-llm resume input.",
            ) from exc
        # A Companion supervision response is intentionally not an LLM resume
        # input. The owning chapter worker will decode it instead.
        return None


execute_task = execute_or_resume_matching


def execute_semantically_validated_task(
    service: LLMTaskService,
    context: RunContext,
    request: LLMRequest,
    *,
    candidate_id: str,
    description: str,
    validate: Callable[[Mapping[str, Any]], T],
    resume_input: ResumeInput | None,
    options: LLMExecutionOptions,
    normalize: (
        Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None,
    fallback: T | object = _NO_FALLBACK,
) -> SemanticTaskCompleted[T] | Paused | LLMFailed:
    """Validate one schema-valid output and make one fresh semantic retry.

    The first and retry candidates remain separate editable files. A second
    deterministic semantic rejection pauses the owning build unless the caller
    supplies an already validated fallback.
    """

    first_path = context.working.find_candidate(candidate_id)
    if first_path is None:
        first_outcome = execute_task(
            service,
            context,
            request,
            resume_input=resume_input,
            options=options,
        )
        if isinstance(first_outcome, LLMPaused):
            return Paused(awaiting_from_pause(first_outcome))
        if isinstance(first_outcome, LLMFailed):
            return first_outcome
        ensure_not_stopped(first_outcome, description)
        assert isinstance(first_outcome, LLMCompleted)
        first_raw = _model_mapping(first_outcome.value, description)
        first_raw = _normalize_candidate(first_raw, normalize)
        first_path = context.working.write_candidate_json(
            candidate_id, first_raw
        )
    else:
        stored_first = context.working.read_candidate_json(candidate_id)
        first_raw = stored_first
        first_raw = _normalize_candidate(first_raw, normalize)
        if first_raw != stored_first:
            first_path = context.working.write_candidate_json(
                candidate_id, first_raw
            )
    try:
        return SemanticTaskCompleted(
            validate(first_raw),
            (first_path,),
        )
    except CompanionContentError as exc:
        first_error = exc

    retry_candidate_id = _semantic_retry_candidate_id(candidate_id)
    retry_path = context.working.find_candidate(retry_candidate_id)
    if retry_path is None:
        retry_request = LLMRequest(
            _semantic_retry_task_id(
                request.task_id,
                candidate=first_raw,
                error=first_error,
            ),
            _semantic_retry_prompt(
                request.prompt,
                error=first_error,
            ),
            request.output,
            request.model,
            request.session,
            request.inputs,
        )
        retry_outcome = execute_task(
            service,
            context,
            retry_request,
            resume_input=resume_input,
            options=options,
        )
        if isinstance(retry_outcome, LLMPaused):
            return Paused(awaiting_from_pause(retry_outcome))
        if isinstance(retry_outcome, LLMFailed):
            return retry_outcome
        ensure_not_stopped(retry_outcome, f"{description} semantic retry")
        assert isinstance(retry_outcome, LLMCompleted)
        retry_raw = _model_mapping(
            retry_outcome.value, f"{description} semantic retry"
        )
        retry_raw = _normalize_candidate(retry_raw, normalize)
        retry_path = context.working.write_candidate_json(
            retry_candidate_id, retry_raw
        )
    else:
        stored_retry = context.working.read_candidate_json(
            retry_candidate_id
        )
        retry_raw = stored_retry
        retry_raw = _normalize_candidate(retry_raw, normalize)
        if retry_raw != stored_retry:
            retry_path = context.working.write_candidate_json(
                retry_candidate_id, retry_raw
            )
    try:
        value = validate(retry_raw)
    except CompanionContentError as retry_error:
        candidate_paths = (first_path, retry_path)
        if fallback is not _NO_FALLBACK:
            return SemanticTaskCompleted(
                cast(T, fallback),
                candidate_paths,
                retry_error,
            )
        return _semantic_retry_pause(
            candidate_id=candidate_id,
            description=description,
            error=retry_error,
            candidate_paths=candidate_paths,
        )
    return SemanticTaskCompleted(value, (first_path, retry_path))


def _model_mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanionContentError(
            "companion_model_output_invalid",
            f"{description} must be a JSON object",
        )
    return dict(value)


def _normalize_candidate(
    value: Mapping[str, Any],
    normalize: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    return dict(normalize(value) if normalize is not None else value)


def _semantic_retry_candidate_id(candidate_id: str) -> str:
    stem = (
        candidate_id.removesuffix(".json")
        if candidate_id.endswith(".json")
        else candidate_id
    )
    return f"{stem}.semantic-retry.json"


def _semantic_retry_task_id(
    task_id: str,
    *,
    candidate: Mapping[str, Any],
    error: CompanionContentError,
) -> str:
    identity = json.dumps(
        {
            "task_id": task_id,
            "error_code": error.code,
            "error_message": str(error)[:4000],
            "candidate": dict(candidate),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    suffix = f"-semantic-retry-{digest}"
    if len(task_id) + len(suffix) <= 128:
        return f"{task_id}{suffix}"
    prefix_length = 128 - len(suffix)
    return f"{task_id[:prefix_length]}{suffix}"


def _semantic_retry_prompt(
    prompt: str,
    *,
    error: CompanionContentError,
) -> str:
    marker = "\n\nInput JSON:\n"
    message = str(error)[:4000]
    feedback = (
        "\n\nSemantic retry feedback: the previous complete JSON satisfied "
        "the output schema but failed deterministic Companion identity, "
        "reference, coverage, or rich-text validation. Produce one fresh "
        "complete JSON answer under the original contract; do not explain "
        "the error. Do not invent caller-owned source or evidence references; "
        "candidate-owned identifiers may be corrected when validation "
        "requires it. "
        f"Validation code: {error.code}. Validation message: {message}."
    )
    if marker not in prompt:
        return f"{prompt}{feedback}"
    return prompt.replace(marker, f"{feedback}{marker}", 1)


def _semantic_retry_pause(
    *,
    candidate_id: str,
    description: str,
    error: CompanionContentError,
    candidate_paths: tuple[Path, Path],
) -> Paused:
    digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:24]
    return Paused(
        Awaiting(
            ResumeReason.SUPERVISION_REQUIRED,
            f"semantic-retry-{digest}",
            False,
            details={
                "code": error.code,
                "message": str(error),
                "stage": description,
                "candidate_paths": [
                    str(path) for path in candidate_paths
                ],
                "active_candidate_path": str(candidate_paths[-1]),
                "automatic_retry_exhausted": True,
                "output_attempts": 2,
            },
        )
    )


def ensure_not_stopped(outcome: LLMTaskOutcome, description: str) -> None:
    if isinstance(outcome, LLMStopped):
        raise StoppedError(f"{description} stopped")


__all__ = [
    "CompanionLLMError",
    "SemanticTaskCompleted",
    "awaiting_from_pause",
    "ensure_not_stopped",
    "execute_semantically_validated_task",
    "execute_task",
    "outer_resume_input",
    "run_error_from_failure",
]
