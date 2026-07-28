"""Deterministic, text-only source inputs for Companion model workers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from arc_paper import RichBlock, RichBlockKind, RichDocument

from .source_planning import SourceChapter, equation_label_provenance


MODEL_SOURCE_INDEX_SCHEMA = "arc.companion.model_source_index.v2"
MODEL_TRANSLATION_INDEX_SCHEMA = (
    "arc.companion.model_translation_index.v1"
)


def model_source_view(
    document: RichDocument,
    chapters: Sequence[SourceChapter],
) -> str:
    """Render every rich block as searchable Markdown without source assets."""

    by_id = {item.block_id: item for item in document.blocks}
    parts: list[str] = [
        "<!-- ARC model source: text only; referenced media are not inputs. -->"
    ]
    for chapter in chapters:
        parts.append(f"<!-- ARC_CHAPTER id={chapter.chapter_id} -->")
        for block_id in chapter.block_ids:
            block = by_id[block_id]
            parts.append(
                "<!-- ARC_BLOCK "
                f"id={block.block_id} ordinal={block.ordinal} "
                f"kind={block.kind.value} -->"
            )
            provenance = equation_label_provenance(
                document, block.block_id
            )
            effective_label = (
                provenance.get("effective_label")
                if provenance is not None
                else None
            )
            parts.append(
                _block_markdown(
                    block,
                    equation_label=(
                        effective_label
                        if isinstance(effective_label, str)
                        else None
                    ),
                )
            )
    return "\n\n".join(parts).rstrip() + "\n"


def model_source_index(
    document: RichDocument,
    chapters: Sequence[SourceChapter],
    *,
    cache_document: Mapping[str, Any] | None,
    cache_relationship: str,
) -> dict[str, Any]:
    """Build a compact body-free source index for cache and fallback access."""

    if cache_relationship not in {
        "exact",
        "equation_label_overlay",
        "fallback_only",
    }:
        raise ValueError("unsupported model source cache relationship")
    chapter_by_block: dict[str, str] = {}
    for chapter in chapters:
        for block_id in chapter.block_ids:
            if block_id in chapter_by_block:
                raise ValueError("model source block belongs to multiple chapters")
            chapter_by_block[block_id] = chapter.chapter_id
    if tuple(chapter_by_block) != tuple(
        item.block_id for item in document.blocks
    ):
        raise ValueError("model source chapters do not exactly cover the document")

    return {
        "schema_version": MODEL_SOURCE_INDEX_SCHEMA,
        "source": {
            "source_format": document.source.source_format.value,
            "source_sha256": document.source.artifact_digest,
            "source_size": document.source.size,
            "media_type": document.source.media_type,
        },
        "effective_document_sha256": document.document_digest,
        "cache_relationship": cache_relationship,
        "cached_document": (
            dict(cache_document) if cache_document is not None else None
        ),
        "cache_operations": (
            {
                "table_of_contents": (
                    "arc-paper get-cached-table-of-contents"
                ),
                "section": "arc-paper get-cached-section",
                "source_range": "arc-paper read-cached-source-range",
                "search": "arc-paper search-cached-document",
            }
            if cache_document is not None
            else {}
        ),
        "chapter_count": len(chapters),
        "block_count": len(document.blocks),
    }


def model_translation_view(
    chapters: Sequence[SourceChapter],
    translations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """Render frozen translations as text with deterministic part ranges."""

    lines = [
        "<!-- ARC model translation: text only; referenced media are not inputs. -->"
    ]
    access_by_chapter: dict[str, list[dict[str, Any]]] = {}
    for chapter in chapters:
        chapter_translations = translations.get(chapter.chapter_id)
        if chapter_translations is None:
            raise ValueError(
                f"model translation is missing chapter: {chapter.chapter_id}"
            )
        translated_ids = [
            str(item.get("block_id")) for item in chapter_translations
        ]
        if translated_ids != list(chapter.block_ids):
            raise ValueError(
                "model translation block order differs from source chapter"
            )
        lines.extend(
            (
                "",
                f"<!-- ARC_CHAPTER id={chapter.chapter_id} -->",
            )
        )
        chapter_access: list[dict[str, Any]] = []
        for part_number, item in enumerate(chapter_translations, 1):
            block_id = str(item["block_id"])
            text = str(item["text"]).strip()
            lines.extend(
                (
                    "",
                    (
                        "<!-- ARC_TRANSLATED_BLOCK "
                        f"id={block_id} part={part_number} -->"
                    ),
                )
            )
            line_start = len(lines) + 1
            translated_lines = text.splitlines() or [""]
            lines.extend(translated_lines)
            chapter_access.append(
                {
                    "block_id": block_id,
                    "part_number": part_number,
                    "line_start": line_start,
                    "line_end": len(lines),
                }
            )
        access_by_chapter[chapter.chapter_id] = chapter_access
    return "\n".join(lines).rstrip() + "\n", access_by_chapter


def model_translation_index(
    view: str,
    chapters: Sequence[SourceChapter],
    access_by_chapter: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    source_document_sha256: str,
    target_language: str,
    cached_document: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a body-free locator index for one frozen translation view."""

    chapter_access = []
    for chapter in chapters:
        access = access_by_chapter.get(chapter.chapter_id)
        if access is None:
            raise ValueError(
                f"model translation index is missing chapter: {chapter.chapter_id}"
            )
        values = [dict(item) for item in access]
        if [item.get("block_id") for item in values] != list(
            chapter.block_ids
        ):
            raise ValueError(
                "model translation index block order differs from source chapter"
            )
        chapter_access.append(
            {
                "chapter_id": chapter.chapter_id,
                "parts": values,
            }
        )
    return {
        "schema_version": MODEL_TRANSLATION_INDEX_SCHEMA,
        "source_document_sha256": source_document_sha256,
        "target_language": target_language,
        "translation_view_sha256": hashlib.sha256(
            view.encode("utf-8")
        ).hexdigest(),
        "translation_view_size": len(view.encode("utf-8")),
        "cached_document": dict(cached_document),
        "chapters": chapter_access,
    }


def validate_model_translation_index(
    value: Mapping[str, Any],
    *,
    view: str,
    chapters: Sequence[SourceChapter],
    source_document_sha256: str,
    target_language: str,
) -> None:
    """Validate translation identity and exact chapter/part coverage."""

    if value.get("schema_version") != MODEL_TRANSLATION_INDEX_SCHEMA:
        raise ValueError("unsupported model translation index schema")
    payload = view.encode("utf-8")
    if value.get("translation_view_sha256") != hashlib.sha256(
        payload
    ).hexdigest() or value.get("translation_view_size") != len(payload):
        raise ValueError("model translation index view identity differs")
    if value.get("source_document_sha256") != source_document_sha256:
        raise ValueError("model translation index source identity differs")
    if value.get("target_language") != target_language:
        raise ValueError("model translation index target language differs")
    cached = value.get("cached_document")
    if not isinstance(cached, Mapping):
        raise ValueError("model translation index has no cached document")
    chapter_values = value.get("chapters")
    if not isinstance(chapter_values, Sequence) or isinstance(
        chapter_values, (str, bytes)
    ):
        raise ValueError("model translation index chapters are invalid")
    if any(not isinstance(item, Mapping) for item in chapter_values):
        raise ValueError("model translation index chapter is invalid")
    if [item.get("chapter_id") for item in chapter_values] != [
        chapter.chapter_id for chapter in chapters
    ]:
        raise ValueError("model translation index chapter order differs")
    for chapter, item in zip(chapters, chapter_values, strict=True):
        parts = item.get("parts")
        if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
            raise ValueError("model translation index parts are invalid")
        if any(not isinstance(part, Mapping) for part in parts):
            raise ValueError("model translation index part is invalid")
        if [part.get("block_id") for part in parts] != list(
            chapter.block_ids
        ):
            raise ValueError(
                "model translation index block coverage differs"
            )
        for part_number, part in enumerate(parts, 1):
            if (
                part.get("part_number") != part_number
                or not isinstance(part.get("line_start"), int)
                or not isinstance(part.get("line_end"), int)
                or int(part["line_start"]) < 1
                or int(part["line_end"]) < int(part["line_start"])
            ):
                raise ValueError(
                    "model translation index part range is invalid"
                )


def model_chapter_block_index(
    document: RichDocument,
    chapter: SourceChapter,
) -> list[dict[str, Any]]:
    """Return locator metadata only for the chapter handled by one model task."""

    return model_block_access_index(document, chapter.block_ids)


def model_block_access_index(
    document: RichDocument,
    block_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Return locator metadata for one bounded model task."""

    by_id = {item.block_id: item for item in document.blocks}
    output: list[dict[str, Any]] = []
    for block_id in block_ids:
        block = by_id[block_id]
        provenance = equation_label_provenance(document, block.block_id)
        output.append(
            {
                "block_id": block.block_id,
                "ordinal": block.ordinal,
                "kind": block.kind.value,
                "line_start": block.locator.line_start,
                "line_end": block.locator.line_end,
                "selector": block.locator.selector,
                "equation_label": (
                    str(
                        (
                            provenance.get("effective_label")
                            if provenance is not None
                            else None
                        )
                        or block.payload["label"]
                    )
                    if block.kind is RichBlockKind.EQUATION
                    else None
                ),
            }
        )
    return output


def validate_model_source_index(
    value: Mapping[str, Any],
    *,
    document: RichDocument,
    chapters: Sequence[SourceChapter],
) -> None:
    """Validate identity and exact block coverage of a frozen model index."""

    if value.get("schema_version") != MODEL_SOURCE_INDEX_SCHEMA:
        raise ValueError("unsupported model source index schema")
    source = value.get("source")
    if not isinstance(source, Mapping) or dict(source) != {
        "source_format": document.source.source_format.value,
        "source_sha256": document.source.artifact_digest,
        "source_size": document.source.size,
        "media_type": document.source.media_type,
    }:
        raise ValueError("model source index source identity differs")
    if value.get("effective_document_sha256") != document.document_digest:
        raise ValueError("model source index effective document differs")
    if value.get("chapter_count") != len(chapters):
        raise ValueError("model source index chapter count differs")
    if value.get("block_count") != len(document.blocks):
        raise ValueError("model source index block count differs")


def _block_markdown(
    block: RichBlock,
    *,
    equation_label: str | None = None,
) -> str:
    payload = block.payload
    if block.kind is RichBlockKind.HEADING:
        level = min(6, max(1, int(payload["level"])))
        return f"{'#' * level} {str(payload['text']).strip()}"
    if block.kind is RichBlockKind.PARAGRAPH:
        return str(payload["text"]).strip()
    if block.kind is RichBlockKind.LIST:
        ordered = bool(payload["ordered"])
        return "\n".join(
            (
                f"{index}. " if ordered else "- "
            )
            + str(item["text"]).strip()
            for index, item in enumerate(payload["items"], 1)
        )
    if block.kind is RichBlockKind.CODE:
        text = str(payload["text"]).rstrip()
        fence = "`" * max(3, _longest_run(text, "`") + 1)
        language = str(payload["language"]).strip()
        return f"{fence}{language}\n{text}\n{fence}"
    if block.kind is RichBlockKind.EQUATION:
        text = f"$$\n{str(payload['tex']).strip()}\n$$"
        label = (
            equation_label
            if equation_label is not None
            else str(payload["label"])
        ).strip()
        return text if not label else f"{text}\n\nEquation label: {label}"
    if block.kind is RichBlockKind.TABLE:
        headers = [str(item) for item in payload["headers"]]
        rows = [[str(cell) for cell in row] for row in payload["rows"]]
        width = len(headers) or (len(rows[0]) if rows else 0)
        table: list[str] = []
        if width:
            normalized_headers = headers or [""] * width
            table.extend(
                (
                    "| " + " | ".join(_table_cell(item) for item in normalized_headers) + " |",
                    "| " + " | ".join("---" for _ in range(width)) + " |",
                )
            )
            table.extend(
                "| " + " | ".join(_table_cell(cell) for cell in row) + " |"
                for row in rows
            )
        caption = str(payload["caption"]).strip()
        if caption:
            table.append(f"Table caption: {caption}")
        return "\n".join(table) or "[Empty table]"
    if block.kind is RichBlockKind.FIGURE:
        description = (
            str(payload["caption"]).strip()
            or str(payload["alt_text"]).strip()
        )
        return (
            f"[Figure: {description}]"
            if description
            else "[Figure omitted from the text-only model source.]"
        )
    raise ValueError(f"unsupported rich block kind: {block.kind.value}")


def _table_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _longest_run(value: str, character: str) -> int:
    longest = current = 0
    for item in value:
        if item == character:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


__all__ = [
    "MODEL_SOURCE_INDEX_SCHEMA",
    "MODEL_TRANSLATION_INDEX_SCHEMA",
    "model_block_access_index",
    "model_chapter_block_index",
    "model_source_index",
    "model_source_view",
    "model_translation_index",
    "model_translation_view",
    "validate_model_source_index",
    "validate_model_translation_index",
]
