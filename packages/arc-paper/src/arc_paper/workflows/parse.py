"""In-run Markdown plus PDF parsing with default full-page visual review."""

from __future__ import annotations

from typing import Mapping

from arc_jobs import (
    Failed,
    JsonValue,
    RunContext,
    RunEngine,
    RunError,
    RunRepository,
    RunSnapshot,
    RunSpec,
    Succeeded,
)
from arc_llm import LLMTaskService, ModelSelection

from ..parse import (
    PDFPageRenderer,
    PDFTextExtractor,
    PaperParserService,
    ParseError,
    PdftoppmFullPageRenderer,
    PdftotextExtractor,
    VisualReviewService,
    parsed_document_to_document,
)
from ..source_repository import SourceRepository, SourceRepositoryError
from ..sources import (
    ParseOutcome,
    ReconciliationEntry,
    SourceArtifact,
    SourceBundle,
    SourceFormat,
    ValidationPolicy,
)


MARKDOWN_PDF_VISUAL_HANDLER = "arc.paper.markdown_pdf_visual_parse.v1"
PARSE_OUTCOME_SCHEMA = "arc.paper.parse_outcome.v1"


class MarkdownPDFVisualParseHandler:
    """Portable RunHandler for the default Markdown+PDF visual contract."""

    name = MARKDOWN_PDF_VISUAL_HANDLER

    def __init__(
        self,
        sources: SourceRepository,
        primary: SourceArtifact,
        pdf_validator: SourceArtifact,
        *,
        renderer: PDFPageRenderer | None = None,
        pdf_text_extractor: PDFTextExtractor | None = None,
        llm: LLMTaskService | None = None,
        model: ModelSelection = ModelSelection(),
    ) -> None:
        if primary.source_format is not SourceFormat.MARKDOWN:
            raise ValueError("Markdown+PDF visual parse requires a Markdown primary")
        if pdf_validator.source_format is not SourceFormat.PDF:
            raise ValueError("Markdown+PDF visual parse requires a PDF validator")
        self.primary = primary
        self.pdf_validator = pdf_validator
        self.model = model
        reviewer = VisualReviewService(
            renderer or PdftoppmFullPageRenderer(),
            llm=llm,
            model=model,
        )
        self.service = PaperParserService(
            sources,
            pdf_text_extractor=pdf_text_extractor or PdftotextExtractor(),
            visual_reviewer=reviewer,
        )

    def semantic_input(self) -> dict[str, JsonValue]:
        return {
            "schema_version": "arc.paper.markdown_pdf_visual_parse_request.v1",
            "primary": _artifact_semantic_document(self.primary),
            "pdf_validator": _artifact_semantic_document(self.pdf_validator),
            "validation_policy": ValidationPolicy.VISUAL_ALL_PAGES.value,
            "model_requirement": {
                "provider": self.model.provider,
                "model": self.model.model,
                "tier": self.model.tier,
            },
        }

    def execute(self, context: RunContext):
        if dict(context.semantic_input) != self.semantic_input():
            return Failed(
                RunError(
                    "parse_binding_mismatch",
                    "Handler bindings differ from the durable parse semantic input.",
                )
            )
        try:
            outcome = self.service.parse(
                SourceBundle(
                    primary=self.primary,
                    validators=(self.pdf_validator,),
                ),
                policy=ValidationPolicy.VISUAL_ALL_PAGES,
                context=context,
            )
        except (ParseError, SourceRepositoryError) as exc:
            return Failed(
                RunError(
                    getattr(exc, "code", "primary_parse_failed"),
                    str(exc),
                )
            )
        return Succeeded(
            context.artifacts.publish_json(
                "paper-parse/result", parse_outcome_to_document(outcome)
            )
        )


class MarkdownPDFVisualParseRunner:
    """Thin run wrapper that installs the renderer, reviewer, and RunContext."""

    def __init__(
        self,
        jobs: RunRepository,
        sources: SourceRepository,
        *,
        renderer: PDFPageRenderer | None = None,
        pdf_text_extractor: PDFTextExtractor | None = None,
        llm: LLMTaskService | None = None,
    ) -> None:
        self.engine = RunEngine(jobs)
        self.sources = sources
        self.renderer = renderer
        self.pdf_text_extractor = pdf_text_extractor
        self.llm = llm

    def execute(
        self,
        run_id: str,
        primary: SourceArtifact,
        pdf_validator: SourceArtifact,
        *,
        model: ModelSelection = ModelSelection(),
    ) -> RunSnapshot:
        handler = self._handler(primary, pdf_validator, model=model)
        return self.engine.execute(
            RunSpec(run_id, handler.name, handler.semantic_input()), handler
        )

    def resume(
        self,
        run_id: str,
        primary: SourceArtifact,
        pdf_validator: SourceArtifact,
        *,
        input: Mapping[str, JsonValue] | None = None,
        model: ModelSelection = ModelSelection(),
    ) -> RunSnapshot:
        handler = self._handler(primary, pdf_validator, model=model)
        return self.engine.resume(run_id, handler, input=input)

    def _handler(
        self,
        primary: SourceArtifact,
        pdf_validator: SourceArtifact,
        *,
        model: ModelSelection,
    ) -> MarkdownPDFVisualParseHandler:
        return MarkdownPDFVisualParseHandler(
            self.sources,
            primary,
            pdf_validator,
            renderer=self.renderer,
            pdf_text_extractor=self.pdf_text_extractor,
            llm=self.llm,
            model=model,
        )


def parse_outcome_to_document(outcome: ParseOutcome) -> dict[str, JsonValue]:
    """Encode a path-free parse result for publication by arc-jobs."""

    return {
        "schema_version": PARSE_OUTCOME_SCHEMA,
        "document": parsed_document_to_document(outcome.document),
        "report": {
            "primary": _artifact_semantic_document(outcome.report.primary),
            "policy": outcome.report.policy.value,
            "entries": [_entry_document(item) for item in outcome.report.entries],
        },
        "warnings": list(outcome.warnings),
    }


def _entry_document(entry: ReconciliationEntry) -> dict[str, JsonValue]:
    return {
        "validator": _artifact_semantic_document(entry.validator),
        "status": entry.status.value,
        "subject_id": entry.subject_id,
        "message": entry.message,
        "provenance": dict(entry.provenance),
    }


def _artifact_semantic_document(
    artifact: SourceArtifact,
) -> dict[str, JsonValue]:
    return {
        "source_format": artifact.source_format.value,
        "media_type": artifact.media_type,
        "artifact_digest": artifact.artifact_digest,
        "size": artifact.size,
    }


__all__ = [
    "MARKDOWN_PDF_VISUAL_HANDLER",
    "PARSE_OUTCOME_SCHEMA",
    "MarkdownPDFVisualParseHandler",
    "MarkdownPDFVisualParseRunner",
    "parse_outcome_to_document",
]
