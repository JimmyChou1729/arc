"""Source resolution, sampling, and immutable block projections."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_paper import (
    ArcPaperService,
    RichBlock,
    RichBlockKind,
    RichDocumentParserService,
    SourceFormat,
    rich_block_to_document,
)

from .contracts import TranslationSource


STRUCTURAL_FIGURE_PLACEHOLDER = "\ufffc"


class TranslationSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_translation_source(
    paper: ArcPaperService,
    source: str | Path,
    *,
    refresh: bool = False,
) -> TranslationSource:
    """Resolve a local path or paper ID through public ``arc-paper`` APIs."""

    source_text = str(source)
    path = Path(source_text)
    artifact = (
        paper.import_source(path)
        if path.is_file()
        else paper.fetch_arxiv_auto(source_text, refresh=refresh)
    )
    parsed = paper.parser.parse_source(artifact)
    if artifact.source_format is SourceFormat.PDF:
        if not bool(parsed.metadata.get("text_layer")):
            raise TranslationSourceError(
                "pdf_text_layer_missing",
                "PDF source has no extractable text layer",
            )
        if not _parsed_text_values(parsed):
            raise TranslationSourceError(
                "pdf_text_layer_missing",
                "PDF source has no extractable text content",
            )
        return TranslationSource(parsed=parsed)
    rich = RichDocumentParserService(paper.repository).parse_source(artifact)
    if not rich.blocks:
        raise TranslationSourceError(
            "source_content_empty", "source contains no translatable blocks"
        )
    return TranslationSource(parsed=parsed, rich=rich)


def source_blocks(source: TranslationSource) -> tuple[dict[str, Any], ...]:
    """Return exact rich blocks, or deterministic PDF text blocks."""

    if source.rich is not None:
        return tuple(rich_block_to_document(item) for item in source.rich.blocks)
    return _pdf_blocks(source)


def deterministic_language_samples(
    source: TranslationSource,
    *,
    maximum_characters: int = 2400,
) -> tuple[str, ...]:
    """Use stable beginning/middle/end samples from natural-language text."""

    if maximum_characters < 3:
        raise ValueError("maximum_characters must be at least three")
    values = (
        [_rich_block_text(item) for item in source.rich.blocks]
        if source.rich is not None
        else _parsed_text_values(_require_parsed(source))
    )
    joined = "\n\n".join(value for value in values if value.strip())
    if not joined:
        return ("",)
    if len(joined) <= maximum_characters:
        return (joined,)
    width = maximum_characters // 3
    middle = max(0, (len(joined) - width) // 2)
    return (
        joined[:width],
        joined[middle : middle + width],
        joined[-width:],
    )


def same_primary_language(source_tag: str, target_tag: str) -> bool:
    source = _primary_language(source_tag)
    target = _primary_language(target_tag)
    return bool(source and target and source == target)


def source_identity(block: Mapping[str, Any]) -> dict[str, Any]:
    """Project the source structure that translation must preserve exactly."""

    kind = str(block.get("kind"))
    payload = block.get("payload")
    if not isinstance(payload, Mapping):
        raise TranslationSourceError(
            "source_block_invalid", "source block payload must be an object"
        )
    equations: list[str] = []
    links: list[str] = []
    if kind == "equation":
        equations.append(str(payload.get("tex", "")))
    elif kind == "paragraph":
        equations.extend(
            str(item["tex"])
            for item in _mapping_items(payload.get("inline_math"))
            if "tex" in item
        )
        links.extend(
            str(item["target"])
            for item in _mapping_items(payload.get("links"))
            if "target" in item
        )
    elif kind == "list":
        for item in _mapping_items(payload.get("items")):
            equations.extend(
                str(span["tex"])
                for span in _mapping_items(item.get("inline_math"))
                if "tex" in span
            )
            links.extend(
                str(link["target"])
                for link in _mapping_items(item.get("links"))
                if "target" in link
            )
    return {
        "equations": equations,
        "code_text": str(payload["text"]) if kind == "code" else None,
        "link_targets": links,
        "asset_digest": (
            str(payload["asset_digest"])
            if kind == "figure" and payload.get("asset_digest")
            else None
        ),
        "asset_target": (
            str(payload["target"])
            if kind == "figure" and payload.get("target")
            else None
        ),
    }


def validate_translation_text(text: str, block: Mapping[str, Any]) -> None:
    if not isinstance(text, str) or not text.strip():
        raise TranslationSourceError(
            "translation_coverage_invalid",
            f"translation text is empty for {block.get('block_id', '<unknown>')}",
        )
    identity = source_identity(block)
    if identity["code_text"] is not None and text != identity["code_text"]:
        raise TranslationSourceError(
            "translation_source_identity_invalid",
            f"translation changed code text for {block['block_id']}",
        )
    occurrences = Counter(
        [*identity["equations"], *identity["link_targets"]]
    )
    if any(text.count(token) < count for token, count in occurrences.items()):
        raise TranslationSourceError(
            "translation_source_identity_invalid",
            "translation omitted a formula or link occurrence for "
            f"{block['block_id']}",
        )
    if identity["asset_digest"] is not None and not str(
        identity["asset_digest"]
    ).strip():
        raise TranslationSourceError(
            "translation_source_identity_invalid",
            f"source asset identity is invalid for {block['block_id']}",
        )


def prompt_block(block: Mapping[str, Any]) -> dict[str, Any]:
    if str(block.get("kind")) != "figure":
        return {**dict(block), "source_identity": source_identity(block)}
    payload = block.get("payload")
    if not isinstance(payload, Mapping):
        raise TranslationSourceError(
            "source_block_invalid", "source block payload must be an object"
        )
    return {
        "block_id": block.get("block_id"),
        "ordinal": block.get("ordinal"),
        "kind": "figure",
        "section_path": block.get("section_path"),
        "payload": {"caption": str(payload.get("caption", ""))},
        "source_identity": {
            "equations": [],
            "code_text": None,
            "link_targets": [],
        },
    }


def block_text(block: Mapping[str, Any]) -> str:
    """Return only human-visible source text used for literal term matching."""

    kind = str(block.get("kind"))
    payload = block.get("payload")
    if not isinstance(payload, Mapping):
        raise TranslationSourceError(
            "source_block_invalid", "source block payload must be an object"
        )
    if kind in {"heading", "paragraph", "code"}:
        return str(payload.get("text", ""))
    if kind == "equation":
        return str(payload.get("tex", ""))
    if kind == "list":
        return "\n".join(
            str(item.get("text", "")) for item in _mapping_items(payload.get("items"))
        )
    if kind == "table":
        rows = [payload.get("headers", []), *payload.get("rows", [])]
        return "\n".join(
            " | ".join(str(cell) for cell in row)
            for row in rows
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes))
        )
    if kind == "figure":
        return str(payload.get("caption", ""))
    raise TranslationSourceError(
        "source_block_invalid", f"unsupported block kind: {kind}"
    )


def block_digest(blocks: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(blocks))).hexdigest()


def _pdf_blocks(source: TranslationSource) -> tuple[dict[str, Any], ...]:
    parsed = _require_parsed(source)
    blocks: list[dict[str, Any]] = []
    units: list[tuple[str, str, int | None]] = []
    if parsed.sections:
        units.extend(
            (item.section_id, item.text, item.page_start)
            for item in parsed.sections
            if item.text.strip()
        )
    else:
        units.extend(
            (f"page-{item.page_number}", item.text, item.page_number)
            for item in parsed.pages
            if item.text.strip()
        )
    for ordinal, (unit_id, text, page) in enumerate(units):
        identity = {
            "source_digest": source.source_digest,
            "unit_id": unit_id,
            "ordinal": ordinal,
            "text": text,
        }
        digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
        blocks.append(
            {
                "block_id": f"pdf-block-{digest}",
                "ordinal": ordinal,
                "kind": "paragraph",
                "section_path": [unit_id],
                "locator": {
                    "source_format": "pdf",
                    "line_start": None,
                    "column_start": None,
                    "line_end": None,
                    "column_end": None,
                    "selector": f"page:{page}" if page is not None else "",
                    "source_id": unit_id,
                },
                "payload": {
                    "text": text,
                    "links": [],
                    "inline_math": [],
                    "inline_spans": [],
                },
            }
        )
    if not blocks:
        raise TranslationSourceError(
            "pdf_text_layer_missing",
            "PDF source has no extractable text content",
        )
    return tuple(blocks)


def _parsed_text_values(parsed: Any) -> list[str]:
    values = [item.text for item in parsed.sections if item.text.strip()]
    if not values:
        values = [item.text for item in parsed.pages if item.text.strip()]
    return values


def _require_parsed(source: TranslationSource) -> Any:
    if source.parsed is None:
        raise TranslationSourceError(
            "parsed_source_required",
            "this source operation requires a ParsedDocument",
        )
    return source.parsed


def _rich_block_text(block: RichBlock) -> str:
    payload = block.payload
    if block.kind in {RichBlockKind.HEADING, RichBlockKind.PARAGRAPH}:
        return str(payload["text"])
    if block.kind is RichBlockKind.LIST:
        return "\n".join(str(item["text"]) for item in payload["items"])
    if block.kind is RichBlockKind.CODE:
        return str(payload["text"])
    if block.kind is RichBlockKind.EQUATION:
        return str(payload["tex"])
    if block.kind is RichBlockKind.TABLE:
        rows = [payload["headers"], *payload["rows"]]
        return "\n".join(" | ".join(map(str, row)) for row in rows)
    if block.kind is RichBlockKind.FIGURE:
        return str(payload["caption"])
    raise TranslationSourceError(
        "source_block_invalid", f"unsupported block kind: {block.kind}"
    )


def _mapping_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TranslationSourceError(
            "source_block_invalid", "source identity array is invalid"
        )
    if any(not isinstance(item, Mapping) for item in value):
        raise TranslationSourceError(
            "source_block_invalid", "source identity item is invalid"
        )
    return tuple(value)


def _primary_language(tag: str) -> str:
    value = str(tag or "").strip().replace("_", "-").casefold()
    primary = value.split("-", 1)[0]
    if not primary.isalpha() or not 2 <= len(primary) <= 8:
        return ""
    return primary


__all__ = [
    "STRUCTURAL_FIGURE_PLACEHOLDER",
    "TranslationSourceError",
    "block_digest",
    "block_text",
    "deterministic_language_samples",
    "prompt_block",
    "resolve_translation_source",
    "same_primary_language",
    "source_blocks",
    "source_identity",
    "validate_translation_text",
]
