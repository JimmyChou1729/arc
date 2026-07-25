from __future__ import annotations

import mimetypes
import re
import unicodedata
from dataclasses import dataclass

from .._parsing import ParseError
from ..parse.parser import PDFTextExtractor
from ..parse.reconcile import reconcile_validator
from ..parse.service import PaperParserService
from ..source_repository import SourceRepository, SourceRepositoryError
from ..sources import (
    ReconciliationEntry,
    ReconciliationReport,
    ReconciliationStatus,
    SourceBundle,
    SourceFormat,
    ValidationPolicy,
)
from .models import RichAsset, RichBlockKind, RichDocument, RichPageMapEntry
from .parser import parse_rich_artifact_bytes, resolve_local_asset_path


PDF_VALIDATOR_MISSING_WARNING = (
    "no PDF validator was supplied; rich source structure remains authoritative"
)


class RichDocumentValidationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RichParseOutcome:
    document: RichDocument
    report: ReconciliationReport
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.document.source.content_identity
            != self.report.primary.content_identity
        ):
            raise ValueError("rich document source does not match report primary")
        object.__setattr__(self, "warnings", tuple(self.warnings))


class RichDocumentParserService:
    """Repository-backed public facade for rich source parsing and PDF checks."""

    def __init__(
        self,
        repository: SourceRepository,
        *,
        pdf_text_extractor: PDFTextExtractor | None = None,
    ):
        self.repository = repository
        self.pdf_text_extractor = pdf_text_extractor
        self.standard_parser = PaperParserService(
            repository,
            pdf_text_extractor=pdf_text_extractor,
        )

    def parse(self, bundle: SourceBundle) -> RichParseOutcome:
        if bundle.primary.source_format not in {
            SourceFormat.MARKDOWN,
            SourceFormat.HTML,
            SourceFormat.TEX,
        }:
            raise RichDocumentValidationError(
                "rich_source_required",
                "the primary must be Markdown, HTML, or flattened single-file TeX",
            )
        if any(
            validator.source_format is not SourceFormat.PDF
            for validator in bundle.validators
        ):
            raise RichDocumentValidationError(
                "invalid_rich_validator",
                "rich document validation accepts only an optional PDF validator",
            )
        if len(bundle.validators) > 1:
            raise RichDocumentValidationError(
                "multiple_pdf_validators",
                "rich document parsing accepts at most one PDF validator",
            )
        payload = self.repository.read_bytes(bundle.primary)
        parsed = parse_rich_artifact_bytes(
            bundle.primary,
            payload,
            asset_importer=self._asset_importer(bundle.primary.origin.locator),
        )
        legacy_primary = self.standard_parser.parse_source(bundle.primary)
        if not bundle.validators:
            return RichParseOutcome(
                document=parsed.document,
                report=ReconciliationReport(
                    primary=bundle.primary,
                    policy=ValidationPolicy.DETERMINISTIC_ONLY,
                ),
                warnings=parsed.warnings + (PDF_VALIDATOR_MISSING_WARNING,),
            )

        validator = bundle.validators[0]
        try:
            parsed_validator = self.standard_parser.parse_source(validator)
        except (ParseError, SourceRepositoryError) as exc:
            code = getattr(exc, "code", "pdf_validator_invalid")
            raise RichDocumentValidationError(
                "pdf_validator_invalid",
                f"PDF validator could not be parsed ({code}): {exc}",
            ) from exc
        if not bool(parsed_validator.metadata.get("text_layer")):
            raise RichDocumentValidationError(
                "pdf_validator_unverifiable",
                "PDF validator has no extractable text layer",
            )
        entries, reconciliation_warnings = reconcile_validator(
            legacy_primary, parsed_validator
        )
        entries = _reconcile_synthetic_section(
            parsed.document, entries, parsed_validator.pages
        )
        conflicts = [
            entry
            for entry in entries
            if entry.status
            in {ReconciliationStatus.MISMATCH, ReconciliationStatus.AMBIGUOUS}
            or (
                entry.status is ReconciliationStatus.MISSING
                and entry.subject_id.startswith("section:")
            )
        ]
        if conflicts:
            status = (
                "ambiguous"
                if any(
                    entry.status is ReconciliationStatus.AMBIGUOUS
                    for entry in conflicts
                )
                else "mismatch"
            )
            subjects = ", ".join(entry.subject_id for entry in conflicts[:5])
            raise RichDocumentValidationError(
                f"pdf_validator_{status}",
                f"PDF validator {status} for source subjects: {subjects}",
            )
        page_map = _build_page_map(
            parsed.document,
            entries,
            legacy_primary.sections,
            parsed_validator.pages,
        )
        document = RichDocument(
            source=parsed.document.source,
            blocks=parsed.document.blocks,
            sections=parsed.document.sections,
            assets=parsed.document.assets,
            page_map=page_map,
            metadata=parsed.document.metadata,
        )
        return RichParseOutcome(
            document=document,
            report=ReconciliationReport(
                primary=bundle.primary,
                policy=ValidationPolicy.DETERMINISTIC_ONLY,
                entries=entries,
            ),
            warnings=tuple(
                dict.fromkeys(parsed.warnings + reconciliation_warnings)
            ),
        )

    def parse_source(self, artifact) -> RichDocument:
        """Parse a primary rich artifact without accepting a validator."""

        return self.parse(SourceBundle(primary=artifact)).document

    def _asset_importer(self, source_locator: str):
        def import_asset(target: str) -> RichAsset | None:
            if not source_locator:
                return None
            path = resolve_local_asset_path(source_locator, target)
            if path is None:
                return None
            if not path.is_file() and not path.suffix:
                path = next(
                    (
                        candidate
                        for suffix in (
                            ".pdf",
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".svg",
                            ".eps",
                        )
                        if (candidate := path.with_suffix(suffix)).is_file()
                    ),
                    path,
                )
            if not path.is_file():
                return None
            media_type = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            stored = self.repository.import_asset_path(path, media_type=media_type)
            return RichAsset(
                artifact_digest=stored.artifact_digest,
                media_type=stored.media_type,
                logical_name=target,
                size=stored.size,
            )

        return import_asset


def _build_page_map(
    document: RichDocument,
    entries: tuple[ReconciliationEntry, ...],
    legacy_sections,
    pages,
) -> tuple[RichPageMapEntry, ...]:
    entries_by_subject = {entry.subject_id: entry for entry in entries}
    unused_legacy = list(legacy_sections)
    heading_page_by_section: dict[str, int] = {}
    for section in document.sections:
        starts_with_heading = (
            section.block_start < len(document.blocks)
            and document.blocks[section.block_start].kind
            is RichBlockKind.HEADING
        )
        if not starts_with_heading and not (
            len(document.sections) == 1 and section.title == "Document"
        ):
            continue
        match_index = next(
            (
                index
                for index, legacy in enumerate(unused_legacy)
                if legacy.title == section.title
                and legacy.level == section.level
            ),
            None,
        )
        if match_index is None:
            continue
        legacy = unused_legacy.pop(match_index)
        entry = entries_by_subject.get(f"section:{legacy.section_id}")
        if entry is None or entry.status is not ReconciliationStatus.VERIFIED:
            continue
        evidence_pages = entry.provenance.get("page_candidates")
        if (
            isinstance(evidence_pages, list)
            and len(evidence_pages) == 1
            and isinstance(evidence_pages[0], int)
        ):
            heading_page_by_section[section.section_id] = evidence_pages[0]

    page_fingerprints = tuple(
        (page.page_number, _text_fingerprint(page.text))
        for page in pages
    )
    mapped: list[RichPageMapEntry] = []
    for block in document.blocks:
        if (
            block.kind is RichBlockKind.HEADING
            and block.section_path
            and block.section_path[-1] in heading_page_by_section
        ):
            mapped.append(
                RichPageMapEntry(
                    block_id=block.block_id,
                    page_number=heading_page_by_section[
                        block.section_path[-1]
                    ],
                )
            )
            continue
        phrase = _text_fingerprint(_block_text(block))
        if not _has_sufficient_page_evidence(phrase):
            continue
        candidates = [
            page_number
            for page_number, page_phrase in page_fingerprints
            if _phrase_occurs_on_page(phrase, page_phrase)
        ]
        if len(candidates) == 1:
            mapped.append(
                RichPageMapEntry(
                    block_id=block.block_id,
                    page_number=candidates[0],
                )
            )
    return tuple(mapped)


def _has_sufficient_page_evidence(phrase: str) -> bool:
    tokens = phrase.split()
    return len(tokens) >= 2 and len("".join(tokens)) >= 8


def _phrase_occurs_on_page(phrase: str, page_phrase: str) -> bool:
    return f" {phrase} " in f" {page_phrase} "


def _reconcile_synthetic_section(document, entries, pages):
    """Use body text when a heading-free source has no literal PDF title."""

    if (
        len(document.sections) != 1
        or document.sections[0].title != "Document"
    ):
        return entries
    section_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.subject_id.startswith("section:")
        ),
        None,
    )
    if section_index is None:
        return entries
    entry = entries[section_index]
    if entry.status is not ReconciliationStatus.MISSING:
        return entries
    source_text = " ".join(_block_text(block) for block in document.blocks)
    source_phrase = _text_fingerprint(source_text)
    source_tokens = set(source_phrase.split())
    candidates: list[int] = []
    for page in pages:
        page_phrase = _text_fingerprint(page.text)
        if not source_tokens:
            continue
        if len(source_tokens) < 3:
            matched = bool(source_phrase and source_phrase in page_phrase)
        else:
            matched = (
                len(source_tokens.intersection(page_phrase.split()))
                / len(source_tokens)
                >= 0.6
            )
        if matched:
            candidates.append(page.page_number)
    if not candidates:
        return entries
    status = (
        ReconciliationStatus.VERIFIED
        if len(candidates) == 1
        else ReconciliationStatus.AMBIGUOUS
    )
    replacement = ReconciliationEntry(
        validator=entry.validator,
        status=status,
        subject_id=entry.subject_id,
        message=(
            "heading-free rich source body maps to one PDF page"
            if status is ReconciliationStatus.VERIFIED
            else "heading-free rich source body maps to multiple PDF pages"
        ),
        provenance={
            "page_candidates": candidates,
            "matching_method": "source_body_text",
        },
    )
    return entries[:section_index] + (replacement,) + entries[section_index + 1 :]


def _block_text(block) -> str:
    payload = block.payload
    if block.kind in {
        RichBlockKind.HEADING,
        RichBlockKind.PARAGRAPH,
        RichBlockKind.CODE,
    }:
        return str(payload["text"])
    if block.kind is RichBlockKind.EQUATION:
        return str(payload["tex"])
    if block.kind is RichBlockKind.LIST:
        return " ".join(str(item["text"]) for item in payload["items"])
    if block.kind is RichBlockKind.TABLE:
        cells = tuple(payload["headers"]) + tuple(
            cell for row in payload["rows"] for cell in row
        )
        return " ".join(str(cell) for cell in cells)
    return f"{payload['alt_text']} {payload['caption']}"


def _text_fingerprint(value: str) -> str:
    return " ".join(
        re.findall(
            r"[^\W_]+",
            unicodedata.normalize("NFKC", value).casefold(),
            flags=re.UNICODE,
        )
    )


__all__ = [
    "PDF_VALIDATOR_MISSING_WARNING",
    "RichDocumentParserService",
    "RichDocumentValidationError",
    "RichParseOutcome",
]
