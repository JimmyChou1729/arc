"""One CommonMark token stream for Companion learning-unit prose."""

from __future__ import annotations

import re
import hashlib
from collections.abc import Callable, Mapping, Sequence
from html import escape as escape_html
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token

from .tex_text import escape_tex_text, sanitize_tex_math


_CITATION = re.compile(r"\[@([A-Za-z0-9][A-Za-z0-9._:-]*)\]")


class RichTextError(ValueError):
    """Learning-unit markdown cannot be represented safely in a release."""


def parse_markdown(value: str) -> tuple[Token, ...]:
    """Parse strict CommonMark plus ARC citations and TeX math delimiters."""

    if not isinstance(value, str) or not value.strip():
        raise RichTextError("learning-unit markdown must be a non-empty string")
    normalized = re.sub(r"(?<!\\)\\n", "\n", value)
    tokens = tuple(_parser().parse(normalized))
    _reject_raw_html(tokens)
    return tokens


def citation_ids(value: str) -> tuple[str, ...]:
    """Return ARC citation IDs in their visible order, including duplicates."""

    return citation_ids_from_tokens(parse_markdown(value))


def citation_ids_from_tokens(tokens: Sequence[Token]) -> tuple[str, ...]:
    """Read ARC citation IDs from an already validated token stream."""

    values: list[str] = []
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        values.extend(
            child.content
            for child in token.children
            if child.type == "arc_citation"
        )
    return tuple(values)


def validate_rich_markdown(
    markdown: str,
    *,
    allowed_evidence_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Validate release markdown and return nearby citation IDs in order."""

    values = citation_ids(markdown)
    allowed = set(allowed_evidence_ids or ())
    if allowed_evidence_ids is not None and any(value not in allowed for value in values):
        unknown = next(value for value in values if value not in allowed)
        raise RichTextError(f"citation is not in bibliography: {unknown}")
    return values


def render_html(
    tokens: Sequence[Token],
    *,
    citation_numbers: Mapping[str, int],
    text_renderer: Callable[[str], str] = escape_html,
) -> str:
    """Render a previously parsed token stream to release-safe HTML."""

    values: list[str] = []
    for token in tokens:
        if token.type == "inline":
            values.append(
                _render_html_inline(
                    token.children or (),
                    citation_numbers,
                    text_renderer,
                )
            )
        elif token.type == "paragraph_open":
            values.append("<p>")
        elif token.type == "paragraph_close":
            values.append("</p>")
        elif token.type == "heading_open":
            values.append(f"<{token.tag}>")
        elif token.type == "heading_close":
            values.append(f"</{token.tag}>")
        elif token.type == "bullet_list_open":
            values.append("<ul>")
        elif token.type == "bullet_list_close":
            values.append("</ul>")
        elif token.type == "ordered_list_open":
            values.append("<ol>")
        elif token.type == "ordered_list_close":
            values.append("</ol>")
        elif token.type == "list_item_open":
            values.append("<li>")
        elif token.type == "list_item_close":
            values.append("</li>")
        elif token.type == "blockquote_open":
            values.append("<blockquote>")
        elif token.type == "blockquote_close":
            values.append("</blockquote>")
        elif token.type in {"fence", "code_block"}:
            values.append(f"<pre><code>{escape_html(token.content)}</code></pre>")
        elif token.type == "math_block":
            values.append(
                '<div class="math math-display" data-tex="'
                f'{escape_html(token.content)}">{escape_html(token.content)}</div>'
            )
        elif token.type in {"hr", "hardbreak"}:
            values.append("<hr>")
        elif token.type in {"softbreak", "text"}:
            values.append(text_renderer(token.content))
        else:
            raise RichTextError(f"unsupported CommonMark block token: {token.type}")
    return "".join(values)


def render_tex(
    tokens: Sequence[Token],
    *,
    citation_numbers: Mapping[str, int],
    text_renderer: Callable[[str], str] | None = None,
) -> str:
    """Render the same CommonMark token stream to conservative XeLaTeX."""

    resolved_text_renderer = text_renderer or _tex_escape
    values: list[str] = []
    list_depth = 0
    for token in tokens:
        if token.type == "inline":
            values.append(
                _render_tex_inline(
                    token.children or (),
                    citation_numbers,
                    resolved_text_renderer,
                )
            )
        elif token.type == "paragraph_open":
            values.append("")
        elif token.type == "paragraph_close":
            values.append("\\par ")
        elif token.type == "heading_open":
            command = {"h1": "subsection", "h2": "subsubsection"}.get(token.tag, "paragraph")
            values.append(rf"\{command}{{")
        elif token.type == "heading_close":
            values.append("}")
        elif token.type == "bullet_list_open":
            list_depth += 1
            values.append(r"\begin{itemize}[leftmargin=*]")
        elif token.type == "bullet_list_close":
            list_depth -= 1
            values.append(r"\end{itemize}")
        elif token.type == "ordered_list_open":
            list_depth += 1
            values.append(r"\begin{enumerate}[leftmargin=*]")
        elif token.type == "ordered_list_close":
            list_depth -= 1
            values.append(r"\end{enumerate}")
        elif token.type == "list_item_open":
            values.append(r"\item ")
        elif token.type == "list_item_close":
            values.append(" ")
        elif token.type == "blockquote_open":
            values.append(r"\begin{quote}")
        elif token.type == "blockquote_close":
            values.append(r"\end{quote}")
        elif token.type in {"fence", "code_block"}:
            values.append(_tex_code(token.content))
        elif token.type == "math_block":
            values.append(rf"\[{_sanitize_math(token.content)}\]")
        elif token.type == "hr":
            values.append(r"\par\hrule\par")
        elif token.type in {"softbreak", "text"}:
            values.append(resolved_text_renderer(token.content))
        else:
            raise RichTextError(f"unsupported CommonMark block token: {token.type}")
    if list_depth:
        raise RichTextError("unclosed CommonMark list")
    return "".join(values).strip()


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": True})
    parser.block.ruler.before("fence", "arc_math_block", _math_block)
    parser.inline.ruler.before("escape", "arc_math_inline", _math_inline)
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


def _math_inline(state: StateInline, silent: bool) -> bool:
    if state.src.startswith(r"\(", state.pos):
        open_delimiter = r"\("
        close_delimiter = r"\)"
    elif (
        state.src[state.pos] == "$"
        and not state.src.startswith("$$", state.pos)
        and (not state.pos or state.src[state.pos - 1] != "\\")
    ):
        open_delimiter = "$"
        close_delimiter = "$"
    else:
        return False
    content_start = state.pos + len(open_delimiter)
    close = state.src.find(close_delimiter, content_start)
    while (
        close >= 0
        and close_delimiter == "$"
        and state.src[close - 1] == "\\"
    ):
        close = state.src.find(close_delimiter, close + 1)
    if (
        close < 0
        or close == content_start
        or "\n" in state.src[content_start:close]
    ):
        return False
    if not silent:
        token = state.push("math_inline", "math", 0)
        token.content = state.src[content_start:close].strip()
    state.pos = close + len(close_delimiter)
    return True


def _math_block(
    state: StateBlock, start_line: int, end_line: int, silent: bool
) -> bool:
    begin = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    opening = state.src[begin:maximum].strip()
    if opening == "$$":
        closing = "$$"
    elif opening == r"\[":
        closing = r"\]"
    else:
        return False
    next_line = start_line + 1
    close_line = -1
    while next_line < end_line:
        start = state.bMarks[next_line] + state.tShift[next_line]
        end = state.eMarks[next_line]
        if state.src[start:end].strip() == closing:
            close_line = next_line
            break
        next_line += 1
    if close_line < 0:
        return False
    if silent:
        return True
    token = state.push("math_block", "math", 0)
    token.block = True
    token.map = [start_line, close_line + 1]
    token.content = state.getLines(start_line + 1, close_line, state.blkIndent, False).strip()
    state.line = close_line + 1
    return True


def _reject_raw_html(tokens: Sequence[Token]) -> None:
    for token in tokens:
        if token.type == "html_block":
            location = f" at source line {token.map[0] + 1}" if token.map else ""
            raise RichTextError("raw HTML is not permitted in learning-unit markdown" + location)
        for child in token.children or ():
            if child.type == "html_inline":
                raise RichTextError("raw HTML is not permitted in learning-unit markdown")


def _render_html_inline(
    tokens: Sequence[Token],
    citation_numbers: Mapping[str, int],
    text_renderer: Callable[[str], str],
) -> str:
    values: list[str] = []
    for token in tokens:
        if token.type == "text":
            values.append(text_renderer(token.content))
        elif token.type == "softbreak":
            values.append("\n")
        elif token.type == "hardbreak":
            values.append("<br>")
        elif token.type == "code_inline":
            values.append(f"<code>{escape_html(token.content)}</code>")
        elif token.type == "em_open":
            values.append("<em>")
        elif token.type == "em_close":
            values.append("</em>")
        elif token.type == "strong_open":
            values.append("<strong>")
        elif token.type == "strong_close":
            values.append("</strong>")
        elif token.type == "s_open":
            values.append("<s>")
        elif token.type == "s_close":
            values.append("</s>")
        elif token.type == "link_open":
            href = token.attrGet("href") or ""
            values.append(f'<a href="{escape_html(href)}">')
        elif token.type == "link_close":
            values.append("</a>")
        elif token.type == "image":
            src = token.attrGet("src") or ""
            alt = token.content or ""
            # Model-authored Markdown is not part of the frozen source-asset
            # contract. Keep its image syntax useful without making the Web
            # release fetch bytes that the PDF cannot reproduce.
            label = alt or src
            values.append(
                f'<a class="unfrozen-image-link" href="{escape_html(src)}">'
                f'{escape_html(label)}</a>'
            )
        elif token.type == "math_inline":
            values.append(
                '<span class="math math-inline" data-tex="'
                f'{escape_html(token.content)}">{escape_html(token.content)}</span>'
            )
        elif token.type == "arc_citation":
            number = citation_numbers.get(token.content)
            if number is None:
                raise RichTextError(f"citation is not in bibliography: {token.content}")
            values.append(
                f'<a class="citation-marker" href="#reference-{escape_html(token.content)}">'
                f'[{number}]</a>'
            )
        else:
            raise RichTextError(f"unsupported CommonMark inline token: {token.type}")
    return "".join(values)


def _render_tex_inline(
    tokens: Sequence[Token],
    citation_numbers: Mapping[str, int],
    text_renderer: Callable[[str], str],
) -> str:
    values: list[str] = []
    for token in tokens:
        if token.type == "text":
            values.append(text_renderer(token.content))
        elif token.type in {"softbreak", "hardbreak"}:
            values.append(r"\newline{} ")
        elif token.type == "code_inline":
            values.append(rf"\texttt{{{_tex_escape(token.content)}}}")
        elif token.type == "em_open":
            values.append(r"\emph{")
        elif token.type == "em_close":
            values.append("}")
        elif token.type == "strong_open":
            values.append(r"\textbf{")
        elif token.type == "strong_close":
            values.append("}")
        elif token.type == "s_open":
            values.append(r"\sout{")
        elif token.type == "s_close":
            values.append("}")
        elif token.type == "link_open":
            href = token.attrGet("href") or ""
            values.append(rf"\href{{{_tex_url(href)}}}{{")
        elif token.type == "link_close":
            values.append("}")
        elif token.type == "image":
            src = token.attrGet("src") or ""
            alt = token.content or ""
            values.append(rf"\href{{{_tex_url(src)}}}{{{_tex_escape(alt)}}}")
        elif token.type == "math_inline":
            values.append(rf"\({_sanitize_math(token.content)}\)")
        elif token.type == "arc_citation":
            number = citation_numbers.get(token.content)
            if number is None:
                raise RichTextError(f"citation is not in bibliography: {token.content}")
            values.append(rf"\hyperlink{{reference-{_tex_label(token.content)}}}{{[{number}]}}")
        else:
            raise RichTextError(f"unsupported CommonMark inline token: {token.type}")
    return "".join(values)


def _tex_escape(value: Any) -> str:
    return escape_tex_text(value)


def _tex_url(value: str) -> str:
    return _tex_escape(value).replace("%", r"\%")


def _tex_label(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _sanitize_math(value: str) -> str:
    return sanitize_tex_math(value)


def _tex_code(value: str) -> str:
    return r"{\ttfamily\small\raggedright " + r"\par ".join(
        _tex_escape(line) or r"\strut" for line in value.splitlines() or [""]
    ) + "}"


__all__ = [
    "RichTextError",
    "citation_ids",
    "citation_ids_from_tokens",
    "validate_rich_markdown",
    "parse_markdown",
    "render_html",
    "render_tex",
]
