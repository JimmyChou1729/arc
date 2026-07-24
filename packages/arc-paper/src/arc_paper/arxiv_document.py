"""Public, path-free results for cache-first arXiv document operations."""

from __future__ import annotations

from dataclasses import dataclass

from .document_search import (
    EquationMatch,
    FullTextMatch,
    TableOfContentsEntry,
)


@dataclass(frozen=True)
class ArxivDocumentProvenance:
    canonical_arxiv_id: str
    provider: str
    source_format: str
    source_digest: str
    document_digest: str


@dataclass(frozen=True)
class ArxivTableOfContents:
    provenance: ArxivDocumentProvenance
    entries: tuple[TableOfContentsEntry, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArxivSection:
    provenance: ArxivDocumentProvenance
    section_id: str
    title: str
    text: str
    level: int
    ordinal: int
    page_start: int | None
    page_end: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArxivFullTextSearch:
    provenance: ArxivDocumentProvenance
    query: str
    matches: tuple[FullTextMatch, ...]
    limit: int
    context_lines: int
    case_sensitive: bool
    truncated: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArxivEquationSearch:
    provenance: ArxivDocumentProvenance
    query: str
    matches: tuple[EquationMatch, ...]
    limit: int
    case_sensitive: bool
    truncated: bool
    warnings: tuple[str, ...] = ()


__all__ = [
    "ArxivDocumentProvenance",
    "ArxivEquationSearch",
    "ArxivFullTextSearch",
    "ArxivSection",
    "ArxivTableOfContents",
]
