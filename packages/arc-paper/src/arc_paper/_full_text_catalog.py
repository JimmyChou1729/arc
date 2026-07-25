"""Logical locators for the current parsed full-text cache.

The catalog contains no source bytes, parsed text, or physical cache paths.
Each representation points to immutable content identities that must still be
verified by :class:`ParsedDocumentCache` before use.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterator

from ._cache_root import resolve_cache_root
from ._durable_io import atomic_write_bytes
from ._file_lock import exclusive_file_lock
from .ids import arxiv_path_id
from .sources import SourceArtifact

if TYPE_CHECKING:
    from .parse.models import ParsedDocument


FULL_TEXT_CATALOG_SCHEMA = "arc.paper.full_text_catalog.v1"
_ENTRY_FIELDS = {
    "schema_version",
    "kind",
    "paper_ids",
    "local_source_identity",
    "representations",
}
_REPRESENTATION_FIELDS = {
    "source_identity",
    "parser_contract",
    "parsed_cache_key",
    "document_digest",
}
_SOURCE_IDENTITY_FIELDS = {
    "source_format",
    "media_type",
    "artifact_digest",
    "size",
}


@dataclass(frozen=True)
class FullTextRepresentation:
    """One current format-specific projection selected by a locator."""

    source_identity: Mapping[str, Any]
    parser_contract: str
    parsed_cache_key: str
    document_digest: str

    def __post_init__(self) -> None:
        identity = _validated_source_identity(self.source_identity)
        parser_contract = str(self.parser_contract).strip()
        if not parser_contract:
            raise ValueError("parser_contract is required")
        if not _is_sha256(self.parsed_cache_key):
            raise ValueError("parsed_cache_key must be a SHA-256 digest")
        if not _is_sha256(self.document_digest):
            raise ValueError("document_digest must be a SHA-256 digest")
        object.__setattr__(self, "source_identity", MappingProxyType(identity))
        object.__setattr__(self, "parser_contract", parser_contract)

    @property
    def source_format(self) -> str:
        return str(self.source_identity["source_format"])


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
                not item.startswith("arXiv:")
                or f"arXiv:{arxiv_path_id(item)}" != item
                for item in paper_ids
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


class FullTextCatalog:
    """Atomic per-entry locator index for materialized full text."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = resolve_cache_root(root)

    def record(
        self,
        source: SourceArtifact,
        document: ParsedDocument,
        *,
        parser_contract: str,
        parsed_cache_key: str,
    ) -> FullTextCatalogEntry:
        if document.source.content_identity != source.content_identity:
            raise ValueError("catalog document source does not match source")
        representation = FullTextRepresentation(
            source_identity=_source_identity(source),
            parser_contract=parser_contract,
            parsed_cache_key=parsed_cache_key,
            document_digest=document.document_digest,
        )
        locator_kind, locator_identity, paper_id = _locator_identity(source)
        locator_key = _locator_key(locator_kind, locator_identity)
        path = self._entry_path(locator_key)
        with self._entry_lock(locator_key):
            previous = self._read_path(path)
            if previous is not None and not _entry_matches_locator(
                previous, locator_kind, locator_identity
            ):
                previous = None
            by_format = (
                {item.source_format: item for item in previous.representations}
                if previous is not None
                else {}
            )
            by_format[representation.source_format] = representation
            if locator_kind == "arxiv":
                paper_ids = set(previous.paper_ids if previous is not None else ())
                paper_ids.add(paper_id)
                entry = FullTextCatalogEntry(
                    kind="arxiv",
                    paper_ids=tuple(paper_ids),
                    local_source_identity=None,
                    representations=tuple(by_format.values()),
                )
            else:
                entry = FullTextCatalogEntry(
                    kind="local",
                    paper_ids=(),
                    local_source_identity=locator_identity,
                    representations=tuple(by_format.values()),
                )
            atomic_write_bytes(path, _canonical_json_bytes(_entry_document(entry)))
            return entry

    def current_entries(self) -> tuple[FullTextCatalogEntry, ...]:
        """Return only strict, current v1 locators; damaged entries are ignored."""

        entries_root = self.root / "full-text-catalog" / "v1" / "entries"
        if not entries_root.is_dir():
            return ()
        entries = tuple(
            entry
            for path in entries_root.glob("*/*/locator.json")
            if (entry := self._read_path(path)) is not None
        )
        return tuple(sorted(entries, key=_entry_sort_key))

    def _read_path(self, path: Path) -> FullTextCatalogEntry | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            entry = _entry_from_document(value)
            if path.parent.name not in _locator_keys(entry):
                return None
            return entry
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def _entry_path(self, locator_key: str) -> Path:
        return (
            self.root
            / "full-text-catalog"
            / "v1"
            / "entries"
            / locator_key[:2]
            / locator_key
            / "locator.json"
        )

    @contextmanager
    def _entry_lock(self, locator_key: str) -> Iterator[None]:
        path = (
            self.root
            / "full-text-catalog"
            / "v1"
            / "locks"
            / f"{locator_key}.lock"
        )
        with exclusive_file_lock(path):
            yield


def _locator_identity(
    source: SourceArtifact,
) -> tuple[str, str | dict[str, Any], str]:
    arxiv_id = arxiv_path_id(str(source.origin.metadata.get("arxiv_id") or ""))
    if arxiv_id:
        canonical = f"arXiv:{arxiv_id}"
        return "arxiv", canonical, canonical
    identity = _source_identity(source)
    return "local", identity, ""


def _locator_key(kind: str, identity: str | Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes({"kind": kind, "identity": identity})
    ).hexdigest()


def _entry_matches_locator(
    entry: FullTextCatalogEntry,
    kind: str,
    identity: str | Mapping[str, Any],
) -> bool:
    if entry.kind != kind:
        return False
    if kind == "arxiv":
        return identity in entry.paper_ids
    return dict(entry.local_source_identity or {}) == dict(identity)


def _locator_keys(entry: FullTextCatalogEntry) -> set[str]:
    if entry.kind == "arxiv":
        return {
            _locator_key("arxiv", paper_id)
            for paper_id in entry.paper_ids
        }
    return {
        _locator_key("local", dict(entry.local_source_identity or {}))
    }


def _entry_document(entry: FullTextCatalogEntry) -> dict[str, Any]:
    return {
        "schema_version": FULL_TEXT_CATALOG_SCHEMA,
        "kind": entry.kind,
        "paper_ids": list(entry.paper_ids),
        "local_source_identity": (
            dict(entry.local_source_identity)
            if entry.local_source_identity is not None
            else None
        ),
        "representations": [
            {
                "source_identity": dict(item.source_identity),
                "parser_contract": item.parser_contract,
                "parsed_cache_key": item.parsed_cache_key,
                "document_digest": item.document_digest,
            }
            for item in entry.representations
        ],
    }


def _entry_from_document(value: object) -> FullTextCatalogEntry:
    if not isinstance(value, Mapping) or set(value) != _ENTRY_FIELDS:
        raise ValueError("catalog locator has invalid fields")
    if value.get("schema_version") != FULL_TEXT_CATALOG_SCHEMA:
        raise ValueError("catalog locator has unsupported schema")
    paper_ids = value.get("paper_ids")
    representations = value.get("representations")
    if not isinstance(paper_ids, list) or not all(
        isinstance(item, str) for item in paper_ids
    ):
        raise ValueError("catalog paper_ids must be strings")
    if not isinstance(representations, list):
        raise ValueError("catalog representations must be a list")
    decoded: list[FullTextRepresentation] = []
    for item in representations:
        if not isinstance(item, Mapping) or set(item) != _REPRESENTATION_FIELDS:
            raise ValueError("catalog representation has invalid fields")
        decoded.append(
            FullTextRepresentation(
                source_identity=item["source_identity"],
                parser_contract=item["parser_contract"],
                parsed_cache_key=item["parsed_cache_key"],
                document_digest=item["document_digest"],
            )
        )
    local_identity = value.get("local_source_identity")
    if local_identity is not None and not isinstance(local_identity, Mapping):
        raise ValueError("catalog local identity must be an object or null")
    return FullTextCatalogEntry(
        kind=value.get("kind"),
        paper_ids=tuple(paper_ids),
        local_source_identity=local_identity,
        representations=tuple(decoded),
    )


def _source_identity(source: SourceArtifact) -> dict[str, Any]:
    return {
        "source_format": source.source_format.value,
        "media_type": source.media_type,
        "artifact_digest": source.artifact_digest,
        "size": source.size,
    }


def _validated_source_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_IDENTITY_FIELDS:
        raise ValueError("source identity has invalid fields")
    source_format = value.get("source_format")
    media_type = value.get("media_type")
    artifact_digest = value.get("artifact_digest")
    size = value.get("size")
    if (
        source_format not in {"html", "markdown", "tex", "pdf"}
        or not isinstance(media_type, str)
        or not media_type
        or "/" not in media_type
        or ";" in media_type
        or not _is_sha256(artifact_digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise ValueError("source identity is invalid")
    return {
        "source_format": source_format,
        "media_type": media_type,
        "artifact_digest": artifact_digest,
        "size": size,
    }


def _entry_sort_key(entry: FullTextCatalogEntry) -> tuple[str, str]:
    if entry.kind == "arxiv":
        return entry.kind, entry.paper_ids[0].casefold()
    return entry.kind, str(
        (entry.local_source_identity or {}).get("artifact_digest", "")
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "FULL_TEXT_CATALOG_SCHEMA",
    "FullTextCatalog",
    "FullTextCatalogEntry",
    "FullTextRepresentation",
]
