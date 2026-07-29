"""Validation for model-authored Companion Markdown."""

from __future__ import annotations

import re
from collections.abc import Sequence

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token


_CITATION = re.compile(r"\[@([A-Za-z0-9][A-Za-z0-9._:-]*)\]")


class RichTextError(ValueError):
    """Companion Markdown is invalid."""


def parse_markdown(value: str) -> tuple[Token, ...]:
    if not isinstance(value, str) or not value.strip():
        raise RichTextError("learning-unit markdown must be a non-empty string")
    tokens = tuple(_parser().parse(re.sub(r"(?<!\\)\\n", "\n", value)))
    _reject_raw_html(tokens)
    return tokens


def citation_ids(value: str) -> tuple[str, ...]:
    return citation_ids_from_tokens(parse_markdown(value))


def citation_ids_from_tokens(tokens: Sequence[Token]) -> tuple[str, ...]:
    values: list[str] = []
    for token in tokens:
        values.extend(
            child.content
            for child in token.children or ()
            if child.type == "arc_citation"
        )
    return tuple(values)


def validate_rich_markdown(
    markdown: str,
    *,
    allowed_evidence_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    values = citation_ids(markdown)
    if allowed_evidence_ids is not None:
        allowed = set(allowed_evidence_ids)
        try:
            unknown = next(item for item in values if item not in allowed)
        except StopIteration:
            pass
        else:
            raise RichTextError(
                f"citation is not in bibliography: {unknown}"
            )
    return values


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": True})
    parser.inline.ruler.before("text", "arc_citation", _citation)
    return parser


def _citation(state: StateInline, silent: bool) -> bool:
    match = _CITATION.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        token = state.push("arc_citation", "", 0)
        token.content = match.group(1)
    state.pos = match.end()
    return True


def _reject_raw_html(tokens: Sequence[Token]) -> None:
    for token in tokens:
        if token.type == "html_block":
            raise RichTextError(
                "raw HTML is not permitted in learning-unit markdown"
            )
        if any(
            child.type == "html_inline" for child in token.children or ()
        ):
            raise RichTextError(
                "raw HTML is not permitted in learning-unit markdown"
            )


__all__ = [
    "RichTextError",
    "citation_ids",
    "citation_ids_from_tokens",
    "parse_markdown",
    "validate_rich_markdown",
]
