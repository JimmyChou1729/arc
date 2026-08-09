"""Typed contracts for independently reviewed Companion supplements."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from arc_jobs import canonical_json_bytes
from arc_paper import RichDocument, rich_block_to_document

from .rich_text import parse_markdown


REVIEWED_COMPANION_SUPPLEMENT_SCHEMA = (
    "arc.companion.reviewed_supplement.v2"
)
LEGACY_REVIEWED_COMPANION_SUPPLEMENT_SCHEMA_V1 = (
    "arc.companion.reviewed_supplement.v1"
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ReviewedOwnedResource:
    """Digest-bound publication resource whose bytes remain external."""

    artifact_digest: str
    logical_name: str
    media_type: str
    size: int

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.artifact_digest) is None:
            raise ValueError("resource artifact_digest must be a SHA-256 digest")
        logical_name = _logical_name(self.logical_name)
        media_type = _text(self.media_type, "resource media_type")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 1:
            raise ValueError("resource size must be a positive integer")
        object.__setattr__(self, "logical_name", logical_name)
        object.__setattr__(self, "media_type", media_type)


@dataclass(frozen=True)
class ReviewedSourceUnit:
    """Exhaustive disposition of one unit in the reviewed source material."""

    unit_id: str
    kind: Literal["text", "image"]
    locator: str
    fingerprint: str
    disposition: Literal["published", "excluded"]
    reason: str
    entry_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.unit_id, "coverage unit_id")
        if self.kind not in {"text", "image"}:
            raise ValueError("coverage kind must be text or image")
        object.__setattr__(self, "locator", _text(self.locator, "coverage locator"))
        if _SHA256.fullmatch(self.fingerprint) is None:
            raise ValueError("coverage fingerprint must be a SHA-256 digest")
        if self.disposition not in {"published", "excluded"}:
            raise ValueError("coverage disposition must be published or excluded")
        object.__setattr__(self, "reason", _text(self.reason, "coverage reason"))
        entry_ids = _identifiers(self.entry_ids, "coverage entry_ids")
        if self.disposition == "published" and not entry_ids:
            raise ValueError("published coverage must map to at least one entry")
        if self.disposition == "excluded" and entry_ids:
            raise ValueError("excluded coverage must not map to entries")
        object.__setattr__(self, "entry_ids", entry_ids)


@dataclass(frozen=True)
class ReviewedSourceDraft:
    """Final disposition of one reviewed draft derived from source units."""

    draft_id: str
    disposition: Literal["published", "excluded"]
    reason: str
    source_unit_ids: tuple[str, ...]
    entry_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.draft_id, "draft_id")
        if self.disposition not in {"published", "excluded"}:
            raise ValueError("draft disposition must be published or excluded")
        object.__setattr__(self, "reason", _text(self.reason, "draft reason"))
        units = _identifiers(self.source_unit_ids, "draft source_unit_ids")
        entries = _identifiers(self.entry_ids, "draft entry_ids")
        if not units:
            raise ValueError("draft source_unit_ids must not be empty")
        if self.disposition == "published" and not entries:
            raise ValueError("published draft must map to at least one entry")
        if self.disposition == "excluded" and entries:
            raise ValueError("excluded draft must not map to entries")
        object.__setattr__(self, "source_unit_ids", units)
        object.__setattr__(self, "entry_ids", entries)


@dataclass(frozen=True)
class ReviewedSupplementEntry:
    """One reviewed publication entry anchored to the immutable main source."""

    entry_id: str
    anchor_block_id: str
    anchor_fingerprint: str
    title: str
    markdown: str
    source_draft_ids: tuple[str, ...]
    source_unit_ids: tuple[str, ...]
    source_basis: Literal[
        "supplement_units", "supplement_drafts", "primary_source"
    ] = (
        "supplement_units"
    )
    source_basis_reason: str = ""

    def __post_init__(self) -> None:
        _identifier(self.entry_id, "entry_id")
        _identifier(self.anchor_block_id, "anchor_block_id")
        if _SHA256.fullmatch(self.anchor_fingerprint) is None:
            raise ValueError("entry anchor_fingerprint must be a SHA-256 digest")
        object.__setattr__(self, "title", _text(self.title, "entry title"))
        markdown = unicodedata.normalize(
            "NFC",
            _text(self.markdown, "entry markdown")
            .replace("\r\n", "\n")
            .replace("\r", "\n"),
        ).strip()
        parse_markdown(markdown)
        object.__setattr__(self, "markdown", markdown)
        drafts = _identifiers(self.source_draft_ids, "entry source_draft_ids")
        units = _identifiers(self.source_unit_ids, "entry source_unit_ids")
        if self.source_basis not in {
            "supplement_units",
            "supplement_drafts",
            "primary_source",
        }:
            raise ValueError(
                "entry source_basis must be supplement_units, "
                "supplement_drafts, or primary_source"
            )
        if self.source_basis == "supplement_units" and not units:
            raise ValueError("entry source_unit_ids must not be empty")
        if self.source_basis == "supplement_drafts" and (
            units or not drafts
        ):
            raise ValueError(
                "supplement-draft entry requires drafts and no direct units"
            )
        if units and self.source_basis not in {"supplement_units"}:
            raise ValueError(
                "entry with source units must use the supplement_units basis"
            )
        if self.source_basis == "primary_source" and drafts:
            raise ValueError(
                "primary-source entry must not refer to supplement drafts"
            )
        reason = self.source_basis_reason.strip()
        if self.source_basis == "primary_source" and not reason:
            raise ValueError(
                "primary-source entry must explain its source basis"
            )
        object.__setattr__(self, "source_draft_ids", drafts)
        object.__setattr__(self, "source_unit_ids", units)
        object.__setattr__(self, "source_basis_reason", reason)


@dataclass(frozen=True)
class ReviewedCompanionSupplement:
    """Reviewed entries plus exact source-unit coverage and owned resources."""

    supplement_id: str
    summary: str
    source_unit_count: int
    source_inventory_digest: str
    entries: tuple[ReviewedSupplementEntry, ...]
    coverage: tuple[ReviewedSourceUnit, ...]
    drafts: tuple[ReviewedSourceDraft, ...]
    resources: tuple[ReviewedOwnedResource, ...] = ()
    schema_version: str = REVIEWED_COMPANION_SUPPLEMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != REVIEWED_COMPANION_SUPPLEMENT_SCHEMA:
            raise ValueError("unsupported reviewed Companion supplement schema")
        _identifier(self.supplement_id, "supplement_id")
        object.__setattr__(self, "summary", _text(self.summary, "supplement summary"))
        if (
            isinstance(self.source_unit_count, bool)
            or not isinstance(self.source_unit_count, int)
            or self.source_unit_count < 1
        ):
            raise ValueError("source_unit_count must be a positive integer")
        if _SHA256.fullmatch(self.source_inventory_digest) is None:
            raise ValueError(
                "source_inventory_digest must be a SHA-256 digest"
            )
        entries = tuple(self.entries)
        coverage = tuple(self.coverage)
        drafts = tuple(self.drafts)
        resources = tuple(self.resources)
        if any(not isinstance(item, ReviewedSupplementEntry) for item in entries):
            raise ValueError("entries must contain ReviewedSupplementEntry values")
        if any(not isinstance(item, ReviewedSourceUnit) for item in coverage):
            raise ValueError("coverage must contain ReviewedSourceUnit values")
        if any(not isinstance(item, ReviewedSourceDraft) for item in drafts):
            raise ValueError("drafts must contain ReviewedSourceDraft values")
        if any(not isinstance(item, ReviewedOwnedResource) for item in resources):
            raise ValueError("resources must contain ReviewedOwnedResource values")
        if not coverage:
            raise ValueError("reviewed supplement coverage must not be empty")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "drafts", drafts)
        object.__setattr__(self, "resources", resources)


def validate_reviewed_companion_supplement(
    supplement: ReviewedCompanionSupplement,
    source: RichDocument,
) -> None:
    """Validate exact coverage, source anchors, and publication resources."""

    if not isinstance(supplement, ReviewedCompanionSupplement):
        raise ValueError("supplement must be a ReviewedCompanionSupplement")
    if not isinstance(source, RichDocument):
        raise ValueError("source must be a RichDocument")

    entries = _unique_by(
        supplement.entries, lambda item: item.entry_id, "duplicate entry_id"
    )
    coverage = _unique_by(
        supplement.coverage, lambda item: item.unit_id, "duplicate coverage unit_id"
    )
    if len(coverage) != supplement.source_unit_count:
        raise ValueError("reviewed supplement source coverage is incomplete")
    if (
        reviewed_source_inventory_digest(supplement.coverage)
        != supplement.source_inventory_digest
    ):
        raise ValueError(
            "reviewed supplement source inventory digest differs"
        )
    drafts = _unique_by(
        supplement.drafts, lambda item: item.draft_id, "duplicate draft_id"
    )
    resources_by_name = _unique_by(
        supplement.resources,
        lambda item: item.logical_name,
        "duplicate resource logical_name",
    )
    _unique_by(
        supplement.resources,
        lambda item: item.artifact_digest,
        "duplicate resource artifact_digest",
    )

    blocks = {item.block_id: item for item in source.blocks}
    for entry in supplement.entries:
        block = blocks.get(entry.anchor_block_id)
        if block is None:
            raise ValueError(
                f"entry anchor_block_id is absent from the source: {entry.anchor_block_id}"
            )
        actual = reviewed_anchor_fingerprint(block)
        if actual != entry.anchor_fingerprint:
            raise ValueError(
                f"entry anchor_fingerprint differs from the source: {entry.entry_id}"
            )
        unknown_units = set(entry.source_unit_ids) - set(coverage)
        if unknown_units:
            raise ValueError(
                "entry refers to unknown coverage source unit: "
                + sorted(unknown_units)[0]
            )

    expected_links: set[tuple[str, str]] = set()
    for item in supplement.coverage:
        for entry_id in item.entry_ids:
            if entry_id not in entries:
                raise ValueError(
                    f"coverage refers to unknown entry_id: {entry_id}"
                )
            expected_links.add((item.unit_id, entry_id))
    actual_links = {
        (unit_id, entry.entry_id)
        for entry in supplement.entries
        for unit_id in entry.source_unit_ids
    }
    if actual_links != expected_links:
        raise ValueError(
            "coverage and entry source-unit mappings must match exactly"
        )

    expected_draft_links: set[tuple[str, str]] = set()
    for draft in supplement.drafts:
        unknown_units = set(draft.source_unit_ids) - set(coverage)
        if unknown_units:
            raise ValueError(
                "draft refers to unknown coverage source unit: "
                + sorted(unknown_units)[0]
            )
        for entry_id in draft.entry_ids:
            if entry_id not in entries:
                raise ValueError(
                    f"draft refers to unknown entry_id: {entry_id}"
                )
            expected_draft_links.add((draft.draft_id, entry_id))
    actual_draft_links = {
        (draft_id, entry.entry_id)
        for entry in supplement.entries
        for draft_id in entry.source_draft_ids
    }
    unknown_drafts = {
        draft_id for draft_id, _entry_id in actual_draft_links
    } - set(drafts)
    if unknown_drafts:
        raise ValueError(
            "entry refers to unknown source draft: "
            + sorted(unknown_drafts)[0]
        )
    if actual_draft_links != expected_draft_links:
        raise ValueError("draft and entry mappings must match exactly")

    source_names = {item.logical_name for item in source.assets}
    source_digests = {item.artifact_digest for item in source.assets}
    for resource in supplement.resources:
        if resource.logical_name in source_names:
            raise ValueError(
                f"owned resource duplicates a source logical name: {resource.logical_name}"
            )
        if resource.artifact_digest in source_digests:
            raise ValueError(
                f"owned resource duplicates a source artifact digest: {resource.artifact_digest}"
            )
    available_names = source_names | set(resources_by_name)
    for entry in supplement.entries:
        for logical_name in _local_image_names(entry.markdown):
            if logical_name not in available_names:
                raise ValueError(
                    "entry Markdown image has no source or owned resource: "
                    f"{logical_name}"
                )


def encode_reviewed_companion_supplement(
    supplement: ReviewedCompanionSupplement,
) -> dict[str, Any]:
    if not isinstance(supplement, ReviewedCompanionSupplement):
        raise ValueError("supplement must be a ReviewedCompanionSupplement")
    return {
        "schema_version": supplement.schema_version,
        "supplement_id": supplement.supplement_id,
        "summary": supplement.summary,
        "source_unit_count": supplement.source_unit_count,
        "source_inventory_digest": supplement.source_inventory_digest,
        "entries": [_encode_entry(item) for item in supplement.entries],
        "coverage": [_encode_coverage(item) for item in supplement.coverage],
        "drafts": [_encode_draft(item) for item in supplement.drafts],
        "resources": [_encode_resource(item) for item in supplement.resources],
    }


def decode_reviewed_companion_supplement(
    value: Mapping[str, Any],
) -> ReviewedCompanionSupplement:
    document = _exact(
        value,
        {
            "schema_version",
            "supplement_id",
            "summary",
            "source_unit_count",
            "source_inventory_digest",
            "entries",
            "coverage",
            "drafts",
            "resources",
        },
        "reviewed supplement",
    )
    schema_version = _required_string(document, "schema_version")
    if schema_version not in {
        REVIEWED_COMPANION_SUPPLEMENT_SCHEMA,
        LEGACY_REVIEWED_COMPANION_SUPPLEMENT_SCHEMA_V1,
    }:
        raise ValueError("unsupported reviewed Companion supplement schema")
    legacy = schema_version == LEGACY_REVIEWED_COMPANION_SUPPLEMENT_SCHEMA_V1
    return ReviewedCompanionSupplement(
        supplement_id=_required_string(document, "supplement_id"),
        summary=_required_string(document, "summary"),
        source_unit_count=_required_integer(
            document, "source_unit_count"
        ),
        source_inventory_digest=_required_string(
            document, "source_inventory_digest"
        ),
        entries=tuple(
            _decode_entry(item, legacy=legacy)
            for item in _object_sequence(document["entries"], "entries")
        ),
        coverage=tuple(
            _decode_coverage(item)
            for item in _object_sequence(document["coverage"], "coverage")
        ),
        drafts=tuple(
            _decode_draft(item)
            for item in _object_sequence(document["drafts"], "drafts")
        ),
        resources=tuple(
            _decode_resource(item)
            for item in _object_sequence(document["resources"], "resources")
        ),
        schema_version=REVIEWED_COMPANION_SUPPLEMENT_SCHEMA,
    )


def _encode_entry(item: ReviewedSupplementEntry) -> dict[str, Any]:
    return {
        "entry_id": item.entry_id,
        "anchor_block_id": item.anchor_block_id,
        "anchor_fingerprint": item.anchor_fingerprint,
        "title": item.title,
        "markdown": item.markdown,
        "source_draft_ids": list(item.source_draft_ids),
        "source_unit_ids": list(item.source_unit_ids),
        "source_basis": item.source_basis,
        "source_basis_reason": item.source_basis_reason,
    }


def _encode_coverage(item: ReviewedSourceUnit) -> dict[str, Any]:
    return {
        "unit_id": item.unit_id,
        "kind": item.kind,
        "locator": item.locator,
        "fingerprint": item.fingerprint,
        "disposition": item.disposition,
        "reason": item.reason,
        "entry_ids": list(item.entry_ids),
    }


def _encode_draft(item: ReviewedSourceDraft) -> dict[str, Any]:
    return {
        "draft_id": item.draft_id,
        "disposition": item.disposition,
        "reason": item.reason,
        "source_unit_ids": list(item.source_unit_ids),
        "entry_ids": list(item.entry_ids),
    }


def _encode_resource(item: ReviewedOwnedResource) -> dict[str, Any]:
    return {
        "artifact_digest": item.artifact_digest,
        "logical_name": item.logical_name,
        "media_type": item.media_type,
        "size": item.size,
    }


def _decode_entry(
    value: Mapping[str, Any], *, legacy: bool
) -> ReviewedSupplementEntry:
    legacy_fields = {
        "entry_id",
        "anchor_block_id",
        "anchor_fingerprint",
        "title",
        "markdown",
        "source_draft_ids",
        "source_unit_ids",
    }
    expected_fields = (
        legacy_fields
        if legacy
        else legacy_fields | {"source_basis", "source_basis_reason"}
    )
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("reviewed entry has invalid fields")
    item = value
    units = _string_tuple(item["source_unit_ids"], "source_unit_ids")
    drafts = _string_tuple(item["source_draft_ids"], "source_draft_ids")
    legacy_basis = (
        "supplement_units"
        if units
        else "supplement_drafts" if drafts else "primary_source"
    )
    return ReviewedSupplementEntry(
        entry_id=_required_string(item, "entry_id"),
        anchor_block_id=_required_string(item, "anchor_block_id"),
        anchor_fingerprint=_required_string(item, "anchor_fingerprint"),
        title=_required_string(item, "title"),
        markdown=_required_string(item, "markdown"),
        source_draft_ids=drafts,
        source_unit_ids=units,
        source_basis=(
            _required_string(item, "source_basis")
            if "source_basis" in item
            else legacy_basis
        ),  # type: ignore[arg-type]
        source_basis_reason=(
            _required_string(item, "source_basis_reason")
            if "source_basis_reason" in item
            else (
                "Imported legacy entry without supplemental unit provenance."
                if legacy and legacy_basis == "primary_source"
                else ""
            )
        ),
    )


def _decode_coverage(value: Mapping[str, Any]) -> ReviewedSourceUnit:
    item = _exact(
        value,
        {"unit_id", "kind", "locator", "fingerprint", "disposition", "reason", "entry_ids"},
        "source coverage",
    )
    return ReviewedSourceUnit(
        unit_id=_required_string(item, "unit_id"),
        kind=_required_string(item, "kind"),  # type: ignore[arg-type]
        locator=_required_string(item, "locator"),
        fingerprint=_required_string(item, "fingerprint"),
        disposition=_required_string(item, "disposition"),  # type: ignore[arg-type]
        reason=_required_string(item, "reason"),
        entry_ids=_string_tuple(item["entry_ids"], "entry_ids"),
    )


def _decode_draft(value: Mapping[str, Any]) -> ReviewedSourceDraft:
    item = _exact(
        value,
        {
            "draft_id",
            "disposition",
            "reason",
            "source_unit_ids",
            "entry_ids",
        },
        "source draft",
    )
    return ReviewedSourceDraft(
        draft_id=_required_string(item, "draft_id"),
        disposition=_required_string(item, "disposition"),  # type: ignore[arg-type]
        reason=_required_string(item, "reason"),
        source_unit_ids=_string_tuple(
            item["source_unit_ids"], "source_unit_ids"
        ),
        entry_ids=_string_tuple(item["entry_ids"], "entry_ids"),
    )


def _decode_resource(value: Mapping[str, Any]) -> ReviewedOwnedResource:
    item = _exact(
        value,
        {"artifact_digest", "logical_name", "media_type", "size"},
        "owned resource",
    )
    size = item["size"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("owned resource size must be an integer")
    return ReviewedOwnedResource(
        artifact_digest=_required_string(item, "artifact_digest"),
        logical_name=_required_string(item, "logical_name"),
        media_type=_required_string(item, "media_type"),
        size=size,
    )


def _local_image_names(markdown: str) -> tuple[str, ...]:
    names: list[str] = []
    for token in parse_markdown(markdown):
        for child in token.children or ():
            if child.type != "image":
                continue
            target = child.attrGet("src") or ""
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                raise ValueError(
                    "entry Markdown images must use a source or owned resource"
                )
            names.append(target)
    return tuple(names)


def reviewed_anchor_fingerprint(block: Any) -> str:
    """Return the durable fingerprint required by reviewed entry anchors."""

    return hashlib.sha256(
        canonical_json_bytes(rich_block_to_document(block))
    ).hexdigest()


def reviewed_source_inventory_digest(
    coverage: Sequence[ReviewedSourceUnit],
) -> str:
    """Bind the complete ordered external source-unit inventory."""

    return hashlib.sha256(
        canonical_json_bytes(
            [
                {
                    "unit_id": item.unit_id,
                    "kind": item.kind,
                    "locator": item.locator,
                    "fingerprint": item.fingerprint,
                }
                for item in coverage
            ]
        )
    ).hexdigest()


def _unique_by(values: Sequence[Any], key: Any, message: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for value in values:
        identity = key(value)
        if identity in output:
            raise ValueError(f"{message}: {identity}")
        output[identity] = value
    return output


def _identifier(value: Any, description: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(
            f"{description} must match [A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}"
        )
    return value


def _identifiers(values: Any, description: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{description} must be a sequence of identifiers")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{description} must be a sequence of identifiers") from exc
    for value in result:
        _identifier(value, description)
    if len(result) != len(set(result)):
        raise ValueError(f"{description} must be unique")
    return result


def _logical_name(value: Any) -> str:
    name = _text(value, "resource logical_name")
    path = PurePosixPath(name)
    if (
        "\\" in name
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("resource logical_name must be a canonical relative path")
    return name


def _text(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{description} must be a non-empty string")
    return value.strip()


def _exact(
    value: Any, fields: set[str], description: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{description} has invalid fields")
    return value


def _object_sequence(value: Any, description: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{description} must be an array of objects")
    return tuple(value)


def _string_tuple(value: Any, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{description} must be an array of strings")
    return tuple(value)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _required_integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


__all__ = [
    "LEGACY_REVIEWED_COMPANION_SUPPLEMENT_SCHEMA_V1",
    "REVIEWED_COMPANION_SUPPLEMENT_SCHEMA",
    "ReviewedCompanionSupplement",
    "ReviewedOwnedResource",
    "ReviewedSourceDraft",
    "ReviewedSourceUnit",
    "ReviewedSupplementEntry",
    "decode_reviewed_companion_supplement",
    "encode_reviewed_companion_supplement",
    "reviewed_anchor_fingerprint",
    "reviewed_source_inventory_digest",
    "validate_reviewed_companion_supplement",
]
