"""Strict, host-independent contracts for atomic render overlays."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from arc_paper import (
    RichBlock,
    RichDocument,
    rich_block_to_document,
    rich_document_from_document,
    rich_document_to_document,
)

from ._json import (
    JsonValue,
    canonical_json_bytes,
    freeze_json,
    require_exact,
    require_integer,
    require_list,
    require_string,
    thaw_json,
)


FRAGMENT_REVISION_SCHEMA_V1 = "arc.render.fragment_revision.v1"
FRAGMENT_REVISION_SCHEMA_V2 = "arc.render.fragment_revision.v2"
FRAGMENT_REVISION_SCHEMA = "arc.render.fragment_revision.v3"
LAYER_SCHEMA = "arc.render.layer.v1"
PUBLICATION_SCHEMA = "arc.render.publication.v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_RICH_SOURCE_FORMATS = {"html", "markdown", "tex"}
_RICH_BLOCK_KINDS = {
    "heading",
    "paragraph",
    "list",
    "code",
    "equation",
    "table",
    "figure",
}
_SOURCE_FIELDS = {
    "source_format",
    "media_type",
    "artifact_digest",
    "size",
    "rich_document_digest",
}
_ANCHOR_BLOCK_FIELDS = {
    "block_id",
    "kind",
    "ordinal",
    "locator",
    "content_fingerprint",
}
_ANCHOR_FIELDS = {"kind", "target_id", "related_blocks"}
_FRAGMENT_V1_FIELDS = {
    "schema_version",
    "source",
    "fragment_id",
    "revision",
    "parent_semantic_digest",
    "anchor",
    "priority",
    "role",
    "language",
    "title",
    "citation_ids",
    "provenance",
}
_FRAGMENT_V2_FIELDS = _FRAGMENT_V1_FIELDS | {"appearance"}
_FRAGMENT_V3_FIELDS = _FRAGMENT_V2_FIELDS | {"deleted"}
_APPEARANCE_FIELDS = {"foreground", "background"}
_REVISION_REF_FIELDS = {
    "path",
    "fragment_id",
    "revision",
    "semantic_digest",
}
_LAYER_FIELDS = {
    "schema_version",
    "source",
    "producer",
    "initial_revisions",
    "layer_digest",
}
_LAYER_REF_FIELDS = {
    "source",
    "producer",
    "path",
    "layer_digest",
}
_PUBLICATION_OUTLINE_ITEM_FIELDS = {
    "section_id",
    "title",
    "level",
    "ordinal",
    "path",
    "block_start",
    "block_end",
    "anchor_block_id",
}
_PUBLICATION_FIELDS = {
    "schema_version",
    "source_document",
    "outline",
    "layers",
    "glossary",
    "bibliography",
    "labels",
    "resources",
    "reader_profile",
    "publication_digest",
}
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


class AnchorKind(str, Enum):
    BLOCK = "block"
    SECTION = "section"


@dataclass(frozen=True)
class SourceIdentity:
    """The exact immutable rich source to which an overlay belongs."""

    source_format: str
    media_type: str
    artifact_digest: str
    size: int
    rich_document_digest: str

    def __post_init__(self) -> None:
        source_format = _nonblank(self.source_format, "source_format")
        if source_format not in _RICH_SOURCE_FORMATS:
            raise ValueError("source_format must identify a rich source")
        media_type = _nonblank(self.media_type, "media_type").casefold()
        if "/" not in media_type or ";" in media_type:
            raise ValueError("media_type must be a normalized MIME type")
        artifact_digest = _digest(
            self.artifact_digest, "artifact_digest"
        )
        rich_document_digest = _digest(
            self.rich_document_digest, "rich_document_digest"
        )
        size = _positive_integer(self.size, "size", allow_zero=True)
        object.__setattr__(self, "source_format", source_format)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "artifact_digest", artifact_digest)
        object.__setattr__(
            self, "rich_document_digest", rich_document_digest
        )
        object.__setattr__(self, "size", size)


@dataclass(frozen=True)
class AnchorBlock:
    """Frozen provenance for one source block related to an anchor."""

    block_id: str
    kind: str
    ordinal: int
    locator: Mapping[str, JsonValue]
    content_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_id", _nonblank(self.block_id, "block_id"))
        kind = _nonblank(self.kind, "block kind")
        if kind not in _RICH_BLOCK_KINDS:
            raise ValueError("block kind is unsupported")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "ordinal", _positive_integer(
                self.ordinal, "block ordinal", allow_zero=True
            )
        )
        locator = freeze_json(self.locator, "block locator")
        if not isinstance(locator, Mapping):
            raise ValueError("block locator must be an object")
        _require_integer_json_numbers(locator, "block locator")
        object.__setattr__(self, "locator", locator)
        object.__setattr__(
            self,
            "content_fingerprint",
            _digest(self.content_fingerprint, "content_fingerprint"),
        )


@dataclass(frozen=True)
class FragmentAnchor:
    kind: AnchorKind | str
    target_id: str
    related_blocks: tuple[AnchorBlock, ...]

    def __post_init__(self) -> None:
        kind = AnchorKind(self.kind)
        target_id = _nonblank(self.target_id, "anchor target_id")
        blocks = tuple(self.related_blocks)
        if any(not isinstance(item, AnchorBlock) for item in blocks):
            raise ValueError("related_blocks must contain AnchorBlock values")
        block_ids = tuple(item.block_id for item in blocks)
        if len(set(block_ids)) != len(block_ids):
            raise ValueError("anchor related block IDs must be unique")
        if len({item.ordinal for item in blocks}) != len(blocks):
            raise ValueError("anchor related block ordinals must be unique")
        if kind is AnchorKind.BLOCK and target_id not in block_ids:
            raise ValueError("block anchor target must be a related block")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "related_blocks", blocks)

    @property
    def related_block_ids(self) -> tuple[str, ...]:
        return tuple(item.block_id for item in self.related_blocks)


@dataclass(frozen=True)
class FragmentAppearance:
    """Optional reader colors owned by one immutable fragment revision."""

    foreground: str
    background: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "foreground", _hex_color(self.foreground, "foreground")
        )
        object.__setattr__(
            self, "background", _hex_color(self.background, "background")
        )


@dataclass(frozen=True)
class FragmentRevision:
    source: SourceIdentity
    fragment_id: str
    revision: int
    parent_semantic_digest: str | None
    anchor: FragmentAnchor
    priority: int
    role: str
    language: str
    title: str | None
    citation_ids: tuple[str, ...]
    provenance: Mapping[str, JsonValue]
    markdown_body: str
    schema_version: str = FRAGMENT_REVISION_SCHEMA
    appearance: FragmentAppearance | None = None
    deleted: bool = False

    def __post_init__(self) -> None:
        if self.schema_version not in {
            FRAGMENT_REVISION_SCHEMA_V1,
            FRAGMENT_REVISION_SCHEMA_V2,
            FRAGMENT_REVISION_SCHEMA,
        }:
            raise ValueError("unsupported fragment revision schema")
        if self.schema_version == FRAGMENT_REVISION_SCHEMA_V1:
            if self.appearance is not None:
                raise ValueError("v1 fragment revisions cannot define appearance")
        elif self.appearance is not None and not isinstance(
            self.appearance, FragmentAppearance
        ):
            raise ValueError("appearance must be a FragmentAppearance or null")
        if not isinstance(self.deleted, bool):
            raise ValueError("deleted must be a boolean")
        if self.schema_version != FRAGMENT_REVISION_SCHEMA and self.deleted:
            raise ValueError("only v3 fragment revisions can be deleted")
        if not isinstance(self.source, SourceIdentity):
            raise ValueError("fragment source must be a SourceIdentity")
        if not isinstance(self.anchor, FragmentAnchor):
            raise ValueError("fragment anchor must be a FragmentAnchor")
        fragment_id = _identifier(self.fragment_id, "fragment_id")
        revision = _positive_integer(self.revision, "revision")
        if revision == 1 and self.parent_semantic_digest is not None:
            raise ValueError("revision 1 cannot have a parent semantic digest")
        if revision > 1 and self.parent_semantic_digest is None:
            raise ValueError("later revisions require a parent semantic digest")
        parent = (
            None
            if self.parent_semantic_digest is None
            else _digest(
                self.parent_semantic_digest, "parent_semantic_digest"
            )
        )
        priority = _positive_integer(self.priority, "priority")
        role = _nonblank(self.role, "role")
        language = _nonblank(self.language, "language")
        title = self.title
        if title is not None:
            title = _nonblank(title, "title")
        citation_ids = tuple(
            _nonblank(item, "citation ID") for item in self.citation_ids
        )
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("citation IDs must be unique")
        provenance = freeze_json(self.provenance, "provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance must be an object")
        _require_integer_json_numbers(provenance, "provenance")
        if not isinstance(self.markdown_body, str):
            raise ValueError("markdown_body must be a string")
        from .markdown import normalize_markdown

        object.__setattr__(self, "fragment_id", fragment_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "parent_semantic_digest", parent)
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "citation_ids", citation_ids)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(
            self, "markdown_body", normalize_markdown(self.markdown_body)
        )

    @property
    def semantic_digest(self) -> str:
        from .markdown import fragment_semantic_digest

        return fragment_semantic_digest(self)


@dataclass(frozen=True)
class FragmentRevisionRef:
    path: str
    fragment_id: str
    revision: int
    semantic_digest: str

    def __post_init__(self) -> None:
        path = _relative_path(self.path, "path")
        object.__setattr__(
            self, "fragment_id", _identifier(self.fragment_id, "fragment_id")
        )
        revision = _positive_integer(self.revision, "revision")
        semantic_digest = _digest(self.semantic_digest, "semantic_digest")
        from .markdown import parse_fragment_revision_filename

        named_revision, named_digest = parse_fragment_revision_filename(
            PurePosixPath(path).name
        )
        if named_revision != revision or named_digest != semantic_digest:
            raise ValueError(
                "revision path does not match its revision and digest"
            )
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "semantic_digest", semantic_digest)


@dataclass(frozen=True)
class Layer:
    source: SourceIdentity
    producer: str
    initial_revisions: tuple[FragmentRevisionRef, ...]
    schema_version: str = LAYER_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != LAYER_SCHEMA:
            raise ValueError("unsupported layer schema")
        if not isinstance(self.source, SourceIdentity):
            raise ValueError("layer source must be a SourceIdentity")
        producer = _nonblank(self.producer, "producer")
        revisions = tuple(self.initial_revisions)
        if any(
            not isinstance(item, FragmentRevisionRef) for item in revisions
        ):
            raise ValueError(
                "initial_revisions must contain FragmentRevisionRef values"
            )
        if any(item.revision != 1 for item in revisions):
            raise ValueError("layer entries must refer to initial revisions")
        fragment_ids = tuple(item.fragment_id for item in revisions)
        if len(set(fragment_ids)) != len(fragment_ids):
            raise ValueError("layer fragment IDs must be unique")
        paths = tuple(item.path for item in revisions)
        if len(set(paths)) != len(paths):
            raise ValueError("layer revision paths must be unique")
        object.__setattr__(self, "producer", producer)
        object.__setattr__(self, "initial_revisions", revisions)

    @property
    def layer_digest(self) -> str:
        return layer_semantic_digest(self)

    def reference(self, path: str) -> "LayerRef":
        return LayerRef(self.source, self.producer, path, self.layer_digest)


@dataclass(frozen=True)
class LayerRef:
    source: SourceIdentity
    producer: str
    path: str
    layer_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceIdentity):
            raise ValueError("layer reference source must be a SourceIdentity")
        object.__setattr__(self, "producer", _nonblank(self.producer, "producer"))
        object.__setattr__(self, "path", _relative_path(self.path, "path"))
        object.__setattr__(
            self, "layer_digest", _digest(self.layer_digest, "layer_digest")
        )


@dataclass(frozen=True)
class PublicationOutlineItem:
    """One explicit publication navigation entry bound to a source block."""

    section_id: str
    title: str
    level: int
    ordinal: int
    path: tuple[str, ...]
    block_start: int
    block_end: int
    anchor_block_id: str

    def __post_init__(self) -> None:
        section_id = _identifier(self.section_id, "outline section_id")
        if not isinstance(self.title, str):
            raise ValueError("outline title must be a string")
        title = self.title
        level = _positive_integer(self.level, "outline level")
        ordinal = _positive_integer(
            self.ordinal, "outline ordinal", allow_zero=True
        )
        path = tuple(
            _identifier(item, "outline path item") for item in self.path
        )
        if not path or path[-1] != section_id:
            raise ValueError("outline path must end with its section ID")
        block_start = _positive_integer(
            self.block_start, "outline block_start", allow_zero=True
        )
        block_end = _positive_integer(self.block_end, "outline block_end")
        if block_end <= block_start:
            raise ValueError("outline block range must be non-empty")
        anchor_block_id = _nonblank(
            self.anchor_block_id, "outline anchor_block_id"
        )
        object.__setattr__(self, "section_id", section_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "block_start", block_start)
        object.__setattr__(self, "block_end", block_end)
        object.__setattr__(self, "anchor_block_id", anchor_block_id)


@dataclass(frozen=True, eq=False)
class Publication:
    """A self-contained source binding plus ordered external overlay files."""

    source_document: RichDocument
    layers: tuple[LayerRef, ...] = ()
    glossary: tuple[Mapping[str, JsonValue], ...] = ()
    bibliography: tuple[Mapping[str, JsonValue], ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    resources: tuple[Mapping[str, JsonValue], ...] = ()
    reader_profile: Mapping[str, JsonValue] = field(default_factory=dict)
    schema_version: str = PUBLICATION_SCHEMA
    outline: tuple[PublicationOutlineItem, ...] | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_SCHEMA:
            raise ValueError("unsupported publication schema")
        if not isinstance(self.source_document, RichDocument):
            raise ValueError("source_document must be a RichDocument")
        outline = (
            _default_publication_outline(self.source_document)
            if self.outline is None
            else tuple(self.outline)
        )
        _validate_publication_outline(self.source_document, outline)
        object.__setattr__(self, "outline", outline)
        source = source_identity_from_rich_document(self.source_document)
        layers = tuple(self.layers)
        if any(not isinstance(item, LayerRef) for item in layers):
            raise ValueError("layers must contain LayerRef values")
        if any(item.source != source for item in layers):
            raise ValueError("publication layers must bind its rich source")
        object.__setattr__(self, "layers", layers)
        for name in ("glossary", "bibliography", "resources"):
            items = tuple(getattr(self, name))
            frozen_items: list[Mapping[str, JsonValue]] = []
            for item in items:
                frozen = freeze_json(item, name)
                if not isinstance(frozen, Mapping):
                    raise ValueError(f"{name} entries must be objects")
                frozen_items.append(frozen)
            object.__setattr__(self, name, tuple(frozen_items))
        labels = dict(self.labels)
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in labels.items()
        ):
            raise ValueError("labels must map non-empty strings to strings")
        object.__setattr__(self, "labels", MappingProxyType(labels))
        reader_profile = freeze_json(self.reader_profile, "reader_profile")
        if not isinstance(reader_profile, Mapping):
            raise ValueError("reader_profile must be an object")
        object.__setattr__(self, "reader_profile", reader_profile)

    @property
    def source(self) -> SourceIdentity:
        return source_identity_from_rich_document(self.source_document)

    @property
    def publication_digest(self) -> str:
        return publication_semantic_digest(self)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Publication):
            return NotImplemented
        return self.publication_digest == other.publication_digest

    def __hash__(self) -> int:
        return hash(self.publication_digest)


def _default_publication_outline(
    document: RichDocument,
) -> tuple[PublicationOutlineItem, ...]:
    return tuple(
        PublicationOutlineItem(
            section_id=section.section_id,
            title=section.title,
            level=section.level,
            ordinal=ordinal,
            path=section.path,
            block_start=section.block_start,
            block_end=section.block_end,
            anchor_block_id=document.blocks[section.block_start].block_id,
        )
        for ordinal, section in enumerate(
            item
            for item in document.sections
            if item.block_start < item.block_end
        )
    )


def _validate_publication_outline(
    document: RichDocument,
    outline: tuple[PublicationOutlineItem, ...],
) -> None:
    if any(not isinstance(item, PublicationOutlineItem) for item in outline):
        raise ValueError(
            "outline must contain PublicationOutlineItem values"
        )
    section_ids = tuple(item.section_id for item in outline)
    if len(set(section_ids)) != len(section_ids):
        raise ValueError("publication outline section IDs must be unique")
    if tuple(item.ordinal for item in outline) != tuple(range(len(outline))):
        raise ValueError("publication outline ordinals must be contiguous")
    blocks = {item.block_id: item for item in document.blocks}
    preceding: dict[str, PublicationOutlineItem] = {}
    for item in outline:
        if item.block_end > len(document.blocks):
            raise ValueError(
                "publication outline block range exceeds the rich source"
            )
        anchor = blocks.get(item.anchor_block_id)
        if (
            anchor is None
            or anchor.ordinal < item.block_start
            or anchor.ordinal >= item.block_end
        ):
            raise ValueError(
                "publication outline anchor must belong to its source block range"
            )
        for depth, ancestor_id in enumerate(item.path[:-1], start=1):
            ancestor = preceding.get(ancestor_id)
            if (
                ancestor is None
                or ancestor.path != item.path[:depth]
                or ancestor.level >= item.level
                or ancestor.block_start > item.block_start
                or ancestor.block_end < item.block_end
            ):
                raise ValueError(
                    "publication outline path ancestry is inconsistent"
                )
        preceding[item.section_id] = item


def source_identity_from_rich_document(
    document: RichDocument,
) -> SourceIdentity:
    if not isinstance(document, RichDocument):
        raise ValueError("document must be a RichDocument")
    return SourceIdentity(
        source_format=document.source.source_format.value,
        media_type=document.source.media_type,
        artifact_digest=document.source.artifact_digest,
        size=document.source.size,
        rich_document_digest=document.document_digest,
    )


def anchor_block_from_rich_block(block: RichBlock) -> AnchorBlock:
    """Freeze the complete source provenance and fingerprint of a rich block."""

    if not isinstance(block, RichBlock):
        raise ValueError("block must be a RichBlock")
    document = rich_block_to_document(block)
    locator = document["locator"]
    assert isinstance(locator, Mapping)
    return AnchorBlock(
        block_id=block.block_id,
        kind=block.kind.value,
        ordinal=block.ordinal,
        locator=locator,
        content_fingerprint=hashlib.sha256(
            canonical_json_bytes(document)
        ).hexdigest(),
    )


def source_identity_to_document(source: SourceIdentity) -> dict[str, Any]:
    return {
        "source_format": source.source_format,
        "media_type": source.media_type,
        "artifact_digest": source.artifact_digest,
        "size": source.size,
        "rich_document_digest": source.rich_document_digest,
    }


def source_identity_from_document(value: Any) -> SourceIdentity:
    item = require_exact(value, _SOURCE_FIELDS, "source identity")
    return SourceIdentity(
        source_format=require_string(
            item["source_format"], "source_format"
        ),
        media_type=require_string(item["media_type"], "media_type"),
        artifact_digest=require_string(
            item["artifact_digest"], "artifact_digest"
        ),
        size=require_integer(item["size"], "size"),
        rich_document_digest=require_string(
            item["rich_document_digest"], "rich_document_digest"
        ),
    )


def anchor_block_to_document(block: AnchorBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "kind": block.kind,
        "ordinal": block.ordinal,
        "locator": thaw_json(block.locator),
        "content_fingerprint": block.content_fingerprint,
    }


def anchor_block_from_document(value: Any) -> AnchorBlock:
    item = require_exact(value, _ANCHOR_BLOCK_FIELDS, "anchor block")
    locator = item["locator"]
    if not isinstance(locator, Mapping):
        raise ValueError("anchor block locator must be an object")
    return AnchorBlock(
        block_id=require_string(item["block_id"], "block_id"),
        kind=require_string(item["kind"], "block kind"),
        ordinal=require_integer(item["ordinal"], "block ordinal"),
        locator=locator,
        content_fingerprint=require_string(
            item["content_fingerprint"], "content_fingerprint"
        ),
    )


def fragment_anchor_to_document(anchor: FragmentAnchor) -> dict[str, Any]:
    return {
        "kind": anchor.kind.value,
        "target_id": anchor.target_id,
        "related_blocks": [
            anchor_block_to_document(item) for item in anchor.related_blocks
        ],
    }


def fragment_anchor_from_document(value: Any) -> FragmentAnchor:
    item = require_exact(value, _ANCHOR_FIELDS, "fragment anchor")
    return FragmentAnchor(
        kind=require_string(item["kind"], "anchor kind"),
        target_id=require_string(item["target_id"], "anchor target_id"),
        related_blocks=tuple(
            anchor_block_from_document(raw)
            for raw in require_list(
                item["related_blocks"], "anchor related_blocks"
            )
        ),
    )


def fragment_appearance_to_document(
    appearance: FragmentAppearance,
) -> dict[str, str]:
    return {
        "foreground": appearance.foreground,
        "background": appearance.background,
    }


def fragment_appearance_from_document(value: Any) -> FragmentAppearance:
    item = require_exact(value, _APPEARANCE_FIELDS, "fragment appearance")
    return FragmentAppearance(
        foreground=require_string(item["foreground"], "appearance foreground"),
        background=require_string(item["background"], "appearance background"),
    )


def fragment_revision_to_document(
    revision: FragmentRevision,
) -> dict[str, Any]:
    """Encode front matter metadata; Markdown remains outside the JSON."""

    value = {
        "schema_version": revision.schema_version,
        "source": source_identity_to_document(revision.source),
        "fragment_id": revision.fragment_id,
        "revision": revision.revision,
        "parent_semantic_digest": revision.parent_semantic_digest,
        "anchor": fragment_anchor_to_document(revision.anchor),
        "priority": revision.priority,
        "role": revision.role,
        "language": revision.language,
        "title": revision.title,
        "citation_ids": list(revision.citation_ids),
        "provenance": thaw_json(revision.provenance),
    }
    if revision.schema_version in {
        FRAGMENT_REVISION_SCHEMA_V2,
        FRAGMENT_REVISION_SCHEMA,
    }:
        value["appearance"] = (
            None
            if revision.appearance is None
            else fragment_appearance_to_document(revision.appearance)
        )
    if revision.schema_version == FRAGMENT_REVISION_SCHEMA:
        value["deleted"] = revision.deleted
    return value


def fragment_revision_from_document(
    value: Any, markdown_body: str
) -> FragmentRevision:
    if not isinstance(value, Mapping):
        raise ValueError("fragment revision must be an object")
    schema_version = value.get("schema_version")
    if schema_version == FRAGMENT_REVISION_SCHEMA_V1:
        item = require_exact(value, _FRAGMENT_V1_FIELDS, "fragment revision")
        appearance = None
        deleted = False
    elif schema_version == FRAGMENT_REVISION_SCHEMA_V2:
        item = require_exact(value, _FRAGMENT_V2_FIELDS, "fragment revision")
        raw_appearance = item["appearance"]
        appearance = (
            None
            if raw_appearance is None
            else fragment_appearance_from_document(raw_appearance)
        )
        deleted = False
    elif schema_version == FRAGMENT_REVISION_SCHEMA:
        item = require_exact(value, _FRAGMENT_V3_FIELDS, "fragment revision")
        raw_appearance = item["appearance"]
        appearance = (
            None
            if raw_appearance is None
            else fragment_appearance_from_document(raw_appearance)
        )
        deleted = item["deleted"]
        if not isinstance(deleted, bool):
            raise ValueError("deleted must be a boolean")
    else:
        raise ValueError("unsupported fragment revision schema")
    parent = item["parent_semantic_digest"]
    if parent is not None and not isinstance(parent, str):
        raise ValueError("parent_semantic_digest must be a string or null")
    title = item["title"]
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string or null")
    citation_ids = tuple(
        require_string(raw, "citation ID")
        for raw in require_list(item["citation_ids"], "citation_ids")
    )
    provenance = item["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError("provenance must be an object")
    return FragmentRevision(
        source=source_identity_from_document(item["source"]),
        fragment_id=require_string(item["fragment_id"], "fragment_id"),
        revision=require_integer(item["revision"], "revision", minimum=1),
        parent_semantic_digest=parent,
        anchor=fragment_anchor_from_document(item["anchor"]),
        priority=require_integer(item["priority"], "priority", minimum=1),
        role=require_string(item["role"], "role"),
        language=require_string(item["language"], "language"),
        title=title,
        citation_ids=citation_ids,
        provenance=provenance,
        markdown_body=markdown_body,
        schema_version=str(schema_version),
        appearance=appearance,
        deleted=deleted,
    )


def fragment_revision_ref_to_document(
    reference: FragmentRevisionRef,
) -> dict[str, Any]:
    return {
        "path": reference.path,
        "fragment_id": reference.fragment_id,
        "revision": reference.revision,
        "semantic_digest": reference.semantic_digest,
    }


def fragment_revision_ref_from_document(value: Any) -> FragmentRevisionRef:
    item = require_exact(value, _REVISION_REF_FIELDS, "fragment revision ref")
    return FragmentRevisionRef(
        path=require_string(item["path"], "revision path"),
        fragment_id=require_string(item["fragment_id"], "fragment_id"),
        revision=require_integer(item["revision"], "revision", minimum=1),
        semantic_digest=require_string(
            item["semantic_digest"], "semantic_digest"
        ),
    )


def _layer_material(layer: Layer) -> dict[str, Any]:
    return {
        "schema_version": layer.schema_version,
        "source": source_identity_to_document(layer.source),
        "producer": layer.producer,
        "initial_revisions": [
            fragment_revision_ref_to_document(item)
            for item in layer.initial_revisions
        ],
    }


def layer_semantic_digest(layer: Layer) -> str:
    return hashlib.sha256(canonical_json_bytes(_layer_material(layer))).hexdigest()


def layer_to_document(layer: Layer) -> dict[str, Any]:
    return {**_layer_material(layer), "layer_digest": layer.layer_digest}


def layer_from_document(value: Any) -> Layer:
    item = require_exact(value, _LAYER_FIELDS, "layer")
    if item["schema_version"] != LAYER_SCHEMA:
        raise ValueError("unsupported layer schema")
    layer = Layer(
        source=source_identity_from_document(item["source"]),
        producer=require_string(item["producer"], "producer"),
        initial_revisions=tuple(
            fragment_revision_ref_from_document(raw)
            for raw in require_list(
                item["initial_revisions"], "initial_revisions"
            )
        ),
    )
    if require_string(item["layer_digest"], "layer_digest") != layer.layer_digest:
        raise ValueError("layer digest does not match its content")
    return layer


def layer_ref_to_document(reference: LayerRef) -> dict[str, Any]:
    return {
        "source": source_identity_to_document(reference.source),
        "producer": reference.producer,
        "path": reference.path,
        "layer_digest": reference.layer_digest,
    }


def layer_ref_from_document(value: Any) -> LayerRef:
    item = require_exact(value, _LAYER_REF_FIELDS, "layer reference")
    return LayerRef(
        source=source_identity_from_document(item["source"]),
        producer=require_string(item["producer"], "producer"),
        path=require_string(item["path"], "layer path"),
        layer_digest=require_string(item["layer_digest"], "layer_digest"),
    )


def publication_outline_item_to_document(
    item: PublicationOutlineItem,
) -> dict[str, Any]:
    return {
        "section_id": item.section_id,
        "title": item.title,
        "level": item.level,
        "ordinal": item.ordinal,
        "path": list(item.path),
        "block_start": item.block_start,
        "block_end": item.block_end,
        "anchor_block_id": item.anchor_block_id,
    }


def publication_outline_item_from_document(
    value: Any,
) -> PublicationOutlineItem:
    item = require_exact(
        value,
        _PUBLICATION_OUTLINE_ITEM_FIELDS,
        "publication outline item",
    )
    return PublicationOutlineItem(
        section_id=require_string(item["section_id"], "outline section_id"),
        title=require_string(item["title"], "outline title", empty=True),
        level=require_integer(item["level"], "outline level", minimum=1),
        ordinal=require_integer(
            item["ordinal"], "outline ordinal", minimum=0
        ),
        path=tuple(
            require_string(raw, "outline path item")
            for raw in require_list(item["path"], "outline path")
        ),
        block_start=require_integer(
            item["block_start"], "outline block_start", minimum=0
        ),
        block_end=require_integer(
            item["block_end"], "outline block_end", minimum=1
        ),
        anchor_block_id=require_string(
            item["anchor_block_id"], "outline anchor_block_id"
        ),
    )


def _publication_material(publication: Publication) -> dict[str, Any]:
    return {
        "schema_version": publication.schema_version,
        "source_document": rich_document_to_document(
            publication.source_document
        ),
        "outline": [
            publication_outline_item_to_document(item)
            for item in publication.outline or ()
        ],
        "layers": [
            layer_ref_to_document(item) for item in publication.layers
        ],
        "glossary": thaw_json(publication.glossary),
        "bibliography": thaw_json(publication.bibliography),
        "labels": dict(publication.labels),
        "resources": thaw_json(publication.resources),
        "reader_profile": thaw_json(publication.reader_profile),
    }


def publication_semantic_digest(publication: Publication) -> str:
    return hashlib.sha256(
        canonical_json_bytes(_publication_material(publication))
    ).hexdigest()


def publication_to_document(publication: Publication) -> dict[str, Any]:
    return {
        **_publication_material(publication),
        "publication_digest": publication.publication_digest,
    }


def publication_from_document(value: Any) -> Publication:
    item = require_exact(value, _PUBLICATION_FIELDS, "publication")
    if item["schema_version"] != PUBLICATION_SCHEMA:
        raise ValueError("unsupported publication schema")
    source_document_value = item["source_document"]
    if not isinstance(source_document_value, Mapping):
        raise ValueError("source_document must be an object")
    labels = item["labels"]
    reader_profile = item["reader_profile"]
    if not isinstance(labels, Mapping):
        raise ValueError("labels must be an object")
    if not isinstance(reader_profile, Mapping):
        raise ValueError("reader_profile must be an object")
    publication = Publication(
        source_document=rich_document_from_document(source_document_value),
        outline=tuple(
            publication_outline_item_from_document(raw)
            for raw in require_list(item["outline"], "outline")
        ),
        layers=tuple(
            layer_ref_from_document(raw)
            for raw in require_list(item["layers"], "layers")
        ),
        glossary=_object_tuple(item["glossary"], "glossary"),
        bibliography=_object_tuple(
            item["bibliography"], "bibliography"
        ),
        labels=dict(labels),
        resources=_object_tuple(item["resources"], "resources"),
        reader_profile=reader_profile,
    )
    claimed = require_string(
        item["publication_digest"], "publication_digest"
    )
    if claimed != publication.publication_digest:
        raise ValueError("publication digest does not match its content")
    return publication


def _object_tuple(value: Any, description: str) -> tuple[Mapping[str, Any], ...]:
    result = []
    for raw in require_list(value, description):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{description} entries must be objects")
        result.append(raw)
    return tuple(result)


def _require_integer_json_numbers(value: Any, description: str) -> None:
    """Keep fragment metadata canonical across Python and browser JSON.

    JSON integer spelling is identical in both runtimes. Floating-point
    spelling is not (for example ``1.0`` versus ``1``), so v1 fragment
    metadata deliberately reserves non-integer numbers instead of allowing
    browser-created revisions with a different semantic digest.
    """

    if isinstance(value, float):
        raise ValueError(f"{description} contains a non-integer number")
    if isinstance(value, Mapping):
        for item in value.values():
            _require_integer_json_numbers(item, description)
    elif isinstance(value, tuple):
        for item in value:
            _require_integer_json_numbers(item, description)


def _hex_color(value: Any, description: str) -> str:
    if not isinstance(value, str) or _HEX_COLOR_RE.fullmatch(value) is None:
        raise ValueError(f"{description} must be a #rrggbb color")
    return value.casefold()


def _digest(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{description} must be a SHA-256 digest")
    digest = value.casefold()
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{description} must be a SHA-256 digest")
    return digest


def _nonblank(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{description} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, description: str) -> str:
    identifier = _nonblank(value, description)
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError(
            f"{description} must be a portable identifier"
        )
    return identifier


def _positive_integer(
    value: Any, description: str, *, allow_zero: bool = False
) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{description} must be a {qualifier} integer")
    return value


def _relative_path(value: Any, description: str) -> str:
    path = _nonblank(value, description)
    if "\\" in path:
        raise ValueError(f"{description} must use POSIX separators")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        raise ValueError(f"{description} must be a normalized relative path")
    return path


__all__ = [
    "FRAGMENT_REVISION_SCHEMA",
    "FRAGMENT_REVISION_SCHEMA_V1",
    "FRAGMENT_REVISION_SCHEMA_V2",
    "LAYER_SCHEMA",
    "PUBLICATION_SCHEMA",
    "AnchorBlock",
    "AnchorKind",
    "FragmentAnchor",
    "FragmentAppearance",
    "FragmentRevision",
    "FragmentRevisionRef",
    "Layer",
    "LayerRef",
    "Publication",
    "PublicationOutlineItem",
    "SourceIdentity",
    "anchor_block_from_rich_block",
    "anchor_block_from_document",
    "anchor_block_to_document",
    "fragment_anchor_from_document",
    "fragment_anchor_to_document",
    "fragment_appearance_from_document",
    "fragment_appearance_to_document",
    "fragment_revision_from_document",
    "fragment_revision_ref_from_document",
    "fragment_revision_ref_to_document",
    "fragment_revision_to_document",
    "layer_from_document",
    "layer_ref_from_document",
    "layer_ref_to_document",
    "layer_semantic_digest",
    "layer_to_document",
    "publication_from_document",
    "publication_outline_item_from_document",
    "publication_outline_item_to_document",
    "publication_semantic_digest",
    "publication_to_document",
    "source_identity_from_document",
    "source_identity_from_rich_document",
    "source_identity_to_document",
]
