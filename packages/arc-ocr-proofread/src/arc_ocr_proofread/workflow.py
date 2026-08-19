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
from arc_paper import PdftoppmFullPageRenderer

from .project import ProofreadProject
from .source import MineruPage, MineruSource, load_mineru_source, sha256_file


HANDLER = "arc.ocr_proofread.document.v1"
PROMPT_VERSION = "arc.ocr_proofread.page_prompt.v3"
RESULT_SCHEMA = "arc.ocr_proofread.result.v1"
PAGE_SCHEMA = "arc.ocr_proofread.page_result.v1"
REVIEW_SCHEMA = "arc.ocr_proofread.review_request.v1"
AUDIT_SCHEMA = "arc.ocr_proofread.audit_request.v1"
GROUP_ID = "pages"
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
                "before": {"type": "string", "minLength": 1},
                "after": {"type": "string"},
                "occurrence": {"type": "integer", "minimum": 1},
                "kind": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
        }
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


class ProofreadWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProofreadHandler:
    name = HANDLER

    def __init__(
        self,
        config: ProofreadConfig,
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

Return only exact edit operations. Each `before` must be a literal non-empty substring of the evolving current-page Markdown; `occurrence` is one-based. Use a larger exact span when inserting omitted text or resolving repeated text. Preserve author wording, notation, paragraph order, emphasis, equation order, equation tags, and all image links. Correct every visible OCR mismatch, equation symbol, omission, heading, list, footnote, caption, and table error. Do not rewrite or explain.

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

    def _audit_awaiting(self, context: RunContext, pages: list[dict[str, Any]]) -> Awaiting:
        request = _audit_request(pages)
        key = _key("audit", request)
        request["resume_key"] = key
        request_ref = context.artifacts.find("audit/request") or context.artifacts.publish_json(
            "audit/request", request
        )
        return Awaiting(
            ResumeReason.SUPERVISION_REQUIRED,
            key,
            True,
            request_ref,
            "arc.ocr_proofread.audit_input.v1",
            {"change_sample": len(request["changes"]), "page_sample": len(request["pages"])},
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
        failed = [item for key in ("changes", "pages") for item in audit[key] if item["verdict"] != "pass"]
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
            chunks.append(f"<!-- Source PDF page {int(page['page_index']) + 1} -->")
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
            or not item["before"]
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
    change_sample = sorted(changes, key=lambda item: hashlib.sha256(item["id"].encode()).hexdigest())[:10]
    page_sample = sorted(
        pages,
        key=lambda item: hashlib.sha256(f"page-{item['page_index']}".encode()).hexdigest(),
    )[:10]
    return {
        "schema_version": AUDIT_SCHEMA,
        "changes": change_sample,
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
    for name in ("changes", "pages"):
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
        result[name] = [dict(item) for item in items]
    return result


def _key(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]}"


__all__ = [
    "GROUP_ID",
    "HANDLER",
    "PAGE_OUTPUT_SCHEMA",
    "ProofreadConfig",
    "ProofreadHandler",
    "ProofreadWorkflowError",
]
