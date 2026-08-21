"""Reference inference through the shared in-run LLM task service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TypeAlias

from ac_jobs import (
    StoppedError,
    Failed,
    JsonValue,
    Paused,
    RunContext,
    RunEngine,
    RunError,
    RunRepository,
    RunSnapshot,
    RunSpec,
    Succeeded,
    canonical_json_bytes,
)
from ac_llm import (
    JsonOutput,
    LLMStopped,
    LLMCompleted,
    LLMFailed,
    LLMExecutionOptions,
    LLMPaused,
    LLMRequest,
    LLMTaskService,
    ModelSelection,
    ResumeInput,
)

from ..ids import arxiv_path_id, doi_value, extract_paper_ids, inspire_recid, normalize_paper_id
from ._llm import (
    LLMCallProvenance,
    PaperWorkflowError,
    awaiting_from_pause,
    execute_routed,
    model_document,
    outer_resume_input,
    provenance,
    run_error_from_failure,
)

REFERENCE_HANDLER = "arc.paper.reference_inference.v3"
REFERENCE_PROMPT_CONTRACT = "arc.paper.reference_inference_prompt.v3"
REFERENCE_OUTPUT_CONTRACT = "arc.paper.reference_inference_output.v2"

REFERENCE_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": REFERENCE_OUTPUT_CONTRACT,
    "type": "object",
    "additionalProperties": False,
    "required": ["focus_scope", "candidates", "warnings"],
    "properties": {
        "focus_scope": {
            "type": "string",
            "enum": [
                "one_domain",
                "two_domains",
                "more_than_two_domains",
                "unclear",
                "not_a_research_request",
            ],
        },
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "domain",
                    "paper_id",
                    "title",
                    "evidence_urls",
                    "reasoning",
                ],
                "properties": {
                    "domain": {"type": "string"},
                    "paper_id": {"type": "string"},
                    "title": {"type": "string"},
                    "evidence_urls": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {"type": "string"},
                    },
                    "reasoning": {"type": "string"},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

MetadataLookup = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class ReferenceInferenceResult:
    request_digest: str
    paper_ids: tuple[str, ...]
    focus_scope: str
    warnings: tuple[str, ...]
    verified_references: tuple[Mapping[str, JsonValue], ...]
    rejected_candidates: tuple[Mapping[str, JsonValue], ...]
    provenance: LLMCallProvenance | None

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": "arc.paper.reference_inference.v3",
            "request_digest": self.request_digest,
            "paper_ids": list(self.paper_ids),
            "focus_scope": self.focus_scope,
            "warnings": list(self.warnings),
            "verified_references": [dict(item) for item in self.verified_references],
            "rejected_candidates": [dict(item) for item in self.rejected_candidates],
            "provenance": (
                None if self.provenance is None else self.provenance.to_document()
            ),
        }


@dataclass(frozen=True)
class ReferenceInferenceCompleted:
    result: ReferenceInferenceResult


ReferenceInferenceOutcome: TypeAlias = (
    ReferenceInferenceCompleted | LLMPaused | LLMFailed | LLMStopped
)


class ReferenceInferenceService:
    def __init__(self, task_service: Any | None = None) -> None:
        self.task_service = task_service or LLMTaskService()

    def infer(
        self,
        context: RunContext,
        text: str,
        *,
        metadata_lookup: MetadataLookup,
        model: ModelSelection = ModelSelection(tier="medium"),
        resume_input: ResumeInput | None = None,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> ReferenceInferenceOutcome:
        request_text = text.strip()
        if not request_text:
            raise PaperWorkflowError(
                "empty_reference_request",
                "Reference inference requires non-empty text.",
            )
        request_digest = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        explicit_ids = _unique_supported_ids(extract_paper_ids(request_text))
        if explicit_ids:
            verified = _verify_explicit_ids(
                explicit_ids, metadata_lookup=metadata_lookup
            )
            return ReferenceInferenceCompleted(
                ReferenceInferenceResult(
                    request_digest=request_digest,
                    paper_ids=tuple(verified["paper_ids"]),
                    focus_scope=(
                        "one_domain"
                        if len(explicit_ids) == 1
                        else (
                            "two_domains"
                            if len(explicit_ids) == 2
                            else "more_than_two_domains"
                        )
                    ),
                    warnings=tuple(verified["warnings"]),
                    verified_references=tuple(verified["verified_references"]),
                    rejected_candidates=tuple(verified["rejected_candidates"]),
                    provenance=None,
                )
            )
        identity = {
            "prompt_contract": REFERENCE_PROMPT_CONTRACT,
            "output_contract": REFERENCE_OUTPUT_CONTRACT,
            "request_digest": request_digest,
            "model_requirement": model_document(model),
        }
        task_id = (
            "reference-"
            + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
        )
        request = LLMRequest(
            task_id,
            _prompt(request_text, request_digest),
            JsonOutput(REFERENCE_OUTPUT_SCHEMA),
            model,
        )
        outcome = execute_routed(
            self.task_service,
            context,
            request,
            resume_input=resume_input,
            options=options,
        )
        if not isinstance(outcome, LLMCompleted):
            return outcome
        payload = _payload(outcome.value)
        verified = _verify_payload(payload, metadata_lookup=metadata_lookup)
        return ReferenceInferenceCompleted(
            ReferenceInferenceResult(
                request_digest=request_digest,
                paper_ids=tuple(verified["paper_ids"]),
                focus_scope=verified["focus_scope"],
                warnings=tuple(verified["warnings"]),
                verified_references=tuple(verified["verified_references"]),
                rejected_candidates=tuple(verified["rejected_candidates"]),
                provenance=provenance(task_id, outcome),
            )
        )


class ReferenceInferenceHandler:
    """Standalone RunHandler using the same in-run reference service."""

    name = REFERENCE_HANDLER

    def __init__(
        self,
        text: str,
        *,
        metadata_lookup: MetadataLookup,
        service: ReferenceInferenceService | None = None,
        model: ModelSelection = ModelSelection(tier="medium"),
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> None:
        self.text = text.strip()
        self.metadata_lookup = metadata_lookup
        self.service = service or ReferenceInferenceService()
        self.model = model
        self.options = options

    def semantic_input(self) -> dict[str, JsonValue]:
        return {
            "schema_version": "arc.paper.reference_inference_request.v3",
            "text": self.text,
            "request_digest": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "model_requirement": model_document(self.model),
            "prompt_contract": REFERENCE_PROMPT_CONTRACT,
            "output_contract": REFERENCE_OUTPUT_CONTRACT,
        }

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "reference_binding_mismatch",
                    "Handler binding differs from the durable semantic input.",
                )
            )
        try:
            outcome = self.service.infer(
                context,
                self.text,
                metadata_lookup=self.metadata_lookup,
                model=self.model,
                resume_input=outer_resume_input(
                    context, error_code="reference_resume_input_invalid"
                ),
                options=self.options,
            )
        except PaperWorkflowError as exc:
            return Failed(RunError(exc.code, str(exc)))
        if isinstance(outcome, ReferenceInferenceCompleted):
            return Succeeded(
                context.artifacts.publish_json(
                    "reference-inference/result", outcome.result.to_document()
                )
            )
        if isinstance(outcome, LLMPaused):
            return Paused(awaiting_from_pause(outcome))
        if isinstance(outcome, LLMFailed):
            return Failed(run_error_from_failure(outcome))
        if isinstance(outcome, LLMStopped):
            raise StoppedError("reference-inference LLM task stopped")
        raise RuntimeError("unknown reference-inference outcome")


class ReferenceInferenceRunner:
    def __init__(self, repository: RunRepository) -> None:
        self.engine = RunEngine(repository)

    def execute(
        self,
        run_id: str,
        text: str,
        *,
        metadata_lookup: MetadataLookup,
        service: ReferenceInferenceService | None = None,
        model: ModelSelection = ModelSelection(tier="medium"),
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> RunSnapshot:
        handler = ReferenceInferenceHandler(
            text,
            metadata_lookup=metadata_lookup,
            service=service,
            model=model,
            options=options,
        )
        return self.engine.execute(
            RunSpec(run_id, handler.name, handler.semantic_input()), handler
        )

    def resume(
        self,
        run_id: str,
        text: str,
        *,
        metadata_lookup: MetadataLookup,
        input: Mapping[str, JsonValue] | None = None,
        service: ReferenceInferenceService | None = None,
        model: ModelSelection = ModelSelection(tier="medium"),
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> RunSnapshot:
        handler = ReferenceInferenceHandler(
            text,
            metadata_lookup=metadata_lookup,
            service=service,
            model=model,
            options=options,
        )
        return self.engine.resume(run_id, handler, input=input)


def _prompt(text: str, request_digest: str) -> str:
    return f"""Contract: {REFERENCE_PROMPT_CONTRACT}
Identify the main reference papers for this theoretical-physics request.
Use web search and return only identifiers whose title and identifier are supported
by a reliable source. Prefer arXiv IDs, then DOI IDs. Return at most one candidate
for each materially distinct domain in the request. Do not omit a requested
domain merely because the request spans more than two domains.
Request digest: {request_digest}
User request:
{text}
"""


def _payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PaperWorkflowError(
            "reference_inference_output_invalid",
            "Reference inference output must be an object.",
        )
    return dict(value)


def _verify_payload(
    payload: Mapping[str, Any],
    *,
    metadata_lookup: MetadataLookup,
) -> dict[str, Any]:
    focus_scope = str(payload.get("focus_scope") or "unclear")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    paper_ids: list[str] = []
    verified_references: list[dict[str, JsonValue]] = []
    rejected_candidates: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    seen_domains: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            rejected_candidates.append({"error": "candidate_not_object"})
            continue
        domain = str(candidate.get("domain") or "").strip()
        domain_key = domain.casefold()
        if domain_key and domain_key in seen_domains:
            rejected_candidates.append(
                {
                    "paper_id": _candidate_identifier(candidate.get("paper_id")),
                    "error": "duplicate_domain_candidate",
                }
            )
            continue
        if focus_scope == "one_domain" and verified_references:
            rejected_candidates.append(
                {
                    "paper_id": _candidate_identifier(candidate.get("paper_id")),
                    "error": "focus_scope_exceeded",
                }
            )
            continue
        identifier = _candidate_identifier(candidate.get("paper_id"))
        urls = _evidence_urls(candidate.get("evidence_urls"))
        if not identifier or not urls:
            rejected_candidates.append(
                {
                    "paper_id": identifier,
                    "error": "missing_identifier_or_evidence",
                }
            )
            continue
        try:
            metadata = dict(metadata_lookup(identifier))
        except Exception as exc:
            rejected_candidates.append(
                {
                    "paper_id": identifier,
                    "error": f"metadata_lookup_failed: {exc}",
                }
            )
            continue
        preferred = _preferred_identifier(metadata, fallback=identifier)
        if not _supported_identifier(preferred) or preferred.casefold() in seen:
            continue
        seen.add(preferred.casefold())
        if domain_key:
            seen_domains.add(domain_key)
        paper_ids.append(preferred)
        verified_references.append(
            {
                "paper_id": preferred,
                "input_paper_id": identifier,
                "domain": domain,
                "llm_title": str(candidate.get("title") or ""),
                "verified_title": str(metadata.get("title") or ""),
                "evidence_urls": urls,
                "reasoning": str(candidate.get("reasoning") or ""),
            }
        )
    warnings = payload.get("warnings")
    normalized_warnings = (
        [str(item) for item in warnings] if isinstance(warnings, list) else []
    )
    if not paper_ids:
        normalized_warnings.append("No candidate passed deterministic metadata verification.")
    return {
        "paper_ids": paper_ids,
        "focus_scope": focus_scope,
        "warnings": normalized_warnings,
        "verified_references": verified_references,
        "rejected_candidates": rejected_candidates,
    }


def _verify_explicit_ids(
    identifiers: list[str],
    *,
    metadata_lookup: MetadataLookup,
) -> dict[str, Any]:
    paper_ids: list[str] = []
    verified_references: list[dict[str, JsonValue]] = []
    rejected_candidates: list[dict[str, JsonValue]] = []
    for identifier in identifiers:
        try:
            metadata = dict(metadata_lookup(identifier))
        except Exception as exc:
            rejected_candidates.append(
                {
                    "paper_id": identifier,
                    "error": f"metadata_lookup_failed: {exc}",
                }
            )
            continue
        preferred = _preferred_identifier(metadata, fallback=identifier)
        if not _supported_identifier(preferred):
            rejected_candidates.append(
                {"paper_id": identifier, "error": "metadata_identifier_unsupported"}
            )
            continue
        paper_ids.append(preferred)
        verified_references.append(
            {
                "paper_id": preferred,
                "input_paper_id": identifier,
                "domain": "",
                "llm_title": "",
                "verified_title": str(metadata.get("title") or ""),
                "evidence_urls": [],
                "reasoning": "Explicit identifier verified by deterministic metadata lookup.",
            }
        )
    warnings = (
        []
        if paper_ids
        else ["No explicit identifier passed deterministic metadata verification."]
    )
    return {
        "paper_ids": paper_ids,
        "warnings": warnings,
        "verified_references": verified_references,
        "rejected_candidates": rejected_candidates,
    }


def _candidate_identifier(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    identifiers = extract_paper_ids(raw)
    if identifiers:
        return identifiers[0]
    normalized = normalize_paper_id(raw)
    return normalized if _supported_identifier(normalized) else ""


def _unique_supported_ids(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_paper_id(value)
        key = normalized.casefold()
        if _supported_identifier(normalized) and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def _evidence_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        url
        for item in value
        if (url := str(item).strip()).startswith(("http://", "https://"))
    ]


def _preferred_identifier(metadata: Mapping[str, Any], *, fallback: str) -> str:
    if arxiv_id := metadata.get("arxiv_id"):
        return normalize_paper_id(f"arXiv:{arxiv_id}")
    paper_id = normalize_paper_id(str(metadata.get("paper_id") or ""))
    if arxiv_path_id(paper_id):
        return paper_id
    if doi := metadata.get("doi"):
        return normalize_paper_id(f"doi:{doi}")
    if _supported_identifier(paper_id):
        return paper_id
    return normalize_paper_id(fallback)


def _supported_identifier(identifier: str) -> bool:
    normalized = normalize_paper_id(identifier)
    return bool(
        arxiv_path_id(normalized)
        or doi_value(normalized)
        or inspire_recid(normalized)
    )
