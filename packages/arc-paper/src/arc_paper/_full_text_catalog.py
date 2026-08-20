"""Paper dialect over :mod:`arc_document` full-text catalog storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from arc_document._full_text_catalog import (
    FullTextCatalog as _DocumentFullTextCatalog,
    FullTextCatalogDialect,
    FullTextCatalogEntry as _DocumentFullTextCatalogEntry,
    FullTextRepresentation,
    _validated_source_identity,
)

from ._cache_root import resolve_cache_root
from .ids import arxiv_path_id
from .sources import SourceArtifact

if TYPE_CHECKING:
    from .parse.models import ParsedDocument


FULL_TEXT_CATALOG_SCHEMA = "arc.paper.full_text_catalog.v2"
FULL_TEXT_CATALOG_ADMIN_SCHEMA = "arc.paper.full_text_catalog_admin.v2"


def _paper_identifier(source: SourceArtifact) -> str:
    arxiv_id = arxiv_path_id(str(source.origin.metadata.get("arxiv_id") or ""))
    return f"arXiv:{arxiv_id}" if arxiv_id else ""


def _valid_paper_identifier(value: str) -> bool:
    return (
        value.startswith("arXiv:")
        and bool(arxiv_path_id(value))
        and f"arXiv:{arxiv_path_id(value)}" == value
    )


_PAPER_FULL_TEXT_CATALOG_DIALECT = FullTextCatalogDialect(
    schema_version=FULL_TEXT_CATALOG_SCHEMA,
    admin_schema_version=FULL_TEXT_CATALOG_ADMIN_SCHEMA,
    directory="full-text-catalog",
    identified_kind="arxiv",
    identifier_field="paper_ids",
    admin_prefix="paper",
    identify_source=_paper_identifier,
    validate_identifier=_valid_paper_identifier,
)


@dataclass(frozen=True)
class FullTextCatalogEntry:
    """One current arXiv or local full-text locator."""

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
        formats = [item.source_format for item in representations]
        if len(formats) != len(set(formats)):
            raise ValueError("catalog entry contains duplicate source formats")
        if not representations:
            raise ValueError("catalog entry requires a representation")
        if self.kind == "arxiv":
            if not paper_ids or any(
                not _valid_paper_identifier(item) for item in paper_ids
            ):
                raise ValueError("arxiv catalog entry requires canonical paper IDs")
            if self.local_source_identity is not None:
                raise ValueError("arxiv catalog entry cannot have a local identity")
            local_identity = None
        else:
            if paper_ids:
                raise ValueError("local catalog entry cannot have paper IDs")
            if self.local_source_identity is None:
                raise ValueError("local catalog entry requires a source identity")
            local_identity = MappingProxyType(
                _validated_source_identity(self.local_source_identity)
            )
            if any(
                dict(item.source_identity) != dict(local_identity)
                for item in representations
            ):
                raise ValueError("local representation must match locator identity")
        object.__setattr__(self, "paper_ids", paper_ids)
        object.__setattr__(self, "local_source_identity", local_identity)
        object.__setattr__(self, "representations", representations)


@dataclass(frozen=True)
class FullTextCatalogAdminEntry:
    entry_id: str
    entry: FullTextCatalogEntry
    cached_at: str


class FullTextCatalog:
    """Paper-named facade over the provider-neutral catalog engine."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = resolve_cache_root(root)
        self._catalog = _DocumentFullTextCatalog(
            self.root,
            _dialect=_PAPER_FULL_TEXT_CATALOG_DIALECT,
        )

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
                source,
                document,
                parser_contract=parser_contract,
                parsed_cache_key=parsed_cache_key,
            )
        )

    def current_entries(self) -> tuple[FullTextCatalogEntry, ...]:
        return tuple(_paper_entry(item) for item in self._catalog.current_entries())

    def admin_entries(self) -> tuple[FullTextCatalogAdminEntry, ...]:
        return tuple(
            FullTextCatalogAdminEntry(
                entry_id=item.entry_id,
                entry=_paper_entry(item.entry),
                cached_at=item.cached_at,
            )
            for item in self._catalog.admin_entries()
        )

    def remove_admin_entry(self, entry_id: str) -> bool:
        return self._catalog.remove_admin_entry(entry_id)


def _paper_entry(entry: _DocumentFullTextCatalogEntry) -> FullTextCatalogEntry:
    return FullTextCatalogEntry(
        kind="arxiv" if entry.kind == "identified" else "local",
        paper_ids=entry.document_ids,
        local_source_identity=entry.local_source_identity,
        representations=entry.representations,
    )


__all__ = [
    "FULL_TEXT_CATALOG_ADMIN_SCHEMA",
    "FULL_TEXT_CATALOG_SCHEMA",
    "FullTextCatalog",
    "FullTextCatalogAdminEntry",
    "FullTextCatalogEntry",
    "FullTextRepresentation",
]
