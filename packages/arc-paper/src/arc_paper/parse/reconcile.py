from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..sources import (
    ReconciliationEntry,
    ReconciliationStatus,
    SourceArtifact,
    SourceFormat,
)
from .models import MathSpan, ParsedDocument, VisualPageReviewInput


RICH_FORMATS = {SourceFormat.HTML, SourceFormat.MARKDOWN, SourceFormat.TEX}


def reconcile_validator(
    primary: ParsedDocument,
    validator: ParsedDocument,
) -> tuple[tuple[ReconciliationEntry, ...], tuple[str, ...]]:
    """Compare one validator independently without modifying the primary."""

    if validator.source.source_format is SourceFormat.PDF:
        return _reconcile_pdf(primary, validator)
    if validator.source.source_format in RICH_FORMATS:
        return _reconcile_rich(primary, validator)
    return (
        (
            _entry(
                validator.source,
                ReconciliationStatus.UNREVIEWED,
                "validator",
                "validator format has no deterministic reconciler",
            ),
        ),
        (f"{validator.source.source_format.value} validator was not reviewed",),
    )


def build_visual_page_review_inputs(
    primary: ParsedDocument,
    pdf_validator: ParsedDocument,
) -> tuple[VisualPageReviewInput, ...]:
    """Build one visual-review descriptor per PDF page, including empty pages."""

    if pdf_validator.source.source_format is not SourceFormat.PDF:
        raise ValueError("visual review input requires a parsed PDF validator")
    return tuple(
        VisualPageReviewInput(
            primary=primary.source,
            pdf_validator=pdf_validator.source,
            page_number=page.page_number,
            math_spans=primary.math_spans,
        )
        for page in pdf_validator.pages
    )


def _reconcile_rich(
    primary: ParsedDocument, validator: ParsedDocument
) -> tuple[tuple[ReconciliationEntry, ...], tuple[str, ...]]:
    entries: list[ReconciliationEntry] = []
    warnings: list[str] = []
    primary_titles = [_fingerprint(item.title) for item in primary.sections]
    validator_titles = [_fingerprint(item.title) for item in validator.sections]
    if primary_titles == validator_titles:
        entries.append(
            _entry(
                validator.source,
                ReconciliationStatus.VERIFIED,
                "structure",
                "validator section order and titles agree with the primary",
                section_count=len(primary_titles),
            )
        )
    else:
        entries.append(
            _entry(
                validator.source,
                ReconciliationStatus.MISMATCH,
                "structure",
                "validator section structure differs from the primary",
                primary_titles=primary_titles,
                observed_titles=validator_titles,
            )
        )
        warnings.append(
            f"{validator.source.source_format.value} validator structure differs from primary"
        )

    unmatched = set(range(len(validator.math_spans)))
    equal_span_counts = len(primary.math_spans) == len(validator.math_spans)
    for ordinal, span in enumerate(primary.math_spans):
        entry, matched = _match_rich_span(
            span,
            ordinal,
            validator.math_spans,
            unmatched,
            validator.source,
            equal_span_counts=equal_span_counts,
        )
        entries.append(entry)
        unmatched.difference_update(matched)
        if entry.status is not ReconciliationStatus.VERIFIED:
            warnings.append(f"validator math conflict for {span.span_id}: {entry.status.value}")
    for index in sorted(unmatched):
        observed = validator.math_spans[index]
        entries.append(
            _entry(
                validator.source,
                ReconciliationStatus.MISMATCH,
                f"validator:{observed.span_id}",
                "validator contains mathematical content not matched to the primary",
                observed_tex=observed.normalized_tex,
                observed_kind=observed.kind.value,
                observed_position=_position(observed),
            )
        )
        warnings.append(f"validator contains unmatched math {observed.span_id}")
    return tuple(entries), tuple(warnings)


def _match_rich_span(
    primary: MathSpan,
    ordinal: int,
    candidates: tuple[MathSpan, ...],
    unmatched: set[int],
    validator: SourceArtifact,
    *,
    equal_span_counts: bool,
) -> tuple[ReconciliationEntry, set[int]]:
    same_label = {
        index
        for index in unmatched
        if primary.source_label
        and candidates[index].source_label
        and candidates[index].source_label == primary.source_label
    }
    same_tex = {
        index
        for index in unmatched
        if _math_fingerprint(candidates[index].normalized_tex)
        == _math_fingerprint(primary.normalized_tex)
    }
    same_kind_tex = {
        index for index in same_tex if candidates[index].kind is primary.kind
    }
    selected = same_label or same_kind_tex or same_tex
    method = "source_label" if same_label else ("kind_and_math" if same_kind_tex else "math")
    if len(selected) > 1:
        context_matches = {
            index
            for index in selected
            if _context_fingerprint(candidates[index])
            and _context_fingerprint(candidates[index]) == _context_fingerprint(primary)
        }
        if len(context_matches) == 1:
            selected = context_matches
            method += "_and_context"
    if len(selected) > 1:
        return (
            _entry(
                validator,
                ReconciliationStatus.AMBIGUOUS,
                primary.span_id,
                "multiple validator math spans match the primary",
                primary_tex=primary.normalized_tex,
                candidate_span_ids=[candidates[index].span_id for index in sorted(selected)],
                matching_method=method,
            ),
            set(),
        )
    if len(selected) == 1:
        index = next(iter(selected))
        observed = candidates[index]
        if _math_fingerprint(observed.normalized_tex) == _math_fingerprint(
            primary.normalized_tex
        ):
            return (
                _entry(
                    validator,
                    ReconciliationStatus.VERIFIED,
                    primary.span_id,
                    "validator mathematical content agrees with the primary",
                    observed_span_id=observed.span_id,
                    observed_tex=observed.normalized_tex,
                    observed_position=_position(observed),
                    matching_method=method,
                ),
                {index},
            )
        return (
            _entry(
                validator,
                ReconciliationStatus.MISMATCH,
                primary.span_id,
                "validator label matches but mathematical content differs",
                primary_tex=primary.normalized_tex,
                observed_span_id=observed.span_id,
                observed_tex=observed.normalized_tex,
                matching_method=method,
            ),
            {index},
        )
    # Stable order is evidence only when both sources expose the same span
    # count. It may identify a disagreement, but never overwrites the primary.
    if equal_span_counts and ordinal < len(candidates) and ordinal in unmatched:
        observed = candidates[ordinal]
        return (
            _entry(
                validator,
                ReconciliationStatus.MISMATCH,
                primary.span_id,
                "validator span at the same sequence position differs",
                primary_tex=primary.normalized_tex,
                observed_span_id=observed.span_id,
                observed_tex=observed.normalized_tex,
                matching_method="sequence",
            ),
            {ordinal},
        )
    return (
        _entry(
            validator,
            ReconciliationStatus.MISSING,
            primary.span_id,
            "validator contains no deterministic match for primary math",
            primary_tex=primary.normalized_tex,
            primary_position=_position(primary),
        ),
        set(),
    )


def _reconcile_pdf(
    primary: ParsedDocument, validator: ParsedDocument
) -> tuple[tuple[ReconciliationEntry, ...], tuple[str, ...]]:
    if not bool(validator.metadata.get("text_layer")):
        message = (
            "PDF validator has no extractable text layer; deterministic validation is partial"
        )
        return (
            (
                _entry(
                    validator.source,
                    ReconciliationStatus.UNREVIEWED,
                    "pdf-text-layer",
                    message,
                    page_count=len(validator.pages),
                ),
            ),
            (message,),
        )

    entries: list[ReconciliationEntry] = []
    warnings: list[str] = []
    raw_pages = [page.text for page in validator.pages]
    for section in primary.sections:
        title = _fingerprint(section.title)
        exact_matching_pages = _pages_for_exact_section_title(
            raw_pages, section.title
        )
        joined_matching_pages = _pages_for_joined_section_title(
            raw_pages, section.title
        )
        body_anchor, body_matching_pages = _pages_for_section_body_anchor(
            raw_pages, section.title, section.text
        )
        substring_matching_pages = _pages_for_section_title_substrings(
            raw_pages,
            _fingerprint(
                _without_conventional_pdf_section_prefix(section.title)
            )
            or title,
        )
        # A unique exact title is strongest.  A title can also occur in a TOC,
        # however, so a bounded joined-heading or body anchor may safely break
        # that tie before the deliberately conservative substring fallback.
        if len(exact_matching_pages) == 1:
            matching_pages, method = exact_matching_pages, "normalized_exact_line"
        elif len(joined_matching_pages) == 1:
            matching_pages, method = joined_matching_pages, "joined_heading_lines"
        elif len(body_matching_pages) == 1:
            matching_pages, method = body_matching_pages, "content_anchor"
        elif exact_matching_pages:
            matching_pages, method = exact_matching_pages, "normalized_exact_line"
        elif joined_matching_pages:
            matching_pages, method = joined_matching_pages, "joined_heading_lines"
        elif body_matching_pages:
            matching_pages, method = body_matching_pages, "content_anchor"
        else:
            matching_pages = substring_matching_pages
            method = "normalized_page_substring" if matching_pages else "none"
        if len(matching_pages) == 1:
            status = ReconciliationStatus.VERIFIED
            message = "primary section title maps to one PDF page"
        elif not matching_pages:
            status = ReconciliationStatus.MISSING
            message = "primary section title was not found in the PDF text layer"
        else:
            status = ReconciliationStatus.AMBIGUOUS
            message = "primary section title maps to multiple PDF pages"
        entries.append(
            _entry(
                validator.source,
                status,
                f"section:{section.section_id}",
                message,
                page_candidates=matching_pages,
                title=section.title,
                matching_method=method,
                body_anchor=body_anchor,
            )
        )
        if status is not ReconciliationStatus.VERIFIED:
            warnings.append(
                f"PDF section evidence {status.value} for {section.section_id}"
            )

    for span in primary.math_spans:
        pages_by_label = _pages_for_printed_label(raw_pages, span.source_label)
        pages_by_math = _pages_for_math(raw_pages, span.normalized_tex)
        # A printed number locates an equation, but it does not independently
        # prove that the equation's mathematical content agrees.  Preserve it
        # as provenance only; content verification requires math evidence.
        matching_pages = sorted(set(pages_by_math))
        method = "normalized_math" if pages_by_math else "none"
        if len(matching_pages) == 1:
            status = ReconciliationStatus.VERIFIED
            message = "PDF text layer provides deterministic math evidence"
        elif len(matching_pages) > 1:
            status = ReconciliationStatus.AMBIGUOUS
            message = "PDF math evidence occurs on multiple pages"
        else:
            status = ReconciliationStatus.UNREVIEWED
            message = "PDF text layer does not provide deterministic evidence for this span"
        provenance: dict[str, Any] = {
            "page_candidates": matching_pages,
            "matching_method": method,
        }
        if pages_by_label:
            provenance["printed_label_page_candidates"] = pages_by_label
        printed = _printed_number(span.source_label)
        if printed:
            provenance["printed_equation_number"] = printed
        entries.append(
            _entry(
                validator.source,
                status,
                span.span_id,
                message,
                **provenance,
            )
        )
        if status is not ReconciliationStatus.VERIFIED:
            warnings.append(f"PDF math evidence {status.value} for {span.span_id}")
    label_entry, label_warning = _strict_pdf_equation_label_mapping(
        primary, validator, raw_pages
    )
    if label_entry is not None:
        entries.append(label_entry)
    if label_warning:
        warnings.append(label_warning)
    return tuple(entries), tuple(warnings)


def _pages_for_exact_section_title(pages: list[str], title: str) -> list[int]:
    """Return pages containing the title as one normalized text-layer line.

    PDF tables of contents normally retain a page number or leader after a
    section title.  Treating the whole extracted line as evidence therefore
    prefers a rendered heading over a TOC mention without requiring layout
    metadata from the PDF extractor.  A source title is authoritative: only
    after an exact source-title match fails may a conventional PDF-only
    section number be ignored.
    """

    needle = _fingerprint(title)
    if not needle:
        return []
    exact_matching_pages = [
        page_number
        for page_number, page in enumerate(pages, 1)
        if any(_fingerprint(line) == needle for line in page.splitlines())
    ]
    if exact_matching_pages:
        return exact_matching_pages
    semantic_needle = _fingerprint(_without_conventional_pdf_section_prefix(title))
    if semantic_needle and semantic_needle != needle:
        semantic_matches = [
            page_number
            for page_number, page in enumerate(pages, 1)
            if any(
                _fingerprint(line) == semantic_needle
                or _fingerprint(_without_conventional_pdf_section_prefix(line))
                == semantic_needle
                for line in page.splitlines()
            )
        ]
        if semantic_matches:
            return semantic_matches
    return [
        page_number
        for page_number, page in enumerate(pages, 1)
        if any(
            _fingerprint(_without_conventional_pdf_section_prefix(line)) == needle
            for line in page.splitlines()
        )
    ]


_PDF_CONVENTIONAL_SECTION_PREFIX = re.compile(
    r"^\s*(?:"
    r"(?:\d{1,2}|[IVXLCDM]+)(?:\s*\.\s*\d{1,2})+(?:\s*[.)])?"
    r"|\d{1,2}\s*[.)]"
    r"|\d{1,2}"
    r"|[IVXLCDM]+\s*[.)]"
    r"|[IVXLCDM]+"
    r"|[A-Z]\s*[.)]"
    r")\s+(?=\S)"
)
_PDF_SECTION_LIKE_PREFIX = re.compile(
    r"^\s*(?:(?:\d+|[IVXLCDM]+)(?:\s*\.\s*\d+)*|[IVXLCDM]+)\s*[.)]?\s+(?=\S)"
)


def _pages_for_section_title_substrings(pages: list[str], title: str) -> list[int]:
    """Find line-level prose evidence without accepting title-like prefixes."""

    if not title:
        return []
    return [
        page_number
        for page_number, page in enumerate(pages, 1)
        if any(
            _line_has_section_title_substring(line, title)
            for line in page.splitlines()
        )
    ]


def _pages_for_joined_section_title(pages: list[str], title: str) -> list[int]:
    """Find an exact heading split across at most three extracted lines."""

    needle = _fingerprint(_without_conventional_pdf_section_prefix(title))
    if not needle:
        return []
    matches: list[int] = []
    for page_number, page in enumerate(pages, 1):
        lines = page.splitlines()
        for index in range(len(lines)):
            for width in (2, 3):
                joined = " ".join(lines[index : index + width])
                if len(lines[index : index + width]) != width:
                    continue
                normalized = _fingerprint(joined)
                stripped = _fingerprint(_without_conventional_pdf_section_prefix(joined))
                if normalized == needle or stripped == needle:
                    matches.append(page_number)
                    break
            else:
                continue
            break
    return matches


def _pages_for_section_body_anchor(
    pages: list[str], title: str, text: str
) -> tuple[list[str], list[int]]:
    """Return the first unique eight-token body anchor within the first 128 tokens."""

    tokens = _fingerprint(text).split()
    title_tokens = _fingerprint(title).split()
    if title_tokens and tokens[: len(title_tokens)] == title_tokens:
        tokens = tokens[len(title_tokens) :]
    tokens = tokens[:128]
    if len(tokens) < 8:
        return [], []
    page_tokens = [_fingerprint(page).split() for page in pages]
    fallback: tuple[list[str], list[int]] = ([], [])
    for start in range(len(tokens) - 7):
        anchor = tokens[start : start + 8]
        candidates = [
            page_number
            for page_number, observed in enumerate(page_tokens, 1)
            if _contains_token_run(observed, anchor)
        ]
        if len(candidates) == 1:
            return anchor, candidates
        if candidates and not fallback[1]:
            fallback = (anchor, candidates)
    return fallback


def _contains_token_run(values: list[str], needle: list[str]) -> bool:
    width = len(needle)
    return any(
        values[index : index + width] == needle
        for index in range(len(values) - width + 1)
    )


def _line_has_section_title_substring(line: str, title: str) -> bool:
    """Return whether one non-heading line provides page-level title evidence."""

    # A one-letter leading token is indistinguishable from prose (notably the
    # article in ``A Model``) in a substring search.  Exact and conventional
    # prefixed-heading matching have already run before this fallback, so do
    # not let weak body prose establish a section page for such titles.
    if re.match(r"^[A-Za-z]\s+", title):
        return False
    normalized = _fingerprint(line)
    if f" {title} " not in f" {normalized} ":
        return False
    if _title_with_only_trailing_page_number(normalized, title):
        return False
    remainder = _PDF_SECTION_LIKE_PREFIX.sub("", line, count=1)
    if remainder != line:
        normalized_remainder = _fingerprint(remainder)
        if normalized_remainder == title or _title_with_only_trailing_page_number(
            normalized_remainder, title
        ):
            return False
    return True


def _title_with_only_trailing_page_number(value: str, title: str) -> bool:
    return re.fullmatch(rf"{re.escape(title)} \d+", value) is not None


def _without_conventional_pdf_section_prefix(value: str) -> str:
    """Remove one conventional decimal or uppercase-Roman PDF section label."""

    return _PDF_CONVENTIONAL_SECTION_PREFIX.sub("", value, count=1)


def _pages_for_printed_label(pages: list[str], label: str) -> list[int]:
    printed = _printed_number(label)
    if not printed:
        return []
    pattern = re.compile(rf"\(\s*{re.escape(printed)}\s*\)")
    return [index for index, page in enumerate(pages, 1) if pattern.search(page)]


def _printed_number(label: str) -> str:
    match = re.search(r"(?:^|[^\d])(\d+(?:\.\d+)+|\d+)(?:$|[^\d])", label)
    return match.group(1) if match else ""


def _pure_equation_number(label: str) -> str:
    value = label.strip()
    return value if re.fullmatch(r"[1-9]\d*", value) else ""


def _strict_pdf_equation_label_mapping(
    primary: ParsedDocument,
    validator: ParsedDocument,
    raw_pages: list[str],
) -> tuple[ReconciliationEntry | None, str]:
    """Map labels only when both complete ordered equation sequences agree.

    The mapping is intentionally separate from math-content reconciliation: a
    sequence of printed numbers can establish a display-label provenance, but
    it cannot prove the TeX extracted from a PDF is equivalent.
    """

    all_primary_display_spans = [
        span for span in primary.math_spans if span.kind.value == "display"
    ]
    if not all_primary_display_spans:
        return None, ""
    primary_spans = [
        span
        for span in all_primary_display_spans
        if _pure_equation_number(span.source_label)
    ]
    if len(primary_spans) != len(all_primary_display_spans):
        return (
            None,
            "PDF equation labels were not canonically mapped: primary display equations are not all uniquely numbered",
        )
    primary_labels = [
        _pure_equation_number(span.source_label) for span in primary_spans
    ]
    if len(set(primary_labels)) != len(primary_labels):
        return (
            None,
            "PDF equation labels were not canonically mapped: primary labels are not unique",
        )
    expected = [str(index) for index in range(1, len(primary_spans) + 1)]
    pdf_units, layout_warning = _pdf_layout_equation_units(
        raw_pages, expected_count=len(primary_spans)
    )
    if pdf_units is None:
        return (
            None,
            layout_warning,
        )
    pdf_labels = [str(unit.label) for unit in pdf_units]
    if pdf_labels != expected:
        return (
            None,
            "PDF equation labels were not canonically mapped: complete numeric sequence is unavailable",
        )
    page_numbers = [unit.page_number for unit in pdf_units]
    if page_numbers != sorted(page_numbers):
        return (
            None,
            "PDF equation labels were not canonically mapped: printed labels are out of document order",
        )
    mappings = [
        {
            "primary_span_id": span.span_id,
            "source_label": source_label,
            "pdf_label": pdf_label,
            "effective_label": pdf_label,
            "page_number": page_number,
            "matching_method": "strict_complete_pdf_sequence",
        }
        for span, source_label, pdf_label, page_number in zip(
            primary_spans, primary_labels, pdf_labels, page_numbers, strict=True
        )
    ]
    return (
        _entry(
            validator.source,
            ReconciliationStatus.VERIFIED,
            "equation-labels",
            "PDF printed equation labels form a complete ordered canonical sequence",
            mappings=mappings,
            matching_method="strict_complete_pdf_sequence",
        ),
        "",
    )


@dataclass(frozen=True)
class _PDFLayoutEquationUnit:
    """One printed equation number located in a layout-text formula region."""

    label: int
    page_number: int


@dataclass(frozen=True)
class _PDFLayoutLabelCandidate:
    label: int
    page_number: int
    line_index: int
    label_column: int
    score: int
    has_direct_formula: bool


def _pdf_layout_equation_units(
    raw_pages: list[str], *, expected_count: int
) -> tuple[tuple[_PDFLayoutEquationUnit, ...] | None, str]:
    """Recover a complete canonical sequence from ``pdftotext -layout`` text.

    A normal prose reference such as ``see (22)`` has neither a column-boundary
    position nor nearby formula-shaped text.  A rendered equation number is
    typically at a line end or followed by the whitespace gap to another
    column, and can therefore be located without treating every short line
    containing ``-`` or ``=`` as a mathematical display.
    """

    if expected_count < 1:
        return (), ""
    candidates = _pdf_layout_label_candidates(raw_pages, expected_count)
    selected: list[_PDFLayoutLabelCandidate] = []
    for label in range(1, expected_count + 1):
        matches = [candidate for candidate in candidates if candidate.label == label]
        if not matches:
            return (
                None,
                "PDF equation labels were not canonically mapped: complete numbered layout sequence is unavailable",
            )
        best_score = max(candidate.score for candidate in matches)
        best = [candidate for candidate in matches if candidate.score == best_score]
        if len(best) != 1:
            return (
                None,
                "PDF equation labels were not canonically mapped: layout evidence is ambiguous",
            )
        selected.append(best[0])
    order_warning = _pdf_layout_order_warning(selected)
    if order_warning:
        return None, order_warning
    evidence_warning = _pdf_layout_label_evidence_warning(selected)
    if evidence_warning:
        return None, evidence_warning
    if _has_independent_compact_unlabelled_formula(raw_pages, selected):
        return (
            None,
            "PDF equation labels were not canonically mapped: an unlabelled compact display block was detected",
        )
    return (
        tuple(
            _PDFLayoutEquationUnit(
                label=candidate.label,
                page_number=candidate.page_number,
            )
            for candidate in selected
        ),
        "",
    )


def _pdf_layout_label_candidates(
    raw_pages: list[str], expected_count: int
) -> list[_PDFLayoutLabelCandidate]:
    output: list[_PDFLayoutLabelCandidate] = []
    for page_number, page in enumerate(raw_pages, 1):
        lines = page.splitlines()
        for line_index, line in enumerate(lines):
            for match in re.finditer(r"\(\s*(\d+)\s*\)", line):
                label = int(match.group(1))
                if not 1 <= label <= expected_count:
                    continue
                region = _pdf_layout_label_region(line, match)
                if region is None:
                    continue
                side, text = region
                direct_score = _pdf_layout_direct_formula_score(line, match, text)
                score = _pdf_layout_formula_score(
                    lines,
                    line_index=line_index,
                    label_column=match.start(),
                    side=side,
                    own_text=text,
                )
                score = max(score, 8 * direct_score)
                if score:
                    output.append(
                        _PDFLayoutLabelCandidate(
                            label=label,
                            page_number=page_number,
                            line_index=line_index,
                            label_column=match.start(),
                            score=score,
                            has_direct_formula=direct_score >= 3,
                        )
                    )
    return output


def _pdf_layout_label_region(
    line: str, match: re.Match[str]
) -> tuple[str, str] | None:
    """Return the formula-side text only for a column-boundary label token."""

    suffix = line[match.end() :]
    next_content = next(
        (index for index, value in enumerate(suffix) if not value.isspace()),
        None,
    )
    if next_content is None:
        return "tail", line[max(0, match.start() - 72) : match.start()]
    if next_content < 3:
        return None
    return "prefix", line[: match.start()]


def _pdf_layout_formula_score(
    lines: list[str],
    *,
    line_index: int,
    label_column: int,
    side: str,
    own_text: str,
) -> int:
    """Score local formula evidence while keeping prose references below zero."""

    score = 8 * _pdf_formula_shape_score(own_text)
    for index in range(max(0, line_index - 4), min(len(lines), line_index + 5)):
        if index == line_index:
            continue
        distance = abs(index - line_index)
        score += (5 - distance) * _pdf_formula_shape_score(
            _pdf_layout_neighbor_region(
                lines[index], label_column=label_column, side=side
            )
        )
    return score


def _pdf_layout_direct_formula_score(
    line: str, match: re.Match[str], own_text: str
) -> int:
    """Require same-line formula evidence before trusting a short label token."""

    score = _pdf_formula_shape_score(own_text)
    suffix = line[match.end() :]
    next_content = next(
        (index for index, value in enumerate(suffix) if not value.isspace()),
        None,
    )
    if next_content is not None and next_content >= 3:
        score = max(score, _pdf_formula_shape_score(suffix[next_content:]))
    return score


def _pdf_layout_neighbor_region(
    line: str, *, label_column: int, side: str
) -> str:
    if side == "prefix":
        return line[:label_column]
    return line[max(0, label_column - 72) : label_column]


def _pdf_layout_order_warning(selected: list[_PDFLayoutLabelCandidate]) -> str:
    """Require visual label order while permitting established text columns."""

    if [candidate.page_number for candidate in selected] != sorted(
        candidate.page_number for candidate in selected
    ):
        return (
            "PDF equation labels were not canonically mapped: printed labels are out of document order"
        )
    by_page: dict[int, list[_PDFLayoutLabelCandidate]] = {}
    for candidate in selected:
        by_page.setdefault(candidate.page_number, []).append(candidate)
    for page_number in sorted(by_page):
        columns = _pdf_layout_columns(by_page[page_number])
        if len(columns) > 1 and all(len(column) == 1 for column in columns):
            return (
                "PDF equation labels were not canonically mapped: layout columns are insufficiently established"
            )
        previous_label: int | None = None
        for column in columns:
            labels = [
                candidate.label
                for candidate in sorted(column, key=lambda item: item.label)
            ]
            if labels != list(range(labels[0], labels[-1] + 1)) or (
                previous_label is not None and labels[0] != previous_label + 1
            ):
                return (
                    "PDF equation labels were not canonically mapped: labels do not form visual layout intervals"
                )
            ordered = sorted(column, key=lambda item: item.line_index)
            if [candidate.label for candidate in ordered] != labels or any(
                left.line_index >= right.line_index
                for left, right in zip(ordered, ordered[1:], strict=False)
            ):
                return (
                    "PDF equation labels were not canonically mapped: printed labels are not in visual layout order"
                )
            previous_label = labels[-1]
    return ""


def _pdf_layout_columns(
    candidates: list[_PDFLayoutLabelCandidate],
) -> tuple[tuple[_PDFLayoutLabelCandidate, ...], ...]:
    """Group label positions only when layout text shows a wide column gap."""

    ordered = sorted(candidates, key=lambda item: item.label_column)
    if not ordered:
        return ()
    columns: list[list[_PDFLayoutLabelCandidate]] = [[ordered[0]]]
    for candidate in ordered[1:]:
        if candidate.label_column - columns[-1][-1].label_column > 24:
            columns.append([])
        columns[-1].append(candidate)
    return tuple(tuple(column) for column in columns)


def _pdf_layout_label_evidence_warning(
    selected: list[_PDFLayoutLabelCandidate],
) -> str:
    """Reject neighbor-promoted labels unless their tag column is established."""

    columns = _pdf_layout_columns(selected)
    for candidate in selected:
        if candidate.has_direct_formula:
            continue
        column = next(column for column in columns if candidate in column)
        if len(column) < 2 or not any(
            item.has_direct_formula
            and abs(item.label_column - candidate.label_column) <= 12
            for item in column
        ):
            return (
                "PDF equation labels were not canonically mapped: label-only layout tokens lack an established tag column"
            )
    return ""


def _pdf_formula_shape_score(value: str) -> int:
    """Identify compact symbolic text without promoting ordinary prose."""

    stripped = _pdf_formula_fragment(value)
    if not stripped:
        return 0
    strong_symbols = re.findall(r"[=≡≈≃≤≥<>∑∫√∂±]", stripped)
    short_words = re.findall(r"[A-Za-z]{3,}", stripped)
    non_prose_symbols = re.findall(
        r"[^\sA-Za-z0-9.,;:()\[\]{}]", stripped
    )
    digits = re.findall(r"\d", stripped)
    if _looks_like_prose_formula(stripped, short_words):
        return 0
    if strong_symbols:
        return 3
    if len(short_words) <= 3 and (non_prose_symbols or len(digits) >= 2):
        return 1
    return 0


def _looks_like_prose_formula(value: str, words: list[str]) -> bool:
    """Do not let ordinary sentence words turn ``x = 1`` into a display."""

    if not words:
        return False
    return re.search(r"[+*/^_|]|[^\x00-\x7F]", value) is None


def _pdf_formula_fragment(value: str) -> str:
    """Use a compact right-column expression instead of adjacent prose."""

    stripped = value.rstrip()
    if not stripped:
        return ""
    parts = re.split(r"\s{3,}", stripped)
    return parts[-1].strip()


def _has_independent_compact_unlabelled_formula(
    raw_pages: list[str], selected: list[_PDFLayoutLabelCandidate]
) -> bool:
    """Reject only unmistakable standalone formula lines without a label.

    Layout text cannot always distinguish a multiline numbered expression from
    an adjacent unnumbered expression. This deliberately narrow check catches
    a compact assignment aligned between two numbered displays, without
    treating ordinary indented continuation fragments as separate units.
    """

    selected_by_page: dict[int, list[_PDFLayoutLabelCandidate]] = {}
    for candidate in selected:
        selected_by_page.setdefault(candidate.page_number, []).append(candidate)
    for page_number, page in enumerate(raw_pages, 1):
        lines = page.splitlines()
        for column in _pdf_layout_columns(selected_by_page.get(page_number, [])):
            ordered = sorted(column, key=lambda item: item.line_index)
            for first, second in zip(ordered, ordered[1:], strict=False):
                first_indent = _pdf_label_formula_indent(lines, first)
                second_indent = _pdf_label_formula_indent(lines, second)
                if first_indent is None or first_indent != second_indent:
                    continue
                for line in lines[first.line_index + 1 : second.line_index]:
                    if _is_compact_independent_assignment(line, first_indent):
                        return True
    return False


def _pdf_label_formula_indent(
    lines: list[str], candidate: _PDFLayoutLabelCandidate
) -> int | None:
    before_label = lines[candidate.line_index][: candidate.label_column].rstrip()
    fragment = _pdf_formula_fragment(before_label)
    if _pdf_formula_shape_score(fragment) < 3:
        return None
    return before_label.rfind(fragment)


def _is_compact_independent_assignment(line: str, expected_indent: int) -> bool:
    """Identify a short standalone assignment aligned with numbered displays."""

    if re.search(r"\(\s*\d+\s*\)", line):
        return False
    indent = len(line) - len(line.lstrip())
    if indent != expected_indent:
        return False
    return re.fullmatch(
        r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9_{}^]*(?:\s*\([^()]{1,20}\))?\s*[=≡≈≃≤≥<>]\s*\S.{0,20}",
        line.strip(),
    ) is not None


def _pages_for_math(pages: list[str], tex: str) -> list[int]:
    needle = _math_text_fingerprint(tex)
    if len(needle) < 3:
        return []
    return [
        index
        for index, page in enumerate(pages, 1)
        if needle in _math_text_fingerprint(page)
    ]


def _math_text_fingerprint(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"\\(?:left|right|mathrm|mathbf|mathcal|operatorname)", "", text)
    text = re.sub(r"\\([a-zA-Z]+)", r"\1", text)
    return "".join(re.findall(r"[\w=+\-*/^]", text, flags=re.UNICODE))


def _math_fingerprint(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _fingerprint(value: str) -> str:
    return " ".join(
        re.findall(
            r"[^\W_]+",
            unicodedata.normalize("NFKC", value).casefold(),
            flags=re.UNICODE,
        )
    )


def _context_fingerprint(span: MathSpan) -> str:
    return _fingerprint(f"{span.context_before} {span.context_after}")


def _position(span: MathSpan) -> dict[str, int | None]:
    return {
        "line_start": span.source_line_start,
        "column_start": span.source_column_start,
        "line_end": span.source_line_end,
        "column_end": span.source_column_end,
    }


def _entry(
    validator: SourceArtifact,
    status: ReconciliationStatus,
    subject_id: str,
    message: str,
    **provenance: Any,
) -> ReconciliationEntry:
    return ReconciliationEntry(
        validator=validator,
        status=status,
        subject_id=subject_id,
        message=message,
        provenance=provenance,
    )


__all__ = ["build_visual_page_review_inputs", "reconcile_validator"]
