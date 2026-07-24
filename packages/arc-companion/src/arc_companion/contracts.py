"""Typed, renderable content contracts for source-anchored companions.

These objects contain book content only. Runtime diagnostics, provider
configuration, cache locations, and job state belong in execution artifacts
outside this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any


ACCEPTED_BOOK_SCHEMA = "arc.companion.accepted_book.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BLOCK_KINDS = {
    "heading",
    "paragraph",
    "list",
    "code",
    "equation",
    "table",
    "figure",
}
_LEARNING_KINDS = {
    "prerequisite",
    "intuition",
    "derivation",
    "example",
    "misconception",
    "further_reading",
}


class ContentCodecError(ValueError):
    """An accepted-content document is malformed or not canonical."""


@dataclass(frozen=True)
class SourceAnchor:
    """One immutable source block snapshot used as a rendering anchor."""

    block_id: str
    ordinal: int
    kind: str
    section_path: tuple[str, ...]
    payload: Mapping[str, Any]
    locator: Mapping[str, Any] = field(default_factory=dict)
    page_number: int | None = None

    def __post_init__(self) -> None:
        if not self.block_id or self.ordinal < 0:
            raise ValueError("source anchor identity is invalid")
        if self.kind not in _BLOCK_KINDS:
            raise ValueError(f"unsupported source block kind: {self.kind}")
        if any(not item for item in self.section_path):
            raise ValueError("source anchor section path contains an empty ID")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("source anchor page number must be positive")
        object.__setattr__(self, "section_path", tuple(self.section_path))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))
        object.__setattr__(self, "locator", _freeze_mapping(self.locator))

    @classmethod
    def from_rich_block(
        cls, block: Any, *, page_number: int | None = None
    ) -> SourceAnchor:
        """Snapshot a public ``arc_paper.RichBlock`` without deep imports."""

        locator = block.locator
        return cls(
            block_id=block.block_id,
            ordinal=block.ordinal,
            kind=getattr(block.kind, "value", str(block.kind)),
            section_path=tuple(block.section_path),
            payload=block.payload,
            locator={
                "source_format": getattr(
                    locator.source_format, "value", str(locator.source_format)
                ),
                "line_start": locator.line_start,
                "column_start": locator.column_start,
                "line_end": locator.line_end,
                "column_end": locator.column_end,
                "selector": locator.selector,
                "source_id": locator.source_id,
            },
            page_number=page_number,
        )


@dataclass(frozen=True)
class PlannedLearningUnit:
    unit_id: str
    kind: str
    title: str
    anchor_ids: tuple[str, ...]
    purpose: str

    def __post_init__(self) -> None:
        if not self.unit_id or self.kind not in _LEARNING_KINDS:
            raise ValueError("planned learning unit identity is invalid")
        if not self.title or not self.purpose or not self.anchor_ids:
            raise ValueError("planned learning unit content is incomplete")
        if any(not item for item in self.anchor_ids):
            raise ValueError("planned learning unit contains an empty anchor")
        object.__setattr__(self, "anchor_ids", tuple(self.anchor_ids))


@dataclass(frozen=True)
class ChapterPlan:
    chapter_id: str
    title: str
    block_ids: tuple[str, ...]
    guide: str
    learning_units: tuple[PlannedLearningUnit, ...] = ()
    glossary_candidates: tuple[str, ...] = ()
    evidence_requests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.chapter_id or not self.title or not self.block_ids:
            raise ValueError("chapter plan identity and coverage are required")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("chapter plan contains duplicate block IDs")
        object.__setattr__(self, "block_ids", tuple(self.block_ids))
        object.__setattr__(self, "learning_units", tuple(self.learning_units))
        object.__setattr__(
            self, "glossary_candidates", tuple(self.glossary_candidates)
        )
        object.__setattr__(self, "evidence_requests", tuple(self.evidence_requests))


@dataclass(frozen=True)
class GlossaryEntry:
    entry_id: str
    term: str
    translated_term: str
    definition: str
    anchor_ids: tuple[str, ...]
    citations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.entry_id or not self.term or not self.definition:
            raise ValueError("glossary entry is incomplete")
        if not self.anchor_ids or any(not item for item in self.anchor_ids):
            raise ValueError("glossary entry requires source anchors")
        object.__setattr__(self, "anchor_ids", tuple(self.anchor_ids))
        object.__setattr__(self, "citations", tuple(self.citations))


@dataclass(frozen=True)
class TranslatedBlock:
    block_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.block_id:
            raise ValueError("translated block ID is required")
        if not isinstance(self.text, str):
            raise ValueError("translated block text must be a string")


@dataclass(frozen=True)
class LearningUnit:
    unit_id: str
    kind: str
    title: str
    anchor_ids: tuple[str, ...]
    content: str
    citations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.unit_id or self.kind not in _LEARNING_KINDS:
            raise ValueError("learning unit identity is invalid")
        if not self.title or not self.content or not self.anchor_ids:
            raise ValueError("learning unit content is incomplete")
        if any(not item for item in self.anchor_ids):
            raise ValueError("learning unit contains an empty anchor")
        object.__setattr__(self, "anchor_ids", tuple(self.anchor_ids))
        object.__setattr__(self, "citations", tuple(self.citations))


@dataclass(frozen=True)
class AcceptedChapter:
    chapter_id: str
    title: str
    guide: str
    source_anchors: tuple[SourceAnchor, ...]
    translations: tuple[TranslatedBlock, ...] = ()
    learning_units: tuple[LearningUnit, ...] = ()

    def __post_init__(self) -> None:
        if not self.chapter_id or not self.title or not self.source_anchors:
            raise ValueError("accepted chapter identity and source are required")
        object.__setattr__(self, "source_anchors", tuple(self.source_anchors))
        object.__setattr__(self, "translations", tuple(self.translations))
        object.__setattr__(self, "learning_units", tuple(self.learning_units))


@dataclass(frozen=True)
class AcceptedBook:
    document_digest: str
    title: str
    source_language: str
    target_language: str
    translation_mode: str
    chapters: tuple[AcceptedChapter, ...]
    glossary: tuple[GlossaryEntry, ...] = ()
    schema_version: str = ACCEPTED_BOOK_SCHEMA

    def __post_init__(self) -> None:
        digest = self.document_digest.casefold()
        if self.schema_version != ACCEPTED_BOOK_SCHEMA:
            raise ValueError("unsupported accepted book schema")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("accepted book document digest must be SHA-256")
        if (
            not self.title
            or not self.source_language
            or not self.target_language
            or not self.chapters
        ):
            raise ValueError("accepted book metadata and chapters are required")
        if self.translation_mode not in {"enabled", "skipped"}:
            raise ValueError("translation mode must be enabled or skipped")
        object.__setattr__(self, "document_digest", digest)
        object.__setattr__(self, "chapters", tuple(self.chapters))
        object.__setattr__(self, "glossary", tuple(self.glossary))

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(
            CompanionContentCodec.dumps(self).encode("utf-8")
        ).hexdigest()


class CompanionContentCodec:
    """Strict canonical JSON codec for immutable accepted books."""

    @staticmethod
    def to_document(book: AcceptedBook) -> dict[str, Any]:
        return _book_to_document(book)

    @staticmethod
    def from_document(value: Mapping[str, Any]) -> AcceptedBook:
        try:
            _require_json_document(value, "accepted book")
            return _book_from_document(_mapping(value, "accepted book"))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ContentCodecError):
                raise
            raise ContentCodecError(str(exc)) from exc

    @staticmethod
    def dumps(book: AcceptedBook) -> str:
        return json.dumps(
            _book_to_document(book),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def loads(value: str | bytes) -> AcceptedBook:
        try:
            document = json.loads(value)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentCodecError("accepted book is not valid JSON") from exc
        if not isinstance(document, Mapping):
            raise ContentCodecError("accepted book must be a JSON object")
        return CompanionContentCodec.from_document(document)

    @staticmethod
    def plan_to_document(plan: ChapterPlan) -> dict[str, Any]:
        return _plan_to_document(plan)

    @staticmethod
    def plan_from_document(value: Mapping[str, Any]) -> ChapterPlan:
        try:
            _require_json_document(value, "chapter plan")
            return _plan_from_document(_mapping(value, "chapter plan"))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ContentCodecError):
                raise
            raise ContentCodecError(str(exc)) from exc

    @staticmethod
    def dumps_chapter_plan(plan: ChapterPlan) -> str:
        return json.dumps(
            _plan_to_document(plan),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def loads_chapter_plan(value: str | bytes) -> ChapterPlan:
        try:
            document = json.loads(value)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentCodecError("chapter plan is not valid JSON") from exc
        if not isinstance(document, Mapping):
            raise ContentCodecError("chapter plan must be a JSON object")
        return CompanionContentCodec.plan_from_document(document)


def _book_to_document(book: AcceptedBook) -> dict[str, Any]:
    return {
        "schema_version": book.schema_version,
        "document_digest": book.document_digest,
        "title": book.title,
        "source_language": book.source_language,
        "target_language": book.target_language,
        "translation_mode": book.translation_mode,
        "chapters": [_chapter_to_document(item) for item in book.chapters],
        "glossary": [_glossary_to_document(item) for item in book.glossary],
    }


def _plan_to_document(plan: ChapterPlan) -> dict[str, Any]:
    return {
        "chapter_id": plan.chapter_id,
        "title": plan.title,
        "block_ids": list(plan.block_ids),
        "guide": plan.guide,
        "learning_units": [
            {
                "unit_id": item.unit_id,
                "kind": item.kind,
                "title": item.title,
                "anchor_ids": list(item.anchor_ids),
                "purpose": item.purpose,
            }
            for item in plan.learning_units
        ],
        "glossary_candidates": list(plan.glossary_candidates),
        "evidence_requests": list(plan.evidence_requests),
    }


def _plan_from_document(value: Mapping[str, Any]) -> ChapterPlan:
    _fields(
        value,
        {
            "chapter_id",
            "title",
            "block_ids",
            "guide",
            "learning_units",
            "glossary_candidates",
            "evidence_requests",
        },
        "chapter plan",
    )
    learning_units = []
    for raw in _sequence(value["learning_units"], "planned learning units"):
        item = _mapping(raw, "planned learning unit")
        _fields(
            item,
            {"unit_id", "kind", "title", "anchor_ids", "purpose"},
            "planned learning unit",
        )
        learning_units.append(
            PlannedLearningUnit(
                unit_id=_string(item["unit_id"], "planned learning unit unit_id"),
                kind=_string(item["kind"], "planned learning unit kind"),
                title=_string(item["title"], "planned learning unit title"),
                anchor_ids=_strings(
                    item["anchor_ids"], "planned learning unit anchors"
                ),
                purpose=_string(item["purpose"], "planned learning unit purpose"),
            )
        )
    return ChapterPlan(
        chapter_id=_string(value["chapter_id"], "chapter plan chapter_id"),
        title=_string(value["title"], "chapter plan title"),
        block_ids=_strings(value["block_ids"], "chapter plan block_ids"),
        guide=_string(value["guide"], "chapter plan guide"),
        learning_units=tuple(learning_units),
        glossary_candidates=_strings(
            value["glossary_candidates"], "chapter plan glossary_candidates"
        ),
        evidence_requests=_strings(
            value["evidence_requests"], "chapter plan evidence_requests"
        ),
    )


def _chapter_to_document(chapter: AcceptedChapter) -> dict[str, Any]:
    return {
        "chapter_id": chapter.chapter_id,
        "title": chapter.title,
        "guide": chapter.guide,
        "source_anchors": [_anchor_to_document(item) for item in chapter.source_anchors],
        "translations": [
            {"block_id": item.block_id, "text": item.text}
            for item in chapter.translations
        ],
        "learning_units": [_learning_to_document(item) for item in chapter.learning_units],
    }


def _anchor_to_document(anchor: SourceAnchor) -> dict[str, Any]:
    return {
        "block_id": anchor.block_id,
        "ordinal": anchor.ordinal,
        "kind": anchor.kind,
        "section_path": list(anchor.section_path),
        "payload": _thaw_json(anchor.payload),
        "locator": _thaw_json(anchor.locator),
        "page_number": anchor.page_number,
    }


def _learning_to_document(unit: LearningUnit) -> dict[str, Any]:
    return {
        "unit_id": unit.unit_id,
        "kind": unit.kind,
        "title": unit.title,
        "anchor_ids": list(unit.anchor_ids),
        "content": unit.content,
        "citations": list(unit.citations),
    }


def _glossary_to_document(entry: GlossaryEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "term": entry.term,
        "translated_term": entry.translated_term,
        "definition": entry.definition,
        "anchor_ids": list(entry.anchor_ids),
        "citations": list(entry.citations),
    }


def _book_from_document(value: Mapping[str, Any]) -> AcceptedBook:
    _fields(
        value,
        {
            "schema_version",
            "document_digest",
            "title",
            "source_language",
            "target_language",
            "translation_mode",
            "chapters",
            "glossary",
        },
        "accepted book",
    )
    chapters = []
    for raw in _sequence(value["chapters"], "accepted book chapters"):
        item = _mapping(raw, "accepted chapter")
        _fields(
            item,
            {
                "chapter_id",
                "title",
                "guide",
                "source_anchors",
                "translations",
                "learning_units",
            },
            "accepted chapter",
        )
        anchors = []
        for anchor_raw in _sequence(item["source_anchors"], "source anchors"):
            anchor = _mapping(anchor_raw, "source anchor")
            _fields(
                anchor,
                {
                    "block_id",
                    "ordinal",
                    "kind",
                    "section_path",
                    "payload",
                    "locator",
                    "page_number",
                },
                "source anchor",
            )
            anchors.append(
                SourceAnchor(
                    block_id=_string(anchor["block_id"], "source anchor block_id"),
                    ordinal=_integer(anchor["ordinal"], "source anchor ordinal"),
                    kind=_string(anchor["kind"], "source anchor kind"),
                    section_path=_strings(anchor["section_path"], "section_path"),
                    payload=_mapping(anchor["payload"], "source anchor payload"),
                    locator=_mapping(anchor["locator"], "source anchor locator"),
                    page_number=_optional_integer(
                        anchor["page_number"], "source anchor page_number"
                    ),
                )
            )
        translations = []
        for translation_raw in _sequence(item["translations"], "translations"):
            translation = _mapping(translation_raw, "translated block")
            _fields(translation, {"block_id", "text"}, "translated block")
            translations.append(
                TranslatedBlock(
                    block_id=_string(
                        translation["block_id"], "translated block block_id"
                    ),
                    text=_string(translation["text"], "translated block text"),
                )
            )
        learning_units = []
        for unit_raw in _sequence(item["learning_units"], "learning units"):
            unit = _mapping(unit_raw, "learning unit")
            _fields(
                unit,
                {
                    "unit_id",
                    "kind",
                    "title",
                    "anchor_ids",
                    "content",
                    "citations",
                },
                "learning unit",
            )
            learning_units.append(
                LearningUnit(
                    unit_id=_string(unit["unit_id"], "learning unit unit_id"),
                    kind=_string(unit["kind"], "learning unit kind"),
                    title=_string(unit["title"], "learning unit title"),
                    anchor_ids=_strings(unit["anchor_ids"], "learning unit anchors"),
                    content=_string(unit["content"], "learning unit content"),
                    citations=_strings(unit["citations"], "learning unit citations"),
                )
            )
        chapters.append(
            AcceptedChapter(
                chapter_id=_string(item["chapter_id"], "accepted chapter chapter_id"),
                title=_string(item["title"], "accepted chapter title"),
                guide=_string(item["guide"], "accepted chapter guide"),
                source_anchors=tuple(anchors),
                translations=tuple(translations),
                learning_units=tuple(learning_units),
            )
        )
    glossary = []
    for raw in _sequence(value["glossary"], "accepted book glossary"):
        item = _mapping(raw, "glossary entry")
        _fields(
            item,
            {
                "entry_id",
                "term",
                "translated_term",
                "definition",
                "anchor_ids",
                "citations",
            },
            "glossary entry",
        )
        glossary.append(
            GlossaryEntry(
                entry_id=_string(item["entry_id"], "glossary entry entry_id"),
                term=_string(item["term"], "glossary entry term"),
                translated_term=_string(
                    item["translated_term"], "glossary entry translated_term"
                ),
                definition=_string(item["definition"], "glossary entry definition"),
                anchor_ids=_strings(item["anchor_ids"], "glossary entry anchors"),
                citations=_strings(item["citations"], "glossary entry citations"),
            )
        )
    return AcceptedBook(
        schema_version=_string(value["schema_version"], "accepted book schema_version"),
        document_digest=_string(
            value["document_digest"], "accepted book document_digest"
        ),
        title=_string(value["title"], "accepted book title"),
        source_language=_string(
            value["source_language"], "accepted book source_language"
        ),
        target_language=_string(
            value["target_language"], "accepted book target_language"
        ),
        translation_mode=_string(
            value["translation_mode"], "accepted book translation_mode"
        ),
        chapters=tuple(chapters),
        glossary=tuple(glossary),
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("content value must be a mapping")
    return MappingProxyType(
        {str(key): _freeze_json(item) for key, item in value.items()}
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("content values must be JSON-compatible")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _fields(
    value: Mapping[str, Any], expected: set[str], description: str
) -> None:
    if set(value) != expected:
        raise ContentCodecError(f"{description} has invalid fields")


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContentCodecError(f"{description} must be an object")
    return value


def _sequence(value: Any, description: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ContentCodecError(f"{description} must be a list")
    return value


def _require_json_document(value: Any, description: str) -> None:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContentCodecError(
                f"{description} object keys must be strings"
            )
        for item in value.values():
            _require_json_document(item, description)
        return
    if isinstance(value, list):
        for item in value:
            _require_json_document(item, description)
        return
    if isinstance(value, tuple):
        raise ContentCodecError(f"{description} arrays must be lists")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContentCodecError(f"{description} numbers must be finite")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ContentCodecError(
            f"{description} must contain JSON-compatible values"
        )


def _strings(value: Any, description: str) -> tuple[str, ...]:
    items = _sequence(value, description)
    if any(not isinstance(item, str) for item in items):
        raise ContentCodecError(f"{description} must contain strings")
    return tuple(items)


def _string(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise ContentCodecError(f"{description} must be a string")
    return value


def _integer(value: Any, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentCodecError(f"{description} must be an integer")
    return value


def _optional_integer(value: Any, description: str) -> int | None:
    if value is None:
        return None
    return _integer(value, description)


__all__ = [
    "ACCEPTED_BOOK_SCHEMA",
    "AcceptedBook",
    "AcceptedChapter",
    "ChapterPlan",
    "CompanionContentCodec",
    "ContentCodecError",
    "GlossaryEntry",
    "LearningUnit",
    "PlannedLearningUnit",
    "SourceAnchor",
    "TranslatedBlock",
]
