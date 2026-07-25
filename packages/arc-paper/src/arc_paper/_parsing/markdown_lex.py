from __future__ import annotations

import re


_ATX_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def match_atx_heading(value: str) -> re.Match[str] | None:
    """Match the heading subset currently recognized by both parsers."""

    return _ATX_HEADING_RE.match(value)


def match_fence(value: str) -> re.Match[str] | None:
    """Match a fenced-code delimiter currently recognized by both parsers."""

    return _FENCE_RE.match(value)


def markdown_quote_content(line: str) -> tuple[str, int]:
    """Remove block-quote containers while retaining content indentation."""

    content = line
    depth = 0
    while match := re.match(r"^ {0,3}>[ \t]?", content):
        content = content[match.end() :]
        depth += 1
    return content, depth


def markdown_indent_width(value: str) -> int:
    width = 0
    for character in value:
        if character == " ":
            width += 1
        elif character == "\t":
            width += 4 - (width % 4)
        else:
            break
    return width


def markdown_column_width(value: str) -> int:
    width = 0
    for character in value:
        if character == "\t":
            width += 4 - (width % 4)
        else:
            width += 1
    return width


def markdown_math_end(line: str) -> str | None:
    """Return the current multiline-math closing marker for one source line."""

    if line.count("$$") % 2:
        return "$$"
    if line.count(r"\[") > line.count(r"\]"):
        return r"\]"
    environment = re.search(
        r"\\begin\{(equation|align|gather|multline|eqnarray)\*?\}",
        line,
    )
    if environment and not re.search(
        rf"\\end\{{{re.escape(environment.group(1))}\*?\}}",
        line,
    ):
        return rf"\end{{{environment.group(1)}"
    return None


__all__ = [
    "markdown_column_width",
    "markdown_indent_width",
    "markdown_math_end",
    "markdown_quote_content",
    "match_atx_heading",
    "match_fence",
]
