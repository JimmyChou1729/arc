"""Source resolution, sampling, and immutable block projections."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_paper import (
    ArcPaperService,
    ParseError,
    RichBlock,
    RichBlockKind,
    RichDocumentParserService,
    RichDocumentValidationError,
    SourceRepositoryError,
    rich_block_to_document,
)

from .contracts import TranslationSource


STRUCTURAL_FIGURE_PLACEHOLDER = "\ufffc"
_MARKDOWN_MATH = re.compile(
    r"(?P<bracket>(?<!\\)\\\[(?P<bracket_tex>.+?)(?<!\\)\\\])"
    r"|(?P<paren>(?<!\\)\\\((?P<paren_tex>.+?)(?<!\\)\\\))"
    r"|(?P<double>(?<!\\)\$\$(?P<double_tex>.+?)(?<!\\)\$\$)"
    r"|(?P<single>(?<!\\)(?<!\$)\$(?!\$)"
    r"(?P<single_tex>.+?)(?<!\\)\$(?!\$))",
    re.DOTALL,
)
_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)"
)
_LINK_TOKEN_CHARACTER = r"A-Za-z0-9._~:/?#@!$&'*+,;=%-"


class TranslationSourceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


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
        paper.repository.import_path(path)
        if path.is_file()
        else paper.fetch_arxiv_auto(source_text, refresh=refresh)
    )
    try:
        rich = RichDocumentParserService(paper.repository).parse_source(
            artifact
        )
    except (
        ParseError,
        RichDocumentValidationError,
        SourceRepositoryError,
    ) as exc:
        raise TranslationSourceError(
            getattr(exc, "code", "rich_source_required"),
            str(exc),
        ) from exc
    if not rich.blocks:
        raise TranslationSourceError(
            "source_content_empty", "source contains no translatable blocks"
        )
    return TranslationSource(rich)


def source_blocks(source: TranslationSource) -> tuple[dict[str, Any], ...]:
    """Return exact blocks from the source RichDocument."""

    return tuple(rich_block_to_document(item) for item in source.rich.blocks)


def deterministic_language_samples(
    source: TranslationSource,
    *,
    maximum_characters: int = 2400,
) -> tuple[str, ...]:
    """Use stable beginning/middle/end samples from natural-language text."""

    if maximum_characters < 3:
        raise ValueError("maximum_characters must be at least three")
    values = [_rich_block_text(item) for item in source.rich.blocks]
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
        _extend_inline_identity(
            payload.get("inline_spans"),
            equations=equations,
            links=links,
        )
    elif kind == "list":
        for item in _mapping_items(payload.get("items")):
            _extend_inline_identity(
                item.get("inline_spans"),
                equations=equations,
                links=links,
            )
    elif kind in {"heading", "table", "figure"}:
        _extend_markdown_identity(
            block_text(block),
            equations=equations,
            links=links,
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
    expected_equations = Counter(identity["equations"])
    if str(block.get("kind")) == "equation":
        expected_text = next(iter(expected_equations), "")
        if text != expected_text:
            raise TranslationSourceError(
                "translation_source_identity_invalid",
                f"translation changed equation text for {block['block_id']}",
            )
    elif _formula_occurrences(text, expected_equations) != expected_equations:
        raise TranslationSourceError(
            "translation_source_identity_invalid",
            "translation changed formula occurrences for "
            f"{block['block_id']}",
            {
                "formula_diagnostics": list(
                    formula_identity_diagnostics(
                        (block,),
                        ({"block_id": block.get("block_id"), "text": text},),
                    )
                )
            },
        )
    if str(block.get("kind")) != "equation":
        expected_links = Counter(identity["link_targets"])
        if _link_occurrences(text, expected_links) != expected_links:
            raise TranslationSourceError(
                "translation_source_identity_invalid",
                f"translation changed link occurrences for {block['block_id']}",
            )
    if identity["asset_digest"] is not None and not str(
        identity["asset_digest"]
    ).strip():
        raise TranslationSourceError(
            "translation_source_identity_invalid",
            f"source asset identity is invalid for {block['block_id']}",
        )


def prompt_block(block: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(block.get("kind"))
    if kind == "paragraph":
        payload = block.get("payload")
        if not isinstance(payload, Mapping):
            raise TranslationSourceError(
                "source_block_invalid", "source block payload must be an object"
            )
        return {
            "block_id": block.get("block_id"),
            "ordinal": block.get("ordinal"),
            "kind": "paragraph",
            "section_path": block.get("section_path"),
            "payload": {"text": _compact_inline_text(payload)},
            "source_identity": source_identity(block),
        }
    if kind == "list":
        payload = block.get("payload")
        if not isinstance(payload, Mapping):
            raise TranslationSourceError(
                "source_block_invalid", "source block payload must be an object"
            )
        return {
            "block_id": block.get("block_id"),
            "ordinal": block.get("ordinal"),
            "kind": "list",
            "section_path": block.get("section_path"),
            "payload": {
                "ordered": bool(payload.get("ordered", False)),
                "items": [
                    {"text": _compact_inline_text(item)}
                    for item in _mapping_items(payload.get("items"))
                ],
            },
            "source_identity": source_identity(block),
        }
    if kind != "figure":
        return {**dict(block), "source_identity": source_identity(block)}
    payload = block.get("payload")
    if not isinstance(payload, Mapping):
        raise TranslationSourceError(
            "source_block_invalid", "source block payload must be an object"
        )
    identity = source_identity(block)
    return {
        "block_id": block.get("block_id"),
        "ordinal": block.get("ordinal"),
        "kind": "figure",
        "section_path": block.get("section_path"),
        "payload": {
            "caption": str(payload.get("caption", "")),
            "alt_text": str(payload.get("alt_text", "")),
        },
        "source_identity": {
            "equations": identity["equations"],
            "code_text": None,
            "link_targets": identity["link_targets"],
        },
    }


def _compact_inline_text(value: Mapping[str, Any]) -> str:
    spans = _mapping_items(value.get("inline_spans"))
    if not spans:
        return str(value.get("text", ""))
    rendered: list[str] = []
    for span in spans:
        kind = str(span.get("kind"))
        if kind == "math" and "tex" in span:
            rendered.append(f"${span['tex']}$")
        elif kind == "link" and "target" in span:
            rendered.append(f"[{span.get('text', '')}]({span['target']})")
        else:
            rendered.append(str(span.get("text", "")))
    return "".join(rendered)


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
        caption = str(payload.get("caption", "")).strip()
        return caption or str(payload.get("alt_text", ""))
    raise TranslationSourceError(
        "source_block_invalid", f"unsupported block kind: {kind}"
    )


def block_digest(blocks: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(blocks))).hexdigest()


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


def _extend_inline_identity(
    value: Any,
    *,
    equations: list[str],
    links: list[str],
) -> None:
    for span in _mapping_items(value):
        kind = str(span.get("kind"))
        if kind == "math" and "tex" in span:
            equations.append(str(span["tex"]))
        elif kind == "link" and "target" in span:
            links.append(str(span["target"]))


def _extend_markdown_identity(
    text: str,
    *,
    equations: list[str],
    links: list[str],
) -> None:
    equations.extend(_markdown_math_occurrences(text))
    links.extend(
        match.group(1)
        for match in _MARKDOWN_LINK.finditer(_without_markdown_math(text))
    )


def _markdown_math_occurrences(text: str) -> tuple[str, ...]:
    return tuple(
        next(
            value
            for value in (
                match.group("bracket_tex"),
                match.group("paren_tex"),
                match.group("double_tex"),
                match.group("single_tex"),
            )
            if value is not None
        )
        for match in _MARKDOWN_MATH.finditer(text)
    )


def _formula_occurrences(
    text: str,
    expected: Counter[str],
) -> Counter[str]:
    delimited = Counter(_markdown_math_occurrences(text))
    if delimited:
        return delimited
    edge_characters = r"A-Za-z0-9\\^_{}[\]"
    occupied: list[tuple[int, int]] = []
    occurrences: Counter[str] = Counter()
    # Undelimited TeX can contain another expected formula verbatim: for
    # example ``{\cal O}(H)`` contains ``H``. Treat the longest source
    # identities as atomic before counting shorter identities so the nested
    # text is not mistaken for an additional formula occurrence.
    for token in sorted(expected, key=lambda value: (-len(value), value)):
        if not token:
            continue
        pattern = re.compile(
            rf"(?<![{edge_characters}]){re.escape(token)}"
            rf"(?![{edge_characters}])"
        )
        matches = [
            match.span()
            for match in pattern.finditer(text)
            if not any(
                match.start() < end and start < match.end()
                for start, end in occupied
            )
        ]
        if matches:
            occurrences[token] = len(matches)
            occupied.extend(matches)
    return occurrences


def formula_identity_diagnostics(
    source_blocks: Sequence[Mapping[str, Any]],
    translations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Describe formula identity differences without altering translations.

    The translation workflow already fixes block order separately.  This helper
    therefore identifies a matching TeX occurrence that left one block and
    appeared in another as a move, while retaining distinct missing and added
    diagnostics for unmatched occurrences.
    """

    source_by_id = {str(block.get("block_id")): block for block in source_blocks}
    translation_by_id = {
        str(item.get("block_id")): item
        for item in translations
        if isinstance(item.get("text"), str)
    }
    diagnostics: list[dict[str, Any]] = []
    source_occurrences: dict[str, list[tuple[str, int]]] = {}
    translation_occurrences: dict[str, list[tuple[str, int]]] = {}
    source_positions = {
        str(block.get("block_id")): index
        for index, block in enumerate(source_blocks)
    }
    translation_positions = {
        str(item.get("block_id")): index
        for index, item in enumerate(translations)
    }

    for block_id, block in source_by_id.items():
        if str(block.get("kind")) == "equation":
            continue
        expected = Counter(source_identity(block)["equations"])
        text = str(translation_by_id.get(block_id, {}).get("text", ""))
        actual = _formula_occurrences(text, expected)
        for tex in set(expected) | set(actual):
            difference = expected[tex] - actual[tex]
            if difference > 0:
                source_occurrences.setdefault(tex, []).append((block_id, difference))
            elif difference < 0:
                translation_occurrences.setdefault(tex, []).append(
                    (block_id, -difference)
                )

    for tex in sorted(set(source_occurrences) | set(translation_occurrences)):
        missing = list(source_occurrences.get(tex, ()))
        added = list(translation_occurrences.get(tex, ()))
        source_index = 0
        translation_index = 0
        while source_index < len(missing) and translation_index < len(added):
            source_block_id, missing_count = missing[source_index]
            translation_block_id, added_count = added[translation_index]
            count = min(missing_count, added_count)
            if source_block_id != translation_block_id:
                diagnostics.append(
                    _formula_diagnostic(
                        "formula_moved",
                        tex,
                        count,
                        source_block_id,
                        translation_block_id,
                        source_blocks,
                        translations,
                        source_positions,
                        translation_positions,
                    )
                )
                missing_count -= count
                added_count -= count
                if missing_count:
                    missing[source_index] = (source_block_id, missing_count)
                else:
                    source_index += 1
                if added_count:
                    added[translation_index] = (translation_block_id, added_count)
                else:
                    translation_index += 1
                continue
            source_index += 1
            translation_index += 1
        for source_block_id, count in missing[source_index:]:
            diagnostics.append(
                _formula_diagnostic(
                    "formula_missing",
                    tex,
                    count,
                    source_block_id,
                    source_block_id,
                    source_blocks,
                    translations,
                    source_positions,
                    translation_positions,
                )
            )
        for translation_block_id, count in added[translation_index:]:
            diagnostics.append(
                _formula_diagnostic(
                    "formula_added",
                    tex,
                    count,
                    translation_block_id,
                    translation_block_id,
                    source_blocks,
                    translations,
                    source_positions,
                    translation_positions,
                )
            )
    return tuple(diagnostics)


def _formula_diagnostic(
    code: str,
    tex: str,
    count: int,
    source_block_id: str,
    translation_block_id: str,
    source_blocks: Sequence[Mapping[str, Any]],
    translations: Sequence[Mapping[str, Any]],
    source_positions: Mapping[str, int],
    translation_positions: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "code": code,
        "tex": tex,
        "occurrence_count": count,
        "source_block_id": source_block_id,
        "translation_block_id": translation_block_id,
        "source_neighbor_block_ids": _neighbor_block_ids(
            source_blocks, source_positions[source_block_id]
        ),
        "translation_neighbor_block_ids": _neighbor_block_ids(
            translations, translation_positions[translation_block_id]
        ),
    }


def _neighbor_block_ids(
    blocks: Sequence[Mapping[str, Any]], index: int
) -> list[str]:
    return [
        str(blocks[neighbor].get("block_id"))
        for neighbor in (index - 1, index + 1)
        if 0 <= neighbor < len(blocks)
    ]


def _link_occurrences(
    text: str,
    expected: Counter[str],
) -> Counter[str]:
    text = _without_markdown_math(text)
    markdown_targets = Counter(
        match.group(1) for match in _MARKDOWN_LINK.finditer(text)
    )
    if any(
        target not in expected or count > expected[target]
        for target, count in markdown_targets.items()
    ):
        return markdown_targets
    return Counter(
        {
            target: count
            for target in expected
            if (
                count := _literal_occurrence_count(
                    text,
                    target,
                    edge_characters=_LINK_TOKEN_CHARACTER,
                )
            )
        }
    )


def _without_markdown_math(text: str) -> str:
    return _MARKDOWN_MATH.sub(
        lambda match: " " * len(match.group(0)),
        text,
    )


def _literal_occurrence_count(
    text: str,
    token: str,
    *,
    edge_characters: str,
) -> int:
    if not token:
        return 0
    return len(
        re.findall(
            rf"(?<![{edge_characters}]){re.escape(token)}"
            rf"(?![{edge_characters}])",
            text,
        )
    )


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
    "formula_identity_diagnostics",
    "prompt_block",
    "resolve_translation_source",
    "same_primary_language",
    "source_blocks",
    "source_identity",
    "validate_translation_text",
]
