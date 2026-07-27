"""Typed paper-summary service and its arc-jobs batch handler."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, TypeAlias

from arc_jobs import (
    ArtifactSourceRef,
    StoppedError,
    Failed,
    FailureMode,
    GroupResult,
    JsonValue,
    Paused,
    RunContext,
    RunEngine,
    RunError,
    RunRepository,
    RunSnapshot,
    RunSpec,
    Succeeded,
    UnitResult,
    WorkUnit,
    canonical_json_bytes,
)
from arc_llm import (
    JsonOutput,
    LLMStopped,
    LLMCompleted,
    LLMFailed,
    LLMExecutionOptions,
    LLMInputArtifact,
    LLMPaused,
    LLMRequest,
    LLMTaskOutcome,
    LLMTaskService,
    ModelSelection,
    ResumeInput,
)

from ..parse import ParsedDocument, parsed_document_from_document
from ._llm import (
    LLMCallProvenance,
    PaperWorkflowError,
    TaskService,
    awaiting_from_pause,
    execute_routed as _execute_routed,
    model_document as _model_document,
    outer_resume_input,
    provenance as _provenance,
    run_error_from_failure,
)

SUMMARY_BATCH_HANDLER = "arc.paper.summary_batch.v1"
SUMMARY_RESULT_SCHEMA = "arc.paper.summary.v2"
SUMMARY_BATCH_RESULT_SCHEMA = "arc.paper.summary_batch_result.v1"
SUMMARY_PROMPT_CONTRACT = "arc.paper.summary_prompt.v2"
SECTION_OUTPUT_CONTRACT = "arc.paper.section_summary.v1"
SYNTHESIS_OUTPUT_CONTRACT = "arc.paper.summary_synthesis.v1"

_SECTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SECTION_OUTPUT_CONTRACT,
    "type": "object",
    "additionalProperties": False,
    "required": ["section_id", "summary", "warnings"],
    "properties": {
        "section_id": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

_SYNTHESIS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SYNTHESIS_OUTPUT_CONTRACT,
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "high_value_summary", "reading_guide", "warnings"],
    "properties": {
        "title": {"type": "string"},
        "high_value_summary": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "reading_guide": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["purpose", "section_ids", "reason"],
                "properties": {
                    "purpose": {"type": "string"},
                    "section_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reason": {"type": "string"},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class SummarySection:
    section_id: str
    title: str
    summary: str
    warnings: tuple[str, ...] = ()

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "summary": self.summary,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PaperSummaryResult:
    document_digest: str
    source_digest: str
    sections: tuple[SummarySection, ...]
    synthesis: Mapping[str, JsonValue]
    provenance: tuple[LLMCallProvenance, ...]

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SUMMARY_RESULT_SCHEMA,
            "document_digest": self.document_digest,
            "source_digest": self.source_digest,
            "sections": [item.to_document() for item in self.sections],
            "synthesis": dict(self.synthesis),
            "provenance": [item.to_document() for item in self.provenance],
        }


@dataclass(frozen=True)
class PaperSummaryCompleted:
    result: PaperSummaryResult


PaperSummaryOutcome: TypeAlias = (
    PaperSummaryCompleted | LLMPaused | LLMFailed | LLMStopped
)


@dataclass(frozen=True)
class SummaryBatchItem:
    """Semantic document identity plus its non-semantic repository locator."""

    item_id: str
    document: ParsedDocument
    source: ArtifactSourceRef

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("summary batch item_id must be non-empty")

    def semantic_document(self) -> dict[str, JsonValue]:
        return {
            "item_id": self.item_id,
            "document_digest": self.document.document_digest,
            "source_digest": self.document.source.artifact_digest,
            "source_size": self.document.source.size,
            "source_media_type": self.document.source.media_type,
            "document_artifact_digest": self.source.expected_digest.value,
            "document_artifact_size": self.source.expected_digest.size_bytes,
        }


class PaperSummaryService:
    """Summarize any strict ParsedDocument artifact inside one parent run."""

    def __init__(self, task_service: TaskService | None = None) -> None:
        self.task_service = task_service or LLMTaskService()

    def summarize(
        self,
        context: RunContext,
        source: ArtifactSourceRef,
        *,
        expected_document_digest: str,
        model: ModelSelection = ModelSelection(tier="low"),
        resume_input: ResumeInput | None = None,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> PaperSummaryOutcome:
        parsed = self._read_document(
            context, source, expected_document_digest=expected_document_digest
        )
        artifact_input = LLMInputArtifact(
            "parsed-document", source, "application/json"
        )
        section_results: list[SummarySection] = []
        provenance: list[LLMCallProvenance] = []
        for section in _summary_sections(parsed):
            request = _section_request(
                parsed,
                section,
                artifact_input=artifact_input,
                model=model,
            )
            outcome = _execute_routed(
                self.task_service,
                context,
                request,
                resume_input=resume_input,
                options=options,
            )
            if not isinstance(outcome, LLMCompleted):
                return outcome
            value = _json_object(outcome.value, "section summary")
            returned_section_id = _required_string(value, "section_id")
            if returned_section_id != section.section_id:
                raise PaperWorkflowError(
                    "summary_output_invalid",
                    (
                        "Section summary identity does not match the requested "
                        f"section: expected {section.section_id!r}, got "
                        f"{returned_section_id!r}"
                    ),
                )
            section_results.append(
                SummarySection(
                    returned_section_id,
                    section.title,
                    _required_string(value, "summary"),
                    _string_tuple(value.get("warnings"), "warnings"),
                )
            )
            provenance.append(_provenance(request.task_id, outcome))

        synthesis_request = _synthesis_request(
            parsed,
            tuple(section_results),
            artifact_input=artifact_input,
            model=model,
        )
        synthesis_outcome = _execute_routed(
            self.task_service,
            context,
            synthesis_request,
            resume_input=resume_input,
            options=options,
        )
        if not isinstance(synthesis_outcome, LLMCompleted):
            return synthesis_outcome
        synthesis = _json_object(synthesis_outcome.value, "paper synthesis")
        provenance.append(_provenance(synthesis_request.task_id, synthesis_outcome))
        return PaperSummaryCompleted(
            PaperSummaryResult(
                document_digest=parsed.document_digest,
                source_digest=parsed.source.artifact_digest,
                sections=tuple(section_results),
                synthesis=synthesis,
                provenance=tuple(provenance),
            )
        )

    @staticmethod
    def _read_document(
        context: RunContext,
        source: ArtifactSourceRef,
        *,
        expected_document_digest: str,
    ) -> ParsedDocument:
        try:
            verified = context.artifacts.read_source(source)
        except Exception as exc:
            raise PaperWorkflowError(
                "document_artifact_invalid", f"Cannot verify parsed document: {exc}"
            ) from exc
        if verified.media_type != "application/json":
            raise PaperWorkflowError(
                "document_media_type_unsupported",
                "ParsedDocument artifacts must use application/json.",
            )
        try:
            value = json.loads(verified.content.decode("utf-8"))
            parsed = parsed_document_from_document(_json_object(value, "parsed document"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PaperWorkflowError(
                "parsed_document_invalid", f"Cannot decode ParsedDocument: {exc}"
            ) from exc
        if parsed.document_digest != expected_document_digest:
            raise PaperWorkflowError(
                "document_identity_mismatch",
                "ParsedDocument digest differs from the requested semantic identity.",
            )
        return parsed


class SummaryBatchHandler:
    """One RunHandler for standalone and batch paper summaries."""

    name = SUMMARY_BATCH_HANDLER

    def __init__(
        self,
        items: tuple[SummaryBatchItem, ...],
        *,
        service: PaperSummaryService | None = None,
        model: ModelSelection = ModelSelection(tier="low"),
        max_workers: int = 1,
        failure_mode: FailureMode = FailureMode.COLLECT,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        self.items = tuple(items)
        self.service = service or PaperSummaryService()
        self.model = model
        self.max_workers = max_workers
        self.failure_mode = failure_mode
        self.options = options
        self._by_unit = {
            _unit_id(item.semantic_document()): item for item in self.items
        }
        if len(self._by_unit) != len(self.items):
            raise ValueError("summary batch contains duplicate semantic items")

    def semantic_input(self) -> dict[str, JsonValue]:
        return {
            "schema_version": "arc.paper.summary_batch.v1",
            "operation": "paper_summary",
            "items": [item.semantic_document() for item in self.items],
            "model_requirement": _model_document(self.model),
            "failure_mode": self.failure_mode.value,
        }

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "summary_batch_binding_mismatch",
                    "Handler bindings do not match the durable batch semantic input.",
                )
            )
        try:
            resume_input = outer_resume_input(
                context, error_code="summary_resume_input_invalid"
            )
        except PaperWorkflowError as exc:
            return Failed(RunError(exc.code, str(exc)))
        units = tuple(
            WorkUnit(_unit_id(item.semantic_document()), item.semantic_document())
            for item in self.items
        )

        def worker(unit: WorkUnit):
            item = self._by_unit[unit.unit_id]
            try:
                outcome = self.service.summarize(
                    context,
                    item.source,
                    expected_document_digest=item.document.document_digest,
                    model=self.model,
                    resume_input=resume_input,
                    options=self.options,
                )
            except PaperWorkflowError as exc:
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=RunError(exc.code, str(exc)),
                )
            if isinstance(outcome, PaperSummaryCompleted):
                return context.artifacts.publish_json(
                    f"summaries/{unit.unit_id}", outcome.result.to_document()
                )
            if isinstance(outcome, LLMPaused):
                return Paused(awaiting_from_pause(outcome))
            if isinstance(outcome, LLMFailed):
                return UnitResult(
                    unit.unit_id,
                    "failed",
                    error=run_error_from_failure(outcome),
                )
            if isinstance(outcome, LLMStopped):
                raise StoppedError("summary LLM task stopped")
            raise RuntimeError("unknown paper-summary outcome")

        result = context.run_group(
            _group_id(self.semantic_input()),
            units,
            worker,
            max_workers=self.max_workers,
            failure_mode=self.failure_mode,
        )
        if isinstance(result, Paused):
            return result
        assert isinstance(result, GroupResult)
        artifact = context.artifacts.publish_json(
            "summary-batch/result",
            _batch_result_document(
                result,
                all_unit_ids=tuple(unit.unit_id for unit in units),
                failure_mode=self.failure_mode,
            ),
        )
        return Succeeded(artifact)


class SummaryBatchRunner:
    """Thin standalone wrapper over the same durable batch RunHandler."""

    def __init__(self, repository: RunRepository) -> None:
        self.repository = repository
        self.engine = RunEngine(repository)

    def execute(
        self,
        run_id: str,
        items: tuple[SummaryBatchItem, ...],
        *,
        service: PaperSummaryService | None = None,
        model: ModelSelection = ModelSelection(tier="low"),
        max_workers: int = 1,
        failure_mode: FailureMode = FailureMode.COLLECT,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> RunSnapshot:
        handler = SummaryBatchHandler(
            items,
            service=service,
            model=model,
            max_workers=max_workers,
            failure_mode=failure_mode,
            options=options,
        )
        return self.engine.execute(
            RunSpec(run_id, handler.name, handler.semantic_input()), handler
        )

    def resume(
        self,
        run_id: str,
        items: tuple[SummaryBatchItem, ...],
        *,
        input: Mapping[str, JsonValue] | None = None,
        service: PaperSummaryService | None = None,
        model: ModelSelection = ModelSelection(tier="low"),
        max_workers: int = 1,
        failure_mode: FailureMode = FailureMode.COLLECT,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> RunSnapshot:
        handler = SummaryBatchHandler(
            items,
            service=service,
            model=model,
            max_workers=max_workers,
            failure_mode=failure_mode,
            options=options,
        )
        return self.engine.resume(run_id, handler, input=input)

    def execute_one(
        self,
        run_id: str,
        item: SummaryBatchItem,
        **kwargs: Any,
    ) -> RunSnapshot:
        return self.execute(run_id, (item,), max_workers=1, **kwargs)


def _summary_sections(parsed: ParsedDocument):
    if parsed.sections:
        return parsed.sections
    text = "\n\n".join(page.text for page in parsed.pages)
    if not text:
        text = json.dumps(dict(parsed.metadata), ensure_ascii=False, sort_keys=True)
    from ..parse import ParsedSection

    return (ParsedSection("document", "Document", 1, text, 0),)


def _section_request(
    parsed: ParsedDocument,
    section: Any,
    *,
    artifact_input: LLMInputArtifact,
    model: ModelSelection,
) -> LLMRequest:
    section_digest = _digest(
        {
            "section_id": section.section_id,
            "title": section.title,
            "level": section.level,
            "text": section.text,
        }
    )
    prompt = "\n".join(
        (
            f"Contract: {SUMMARY_PROMPT_CONTRACT}",
            "Summarize exactly one section of the ParsedDocument copied into your workspace.",
            "Use only that workspace document. Do not add facts from memory.",
            f"Document digest: {parsed.document_digest}",
            f"Source digest: {parsed.source.artifact_digest}",
            f"Section digest: {section_digest}",
            f"Section ID: {section.section_id}",
            f"Section title: {section.title}",
            "Section text:",
            section.text,
        )
    )
    identity = {
        "kind": "section",
        "prompt_contract": SUMMARY_PROMPT_CONTRACT,
        "output_contract": SECTION_OUTPUT_CONTRACT,
        "document_digest": parsed.document_digest,
        "source_digest": parsed.source.artifact_digest,
        "section_digest": section_digest,
        "model_requirement": _model_document(model),
    }
    return LLMRequest(
        _task_id("summary-section", identity),
        prompt,
        JsonOutput(_section_schema(section.section_id)),
        model,
        inputs=(artifact_input,),
    )


def _section_schema(section_id: str) -> dict[str, Any]:
    """Bind the machine-authored section identity before LLM acceptance."""

    schema = deepcopy(_SECTION_SCHEMA)
    schema["properties"]["section_id"]["const"] = section_id
    return schema


def _synthesis_request(
    parsed: ParsedDocument,
    sections: tuple[SummarySection, ...],
    *,
    artifact_input: LLMInputArtifact,
    model: ModelSelection,
) -> LLMRequest:
    section_documents = [item.to_document() for item in sections]
    section_digest = _digest(section_documents)
    prompt = "\n".join(
        (
            f"Contract: {SUMMARY_PROMPT_CONTRACT}",
            "Synthesize the ParsedDocument copied into your workspace from the deterministic section summaries below.",
            "Use only that workspace document and the summaries. Return concise research guidance.",
            f"Document digest: {parsed.document_digest}",
            f"Source digest: {parsed.source.artifact_digest}",
            f"Section summaries digest: {section_digest}",
            json.dumps(section_documents, ensure_ascii=False, sort_keys=True),
        )
    )
    identity = {
        "kind": "synthesis",
        "prompt_contract": SUMMARY_PROMPT_CONTRACT,
        "output_contract": SYNTHESIS_OUTPUT_CONTRACT,
        "document_digest": parsed.document_digest,
        "source_digest": parsed.source.artifact_digest,
        "section_digest": section_digest,
        "model_requirement": _model_document(model),
    }
    return LLMRequest(
        _task_id("summary-synthesis", identity),
        prompt,
        JsonOutput(_SYNTHESIS_SCHEMA),
        model,
        inputs=(artifact_input,),
    )


def _batch_result_document(
    result: GroupResult,
    *,
    all_unit_ids: tuple[str, ...],
    failure_mode: FailureMode,
) -> dict[str, JsonValue]:
    completed_ids = {item.unit_id for item in result.units}
    return {
        "schema_version": SUMMARY_BATCH_RESULT_SCHEMA,
        "group_id": result.group_id,
        "failure_mode": failure_mode.value,
        "complete": len(completed_ids) == len(all_unit_ids),
        "units": [
            {
                "unit_id": item.unit_id,
                "status": item.status,
                "value": item.value,
                "error": (
                    None
                    if item.error is None
                    else {
                        "code": item.error.code,
                        "message": item.error.message,
                        "details": dict(item.error.details),
                    }
                ),
            }
            for item in result.units
        ],
        "pending_unit_ids": [
            unit_id for unit_id in all_unit_ids if unit_id not in completed_ids
        ],
    }


def _unit_id(value: Mapping[str, JsonValue]) -> str:
    return f"item-{_digest(value)[:24]}"


def _group_id(value: Mapping[str, JsonValue]) -> str:
    return f"summary-{_digest(value)[:24]}"


def _task_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}-{_digest(value)[:24]}"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PaperWorkflowError(
            "summary_output_invalid", f"{description} must be a JSON object"
        )
    return dict(value)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise PaperWorkflowError(
            "summary_output_invalid", f"{key} must be a non-empty string"
        )
    return item


def _string_tuple(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PaperWorkflowError(
            "summary_output_invalid", f"{key} must be an array of strings"
        )
    return tuple(value)
