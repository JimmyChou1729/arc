"""Private, content-addressed cache for deterministic parsed documents.

The cache is deliberately derived from verified ``SourceRepository`` content.
It is not a paper-ID cache and does not own provider requests, workflow state,
or cache administration.  A damaged derived entry can therefore be discarded
and rebuilt; a damaged source remains a source-repository failure.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ._cache_root import resolve_cache_root
from ._durable_io import atomic_write_bytes, payload_matches
from ._file_lock import exclusive_file_lock
from .parse import (
    ParsedDocument,
    parsed_document_from_document,
    parsed_document_to_document,
)
from .source_repository import SourceRepository
from .sources import SourceArtifact


PARSER_CONTRACT = "arc.paper.parser.v1"
PARSED_DOCUMENT_CACHE_SCHEMA = "arc.paper.parsed_document_cache.v1"
DERIVED_CACHE_REBUILT_WARNING = (
    "parsed-document derived cache was corrupt and was rebuilt from verified source"
)

_MANIFEST_FIELDS = {
    "schema_version",
    "source_identity",
    "parser_contract",
    "payload_digest",
    "payload_size",
    "document_digest",
}
_SOURCE_IDENTITY_FIELDS = {
    "source_format",
    "media_type",
    "artifact_digest",
    "size",
}


class _DerivedCacheCorrupt(RuntimeError):
    """A derived entry cannot be trusted and must be rebuilt."""


class ParsedDocumentCache:
    """Read and rebuild parsed-document projections for verified sources.

    ``parser_contract`` is intentionally injected rather than inferred from a
    package version.  A parser-contract change creates a new key namespace and
    leaves previous derived entries harmlessly unused.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        repository: SourceRepository | None = None,
        parser_contract: str = PARSER_CONTRACT,
    ) -> None:
        if not isinstance(parser_contract, str) or not parser_contract.strip():
            raise ValueError("parser_contract is required")
        self.root = resolve_cache_root(root, repository=repository)
        self.repository = repository or SourceRepository(self.root)
        self.parser_contract = parser_contract.strip()

    def get_or_parse(
        self,
        source: SourceArtifact,
        parse_callable: Callable[[SourceArtifact], ParsedDocument],
    ) -> tuple[ParsedDocument, tuple[str, ...]]:
        """Return a verified document, rebuilding only invalid derived data.

        Source verification occurs before every cache read.  Consequently a
        stale or corrupt source never becomes masked by a valid-looking parsed
        projection and its existing typed repository error propagates unchanged.
        """

        if not isinstance(source, SourceArtifact):
            raise TypeError("source must be a SourceArtifact")
        if not callable(parse_callable):
            raise TypeError("parse_callable must be callable")

        self.repository.read_bytes(source)
        detected_corruption = False
        try:
            cached = self._read(source)
        except _DerivedCacheCorrupt:
            cached = None
            detected_corruption = True
        if cached is not None:
            return cached, ()

        key = self._key(source)
        with self._key_lock(key):
            # A concurrent writer may have supplied the complete entry while
            # this caller waited. Validate the source again before trusting it.
            self.repository.read_bytes(source)
            try:
                cached = self._read(source)
            except _DerivedCacheCorrupt:
                cached = None
                detected_corruption = True
            if cached is not None:
                warnings = (
                    (DERIVED_CACHE_REBUILT_WARNING,) if detected_corruption else ()
                )
                return cached, warnings

            document = parse_callable(source)
            self._validate_parsed_document(document, source)
            self._write(source, document)
            warnings = (
                (DERIVED_CACHE_REBUILT_WARNING,) if detected_corruption else ()
            )
            return document, warnings

    def _read(self, source: SourceArtifact) -> ParsedDocument | None:
        entry_dir = self._entry_dir(self._key(source))
        manifest_path = entry_dir / "manifest.json"
        payload_path = entry_dir / "document.json"
        manifest_exists = manifest_path.exists()
        payload_exists = payload_path.exists()
        if not manifest_exists and not payload_exists:
            return None
        if not manifest_exists or not payload_exists:
            raise _DerivedCacheCorrupt("parsed-document cache entry is incomplete")

        manifest = self._read_manifest(manifest_path)
        source_identity = _source_identity(source)
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
            raise _DerivedCacheCorrupt("parsed-document cache manifest has invalid fields")
        if (
            manifest.get("schema_version") != PARSED_DOCUMENT_CACHE_SCHEMA
            or manifest.get("parser_contract") != self.parser_contract
            or manifest.get("source_identity") != source_identity
        ):
            raise _DerivedCacheCorrupt("parsed-document cache manifest identity mismatch")

        payload_digest = manifest.get("payload_digest")
        payload_size = manifest.get("payload_size")
        document_digest = manifest.get("document_digest")
        if (
            not _is_sha256(payload_digest)
            or not isinstance(payload_size, int)
            or isinstance(payload_size, bool)
            or payload_size < 0
            or not _is_sha256(document_digest)
        ):
            raise _DerivedCacheCorrupt("parsed-document cache manifest metadata invalid")
        if not payload_matches(payload_path, payload_digest, payload_size):
            raise _DerivedCacheCorrupt("parsed-document cache payload digest mismatch")

        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _DerivedCacheCorrupt(
                "parsed-document cache payload is unreadable or malformed"
            ) from exc
        if not isinstance(payload, Mapping):
            raise _DerivedCacheCorrupt("parsed-document cache payload must be an object")
        try:
            document = parsed_document_from_document(payload)
        except (TypeError, ValueError) as exc:
            raise _DerivedCacheCorrupt(
                "parsed-document cache payload failed document validation"
            ) from exc
        if document.source.content_identity != source.content_identity:
            raise _DerivedCacheCorrupt(
                "parsed-document cache document source identity mismatch"
            )
        if document.document_digest != document_digest:
            raise _DerivedCacheCorrupt(
                "parsed-document cache document digest mismatch"
            )
        return document

    def _write(self, source: SourceArtifact, document: ParsedDocument) -> None:
        payload = _canonical_json_bytes(parsed_document_to_document(document))
        payload_digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "schema_version": PARSED_DOCUMENT_CACHE_SCHEMA,
            "source_identity": _source_identity(source),
            "parser_contract": self.parser_contract,
            "payload_digest": payload_digest,
            "payload_size": len(payload),
            "document_digest": document.document_digest,
        }
        entry_dir = self._entry_dir(self._key(source))
        atomic_write_bytes(entry_dir / "document.json", payload)
        atomic_write_bytes(entry_dir / "manifest.json", _canonical_json_bytes(manifest))

    def _validate_parsed_document(
        self, document: ParsedDocument, source: SourceArtifact
    ) -> None:
        if not isinstance(document, ParsedDocument):
            raise TypeError("parse_callable must return a ParsedDocument")
        if document.source.content_identity != source.content_identity:
            raise ValueError("parsed document source does not match requested source")

    def _key(self, source: SourceArtifact) -> str:
        return hashlib.sha256(
            _canonical_json_bytes(
                {
                    "source_identity": _source_identity(source),
                    "parser_contract": self.parser_contract,
                }
            )
        ).hexdigest()

    def _entry_dir(self, key: str) -> Path:
        return (
            self.root
            / "parsed-document-cache"
            / "v1"
            / "sha256"
            / key[:2]
            / key
        )

    @contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        path = (
            self.root
            / "parsed-document-cache"
            / "v1"
            / "locks"
            / f"{key}.lock"
        )
        with exclusive_file_lock(path):
            yield

    @staticmethod
    def _read_manifest(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _DerivedCacheCorrupt(
                "parsed-document cache manifest is unreadable or malformed"
            ) from exc


def _source_identity(source: SourceArtifact) -> dict[str, Any]:
    return {
        "source_format": source.source_format.value,
        "media_type": source.media_type,
        "artifact_digest": source.artifact_digest,
        "size": source.size,
    }


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
    "DERIVED_CACHE_REBUILT_WARNING",
    "PARSER_CONTRACT",
    "PARSED_DOCUMENT_CACHE_SCHEMA",
    "ParsedDocumentCache",
]
