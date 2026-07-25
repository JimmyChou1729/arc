from __future__ import annotations

from bs4 import BeautifulSoup, Tag


def standard_html_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """Return the standard parser's current single-root projection."""

    return soup.select_one("article") or soup.body or soup


def rich_html_roots(soup: BeautifulSoup) -> list[Tag | BeautifulSoup]:
    """Return the Rich parser's current ordered roots without nested repeats."""

    articles = [
        node
        for node in soup.find_all("article")
        if not isinstance(node.find_parent("article"), Tag)
    ]
    return articles or [soup.body or soup]


def legacy_html_source_line(text: str, node: Tag, ordinal: int) -> int:
    """Preserve the standard parser's current serialized-node line lookup."""

    serialized = str(node)
    if serialized in text:
        return text[: text.find(serialized)].count("\n") + 1
    return ordinal + 1


def rich_html_source_position(
    node: Tag,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Return the Rich parser's current opening-tag point anchor."""

    source_line = getattr(node, "sourceline", None)
    source_position = getattr(node, "sourcepos", None)
    has_position = (
        isinstance(source_line, int)
        and not isinstance(source_line, bool)
        and source_line >= 1
        and isinstance(source_position, int)
        and not isinstance(source_position, bool)
        and source_position >= 0
    )
    if not has_position:
        return None, None, None, None
    return source_line, source_position + 1, source_line, source_position + 1


def rich_html_selector(node: Tag, ordinal: int) -> str:
    """Return the Rich parser's current source selector."""

    if node.get("id"):
        return f"#{node['id']}"
    return f"{node.name}:nth-block({ordinal + 1})"


__all__ = [
    "legacy_html_source_line",
    "rich_html_roots",
    "rich_html_selector",
    "rich_html_source_position",
    "standard_html_root",
]
