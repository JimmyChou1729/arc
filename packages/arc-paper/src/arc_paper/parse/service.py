from __future__ import annotations

from ..source_repository import SourceRepository, SourceRepositoryError
from ..sources import (
    ParseOutcome,
    ReconciliationEntry,
    ReconciliationReport,
    ReconciliationStatus,
    SourceBundle,
    SourceFormat,
    ValidationPolicy,
)
from .models import ParsedDocument
from .parser import PDFTextExtractor, ParseError, parse_artifact_bytes
from .reconcile import reconcile_validator


class PaperParserService:
    """The public repository-backed parser and deterministic reconciler."""

    def __init__(
        self,
        repository: SourceRepository,
        *,
        pdf_text_extractor: PDFTextExtractor | None = None,
    ):
        self.repository = repository
        self.pdf_text_extractor = pdf_text_extractor

    def parse(
        self,
        bundle: SourceBundle,
        *,
        policy: ValidationPolicy | None = None,
    ) -> ParseOutcome:
        resolved_policy = _resolve_policy(bundle, policy)
        primary = self.parse_source(bundle.primary)
        entries: list[ReconciliationEntry] = []
        warnings = list(primary.warnings)
        for validator in bundle.validators:
            if resolved_policy is ValidationPolicy.NONE:
                entries.append(
                    ReconciliationEntry(
                        validator=validator,
                        status=ReconciliationStatus.UNREVIEWED,
                        subject_id="validator",
                        message="validation was explicitly disabled",
                    )
                )
                continue
            try:
                parsed_validator = self.parse_source(validator)
            except (ParseError, SourceRepositoryError) as exc:
                code = getattr(exc, "code", "validator_parse_failed")
                message = f"validator could not be parsed ({code}): {exc}"
                entries.append(
                    ReconciliationEntry(
                        validator=validator,
                        status=ReconciliationStatus.UNREVIEWED,
                        subject_id="validator",
                        message=message,
                        provenance={"error_code": code},
                    )
                )
                warnings.append(message)
                continue
            validator_entries, validator_warnings = reconcile_validator(
                primary, parsed_validator
            )
            if (
                resolved_policy is ValidationPolicy.VISUAL_ALL_PAGES
                and bundle.primary.source_format is SourceFormat.MARKDOWN
                and validator.source_format is SourceFormat.PDF
            ):
                primary_span_ids = {span.span_id for span in primary.math_spans}
                validator_entries = tuple(
                    ReconciliationEntry(
                        validator=entry.validator,
                        status=entry.status,
                        subject_id=f"deterministic:{entry.subject_id}",
                        message=entry.message,
                        provenance={
                            **dict(entry.provenance),
                            "evidence_method": "deterministic_pdf",
                            "primary_span_id": entry.subject_id,
                        },
                    )
                    if entry.subject_id in primary_span_ids
                    else entry
                    for entry in validator_entries
                )
            entries.extend(validator_entries)
            warnings.extend(parsed_validator.warnings)
            warnings.extend(validator_warnings)
            if (
                resolved_policy is ValidationPolicy.VISUAL_ALL_PAGES
                and bundle.primary.source_format is SourceFormat.MARKDOWN
                and validator.source_format is SourceFormat.PDF
            ):
                message = (
                    "PDF visual review requires the durable Markdown+PDF visual "
                    "workflow and was not run by the deterministic parser"
                )
                entries.extend(
                    _unreviewed_visual_entries(primary, parsed_validator, message)
                )
                warnings.append(message)
        return ParseOutcome(
            document=primary,
            report=ReconciliationReport(
                primary=bundle.primary,
                policy=resolved_policy,
                entries=tuple(entries),
            ),
            warnings=tuple(_dedupe(warnings)),
        )

    def parse_source(self, artifact) -> ParsedDocument:
        payload = self.repository.read_bytes(artifact)
        return parse_artifact_bytes(
            artifact,
            payload,
            pdf_text_extractor=self.pdf_text_extractor,
        )


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            output.append(value)
            seen.add(value)
    return output


def _unreviewed_visual_entries(
    primary: ParsedDocument,
    pdf_validator: ParsedDocument,
    message: str,
) -> tuple[ReconciliationEntry, ...]:
    page_entries = tuple(
        ReconciliationEntry(
            validator=pdf_validator.source,
            status=ReconciliationStatus.UNREVIEWED,
            subject_id=f"visual-page:{page.page_number}",
            message=message,
            provenance={"page_number": page.page_number},
        )
        for page in pdf_validator.pages
    )
    span_entries = tuple(
        ReconciliationEntry(
            validator=pdf_validator.source,
            status=ReconciliationStatus.UNREVIEWED,
            subject_id=span.span_id,
            message=message,
            provenance={
                "review_method": "visual_all_pages",
                "global_unreviewed": True,
            },
        )
        for span in primary.math_spans
    )
    return page_entries + span_entries


def _resolve_policy(
    bundle: SourceBundle, policy: ValidationPolicy | None
) -> ValidationPolicy:
    if policy is not None:
        return ValidationPolicy(policy)
    if (
        bundle.primary.source_format is SourceFormat.MARKDOWN
        and any(
            validator.source_format is SourceFormat.PDF
            for validator in bundle.validators
        )
    ):
        return ValidationPolicy.VISUAL_ALL_PAGES
    return ValidationPolicy.DETERMINISTIC_ONLY


__all__ = ["PaperParserService"]
