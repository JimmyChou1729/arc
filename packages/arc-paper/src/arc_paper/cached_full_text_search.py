"""Paper-named facade over :mod:`arc_document` cached full-text search."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from arc_document import (
    CachedFullTextDocument as _DocumentFullTextDocument,
    CachedFullTextOccurrence as _DocumentFullTextOccurrence,
    CachedFullTextSearchError as _DocumentFullTextSearchError,
    CachedFullTextSearcher as _DocumentFullTextSearcher,
    CandidateSelector,
    FullTextCatalog,
    search_document_occurrences as _search_document_occurrences,
)

from ._cache_root import resolve_cache_root
from .cached_document import CachedDocumentRef
from .parse.models import ParsedDocument


class CachedFullTextSearchMode(str, Enum):
    OCCURRENCES = "occurrences"
    REFINEMENT_REQUIRED = "refinement_required"


class CachedFullTextContextStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    INCLUDED = "included"
    OMITTED_TOO_BROAD = "omitted_too_broad"
    OMITTED_REFINEMENT_REQUIRED = "omitted_refinement_required"


class CachedFullTextLocation(str, Enum):
    SECTION = "section"
    PAGE = "page"


class CachedFullTextSearchError(ValueError):
    """Typed invalid cached-search request."""

    code = "invalid_search_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CachedFullTextOccurrence:
    source_kind: str
    arxiv_ids: tuple[str, ...]
    source_format: str
    source_digest: str
    document_digest: str
    location: CachedFullTextLocation
    location_id: str
    title: str
    page_number: int | None
    line: int
    column: int
    matched_terms: tuple[str, ...]
    context: str = ""


@dataclass(frozen=True)
class CachedFullTextSearchResult:
    mode: CachedFullTextSearchMode
    terms: tuple[str, ...]
    limit: int
    context_lines: int
    case_sensitive: bool
    total_occurrences: int
    matched_document_count: int
    occurrences: tuple[CachedFullTextOccurrence, ...]
    top_paper_titles: tuple[str, ...]
    context_status: CachedFullTextContextStatus
    message: str
    warnings: tuple[str, ...] = ()
    documents: tuple["CachedFullTextDocument", ...] = ()


@dataclass(frozen=True)
class CachedFullTextDocument:
    source_kind: str
    arxiv_ids: tuple[str, ...]
    document: CachedDocumentRef


class CachedFullTextSearcher:
    """Search paper catalog locators through the document-owned engine."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        candidate_selector: CandidateSelector | None = None,
    ) -> None:
        self.root = resolve_cache_root(root)
        self.catalog = FullTextCatalog(self.root)
        self._searcher = _DocumentFullTextSearcher(
            self.root,
            candidate_selector=candidate_selector,
        )
        self.candidate_selector = self._searcher.candidate_selector

    def search(
        self,
        terms: Sequence[str],
        *,
        limit: int = 100,
        context_lines: int = 0,
        case_sensitive: bool = False,
    ) -> CachedFullTextSearchResult:
        try:
            result = self._searcher.search(
                terms,
                limit=limit,
                context_lines=context_lines,
                case_sensitive=case_sensitive,
            )
        except _DocumentFullTextSearchError as exc:
            raise CachedFullTextSearchError(exc.message) from exc
        return CachedFullTextSearchResult(
            mode=CachedFullTextSearchMode(result.mode.value),
            terms=result.terms,
            limit=result.limit,
            context_lines=result.context_lines,
            case_sensitive=result.case_sensitive,
            total_occurrences=result.total_occurrences,
            matched_document_count=result.matched_document_count,
            occurrences=tuple(_paper_occurrence(item) for item in result.occurrences),
            top_paper_titles=result.top_document_titles,
            context_status=CachedFullTextContextStatus(result.context_status.value),
            message=result.message,
            warnings=result.warnings,
            documents=tuple(_paper_document(item) for item in result.documents),
        )


def search_document_occurrences(
    document: ParsedDocument,
    terms: Sequence[str],
    *,
    context_lines: int = 0,
    case_sensitive: bool = False,
) -> tuple[CachedFullTextOccurrence, ...]:
    try:
        occurrences = _search_document_occurrences(
            document,
            terms,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )
    except _DocumentFullTextSearchError as exc:
        raise CachedFullTextSearchError(exc.message) from exc
    return tuple(_paper_occurrence(item) for item in occurrences)


def _paper_occurrence(
    occurrence: _DocumentFullTextOccurrence,
) -> CachedFullTextOccurrence:
    return CachedFullTextOccurrence(
        source_kind=_paper_source_kind(occurrence.source_kind),
        arxiv_ids=_arxiv_ids(occurrence.document_ids),
        source_format=occurrence.source_format,
        source_digest=occurrence.source_digest,
        document_digest=occurrence.document_digest,
        location=CachedFullTextLocation(occurrence.location.value),
        location_id=occurrence.location_id,
        title=occurrence.title,
        page_number=occurrence.page_number,
        line=occurrence.line,
        column=occurrence.column,
        matched_terms=occurrence.matched_terms,
        context=occurrence.context,
    )


def _paper_document(
    document: _DocumentFullTextDocument,
) -> CachedFullTextDocument:
    return CachedFullTextDocument(
        source_kind=_paper_source_kind(document.source_kind),
        arxiv_ids=_arxiv_ids(document.document_ids),
        document=document.document,
    )


def _paper_source_kind(value: str) -> str:
    return "arxiv" if value == "identified" else value


def _arxiv_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for value in values if value.startswith("arXiv:"))


__all__ = [
    "CachedFullTextContextStatus",
    "CachedFullTextDocument",
    "CachedFullTextLocation",
    "CachedFullTextOccurrence",
    "CachedFullTextSearchError",
    "CachedFullTextSearchMode",
    "CachedFullTextSearchResult",
    "CachedFullTextSearcher",
    "CandidateSelector",
    "search_document_occurrences",
]
