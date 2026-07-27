"""Deterministic, text-only source inputs for Companion model workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from arc_paper import RichBlock, RichBlockKind, RichDocument

from .source_planning import SourceChapter, equation_label_provenance


MODEL_SOURCE_INDEX_SCHEMA = "arc.companion.model_source_index.v2"


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
    "model_block_access_index",
    "model_chapter_block_index",
    "model_source_index",
    "model_source_view",
    "validate_model_source_index",
]
