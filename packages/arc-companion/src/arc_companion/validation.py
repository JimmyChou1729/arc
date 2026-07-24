"""Business validation for immutable source-anchored companion content."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import re
from urllib.parse import urlparse

from .contracts import AcceptedBook, SourceAnchor


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str


class AcceptedBookValidationError(ValueError):
    """An accepted book violates the renderable-content contract."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        summary = "; ".join(
            f"{item.path}: {item.message}" for item in self.issues[:5]
        )
        if len(self.issues) > 5:
            summary += f"; and {len(self.issues) - 5} more"
        super().__init__(summary)


def validate_accepted_book(
    book: AcceptedBook,
    *,
    expected_block_ids: Iterable[str] | None = None,
) -> tuple[ValidationIssue, ...]:
    """Return all business-contract violations without changing content."""

    issues: list[ValidationIssue] = []
    chapter_ids: set[str] = set()
    anchor_ids: set[str] = set()
    ordinals: list[int] = []
    bibliography_ids: set[str] = set()
    for evidence_index, evidence in enumerate(book.bibliography):
        path = f"bibliography[{evidence_index}]"
        if evidence.evidence_id in bibliography_ids:
            _issue(
                issues,
                "duplicate_evidence_id",
                f"{path}.evidence_id",
                "bibliography evidence IDs must be unique",
            )
        bibliography_ids.add(evidence.evidence_id)

    for chapter_index, chapter in enumerate(book.chapters):
        chapter_path = f"chapters[{chapter_index}]"
        if chapter.chapter_id in chapter_ids:
            _issue(
                issues,
                "duplicate_chapter_id",
                f"{chapter_path}.chapter_id",
                "chapter IDs must be unique",
            )
        chapter_ids.add(chapter.chapter_id)
        local_anchor_ids: set[str] = set()
        local_ordinals: list[int] = []
        for anchor_index, anchor in enumerate(chapter.source_anchors):
            path = f"{chapter_path}.source_anchors[{anchor_index}]"
            if anchor.block_id in anchor_ids:
                _issue(
                    issues,
                    "duplicate_source_anchor",
                    f"{path}.block_id",
                    "each source block must appear exactly once in the book",
                )
            anchor_ids.add(anchor.block_id)
            local_anchor_ids.add(anchor.block_id)
            ordinals.append(anchor.ordinal)
            local_ordinals.append(anchor.ordinal)
            _validate_anchor(anchor, path=path, issues=issues)
        if local_ordinals != sorted(local_ordinals):
            _issue(
                issues,
                "chapter_anchor_order",
                f"{chapter_path}.source_anchors",
                "source anchors must follow source ordinal order",
            )

        translated_ids = [item.block_id for item in chapter.translations]
        if len(set(translated_ids)) != len(translated_ids):
            _issue(
                issues,
                "duplicate_translation",
                f"{chapter_path}.translations",
                "a source block may have only one translation",
            )
        if book.translation_mode == "enabled":
            expected = [item.block_id for item in chapter.source_anchors]
            if translated_ids != expected:
                _issue(
                    issues,
                    "translation_coverage",
                    f"{chapter_path}.translations",
                    "enabled translation must cover every source block in anchor order",
                )
        elif translated_ids:
            _issue(
                issues,
                "translation_forbidden",
                f"{chapter_path}.translations",
                "skipped translation mode must not contain translations",
            )

        unit_ids: set[str] = set()
        for unit_index, unit in enumerate(chapter.learning_units):
            path = f"{chapter_path}.learning_units[{unit_index}]"
            if unit.unit_id in unit_ids:
                _issue(
                    issues,
                    "duplicate_learning_unit",
                    f"{path}.unit_id",
                    "learning unit IDs must be unique within a chapter",
                )
            unit_ids.add(unit.unit_id)
            unknown = set(unit.anchor_ids) - local_anchor_ids
            if unknown:
                _issue(
                    issues,
                    "unknown_learning_anchor",
                    f"{path}.anchor_ids",
                    f"learning unit refers to unknown anchors: {sorted(unknown)}",
                )
            if unit.kind == "further_reading" and not unit.citations:
                _issue(
                    issues,
                    "missing_evidence_citation",
                    f"{path}.citations",
                    "further-reading units require frozen evidence citations",
                )
            _validate_citations(
                unit.citations,
                allowed=bibliography_ids,
                path=f"{path}.citations",
                issues=issues,
            )

    if ordinals != sorted(ordinals):
        _issue(
            issues,
            "book_anchor_order",
            "chapters",
            "chapters and anchors must preserve source ordinal order",
        )
    if expected_block_ids is not None:
        expected = tuple(expected_block_ids)
        actual = tuple(
            anchor.block_id
            for chapter in book.chapters
            for anchor in chapter.source_anchors
        )
        if actual != expected:
            _issue(
                issues,
                "source_coverage",
                "chapters",
                "accepted chapters must exactly cover the expected source blocks",
            )

    entry_ids: set[str] = set()
    normalized_terms: set[str] = set()
    for entry_index, entry in enumerate(book.glossary):
        path = f"glossary[{entry_index}]"
        normalized = entry.term.casefold().strip()
        if entry.entry_id in entry_ids:
            _issue(
                issues,
                "duplicate_glossary_id",
                f"{path}.entry_id",
                "glossary entry IDs must be unique",
            )
        if normalized in normalized_terms:
            _issue(
                issues,
                "duplicate_glossary_term",
                f"{path}.term",
                "glossary source terms must be unique",
            )
        entry_ids.add(entry.entry_id)
        normalized_terms.add(normalized)
        unknown = set(entry.anchor_ids) - anchor_ids
        if unknown:
            _issue(
                issues,
                "unknown_glossary_anchor",
                f"{path}.anchor_ids",
                f"glossary entry refers to unknown anchors: {sorted(unknown)}",
            )
        _validate_citations(
            entry.citations,
            allowed=bibliography_ids,
            path=f"{path}.citations",
            issues=issues,
        )
    return tuple(issues)


def require_valid_accepted_book(
    book: AcceptedBook,
    *,
    expected_block_ids: Iterable[str] | None = None,
) -> None:
    issues = validate_accepted_book(book, expected_block_ids=expected_block_ids)
    if issues:
        raise AcceptedBookValidationError(issues)


def _validate_anchor(
    anchor: SourceAnchor, *, path: str, issues: list[ValidationIssue]
) -> None:
    payload = anchor.payload
    if anchor.kind == "heading":
        _require_payload_fields(payload, {"text", "level"}, path, issues)
    elif anchor.kind == "paragraph":
        _require_payload_fields(
            payload,
            {"text", "links", "inline_math", "inline_spans"},
            path,
            issues,
        )
        _validate_links(payload.get("links"), path=f"{path}.payload.links", issues=issues)
        _validate_inline_spans(
            payload,
            path=f"{path}.payload",
            issues=issues,
        )
    elif anchor.kind == "list":
        _require_payload_fields(payload, {"ordered", "items"}, path, issues)
        items = payload.get("items")
        if isinstance(items, (list, tuple)):
            for index, item in enumerate(items):
                if not isinstance(item, Mapping):
                    _issue(
                        issues,
                        "invalid_list_item",
                        f"{path}.payload.items[{index}]",
                        "list items must be objects",
                    )
                    continue
                _validate_links(
                    item.get("links"),
                    path=f"{path}.payload.items[{index}].links",
                    issues=issues,
                )
                if set(item) != {"text", "links", "inline_math", "inline_spans"}:
                    _issue(
                        issues,
                        "invalid_list_item",
                        f"{path}.payload.items[{index}]",
                        "list item fields do not match the inline contract",
                    )
                else:
                    _validate_inline_spans(
                        item,
                        path=f"{path}.payload.items[{index}]",
                        issues=issues,
                    )
    elif anchor.kind == "code":
        _require_payload_fields(payload, {"text", "language"}, path, issues)
    elif anchor.kind == "equation":
        _require_payload_fields(payload, {"tex", "display", "label"}, path, issues)
        if not isinstance(payload.get("tex"), str) or not payload.get("tex"):
            _issue(
                issues,
                "invalid_equation",
                f"{path}.payload.tex",
                "equation TeX must be non-empty",
            )
    elif anchor.kind == "table":
        _require_payload_fields(payload, {"headers", "rows", "caption"}, path, issues)
        headers = payload.get("headers")
        rows = payload.get("rows")
        if isinstance(headers, (list, tuple)) and isinstance(rows, (list, tuple)):
            width = len(headers)
            if any(not isinstance(row, (list, tuple)) or len(row) != width for row in rows):
                _issue(
                    issues,
                    "invalid_table_shape",
                    f"{path}.payload.rows",
                    "table rows must match the header width",
                )
    elif anchor.kind == "figure":
        _require_payload_fields(
            payload,
            {
                "asset_digest",
                "alt_text",
                "caption",
                "target",
                "media_type",
                "logical_name",
                "size",
            },
            path,
            issues,
        )
        digest = payload.get("asset_digest")
        if digest and (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest.casefold()) is None
        ):
            _issue(
                issues,
                "invalid_asset_identity",
                f"{path}.payload.asset_digest",
                "figure asset identity must be a SHA-256 digest",
            )
        target = payload.get("target")
        if target and (
            not isinstance(target, str) or not _safe_resource_target(target)
        ):
            _issue(
                issues,
                "invalid_asset_target",
                f"{path}.payload.target",
                "figure target must be a safe relative or HTTP(S) resource",
            )
        media_type = payload.get("media_type")
        logical_name = payload.get("logical_name")
        size = payload.get("size")
        if digest and (
            not isinstance(media_type, str)
            or "/" not in media_type
            or not isinstance(logical_name, str)
            or not logical_name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            _issue(
                issues,
                "invalid_asset_metadata",
                f"{path}.payload",
                "imported figure metadata is incomplete",
            )
        if not digest and (media_type or size):
            _issue(
                issues,
                "unfrozen_asset_metadata",
                f"{path}.payload",
                "unfrozen figures cannot claim imported asset metadata",
            )


def _require_payload_fields(
    payload: Mapping[str, object],
    fields: set[str],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if set(payload) != fields:
        _issue(
            issues,
            "invalid_source_payload",
            f"{path}.payload",
            "source payload fields do not match its block kind",
        )


def _validate_links(
    value: object, *, path: str, issues: list[ValidationIssue]
) -> None:
    if not isinstance(value, (list, tuple)):
        _issue(issues, "invalid_links", path, "links must be a list")
        return
    for index, link in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(link, Mapping) or set(link) != {"text", "target"}:
            _issue(
                issues,
                "invalid_link",
                item_path,
                "links must contain exactly text and target",
            )
            continue
        target = link.get("target")
        if not isinstance(target, str) or not _safe_link_target(target):
            _issue(
                issues,
                "unsafe_link",
                f"{item_path}.target",
                "link target uses an unsupported or unsafe scheme",
            )


def _validate_inline_spans(
    payload: Mapping[str, object],
    *,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    text = payload.get("text")
    spans = payload.get("inline_spans")
    if not isinstance(text, str) or not isinstance(spans, (list, tuple)):
        _issue(
            issues,
            "invalid_inline_spans",
            f"{path}.inline_spans",
            "inline spans require text and an ordered span list",
        )
        return
    cursor = 0
    reconstructed: list[str] = []
    derived_links: list[tuple[str, str]] = []
    derived_math: list[tuple[str, str]] = []
    for index, raw in enumerate(spans):
        item_path = f"{path}.inline_spans[{index}]"
        if not isinstance(raw, Mapping):
            _issue(issues, "invalid_inline_span", item_path, "inline span must be an object")
            continue
        kind = raw.get("kind")
        expected = {
            "text": {"kind", "start", "end", "text"},
            "link": {"kind", "start", "end", "text", "target"},
            "math": {"kind", "start", "end", "text", "tex", "source"},
        }.get(kind)
        value = raw.get("text")
        start = raw.get("start")
        end = raw.get("end")
        if (
            expected is None
            or set(raw) != expected
            or not isinstance(value, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start != cursor
            or end <= start
            or end - start != len(value)
        ):
            _issue(
                issues,
                "invalid_inline_span",
                item_path,
                "inline span fields or offsets are invalid",
            )
            continue
        reconstructed.append(value)
        cursor = end
        if kind == "link":
            derived_links.append((value, str(raw.get("target") or "")))
        elif kind == "math":
            derived_math.append(
                (str(raw.get("tex") or ""), str(raw.get("source") or ""))
            )
    if "".join(reconstructed) != text:
        _issue(
            issues,
            "inline_span_coverage",
            f"{path}.inline_spans",
            "inline spans must reconstruct source text exactly",
        )
    links = payload.get("links")
    if isinstance(links, (list, tuple)):
        expected_links = [
            (str(item.get("text") or ""), str(item.get("target") or ""))
            for item in links
            if isinstance(item, Mapping)
        ]
        if derived_links != expected_links:
            _issue(
                issues,
                "inline_link_mismatch",
                f"{path}.links",
                "link compatibility field differs from ordered spans",
            )
    inline_math = payload.get("inline_math")
    if isinstance(inline_math, (list, tuple)):
        expected_math = [
            (str(item.get("tex") or ""), str(item.get("source") or ""))
            for item in inline_math
            if isinstance(item, Mapping)
        ]
        if derived_math != expected_math:
            _issue(
                issues,
                "inline_math_mismatch",
                f"{path}.inline_math",
                "inline math compatibility field differs from ordered spans",
            )


def _validate_citations(
    citations: Sequence[str],
    *,
    allowed: set[str],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    for index, citation in enumerate(citations):
        if not citation.strip():
            _issue(
                issues,
                "empty_citation",
                f"{path}[{index}]",
                "citation identifiers must be non-empty",
            )
        elif citation not in allowed:
            _issue(
                issues,
                "unknown_evidence_citation",
                f"{path}[{index}]",
                "citation must resolve to a bibliography evidence ID",
            )


def _safe_link_target(target: str) -> bool:
    if target.startswith("#"):
        return True
    parsed = urlparse(target)
    return parsed.scheme.casefold() in {"http", "https", "mailto"} or (
        not parsed.scheme and not target.startswith("//")
    )


def _safe_resource_target(target: str) -> bool:
    parsed = urlparse(target)
    return parsed.scheme.casefold() in {"http", "https"} or (
        not parsed.scheme
        and not target.startswith(("/", "//"))
        and ".." not in target.split("/")
    )


def _issue(
    issues: list[ValidationIssue], code: str, path: str, message: str
) -> None:
    issues.append(ValidationIssue(code=code, path=path, message=message))


__all__ = [
    "AcceptedBookValidationError",
    "ValidationIssue",
    "require_valid_accepted_book",
    "validate_accepted_book",
]
