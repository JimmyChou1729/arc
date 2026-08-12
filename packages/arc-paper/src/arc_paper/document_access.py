"""Provider-neutral document targets and read results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .cached_document import CachedDocumentRef
from .document_search import TableOfContentsEntry
from .reference_cache import ReferenceIdentity


class DocumentTargetKind(str, Enum):
    REFERENCE = "reference"
    DOCUMENT = "document"


@dataclass(frozen=True)
class DocumentTarget:
    """One exact reference identity or immutable cached document handle."""

    kind: DocumentTargetKind | str
    reference: str = ""
    document: CachedDocumentRef | None = None

    def __post_init__(self) -> None:
        kind = DocumentTargetKind(self.kind)
        reference = str(self.reference or "").strip()
        if kind is DocumentTargetKind.REFERENCE:
            if not reference or self.document is not None:
                raise ValueError("reference target requires only a reference")
        elif not isinstance(self.document, CachedDocumentRef) or reference:
            raise ValueError("document target requires only a CachedDocumentRef")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "reference", reference)


@dataclass(frozen=True)
class ResolvedDocumentInfo:
    document: CachedDocumentRef
    identity: ReferenceIdentity | None
    requested_reference: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperTableOfContents:
    source: ResolvedDocumentInfo
    entries: tuple[TableOfContentsEntry, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperSection:
    source: ResolvedDocumentInfo
    section_id: str
    title: str
    text: str
    level: int
    ordinal: int
    page_start: int | None
    page_end: int | None
    warnings: tuple[str, ...] = ()


__all__ = [
    "DocumentTarget",
    "DocumentTargetKind",
    "PaperSection",
    "PaperTableOfContents",
    "ResolvedDocumentInfo",
]
