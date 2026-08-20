"""Durable page-by-page PDF-vision proofreading workflow."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from arc_jobs import (
    ArtifactSourceRef,
    Awaiting,
    Failed,
    FailureMode,
    Paused,
    ResumeReason,
    RunContext,
    RunError,
    Succeeded,
    UnitResult,
    WorkUnit,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
)
from arc_llm import (
    ExecutionLimits,
    JsonOutput,
    LLMCompleted,
    LLMExecutionOptions,
    LLMExecutionProfile,
    LLMFailed,
    LLMInputArtifact,
    LLMPaused,
    LLMRequest,
    LLMStopped,
    LLMTaskService,
    ModelSelection,
    ProviderGateOptions,
)
from arc_paper import (
    PdftoppmFullPageRenderer,
    RichBlockKind,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    parse_rich_artifact_bytes,
)

from .project import ProofreadProject
from .source import MineruPage, MineruSource, load_mineru_source, sha256_file


HANDLER = "arc.ocr_proofread.document.v1"
BOUNDARY_REPAIR_HANDLER = "arc.ocr_proofread.boundary_repair.v1"
PROMPT_VERSION = "arc.ocr_proofread.page_prompt.v6"
BOUNDARY_PROMPT_VERSION = "arc.ocr_proofread.boundary_prompt.v1"
RESULT_SCHEMA = "arc.ocr_proofread.result.v1"
PAGE_SCHEMA = "arc.ocr_proofread.page_result.v1"
REVIEW_SCHEMA = "arc.ocr_proofread.review_request.v1"
AUDIT_SCHEMA = "arc.ocr_proofread.audit_request.v1"
GROUP_ID = "pages"
BOUNDARY_GROUP_ID = "boundaries"
BOUNDARY_IMAGE_GROUP_ID = "boundary-page-images"
BOUNDARY_REVIEW_SCHEMA = "arc.ocr_proofread.boundary_review_request.v3"
PROVIDER_IDLE_TIMEOUT_SECONDS = 600
_IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


PAGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edits", "source_typo_candidates", "uncertainties", "checks"],
    "properties": {
        "edits": {
            "type": "array",
            "items": {"$ref": "#/$defs/edit"},
        },
        "source_typo_candidates": {
            "type": "array",
            "items": {"$ref": "#/$defs/edit"},
        },
        "uncertainties": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["excerpt", "reason"],
                "properties": {
                    "excerpt": {"type": "string"},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
        "checks": {
            "type": "object",
            "additionalProperties": False,
            "required": ["all_visible_text", "all_visible_equations", "page_boundary"],
            "properties": {
                "all_visible_text": {"type": "boolean"},
                "all_visible_equations": {"type": "boolean"},
                "page_boundary": {"type": "boolean"},
            },
        },
    },
    "$defs": {
        "edit": {
            "type": "object",
            "additionalProperties": False,
            "required": ["before", "after", "occurrence", "kind", "reason"],
            "properties": {
                "before": {"type": "string"},
                "after": {"type": "string"},
                "occurrence": {"type": "integer", "minimum": 1},
                "kind": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
        }
    },
}


BOUNDARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "join_mode", "reason"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["separate", "join", "uncertain"],
        },
        "join_mode": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": ["space", "none", "drop_hyphen"],
                },
                {"type": "null"},
            ]
        },
        "reason": {"type": "string", "minLength": 1},
    },
}


@dataclass(frozen=True)
class ProofreadConfig:
    project_dir: str
    markdown: str
    pdf: str
    content_list: str
    markdown_sha256: str
    pdf_sha256: str
    content_list_sha256: str
    provider: str = "auto"
    model: str | None = None
    model_tier: str = "medium"
    workers: int = 30
    max_workers: int = 200

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= self.max_workers <= 200:
            raise ValueError("workers must satisfy 1 <= workers <= max_workers <= 200")

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": "arc.ocr_proofread.request.v1",
            "project_dir": self.project_dir,
            "markdown": self.markdown,
            "pdf": self.pdf,
            "content_list": self.content_list,
            "markdown_sha256": self.markdown_sha256,
            "pdf_sha256": self.pdf_sha256,
            "content_list_sha256": self.content_list_sha256,
            "provider": self.provider,
            "model": self.model,
            "model_tier": self.model_tier,
            "workers": self.workers,
            "max_workers": self.max_workers,
            "prompt_version": PROMPT_VERSION,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "ProofreadConfig":
        if value.get("schema_version") != "arc.ocr_proofread.request.v1":
            raise ValueError("unsupported OCR proofreading request")
        return cls(
            project_dir=str(value["project_dir"]),
            markdown=str(value["markdown"]),
            pdf=str(value["pdf"]),
            content_list=str(value["content_list"]),
            markdown_sha256=str(value["markdown_sha256"]),
            pdf_sha256=str(value["pdf_sha256"]),
            content_list_sha256=str(value["content_list_sha256"]),
            provider=str(value["provider"]),
            model=value.get("model") if isinstance(value.get("model"), str) else None,
            model_tier=str(value["model_tier"]),
            workers=int(value["workers"]),
            max_workers=int(value["max_workers"]),
        )


@dataclass(frozen=True)
class BoundaryRepairConfig:
    project_dir: str
    pdf: str
    baseline_markdown_sha256: str
    baseline_manifest_sha256: str
    baseline_changes_sha256: str
    pdf_sha256: str
    provider: str = "auto"
    model: str | None = None
    model_tier: str = "medium"
    workers: int = 30
    max_workers: int = 200

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= self.max_workers <= 200:
            raise ValueError(
                "workers must satisfy 1 <= workers <= max_workers <= 200"
            )

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": "arc.ocr_proofread.boundary_repair_request.v1",
            "project_dir": self.project_dir,
            "pdf": self.pdf,
            "baseline_markdown_sha256": self.baseline_markdown_sha256,
            "baseline_manifest_sha256": self.baseline_manifest_sha256,
            "baseline_changes_sha256": self.baseline_changes_sha256,
            "pdf_sha256": self.pdf_sha256,
            "provider": self.provider,
            "model": self.model,
            "model_tier": self.model_tier,
            "workers": self.workers,
            "max_workers": self.max_workers,
            "boundary_prompt_version": BOUNDARY_PROMPT_VERSION,
        }

    @classmethod
    def from_document(
        cls, value: Mapping[str, Any]
    ) -> "BoundaryRepairConfig":
        if value.get("schema_version") != (
            "arc.ocr_proofread.boundary_repair_request.v1"
        ):
            raise ValueError("unsupported OCR boundary-repair request")
        return cls(
            project_dir=str(value["project_dir"]),
            pdf=str(value["pdf"]),
            baseline_markdown_sha256=str(
                value["baseline_markdown_sha256"]
            ),
            baseline_manifest_sha256=str(
                value["baseline_manifest_sha256"]
            ),
            baseline_changes_sha256=str(value["baseline_changes_sha256"]),
            pdf_sha256=str(value["pdf_sha256"]),
            provider=str(value["provider"]),
            model=(
                value.get("model")
                if isinstance(value.get("model"), str)
                else None
            ),
            model_tier=str(value["model_tier"]),
            workers=int(value["workers"]),
            max_workers=int(value["max_workers"]),
        )


class ProofreadWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProofreadHandler:
    name = HANDLER

    def __init__(
        self,
        config: ProofreadConfig | BoundaryRepairConfig,
        *,
        task_service: Any | None = None,
        renderer: Any | None = None,
    ) -> None:
        self.config = config
        self.task_service = task_service or LLMTaskService()
        self.renderer = renderer or PdftoppmFullPageRenderer(longest_edge=2400)

    def semantic_input(self) -> dict[str, Any]:
        return self.config.document()

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(RunError("request_binding_mismatch", "handler request differs from durable spec"))
        try:
            source = load_mineru_source(
                self.config.markdown,
                self.config.pdf,
                self.config.content_list,
            )
            self._verify_hashes(source)
            pdf_bytes = source.pdf_path.read_bytes()
            units = tuple(
                WorkUnit(
                    f"page-{page.page_index + 1:06d}",
                    {
                        "page_index": page.page_index,
                        "markdown_sha256": hashlib.sha256(page.markdown.encode()).hexdigest(),
                        "pdf_sha256": source.pdf_sha256,
                        "prompt_version": PROMPT_VERSION,
                    },
                )
                for page in source.pages
            )
            page_by_id = {unit.unit_id: source.pages[index] for index, unit in enumerate(units)}

            def worker(unit: WorkUnit):
                context.checkpoint()
                page = page_by_id[unit.unit_id]
                return self._proofread_page(context, source, page, pdf_bytes)

            grouped = context.run_group(
                GROUP_ID,
                units,
                worker,
                max_workers=self.config.workers,
                failure_mode=FailureMode.COLLECT,
            )
            if isinstance(grouped, Paused):
                return grouped
            failed = [unit for unit in grouped.units if unit.status == "failed"]
            if failed:
                return Failed(
                    RunError(
                        "page_proofreading_failed",
                        f"{len(failed)} page tasks failed",
                        {"pages": [unit.unit_id for unit in failed[:50]]},
                    )
                )
            pages = [dict(unit.value) for unit in grouped.units if isinstance(unit.value, Mapping)]
            pages.sort(key=lambda value: int(value["page_index"]))
            review = self._resolve_review(context, pages)
            if isinstance(review, Paused):
                return review
            corrected_pages, source_corrections = review
            boundaries = self._reconcile_boundaries(
                context, corrected_pages
            )
            if isinstance(boundaries, Paused):
                return boundaries
            corrected_pages = boundaries
            audit = self._resolve_audit(context, corrected_pages)
            if isinstance(audit, Paused):
                return audit
            if isinstance(audit, RunError):
                return Failed(audit)
            result_ref = self._publish(context, source, corrected_pages, source_corrections, audit)
            return Succeeded(result_ref)
        except ProofreadWorkflowError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except Exception as exc:
            return Failed(RunError(getattr(exc, "code", "proofread_failed"), str(exc)))

    def _verify_hashes(self, source: MineruSource) -> None:
        actual = (source.markdown_sha256, source.pdf_sha256, source.content_list_sha256)
        expected = (
            self.config.markdown_sha256,
            self.config.pdf_sha256,
            self.config.content_list_sha256,
        )
        if actual != expected:
            raise ProofreadWorkflowError("source_changed", "source bytes changed after run creation")
        if source.page_count < 1:
            raise ProofreadWorkflowError("page_map_empty", "source has no mapped pages")

    def _proofread_page(
        self,
        context: RunContext,
        source: MineruSource,
        page: MineruPage,
        pdf_bytes: bytes,
    ) -> UnitResult | Paused:
        rendered = self.renderer.render_page(pdf_bytes, page.page_index + 1)
        page_ref = context.artifacts.publish_bytes(
            f"page-images/{page.page_index + 1:06d}",
            rendered.png_bytes,
            media_type="image/png",
        )
        image_input = LLMInputArtifact(
            "page",
            ArtifactSourceRef(context.run_id, page_ref.artifact_id, page_ref.digest),
            "image/png",
        )
        request = self._page_request(source, page, image_input)
        options = LLMExecutionOptions(
            profile=LLMExecutionProfile.BOUNDED,
            internet=False,
            limits=ExecutionLimits(
                idle_timeout_seconds=PROVIDER_IDLE_TIMEOUT_SECONDS
            ),
            gate=ProviderGateOptions(
                global_limit=self.config.max_workers,
                minimum_available_memory_fraction=0.10,
            ),
        )
        feedback = None
        for attempt in (1, 2):
            current = request if feedback is None else _retry_request(request, feedback)
            outcome = self.task_service.execute_or_resume(context, current, options=options)
            if isinstance(outcome, LLMPaused):
                return Paused(
                    Awaiting(
                        outcome.reason,
                        outcome.resume_key,
                        outcome.input_required,
                        outcome.request_ref,
                        outcome.response_contract,
                        outcome.details,
                    )
                )
            if isinstance(outcome, LLMStopped):
                context.checkpoint()
                return UnitResult(
                    f"page-{page.page_index + 1:06d}",
                    "failed",
                    error=RunError("provider_stopped", "page provider stopped"),
                )
            if isinstance(outcome, LLMFailed):
                return UnitResult(
                    f"page-{page.page_index + 1:06d}",
                    "failed",
                    error=RunError(outcome.error.code.value, str(outcome.error)),
                )
            assert isinstance(outcome, LLMCompleted)
            try:
                value = _validate_page_output(page, outcome.value)
            except ProofreadWorkflowError as exc:
                feedback = exc
                context.working.write_candidate_json(
                    f"pages/{page.page_index + 1:06d}-attempt-{attempt}.json",
                    outcome.value if isinstance(outcome.value, Mapping) else {"value": outcome.value},
                )
                continue
            value["provider"] = outcome.provider
            value["model"] = outcome.model
            value["image_artifact"] = page_ref.artifact_id
            return UnitResult(f"page-{page.page_index + 1:06d}", "succeeded", value)
        assert feedback is not None
        return UnitResult(
            f"page-{page.page_index + 1:06d}",
            "failed",
            error=RunError(feedback.code, str(feedback)),
        )

    def _page_request(
        self,
        source: MineruSource,
        page: MineruPage,
        image_input: LLMInputArtifact,
    ) -> LLMRequest:
        previous = source.pages[page.page_index - 1].markdown[-1200:] if page.page_index else ""
        following = (
            source.pages[page.page_index + 1].markdown[:1200]
            if page.page_index + 1 < source.page_count
            else ""
        )
        prompt = f"""Proofread OCR Markdown against the attached complete PDF page.

Do not call tools or access files. All required OCR text is included below; inspect the attached image directly and return JSON only.

Return only exact edit operations. Each `before` must be a literal non-empty substring of the evolving current-page Markdown; `occurrence` is one-based. Only when the entire current-page Markdown is empty, use `before` as an empty string with `occurrence` 1 to insert the complete page transcription. Use a larger exact span when inserting omitted text or resolving repeated text. Preserve author wording, notation, paragraph order, emphasis, equation order, equation tags, and all image links. Correct every visible OCR mismatch, equation symbol, omission, heading, list, footnote, caption, and table error. Do not rewrite or explain.

Actively identify obvious errors printed in the original source, but put them only in `source_typo_candidates`; never put them in applied `edits`. If unsure, add an uncertainty and preserve the OCR text. Mark checks true only after exhaustive visual comparison.

Previous-page boundary context:
{previous}

CURRENT PAGE MARKDOWN:
{page.markdown}

Next-page boundary context:
{following}
"""
        return LLMRequest(
            f"ocr-proofread-page-{page.page_index + 1:06d}",
            prompt,
            JsonOutput(PAGE_OUTPUT_SCHEMA, repair="format"),
            ModelSelection(self.config.provider, self.config.model, self.config.model_tier),
            inputs=(image_input,),
        )

    def _resolve_review(
        self, context: RunContext, pages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | Paused:
        candidates: list[dict[str, Any]] = []
        for page in pages:
            for kind in ("source_typo_candidates", "uncertainties"):
                for index, item in enumerate(page[kind]):
                    candidate = dict(item)
                    candidate["id"] = f"p{int(page['page_index']) + 1:06d}-{kind}-{index + 1:03d}"
                    candidate["page_index"] = page["page_index"]
                    candidate["type"] = kind
                    candidates.append(candidate)
        decisions_ref = context.artifacts.find("review/decisions")
        if candidates and decisions_ref is None:
            key = _key("review", candidates)
            if context.resume_input is None:
                request_ref = context.artifacts.publish_json(
                    "review/request",
                    {"schema_version": REVIEW_SCHEMA, "resume_key": key, "items": candidates},
                )
                return Paused(
                    Awaiting(
                        ResumeReason.SUPERVISION_REQUIRED,
                        key,
                        True,
                        request_ref,
                        "arc.ocr_proofread.review_input.v1",
                        {"count": len(candidates)},
                    )
                )
            decisions = _validate_review_input(context.resume_input, key, candidates)
            decisions_ref = context.artifacts.publish_json("review/decisions", decisions)
        decision_map: dict[str, Mapping[str, Any]] = {}
        if decisions_ref is not None:
            decisions_doc = json.loads(context.artifacts.read_bytes(decisions_ref).decode("utf-8"))
            decision_map = {str(item["id"]): item for item in decisions_doc["decisions"]}

        source_corrections: list[dict[str, Any]] = []
        for candidate in candidates:
            decision = decision_map.get(candidate["id"])
            if decision is None or decision["action"] == "reject":
                continue
            page = pages[int(candidate["page_index"])]
            edit = candidate if candidate["type"] == "source_typo_candidates" else decision.get("edit")
            if not isinstance(edit, Mapping):
                raise ProofreadWorkflowError("review_input_invalid", "accepted uncertainty requires edit")
            page["corrected_markdown"] = _apply_edits(page["corrected_markdown"], [edit])
            record = _change_record(int(page["page_index"]), len(source_corrections), edit, "approved_source_correction")
            page["changes"].append(record)
            source_corrections.append(record)
        return pages, source_corrections

    def _reconcile_boundaries(
        self, context: RunContext, pages: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | Paused:
        if len(pages) < 2:
            return pages
        candidates = [
            {
                "boundary_index": index,
                "left_page_index": index,
                "right_page_index": index + 1,
                "left": _boundary_paragraph(
                    str(pages[index]["corrected_markdown"]), side="left"
                ),
                "right": _boundary_paragraph(
                    str(pages[index + 1]["corrected_markdown"]), side="right"
                ),
            }
            for index in range(len(pages) - 1)
        ]
        units = tuple(
            WorkUnit(
                f"boundary-{item['left_page_index'] + 1:06d}-{item['right_page_index'] + 1:06d}",
                {
                    "left_page_index": item["left_page_index"],
                    "right_page_index": item["right_page_index"],
                    "left_markdown_sha256": hashlib.sha256(
                        str(pages[item["left_page_index"]]["corrected_markdown"]).encode()
                    ).hexdigest(),
                    "right_markdown_sha256": hashlib.sha256(
                        str(pages[item["right_page_index"]]["corrected_markdown"]).encode()
                    ).hexdigest(),
                    "prompt_version": BOUNDARY_PROMPT_VERSION,
                },
            )
            for item in candidates
        )
        by_id = {
            unit.unit_id: candidates[index]
            for index, unit in enumerate(units)
        }

        def worker(unit: WorkUnit):
            context.checkpoint()
            item = by_id[unit.unit_id]
            return self._review_boundary(context, pages, item, unit.unit_id)

        grouped = context.run_group(
            BOUNDARY_GROUP_ID,
            units,
            worker,
            max_workers=self.config.workers,
            failure_mode=FailureMode.COLLECT,
        )
        if isinstance(grouped, Paused):
            return grouped
        failed = [unit for unit in grouped.units if unit.status == "failed"]
        if failed:
            raise ProofreadWorkflowError(
                "boundary_review_failed",
                f"{len(failed)} page-boundary tasks failed",
            )
        results = [
            dict(unit.value)
            for unit in grouped.units
            if isinstance(unit.value, Mapping)
        ]
        results.sort(key=lambda item: int(item["boundary_index"]))
        decisions = self._resolve_boundary_review(context, results, pages)
        if isinstance(decisions, Paused):
            return decisions
        return _apply_boundary_decisions(pages, results, decisions)

    def _review_boundary(
        self,
        context: RunContext,
        pages: list[dict[str, Any]],
        item: Mapping[str, Any],
        unit_id: str,
    ) -> UnitResult | Paused:
        left_page = pages[int(item["left_page_index"])]
        right_page = pages[int(item["right_page_index"])]
        inputs: list[LLMInputArtifact] = []
        for name, page in (("left-page", left_page), ("right-page", right_page)):
            image_ref = context.artifacts.find(str(page["image_artifact"]))
            if image_ref is None:
                raise ProofreadWorkflowError(
                    "boundary_image_missing", "page image is unavailable"
                )
            inputs.append(
                LLMInputArtifact(
                    name,
                    ArtifactSourceRef(
                        context.run_id,
                        image_ref.artifact_id,
                        image_ref.digest,
                    ),
                    "image/png",
                )
            )
        request = self._boundary_request(
            item,
            left_markdown=str(left_page["corrected_markdown"]),
            right_markdown=str(right_page["corrected_markdown"]),
            inputs=tuple(inputs),
        )
        options = LLMExecutionOptions(
            profile=LLMExecutionProfile.BOUNDED,
            internet=False,
            limits=ExecutionLimits(
                idle_timeout_seconds=PROVIDER_IDLE_TIMEOUT_SECONDS
            ),
            gate=ProviderGateOptions(
                global_limit=self.config.max_workers,
                minimum_available_memory_fraction=0.10,
            ),
        )
        feedback: ProofreadWorkflowError | None = None
        for attempt in (1, 2):
            current = request if feedback is None else _retry_request(request, feedback)
            outcome = self.task_service.execute_or_resume(
                context, current, options=options
            )
            if isinstance(outcome, LLMPaused):
                if outcome.reason is ResumeReason.EXECUTION_INTERRUPTED:
                    return UnitResult(
                        unit_id,
                        "succeeded",
                        {
                            "schema_version": "arc.ocr_proofread.boundary_result.v1",
                            "boundary_index": item["boundary_index"],
                            "left_page_index": item["left_page_index"],
                            "right_page_index": item["right_page_index"],
                            "left": item["left"],
                            "right": item["right"],
                            "action": "uncertain",
                            "join_mode": None,
                            "reason": (
                                "The provider was interrupted before returning a "
                                "usable boundary decision; main-agent review is required."
                            ),
                            "provider": None,
                            "model": None,
                            "left_image_artifact": left_page["image_artifact"],
                            "right_image_artifact": right_page["image_artifact"],
                        },
                    )
                return Paused(
                    Awaiting(
                        outcome.reason,
                        outcome.resume_key,
                        outcome.input_required,
                        outcome.request_ref,
                        outcome.response_contract,
                        outcome.details,
                    )
                )
            if isinstance(outcome, LLMStopped):
                context.checkpoint()
                return UnitResult(
                    unit_id,
                    "failed",
                    error=RunError("provider_stopped", "boundary provider stopped"),
                )
            if isinstance(outcome, LLMFailed):
                return UnitResult(
                    unit_id,
                    "failed",
                    error=RunError(outcome.error.code.value, str(outcome.error)),
                )
            assert isinstance(outcome, LLMCompleted)
            try:
                value = _validate_boundary_output(item, outcome.value)
            except ProofreadWorkflowError as exc:
                feedback = exc
                context.working.write_candidate_json(
                    f"boundaries/{int(item['boundary_index']) + 1:06d}-attempt-{attempt}.json",
                    outcome.value
                    if isinstance(outcome.value, Mapping)
                    else {"value": outcome.value},
                )
                continue
            value.update(
                {
                    "schema_version": "arc.ocr_proofread.boundary_result.v1",
                    "boundary_index": item["boundary_index"],
                    "left_page_index": item["left_page_index"],
                    "right_page_index": item["right_page_index"],
                    "left": item["left"],
                    "right": item["right"],
                    "provider": outcome.provider,
                    "model": outcome.model,
                    "left_image_artifact": left_page["image_artifact"],
                    "right_image_artifact": right_page["image_artifact"],
                }
            )
            return UnitResult(unit_id, "succeeded", value)
        assert feedback is not None
        return UnitResult(
            unit_id,
            "failed",
            error=RunError(feedback.code, str(feedback)),
        )

    def _boundary_request(
        self,
        item: Mapping[str, Any],
        *,
        left_markdown: str,
        right_markdown: str,
        inputs: tuple[LLMInputArtifact, ...],
    ) -> LLMRequest:
        left = item.get("left")
        right = item.get("right")
        variants = _boundary_variants(left, right)
        prompt = f"""Review the boundary between two consecutive OCR-proofread PDF pages using both attached full-page images.

Decide whether the candidate paragraph at the end of the left page and the candidate paragraph at the start of the right page are one paragraph split by pagination. Return JSON only. Do not rewrite either page and do not correct source wording here.

Use `separate` when they are distinct paragraphs. Use `join` only when they are visibly and semantically one paragraph, and select exactly one offered `join_mode`: `space`, `none`, or `drop_hyphen`. Use `uncertain` when the images or context do not justify a decision. `join_mode` must be null unless action is `join`.

LEFT CANDIDATE:
{json.dumps(left, ensure_ascii=False, sort_keys=True)}

RIGHT CANDIDATE:
{json.dumps(right, ensure_ascii=False, sort_keys=True)}

SAFE JOIN VARIANTS:
{json.dumps(variants, ensure_ascii=False, sort_keys=True)}

LEFT PAGE TAIL:
{left_markdown[-1800:]}

RIGHT PAGE HEAD:
{right_markdown[:1800]}
"""
        return LLMRequest(
            f"ocr-proofread-{BOUNDARY_PROMPT_VERSION}-{int(item['boundary_index']) + 1:06d}",
            prompt,
            JsonOutput(BOUNDARY_OUTPUT_SCHEMA, repair="format"),
            ModelSelection(
                self.config.provider,
                self.config.model,
                self.config.model_tier,
            ),
            inputs=inputs,
        )

    def _resolve_boundary_review(
        self,
        context: RunContext,
        results: list[dict[str, Any]],
        pages: list[dict[str, Any]],
    ) -> dict[str, Mapping[str, Any]] | Paused:
        items = [
            item
            for item in results
            if item["action"] in {"join", "uncertain"}
        ]
        if not items:
            return {}
        review_ref = context.artifacts.find("boundary-review/decisions")
        if review_ref is None:
            request_items = [
                {
                    "id": _boundary_id(item),
                    **item,
                    "left_candidates": _nearest_boundary_candidates(
                        pages, int(item["left_page_index"]), step=-1
                    ),
                    "right_candidates": _nearest_boundary_candidates(
                        pages, int(item["right_page_index"]), step=1
                    ),
                }
                for item in items
            ]
            key = _key("boundary-review", request_items)
            if (
                context.resume_input is None
                or context.resume_input.get("resume_key") != key
            ):
                request_ref = context.artifacts.find(
                    "boundary-review/request-v3"
                ) or context.artifacts.publish_json(
                    "boundary-review/request-v3",
                    {
                        "schema_version": BOUNDARY_REVIEW_SCHEMA,
                        "resume_key": key,
                        "items": request_items,
                    },
                )
                return Paused(
                    Awaiting(
                        ResumeReason.SUPERVISION_REQUIRED,
                        key,
                        True,
                        request_ref,
                        "arc.ocr_proofread.boundary_review_input.v3",
                        {"count": len(request_items)},
                    )
                )
            decisions = _validate_boundary_review_input(
                context.resume_input, key, request_items
            )
            review_ref = context.artifacts.publish_json(
                "boundary-review/decisions", decisions
            )
        document = json.loads(
            context.artifacts.read_bytes(review_ref).decode("utf-8")
        )
        return {
            str(item["id"]): item for item in document["decisions"]
        }

    def _audit_awaiting(self, context: RunContext, pages: list[dict[str, Any]]) -> Awaiting:
        request = _audit_request(pages)
        key = _key("audit", request)
        request["resume_key"] = key
        request_ref = context.artifacts.find(
            "audit/request-v2"
        ) or context.artifacts.publish_json(
            "audit/request-v2", request
        )
        return Awaiting(
            ResumeReason.SUPERVISION_REQUIRED,
            key,
            True,
            request_ref,
            "arc.ocr_proofread.audit_input.v1",
            {
                "change_sample": len(request["changes"]),
                "page_sample": len(request["pages"]),
                "boundary_sample": len(request["boundaries"]),
            },
        )

    def _resolve_audit(
        self, context: RunContext, pages: list[dict[str, Any]]
    ) -> dict[str, Any] | Paused | RunError:
        audit_ref = context.artifacts.find("audit/decisions")
        if audit_ref is None:
            awaiting = self._audit_awaiting(context, pages)
            if context.resume_input is None or context.resume_input.get("resume_key") != awaiting.resume_key:
                return Paused(awaiting)
            request_doc = json.loads(
                context.artifacts.read_bytes(awaiting.request_ref).decode("utf-8")
            )
            audit = _validate_audit_input(
                context.resume_input, awaiting.resume_key, request_doc
            )
            audit_ref = context.artifacts.publish_json("audit/decisions", audit)
        audit = json.loads(context.artifacts.read_bytes(audit_ref).decode("utf-8"))
        page_map = {f"page-{int(page['page_index']) + 1:06d}": page for page in pages}
        for decision in audit["pages"]:
            edits = decision.get("edits", [])
            if not edits:
                continue
            page = page_map[str(decision["id"])]
            corrected = _apply_edits(page["corrected_markdown"], edits)
            if sorted(_IMAGE_LINK.findall(corrected)) != sorted(
                _IMAGE_LINK.findall(page["corrected_markdown"])
            ):
                raise ProofreadWorkflowError(
                    "asset_links_changed", "audit edits changed image links"
                )
            start = len(page["changes"])
            page["corrected_markdown"] = corrected
            page["changes"].extend(
                _change_record(
                    int(page["page_index"]),
                    start + index,
                    edit,
                    "ocr_correction",
                )
                for index, edit in enumerate(edits)
            )
        failed = [
            item
            for key in ("changes", "pages", "boundaries")
            for item in audit[key]
            if item["verdict"] != "pass"
        ]
        if failed:
            return RunError("audit_failed", f"{len(failed)} sampled items failed main-agent audit")
        return audit

    def _publish(
        self,
        context: RunContext,
        source: MineruSource,
        pages: list[dict[str, Any]],
        source_corrections: list[dict[str, Any]],
        audit: dict[str, Any],
    ):
        project = ProofreadProject.load(self.config.project_dir)
        chunks = [
            "<!-- Generated by ARC OCR proofreading. -->",
            f"<!-- Source Markdown SHA-256: {source.markdown_sha256} -->",
            f"<!-- Source PDF SHA-256: {source.pdf_sha256} -->",
        ]
        changes: list[dict[str, Any]] = []
        for page in pages:
            if not page.get("suppress_page_marker", False):
                chunks.append(
                    f"<!-- Source PDF page {int(page['page_index']) + 1} -->"
                )
            if page["corrected_markdown"]:
                chunks.append(page["corrected_markdown"])
            changes.extend(page["changes"])
        markdown = "\n\n".join(chunks).rstrip() + "\n"
        ledger = b"".join(canonical_json_bytes(item) + b"\n" for item in changes)
        manifest = {
            "schema_version": RESULT_SCHEMA,
            "run_id": context.run_id,
            "source": {
                "markdown_sha256": source.markdown_sha256,
                "pdf_sha256": source.pdf_sha256,
                "content_list_sha256": source.content_list_sha256,
            },
            "pages": source.page_count,
            "ocr_corrections": sum(item["category"] == "ocr_correction" for item in changes),
            "approved_source_corrections": len(source_corrections),
            "page_boundary_repairs": sum(
                item["category"] == "page_boundary_repair"
                for item in changes
            ),
            "corrections_per_page": len(changes) / source.page_count,
            "unresolved": 0,
            "audit": audit,
            "delivery_sha256": {
                "markdown": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                "changes": hashlib.sha256(ledger).hexdigest(),
                "assets": {asset.delivery_name: asset.sha256 for asset in source.assets},
            },
            "artifacts": {
                "markdown": "proofread.md",
                "changes": "proofread.changes.jsonl",
                "assets": "proofread-assets/",
            },
        }
        result_ref = context.artifacts.publish_json("result", manifest)
        atomic_write_bytes(project.markdown, markdown.encode("utf-8"))
        atomic_write_bytes(project.changes, ledger)
        atomic_write_json(project.manifest, manifest)
        temporary = project.runtime_root / "delivery-assets"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        for asset in source.assets:
            payload = asset.source.read_bytes()
            if hashlib.sha256(payload).hexdigest() != asset.sha256:
                raise ProofreadWorkflowError("asset_changed", f"asset changed: {asset.source}")
            atomic_write_bytes(temporary / asset.delivery_name, payload)
        if project.assets.exists():
            shutil.rmtree(project.assets)
        temporary.replace(project.assets)
        return result_ref


class BoundaryRepairHandler(ProofreadHandler):
    """Review page boundaries in an existing verified proofreading delivery."""

    name = BOUNDARY_REPAIR_HANDLER

    def __init__(
        self,
        config: BoundaryRepairConfig,
        *,
        task_service: Any | None = None,
        renderer: Any | None = None,
    ) -> None:
        super().__init__(config, task_service=task_service, renderer=renderer)
        self.config = config

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "request_binding_mismatch",
                    "handler request differs from durable spec",
                )
            )
        try:
            project = ProofreadProject.load(self.config.project_dir)
            manifest, baseline_changes, pages = self._load_baseline(project)
            pdf_bytes = Path(self.config.pdf).read_bytes()
            units = tuple(
                WorkUnit(
                    f"boundary-page-{index + 1:06d}",
                    {
                        "page_index": index,
                        "pdf_sha256": self.config.pdf_sha256,
                    },
                )
                for index in range(len(pages))
            )

            def worker(unit: WorkUnit):
                context.checkpoint()
                index = int(unit.semantic_input["page_index"])
                rendered = self.renderer.render_page(pdf_bytes, index + 1)
                page_ref = context.artifacts.publish_bytes(
                    f"page-images/{index + 1:06d}",
                    rendered.png_bytes,
                    media_type="image/png",
                )
                return UnitResult(
                    unit.unit_id,
                    "succeeded",
                    {
                        "page_index": index,
                        "corrected_markdown": pages[index],
                        "changes": [],
                        "image_artifact": page_ref.artifact_id,
                    },
                )

            grouped = context.run_group(
                BOUNDARY_IMAGE_GROUP_ID,
                units,
                worker,
                max_workers=self.config.workers,
                failure_mode=FailureMode.COLLECT,
            )
            if isinstance(grouped, Paused):
                return grouped
            failed = [unit for unit in grouped.units if unit.status == "failed"]
            if failed:
                return Failed(
                    RunError(
                        "boundary_page_render_failed",
                        f"{len(failed)} page renders failed",
                    )
                )
            rendered_pages = [
                dict(unit.value)
                for unit in grouped.units
                if isinstance(unit.value, Mapping)
            ]
            rendered_pages.sort(key=lambda item: int(item["page_index"]))
            reconciled = self._reconcile_boundaries(context, rendered_pages)
            if isinstance(reconciled, Paused):
                return reconciled
            audit = self._resolve_audit(context, reconciled)
            if isinstance(audit, Paused):
                return audit
            if isinstance(audit, RunError):
                return Failed(audit)
            result_ref = self._publish_boundary_repair(
                context,
                project,
                manifest,
                baseline_changes,
                reconciled,
                audit,
            )
            return Succeeded(result_ref)
        except ProofreadWorkflowError as exc:
            return Failed(RunError(exc.code, str(exc)))
        except Exception as exc:
            return Failed(
                RunError(getattr(exc, "code", "boundary_repair_failed"), str(exc))
            )

    def _load_baseline(
        self, project: ProofreadProject
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        expected = (
            self.config.baseline_markdown_sha256,
            self.config.baseline_manifest_sha256,
            self.config.baseline_changes_sha256,
            self.config.pdf_sha256,
        )
        actual = (
            sha256_file(project.markdown),
            sha256_file(project.manifest),
            sha256_file(project.changes),
            sha256_file(Path(self.config.pdf)),
        )
        if actual != expected:
            raise ProofreadWorkflowError(
                "source_changed", "proofreading delivery or PDF changed after run creation"
            )
        try:
            manifest = json.loads(project.manifest.read_text(encoding="utf-8"))
            baseline_changes = [
                json.loads(line)
                for line in project.changes.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProofreadWorkflowError(
                "baseline_invalid", "proofreading delivery is unreadable"
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != RESULT_SCHEMA:
            raise ProofreadWorkflowError(
                "baseline_invalid", "proofreading manifest is invalid"
            )
        if int(manifest.get("page_boundary_repairs") or 0):
            raise ProofreadWorkflowError(
                "boundaries_already_reconciled",
                "proofreading delivery already contains page-boundary repairs",
            )
        markdown = project.markdown.read_text(encoding="utf-8")
        pages = _split_proofread_pages(markdown, int(manifest.get("pages", 0)))
        return manifest, baseline_changes, pages

    def _publish_boundary_repair(
        self,
        context: RunContext,
        project: ProofreadProject,
        baseline_manifest: dict[str, Any],
        baseline_changes: list[dict[str, Any]],
        pages: list[dict[str, Any]],
        audit: dict[str, Any],
    ):
        source = baseline_manifest.get("source")
        if not isinstance(source, Mapping):
            raise ProofreadWorkflowError("baseline_invalid", "source metadata is invalid")
        chunks = [
            "<!-- Generated by ARC OCR proofreading. -->",
            f"<!-- Source Markdown SHA-256: {source['markdown_sha256']} -->",
            f"<!-- Source PDF SHA-256: {source['pdf_sha256']} -->",
        ]
        repair_changes: list[dict[str, Any]] = []
        for page in pages:
            if not page.get("suppress_page_marker", False):
                chunks.append(
                    f"<!-- Source PDF page {int(page['page_index']) + 1} -->"
                )
            if page["corrected_markdown"]:
                chunks.append(str(page["corrected_markdown"]))
            repair_changes.extend(page["changes"])
        all_changes = [*baseline_changes, *repair_changes]
        markdown = "\n\n".join(chunks).rstrip() + "\n"
        ledger = b"".join(canonical_json_bytes(item) + b"\n" for item in all_changes)
        manifest = dict(baseline_manifest)
        manifest.update(
            {
                "run_id": context.run_id,
                "ocr_corrections": sum(
                    item.get("category") == "ocr_correction"
                    for item in all_changes
                ),
                "approved_source_corrections": sum(
                    item.get("category") == "approved_source_correction"
                    for item in all_changes
                ),
                "page_boundary_repairs": sum(
                    item.get("category") == "page_boundary_repair"
                    for item in all_changes
                ),
                "corrections_per_page": len(all_changes) / len(pages),
                "boundary_audit": audit,
                "delivery_sha256": {
                    "markdown": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                    "changes": hashlib.sha256(ledger).hexdigest(),
                    "assets": dict(
                        baseline_manifest.get("delivery_sha256", {}).get(
                            "assets", {}
                        )
                    ),
                },
            }
        )
        for name, digest in manifest["delivery_sha256"]["assets"].items():
            if sha256_file(project.assets / name) != digest:
                raise ProofreadWorkflowError(
                    "asset_changed", f"asset changed: {name}"
                )
        result_ref = context.artifacts.publish_json("result", manifest)
        atomic_write_bytes(project.markdown, markdown.encode("utf-8"))
        atomic_write_bytes(project.changes, ledger)
        atomic_write_json(project.manifest, manifest)
        return result_ref


def _boundary_paragraph(markdown: str, *, side: str) -> dict[str, Any] | None:
    paragraphs = _boundary_paragraphs(markdown)
    if not paragraphs:
        return None
    return paragraphs[-1] if side == "left" else paragraphs[0]


def _boundary_paragraphs(markdown: str) -> list[dict[str, Any]]:
    payload = markdown.encode("utf-8")
    artifact = SourceArtifact(
        source_format=SourceFormat.MARKDOWN,
        artifact_digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        media_type="text/markdown",
        origin=SourceOrigin(
            kind=SourceOriginKind.LOCAL_IMPORT,
            locator="ocr-boundary-candidates",
        ),
    )
    document = parse_rich_artifact_bytes(artifact, payload).document
    paragraph_blocks = [
        block
        for block in document.blocks
        if block.kind is RichBlockKind.PARAGRAPH
    ]
    lines = markdown.splitlines()
    result: list[dict[str, Any]] = []
    for block in paragraph_blocks:
        line_start = block.locator.line_start
        line_end = block.locator.line_end
        if line_start is None or line_end is None:
            raise ProofreadWorkflowError(
                "boundary_candidate_invalid", "paragraph has no Markdown line range"
            )
        result.append(
            {
                "block_id": block.block_id,
                "line_start": line_start,
                "line_end": line_end,
                "markdown": "\n".join(lines[line_start - 1 : line_end]),
                "is_edge": block.ordinal in {0, len(document.blocks) - 1},
            }
        )
    return result


def _nearest_boundary_candidates(
    pages: list[dict[str, Any]], page_index: int, *, step: int
) -> list[dict[str, Any]]:
    if step not in {-1, 1}:
        raise ValueError("boundary candidate step must be -1 or 1")
    current = page_index
    while 0 <= current < len(pages):
        candidates = _boundary_paragraphs(
            str(pages[current]["corrected_markdown"])
        )
        if candidates:
            return [
                {**candidate, "page_index": current}
                for candidate in candidates
            ]
        current += step
    return []


def _split_proofread_pages(markdown: str, expected_pages: int) -> list[str]:
    marker = re.compile(r"(?m)^<!-- Source PDF page ([1-9][0-9]*) -->[ \t]*$")
    matches = list(marker.finditer(markdown))
    numbers = [int(match.group(1)) for match in matches]
    if expected_pages < 1 or numbers != list(range(1, expected_pages + 1)):
        raise ProofreadWorkflowError(
            "baseline_page_map_invalid",
            "proofread Markdown does not contain one ordered marker per PDF page",
        )
    return [
        markdown[
            match.end() : (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(markdown)
            )
        ].strip()
        for index, match in enumerate(matches)
    ]


def _boundary_variants(left: Any, right: Any) -> dict[str, str]:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return {}
    left_text = str(left["markdown"])
    right_text = str(right["markdown"])
    result = {
        "space": _join_boundary_text(left_text, right_text, "space"),
        "none": _join_boundary_text(left_text, right_text, "none"),
    }
    if left_text.rstrip().endswith("-"):
        result["drop_hyphen"] = _join_boundary_text(
            left_text, right_text, "drop_hyphen"
        )
    return result


def _join_boundary_text(left: str, right: str, mode: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if mode == "space":
        return left + " " + right
    if mode == "none":
        return left + right
    if mode == "drop_hyphen" and left.endswith("-"):
        return left[:-1] + right
    raise ProofreadWorkflowError(
        "boundary_join_invalid", "selected boundary join mode is unavailable"
    )


def _validate_boundary_output(
    candidate: Mapping[str, Any], value: Any
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"action", "join_mode", "reason"}
        or value.get("action") not in {"separate", "join", "uncertain"}
        or not isinstance(value.get("reason"), str)
        or not str(value["reason"]).strip()
    ):
        raise ProofreadWorkflowError(
            "boundary_output_invalid", "boundary result has invalid fields"
        )
    action = str(value["action"])
    mode = value.get("join_mode")
    if action == "join":
        variants = _boundary_variants(
            candidate.get("left"), candidate.get("right")
        )
        if not isinstance(mode, str) or mode not in variants:
            raise ProofreadWorkflowError(
                "boundary_output_invalid",
                "joined boundary must select one available safe join mode",
            )
    elif mode is not None:
        raise ProofreadWorkflowError(
            "boundary_output_invalid",
            "non-joined boundary must use a null join_mode",
        )
    return {
        "action": action,
        "join_mode": mode,
        "reason": str(value["reason"]).strip(),
    }


def _boundary_id(item: Mapping[str, Any]) -> str:
    return (
        f"boundary-{int(item['left_page_index']) + 1:06d}-"
        f"{int(item['right_page_index']) + 1:06d}"
    )


def _validate_boundary_review_input(
    value: Mapping[str, Any],
    key: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    if value.get("resume_key") != key or not isinstance(
        value.get("decisions"), list
    ):
        raise ProofreadWorkflowError(
            "boundary_review_input_invalid", "boundary review input is invalid"
        )
    expected = {str(item["id"]) for item in items}
    decisions = value["decisions"]
    actual = {
        str(item.get("id"))
        for item in decisions
        if isinstance(item, Mapping)
    }
    if actual != expected or len(decisions) != len(expected):
        raise ProofreadWorkflowError(
            "boundary_review_input_invalid",
            "boundary decisions do not cover every requested item",
        )
    by_id = {str(item["id"]): item for item in items}
    normalized: list[dict[str, Any]] = []
    for item in decisions:
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {
                "id",
                "action",
                "join_mode",
                "left_block_id",
                "right_block_id",
            }
            or item.get("action") not in {"separate", "join"}
        ):
            raise ProofreadWorkflowError(
                "boundary_review_input_invalid", "boundary action is invalid"
            )
        result = {
            "id": str(item["id"]),
            "action": str(item["action"]),
            "join_mode": item.get("join_mode"),
            "left_block_id": item.get("left_block_id"),
            "right_block_id": item.get("right_block_id"),
        }
        candidate = by_id[result["id"]]
        if result["action"] == "join":
            left = _candidate_by_id(
                candidate.get("left_candidates"), result["left_block_id"]
            )
            right = _candidate_by_id(
                candidate.get("right_candidates"), result["right_block_id"]
            )
            variants = _boundary_variants(left, right)
            if result["join_mode"] not in variants:
                raise ProofreadWorkflowError(
                    "boundary_review_input_invalid",
                    "joined boundary requires an available safe join mode",
                )
            result["left"] = left
            result["right"] = right
        elif any(
            result[key] is not None
            for key in ("join_mode", "left_block_id", "right_block_id")
        ):
            raise ProofreadWorkflowError(
                "boundary_review_input_invalid",
                "separate boundary must use null join fields",
            )
        else:
            result["left"] = None
            result["right"] = None
        normalized.append(result)
    return {
        "schema_version": "arc.ocr_proofread.boundary_review_decisions.v3",
        "resume_key": key,
        "decisions": normalized,
    }


def _candidate_by_id(value: Any, block_id: Any) -> Mapping[str, Any]:
    if not isinstance(value, list) or not isinstance(block_id, str):
        raise ProofreadWorkflowError(
            "boundary_review_input_invalid", "joined boundary must select paragraph blocks"
        )
    matches = [
        item
        for item in value
        if isinstance(item, Mapping) and item.get("block_id") == block_id
    ]
    if len(matches) != 1:
        raise ProofreadWorkflowError(
            "boundary_review_input_invalid", "selected boundary paragraph is unavailable"
        )
    return matches[0]


def _apply_boundary_decisions(
    pages: list[dict[str, Any]],
    results: list[dict[str, Any]],
    reviewed: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    joins: list[dict[str, Any]] = []
    for result in results:
        decision: Mapping[str, Any] = reviewed.get(
            _boundary_id(result), result
        )
        if decision["action"] != "join":
            if result["action"] in {"join", "uncertain"}:
                pages[int(result["left_page_index"])][
                    "boundary_review_rejected"
                ] = True
                pages[int(result["right_page_index"])][
                    "boundary_review_rejected"
                ] = True
            continue
        left = decision.get("left", result.get("left"))
        right = decision.get("right", result.get("right"))
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            raise ProofreadWorkflowError(
                "boundary_join_invalid", "joined boundary has no paragraphs"
            )
        joins.append(
            {
                **result,
                "left_page_index": int(
                    left.get("page_index", result["left_page_index"])
                ),
                "right_page_index": int(
                    right.get("page_index", result["right_page_index"])
                ),
                "join_mode": str(decision["join_mode"]),
                "left": left,
                "right": right,
            }
        )
    if not joins:
        return pages

    parent: dict[str, str] = {}

    def node(page_index: int, block: Mapping[str, Any]) -> str:
        value = f"{page_index}:{block['block_id']}"
        parent.setdefault(value, value)
        return value

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    node_values: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for join in joins:
        left_block = join["left"]
        right_block = join["right"]
        left_node = node(int(join["left_page_index"]), left_block)
        right_node = node(int(join["right_page_index"]), right_block)
        node_values[left_node] = (int(join["left_page_index"]), left_block)
        node_values[right_node] = (int(join["right_page_index"]), right_block)
        union(left_node, right_node)
        edges[(left_node, right_node)] = join

    components: dict[str, list[str]] = {}
    for value in parent:
        components.setdefault(find(value), []).append(value)
    edits_by_page: dict[int, list[tuple[int, int, str]]] = {}
    for values in components.values():
        values.sort(key=lambda value: node_values[value][0])
        first_page, first_block = node_values[values[0]]
        merged = str(first_block["markdown"])
        for left_node, right_node in zip(values, values[1:]):
            edge = edges.get((left_node, right_node))
            if edge is None:
                raise ProofreadWorkflowError(
                    "boundary_join_invalid", "boundary join chain is discontinuous"
                )
            _, right_block = node_values[right_node]
            merged = _join_boundary_text(
                merged,
                str(right_block["markdown"]),
                str(edge["join_mode"]),
            )
        edits_by_page.setdefault(first_page, []).append(
            (
                int(first_block["line_start"]),
                int(first_block["line_end"]),
                merged,
            )
        )
        for value in values[1:]:
            page_index, block = node_values[value]
            edits_by_page.setdefault(page_index, []).append(
                (int(block["line_start"]), int(block["line_end"]), "")
            )

    for page_index, edits in edits_by_page.items():
        pages[page_index]["corrected_markdown"] = _replace_line_ranges(
            str(pages[page_index]["corrected_markdown"]), edits
        )
    for repair_index, join in enumerate(joins):
        right_page = int(join["right_page_index"])
        left_page = int(join["left_page_index"])
        left_text = str(join["left"]["markdown"])
        right_text = str(join["right"]["markdown"])
        record = {
            "id": (
                f"p{right_page + 1:06d}-page_boundary_repair-"
                f"{repair_index + 1:04d}"
            ),
            "page_index": right_page,
            "left_page_index": left_page,
            "category": "page_boundary_repair",
            "kind": "paragraph_join",
            "before": left_text + "\n\n--- PAGE BREAK ---\n\n" + right_text,
            "after": _join_boundary_text(
                left_text, right_text, str(join["join_mode"])
            ),
            "occurrence": 1,
            "reason": str(join["reason"]),
            "left_image_artifact": join["left_image_artifact"],
            "right_image_artifact": join["right_image_artifact"],
        }
        pages[right_page]["changes"].append(record)
    return pages


def _replace_line_ranges(
    markdown: str, edits: list[tuple[int, int, str]]
) -> str:
    lines = markdown.splitlines()
    for line_start, line_end, replacement in sorted(
        edits, key=lambda item: item[0], reverse=True
    ):
        replacement_lines = replacement.splitlines() if replacement else []
        lines[line_start - 1 : line_end] = replacement_lines
    return "\n".join(lines).strip()


def _validate_page_output(page: MineruPage, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "edits", "source_typo_candidates", "uncertainties", "checks"
    }:
        raise ProofreadWorkflowError("page_output_invalid", "page output has invalid fields")
    edits = _validated_edits(value["edits"], "edits")
    source_typos = _validated_edits(value["source_typo_candidates"], "source typo candidates")
    uncertainties = value["uncertainties"]
    checks = value["checks"]
    if not isinstance(uncertainties, list) or not isinstance(checks, Mapping):
        raise ProofreadWorkflowError("page_output_invalid", "page output collections are invalid")
    if set(checks) != {"all_visible_text", "all_visible_equations", "page_boundary"} or any(
        type(checks[key]) is not bool for key in checks
    ):
        raise ProofreadWorkflowError("page_output_invalid", "page checks are invalid")
    if not all(checks.values()) and not uncertainties:
        raise ProofreadWorkflowError("page_output_invalid", "incomplete checks require uncertainty")
    normalized_uncertainties = []
    for item in uncertainties:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"excerpt", "reason"}
            or not isinstance(item["excerpt"], str)
            or not isinstance(item["reason"], str)
            or not item["reason"]
        ):
            raise ProofreadWorkflowError("page_output_invalid", "uncertainty is invalid")
        normalized_uncertainties.append(dict(item))
    corrected = _apply_edits(page.markdown, edits)
    if sorted(_IMAGE_LINK.findall(corrected)) != sorted(_IMAGE_LINK.findall(page.markdown)):
        raise ProofreadWorkflowError("asset_links_changed", "page edits changed image links")
    changes = [
        _change_record(page.page_index, index, edit, "ocr_correction")
        for index, edit in enumerate(edits)
    ]
    return {
        "schema_version": PAGE_SCHEMA,
        "page_index": page.page_index,
        "status": "uncertain" if normalized_uncertainties or source_typos else ("corrected" if edits else ("blank" if not page.markdown else "verified")),
        "baseline_markdown": page.markdown,
        "corrected_markdown": corrected,
        "changes": changes,
        "source_typo_candidates": source_typos,
        "uncertainties": normalized_uncertainties,
        "checks": dict(checks),
    }


def _validated_edits(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProofreadWorkflowError("page_output_invalid", f"{label} must be an array")
    result = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"before", "after", "occurrence", "kind", "reason"}
            or not isinstance(item["before"], str)
            or not isinstance(item["after"], str)
            or type(item["occurrence"]) is not int
            or item["occurrence"] < 1
            or not isinstance(item["kind"], str)
            or not item["kind"]
            or not isinstance(item["reason"], str)
            or not item["reason"]
        ):
            raise ProofreadWorkflowError("page_output_invalid", f"{label} contains invalid edit")
        result.append(dict(item))
    return result


def _apply_edits(text: str, edits: list[Mapping[str, Any]]) -> str:
    current = text
    for edit in edits:
        before = str(edit["before"])
        occurrence = int(edit["occurrence"])
        if not before:
            if current or occurrence != 1:
                raise ProofreadWorkflowError(
                    "edit_anchor_missing",
                    "empty edit anchor is valid only for an entirely empty page",
                )
            current = str(edit["after"])
            continue
        starts = [match.start() for match in re.finditer(re.escape(before), current)]
        if occurrence > len(starts):
            raise ProofreadWorkflowError("edit_anchor_missing", "edit occurrence is absent from page")
        start = starts[occurrence - 1]
        current = current[:start] + str(edit["after"]) + current[start + len(before):]
    return current


def _change_record(page_index: int, index: int, edit: Mapping[str, Any], category: str) -> dict[str, Any]:
    return {
        "id": f"p{page_index + 1:06d}-{category}-{index + 1:04d}",
        "page_index": page_index,
        "category": category,
        "kind": str(edit["kind"]),
        "before": str(edit["before"]),
        "after": str(edit["after"]),
        "occurrence": int(edit["occurrence"]),
        "reason": str(edit["reason"]),
    }


def _retry_request(request: LLMRequest, error: ProofreadWorkflowError) -> LLMRequest:
    message = str(error)[:500]
    digest = hashlib.sha256(f"{error.code}\0{message}".encode()).hexdigest()[:16]
    return LLMRequest(
        f"{request.task_id}-semantic-retry-{digest}",
        f"{request.prompt}\n\nMachine validation failed: {error.code}: {message}. Return a complete fresh result.",
        request.output,
        request.model,
        inputs=request.inputs,
    )


def _validate_review_input(
    value: Mapping[str, Any], key: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    if value.get("resume_key") != key or not isinstance(value.get("decisions"), list):
        raise ProofreadWorkflowError("review_input_invalid", "review input is invalid")
    expected = {item["id"] for item in candidates}
    decisions = value["decisions"]
    if {item.get("id") for item in decisions if isinstance(item, Mapping)} != expected:
        raise ProofreadWorkflowError("review_input_invalid", "review decisions do not cover every item")
    for item in decisions:
        if not isinstance(item, Mapping) or item.get("action") not in {"accept", "reject"}:
            raise ProofreadWorkflowError("review_input_invalid", "review action is invalid")
    return {
        "schema_version": "arc.ocr_proofread.review_decisions.v1",
        "resume_key": key,
        "decisions": [dict(item) for item in decisions],
    }


def _audit_request(pages: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [change for page in pages for change in page["changes"]]
    change_sample = sorted(
        (
            item
            for item in changes
            if item["category"] != "page_boundary_repair"
        ),
        key=lambda item: hashlib.sha256(item["id"].encode()).hexdigest(),
    )[:10]
    boundary_sample = sorted(
        (
            item
            for item in changes
            if item["category"] == "page_boundary_repair"
        ),
        key=lambda item: hashlib.sha256(item["id"].encode()).hexdigest(),
    )[:10]
    affected = {
        int(item["page_index"])
        for item in boundary_sample
    } | {
        int(item["left_page_index"])
        for item in boundary_sample
    }
    rejected = [page for page in pages if page.get("boundary_review_rejected")]
    affected_pages = [
        page
        for page in pages
        if int(page["page_index"]) in affected
        and not page.get("boundary_review_rejected")
    ]
    page_sample = []
    for group in (rejected, affected_pages) if rejected or affected else (pages,):
        page_sample.extend(
            sorted(
                group,
                key=lambda item: hashlib.sha256(
                    f"page-{item['page_index']}".encode()
                ).hexdigest(),
            )
        )
    page_sample = page_sample[:10]
    return {
        "schema_version": AUDIT_SCHEMA,
        "changes": change_sample,
        "boundaries": boundary_sample,
        "pages": [
            {
                "id": f"page-{int(item['page_index']) + 1:06d}",
                "page_index": item["page_index"],
                "image_artifact": item["image_artifact"],
                "corrected_markdown": item["corrected_markdown"],
            }
            for item in page_sample
        ],
    }


def _validate_audit_input(
    value: Mapping[str, Any], key: str, request: Mapping[str, Any]
) -> dict[str, Any]:
    if value.get("resume_key") != key:
        raise ProofreadWorkflowError("audit_input_invalid", "audit resume key is invalid")
    result = {"schema_version": "arc.ocr_proofread.audit_decisions.v1", "resume_key": key}
    for name in ("changes", "pages", "boundaries"):
        items = value.get(name)
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("id"), str)
            or item.get("verdict") not in {"pass", "fail"}
            for item in items
        ):
            raise ProofreadWorkflowError("audit_input_invalid", f"audit {name} are invalid")
        expected = {str(item["id"]) for item in request[name]}
        actual = {str(item["id"]) for item in items}
        if actual != expected or len(items) != len(expected):
            raise ProofreadWorkflowError(
                "audit_input_invalid", f"audit {name} do not cover the requested sample"
            )
        normalized = []
        for item in items:
            normalized_item = dict(item)
            if name in {"changes", "boundaries"} and "edits" in item:
                raise ProofreadWorkflowError(
                    "audit_input_invalid",
                    "audit change decisions cannot contain edits",
                )
            if name == "pages":
                edits = item.get("edits", [])
                if item["verdict"] != "pass" and edits:
                    raise ProofreadWorkflowError(
                        "audit_input_invalid", "audit corrections require a pass verdict"
                    )
                try:
                    normalized_item["edits"] = _validated_edits(edits, "audit edits")
                except ProofreadWorkflowError as exc:
                    raise ProofreadWorkflowError("audit_input_invalid", str(exc)) from exc
            normalized.append(normalized_item)
        result[name] = normalized
    return result


def _key(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]}"


__all__ = [
    "BOUNDARY_IMAGE_GROUP_ID",
    "BOUNDARY_GROUP_ID",
    "BOUNDARY_OUTPUT_SCHEMA",
    "BOUNDARY_REPAIR_HANDLER",
    "BoundaryRepairConfig",
    "BoundaryRepairHandler",
    "GROUP_ID",
    "HANDLER",
    "PAGE_OUTPUT_SCHEMA",
    "ProofreadConfig",
    "ProofreadHandler",
    "ProofreadWorkflowError",
]
