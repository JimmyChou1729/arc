"""Paper-shaped compatibility view of the document-owned full-text catalog."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from arc_document import (
    FULL_TEXT_CATALOG_ADMIN_SCHEMA,
    FULL_TEXT_CATALOG_SCHEMA,
    FullTextCatalog as _DocumentFullTextCatalog,
    FullTextCatalogAdminEntry as _DocumentFullTextCatalogAdminEntry,
    FullTextCatalogEntry as _DocumentFullTextCatalogEntry,
    FullTextRepresentation,
    SourceArtifact,
    SourceOrigin,
)

from ._cache_root import resolve_cache_root
from .ids import arxiv_path_id

if TYPE_CHECKING:
    from .parse.models import ParsedDocument


@dataclass(frozen=True)
class FullTextCatalogEntry:
    """Backward-compatible paper naming for one document catalog entry."""

    kind: str
    paper_ids: tuple[str, ...]
    local_source_identity: Mapping[str, Any] | None
    representations: tuple[FullTextRepresentation, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"arxiv", "local"}:
            raise ValueError("catalog entry kind must be arxiv or local")
        paper_ids = tuple(sorted(set(self.paper_ids), key=str.casefold))
        representations = tuple(
            sorted(self.representations, key=lambda item: item.source_format)
        )
        if not representations:
            raise ValueError("catalog entry requires a representation")
        if len({item.source_format for item in representations}) != len(
            representations
        ):
            raise ValueError("catalog entry contains duplicate source formats")
        if self.kind == "arxiv":
            if not paper_ids or any(not _canonical_arxiv(item) for item in paper_ids):
                raise ValueError("arxiv catalog entry requires canonical paper IDs")
            if self.local_source_identity is not None:
                raise ValueError("arxiv catalog entry cannot have a local identity")
            local_identity = None
        else:
            if paper_ids or self.local_source_identity is None:
                raise ValueError("local catalog entry requires only a source identity")
            local_identity = MappingProxyType(dict(self.local_source_identity))
        object.__setattr__(self, "paper_ids", paper_ids)
        object.__setattr__(self, "local_source_identity", local_identity)
        object.__setattr__(self, "representations", representations)


@dataclass(frozen=True)
class FullTextCatalogAdminEntry:
    entry_id: str
    entry: FullTextCatalogEntry
    cached_at: str


class FullTextCatalog:
    """Preserve paper result names without owning a second durable catalog."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = resolve_cache_root(root)
        self._catalog = _DocumentFullTextCatalog(self.root)

    def record(
        self,
        source: SourceArtifact,
        document: ParsedDocument,
        *,
        parser_contract: str,
        parsed_cache_key: str,
    ) -> FullTextCatalogEntry:
        return _paper_entry(
            self._catalog.record(
                _paper_identified_source(source),
                document,
                parser_contract=parser_contract,
                parsed_cache_key=parsed_cache_key,
            )
        )

    def current_entries(self) -> tuple[FullTextCatalogEntry, ...]:
        return tuple(_paper_entry(item) for item in self._catalog.current_entries())

    def admin_entries(self) -> tuple[FullTextCatalogAdminEntry, ...]:
        return tuple(_paper_admin_entry(item) for item in self._catalog.admin_entries())

    def remove_admin_entry(self, entry_id: str) -> bool:
        return self._catalog.remove_admin_entry(entry_id)


def _paper_identified_source(source: SourceArtifact) -> SourceArtifact:
    metadata = dict(source.origin.metadata)
    if metadata.get("document_id"):
        return source
    arxiv_id = arxiv_path_id(str(metadata.get("arxiv_id") or ""))
    if not arxiv_id:
        return source
    metadata["document_id"] = f"arXiv:{arxiv_id}"
    return replace(source, origin=replace(source.origin, metadata=metadata))


def _paper_entry(entry: _DocumentFullTextCatalogEntry) -> FullTextCatalogEntry:
    return FullTextCatalogEntry(
        kind="arxiv" if entry.kind == "identified" else "local",
        paper_ids=entry.document_ids,
        local_source_identity=entry.local_source_identity,
        representations=entry.representations,
    )


def _paper_admin_entry(
    item: _DocumentFullTextCatalogAdminEntry,
) -> FullTextCatalogAdminEntry:
    return FullTextCatalogAdminEntry(
        entry_id=item.entry_id,
        entry=_paper_entry(item.entry),
        cached_at=item.cached_at,
    )


def _canonical_arxiv(value: str) -> bool:
    normalized = arxiv_path_id(value)
    return bool(normalized) and value == f"arXiv:{normalized}"


__all__ = [
    "FULL_TEXT_CATALOG_ADMIN_SCHEMA",
    "FULL_TEXT_CATALOG_SCHEMA",
    "FullTextCatalog",
    "FullTextCatalogAdminEntry",
    "FullTextCatalogEntry",
    "FullTextRepresentation",
]
