"""Provider-neutral content-search result contracts."""

from __future__ import annotations

from dataclasses import dataclass

from .cached_full_text_search import (
    CachedFullTextContextStatus,
    CachedFullTextOccurrence,
    CachedFullTextSearchMode,
)
from .document_access import DocumentTarget, ResolvedDocumentInfo
from .document_search import EquationMatch


@dataclass(frozen=True)
class DocumentTargetFailure:
    target_index: int
    target: DocumentTarget
    code: str
    message: str


@dataclass(frozen=True)
class ResolvedSearchDocument:
    source: ResolvedDocumentInfo
    target_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class PaperFullTextSearch:
    scope: str
    mode: CachedFullTextSearchMode
    terms: tuple[str, ...]
    limit: int
    context_lines: int
    case_sensitive: bool
    total_occurrences: int
    matched_document_count: int
    documents: tuple[ResolvedSearchDocument, ...]
    failures: tuple[DocumentTargetFailure, ...]
    occurrences: tuple[CachedFullTextOccurrence, ...]
    top_paper_titles: tuple[str, ...]
    context_status: CachedFullTextContextStatus
    message: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperEquationSearch:
    terms: tuple[str, ...]
    limit: int
    context_lines: int
    case_sensitive: bool
    searched_document_count: int
    documents: tuple[ResolvedSearchDocument, ...]
    failures: tuple[DocumentTargetFailure, ...]
    matches: tuple[EquationMatch, ...]
    truncated: bool
    warnings: tuple[str, ...] = ()


__all__ = [
    "DocumentTargetFailure",
    "PaperEquationSearch",
    "PaperFullTextSearch",
    "ResolvedSearchDocument",
]
