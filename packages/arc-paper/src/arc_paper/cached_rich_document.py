"""Verified logical references and durable storage for rich documents.

The public reference contains only semantic identities.  Physical cache paths
remain an implementation detail of :class:`CachedRichDocumentStore`, and asset
bytes remain in :class:`~arc_paper.source_repository.SourceRepository`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ._durable_io import atomic_write_bytes
from ._file_lock import exclusive_file_lock
from .cached_document import (
    CachedDocumentError,
    CachedDocumentRef,
    cached_document_ref_from_document,
    cached_document_ref_to_document,
)
from .rich_document import RichAsset, RichBlockKind, RichDocument
from .rich_document.models import (
    rich_document_from_document,
    rich_document_to_document,
)


CACHED_RICH_DOCUMENT_REF_SCHEMA = "arc.paper.cached_rich_document_ref.v1"
RICH_DOCUMENT_PARSER_CONTRACT = "arc.paper.rich_document_parser.v1"
RICH_ASSET_MANIFEST_SCHEMA = "arc.paper.rich_asset_manifest.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REF_FIELDS = {
    "primary",
    "validators",
    "rich_parser_contract",
    "rich_document_sha256",
    "asset_manifest_sha256",
}
_MANIFEST_FIELDS = {"schema_version", "assets", "targets"}
_ASSET_FIELDS = {"artifact_digest", "media_type", "logical_name", "size"}
_TARGET_FIELDS = {"target", "artifact_digest"}


class CachedRichDocumentError(CachedDocumentError):
    """A stable failure while caching or opening a rich document."""


@dataclass(frozen=True)
class CachedRichDocumentRef:
    """Logical identity for one reproducible, repository-backed RichDocument."""

    primary: CachedDocumentRef
    validators: tuple[CachedDocumentRef, ...]
    rich_parser_contract: str
    rich_document_sha256: str
    asset_manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.primary, CachedDocumentRef):
            raise TypeError("primary must be a CachedDocumentRef")
        validators = tuple(self.validators)
        if any(not isinstance(item, CachedDocumentRef) for item in validators):
            raise TypeError("validators must contain CachedDocumentRef values")
        if len(set(validators)) != len(validators):
            raise ValueError("validators must be unique")
        if self.primary in validators:
            raise ValueError("primary cannot also be a validator")
        if not isinstance(self.rich_parser_contract, str):
            raise TypeError("rich_parser_contract must be a string")
        if not isinstance(self.rich_document_sha256, str):
            raise TypeError("rich_document_sha256 must be a string")
        if not isinstance(self.asset_manifest_sha256, str):
            raise TypeError("asset_manifest_sha256 must be a string")
        contract = self.rich_parser_contract.strip()
        rich_digest = self.rich_document_sha256.casefold()
        manifest_digest = self.asset_manifest_sha256.casefold()
        if not contract:
            raise ValueError("rich_parser_contract is required")
        if _SHA256_RE.fullmatch(rich_digest) is None:
            raise ValueError("rich_document_sha256 must be a SHA-256 digest")
        if _SHA256_RE.fullmatch(manifest_digest) is None:
            raise ValueError("asset_manifest_sha256 must be a SHA-256 digest")
        object.__setattr__(self, "validators", validators)
        object.__setattr__(self, "rich_parser_contract", contract)
        object.__setattr__(self, "rich_document_sha256", rich_digest)
        object.__setattr__(self, "asset_manifest_sha256", manifest_digest)


@dataclass(frozen=True)
class RichAssetManifest:
    """Asset metadata and source-target bindings scoped to one RichDocument."""

    assets: tuple[RichAsset, ...]
    targets: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        assets = tuple(self.assets)
        targets = tuple(
            (str(target), str(digest).casefold())
            for target, digest in self.targets
        )
        if len({item.artifact_digest for item in assets}) != len(assets):
            raise ValueError("rich asset manifest contains duplicate assets")
        asset_digests = {item.artifact_digest for item in assets}
        if len({target for target, _ in targets}) != len(targets):
            raise ValueError("rich asset manifest contains duplicate targets")
        if any(not target for target, _ in targets):
            raise ValueError("rich asset manifest target cannot be empty")
        if any(digest not in asset_digests for _, digest in targets):
            raise ValueError("rich asset manifest target refers to an unknown asset")
        object.__setattr__(self, "assets", assets)
        object.__setattr__(self, "targets", targets)

    @property
    def digest(self) -> str:
        payload = _json_bytes(rich_asset_manifest_to_document(self))
        return hashlib.sha256(payload).hexdigest()


def cached_rich_document_ref_to_document(
    value: CachedRichDocumentRef,
) -> dict[str, Any]:
    if not isinstance(value, CachedRichDocumentRef):
        raise TypeError("value must be a CachedRichDocumentRef")
    return {
        "primary": cached_document_ref_to_document(value.primary),
        "validators": [
            cached_document_ref_to_document(item) for item in value.validators
        ],
        "rich_parser_contract": value.rich_parser_contract,
        "rich_document_sha256": value.rich_document_sha256,
        "asset_manifest_sha256": value.asset_manifest_sha256,
    }


def cached_rich_document_ref_from_document(
    value: Mapping[str, Any],
) -> CachedRichDocumentRef:
    if not isinstance(value, Mapping) or set(value) != _REF_FIELDS:
        raise ValueError("cached rich document reference has invalid fields")
    validators = value.get("validators")
    if not isinstance(validators, list):
        raise ValueError("cached rich document validators must be a list")
    if any(not isinstance(item, Mapping) for item in validators):
        raise ValueError("cached rich document validator is invalid")
    primary = value.get("primary")
    if not isinstance(primary, Mapping):
        raise ValueError("cached rich document primary is invalid")
    if any(
        not isinstance(value.get(field), str)
        for field in (
            "rich_parser_contract",
            "rich_document_sha256",
            "asset_manifest_sha256",
        )
    ):
        raise ValueError("cached rich document reference metadata is invalid")
    try:
        return CachedRichDocumentRef(
            primary=cached_document_ref_from_document(primary),
            validators=tuple(
                cached_document_ref_from_document(item)
                for item in validators
            ),
            rich_parser_contract=value["rich_parser_contract"],
            rich_document_sha256=value["rich_document_sha256"],
            asset_manifest_sha256=value["asset_manifest_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid cached rich document reference: {exc}") from exc


def rich_asset_manifest(document: RichDocument) -> RichAssetManifest:
    """Build the canonical scoped asset manifest for ``document``."""

    if not isinstance(document, RichDocument):
        raise TypeError("document must be a RichDocument")
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in document.blocks:
        if block.kind is not RichBlockKind.FIGURE:
            continue
        target = str(block.payload["target"])
        digest = str(block.payload["asset_digest"])
        if not target or not digest or target in seen:
            continue
        seen.add(target)
        targets.append((target, digest))
    return RichAssetManifest(document.assets, tuple(targets))


def rich_asset_manifest_to_document(
    manifest: RichAssetManifest,
) -> dict[str, Any]:
    if not isinstance(manifest, RichAssetManifest):
        raise TypeError("manifest must be a RichAssetManifest")
    return {
        "schema_version": RICH_ASSET_MANIFEST_SCHEMA,
        "assets": [
            {
                "artifact_digest": item.artifact_digest,
                "media_type": item.media_type,
                "logical_name": item.logical_name,
                "size": item.size,
            }
            for item in manifest.assets
        ],
        "targets": [
            {"target": target, "artifact_digest": digest}
            for target, digest in manifest.targets
        ],
    }


def rich_asset_manifest_from_document(
    value: Mapping[str, Any],
) -> RichAssetManifest:
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise ValueError("rich asset manifest has invalid fields")
    if value.get("schema_version") != RICH_ASSET_MANIFEST_SCHEMA:
        raise ValueError("unsupported rich asset manifest schema")
    raw_assets = value.get("assets")
    raw_targets = value.get("targets")
    if not isinstance(raw_assets, list) or not isinstance(raw_targets, list):
        raise ValueError("rich asset manifest collections must be lists")
    assets: list[RichAsset] = []
    for raw in raw_assets:
        if not isinstance(raw, Mapping) or set(raw) != _ASSET_FIELDS:
            raise ValueError("rich asset manifest asset has invalid fields")
        if (
            not isinstance(raw["artifact_digest"], str)
            or not isinstance(raw["media_type"], str)
            or not isinstance(raw["logical_name"], str)
            or not isinstance(raw["size"], int)
            or isinstance(raw["size"], bool)
        ):
            raise ValueError("rich asset manifest asset metadata is invalid")
        assets.append(
            RichAsset(
                artifact_digest=raw["artifact_digest"],
                media_type=raw["media_type"],
                logical_name=raw["logical_name"],
                size=raw["size"],
            )
        )
    targets: list[tuple[str, str]] = []
    for raw in raw_targets:
        if not isinstance(raw, Mapping) or set(raw) != _TARGET_FIELDS:
            raise ValueError("rich asset manifest target has invalid fields")
        target = raw["target"]
        digest = raw["artifact_digest"]
        if not isinstance(target, str) or not isinstance(digest, str):
            raise ValueError("rich asset manifest target metadata is invalid")
        targets.append((target, digest))
    return RichAssetManifest(tuple(assets), tuple(targets))


class CachedRichDocumentStore:
    """Atomic derived-object storage addressed by deterministic parser inputs."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def store(
        self,
        reference: CachedRichDocumentRef,
        document: RichDocument,
    ) -> None:
        _validate_binding(reference, document)
        manifest = rich_asset_manifest(document)
        key = _reference_key(reference)
        with exclusive_file_lock(self._lock_path(key)):
            atomic_write_bytes(
                self._document_path(key),
                _json_bytes(rich_document_to_document(document)),
            )
            atomic_write_bytes(
                self._manifest_path(key),
                _json_bytes(rich_asset_manifest_to_document(manifest)),
            )

    def read_document(
        self, reference: CachedRichDocumentRef
    ) -> RichDocument:
        key = _reference_key(reference)
        with exclusive_file_lock(self._lock_path(key)):
            try:
                value = json.loads(
                    self._document_path(key).read_text(encoding="utf-8")
                )
                document = rich_document_from_document(value)
            except (
                OSError,
                TypeError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise CachedRichDocumentError(
                    "cached_rich_document_corrupt",
                    "cached rich document is unreadable or invalid",
                ) from exc
        _validate_binding(reference, document)
        return document

    def read_manifest(
        self, reference: CachedRichDocumentRef
    ) -> RichAssetManifest:
        key = _reference_key(reference)
        with exclusive_file_lock(self._lock_path(key)):
            try:
                value = json.loads(
                    self._manifest_path(key).read_text(encoding="utf-8")
                )
                manifest = rich_asset_manifest_from_document(value)
            except (
                OSError,
                TypeError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise CachedRichDocumentError(
                    "cached_rich_asset_manifest_corrupt",
                    "cached rich asset manifest is unreadable or invalid",
                ) from exc
        if manifest.digest != reference.asset_manifest_sha256:
            raise CachedRichDocumentError(
                "cached_rich_asset_manifest_mismatch",
                "cached rich asset manifest does not match its logical reference",
            )
        return manifest

    def _base(self) -> Path:
        return self.root / "rich-document" / "v1"

    def _document_path(self, key: str) -> Path:
        return self._base() / "objects" / key[:2] / key / "document.json"

    def _manifest_path(self, key: str) -> Path:
        return self._base() / "objects" / key[:2] / key / "asset-manifest.json"

    def _lock_path(self, key: str) -> Path:
        return self._base() / "locks" / f"{key}.lock"


def _validate_binding(
    reference: CachedRichDocumentRef,
    document: RichDocument,
) -> None:
    if document.document_digest != reference.rich_document_sha256:
        raise CachedRichDocumentError(
            "cached_rich_document_digest_mismatch",
            "rich document does not match its logical reference",
        )
    source = document.source
    primary = reference.primary
    if (
        source.source_format is not primary.source_format
        or source.artifact_digest != primary.source_sha256
        or source.size != primary.source_size
        or source.media_type != primary.media_type
    ):
        raise CachedRichDocumentError(
            "cached_rich_document_primary_mismatch",
            "rich document primary does not match its logical reference",
        )
    if rich_asset_manifest(document).digest != reference.asset_manifest_sha256:
        raise CachedRichDocumentError(
            "cached_rich_asset_manifest_mismatch",
            "rich document asset manifest does not match its logical reference",
        )


def _reference_key(reference: CachedRichDocumentRef) -> str:
    # The storage identity names deterministic parser inputs.  Output digests
    # remain independent verification identities and therefore are not used to
    # locate the bytes that they must verify.
    material = {
        "primary": cached_document_ref_to_document(reference.primary),
        "validators": [
            cached_document_ref_to_document(item)
            for item in reference.validators
        ],
        "rich_parser_contract": reference.rich_parser_contract,
    }
    return hashlib.sha256(_json_bytes(material)).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "CACHED_RICH_DOCUMENT_REF_SCHEMA",
    "RICH_ASSET_MANIFEST_SCHEMA",
    "RICH_DOCUMENT_PARSER_CONTRACT",
    "CachedRichDocumentError",
    "CachedRichDocumentRef",
    "cached_rich_document_ref_from_document",
    "cached_rich_document_ref_to_document",
]
