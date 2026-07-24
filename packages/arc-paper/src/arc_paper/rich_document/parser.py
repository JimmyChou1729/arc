from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from ..parse.parser import ParseError, normalize_tex
from ..sources import SourceArtifact, SourceFormat
from .models import (
    RichAsset,
    RichBlock,
    RichBlockKind,
    RichDocument,
    RichSection,
    SourceLocator,
)


AssetImporter = Callable[[str], RichAsset | None]


@dataclass(frozen=True)
class RichSourceParseResult:
    document: RichDocument
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RawBlock:
    kind: RichBlockKind
    locator: SourceLocator
    payload: Mapping[str, Any]


def parse_rich_artifact_bytes(
    artifact: SourceArtifact,
    payload: bytes,
    *,
    asset_importer: AssetImporter | None = None,
) -> RichSourceParseResult:
    """Parse one rich primary source without changing the legacy parser."""

    if artifact.source_format not in {
        SourceFormat.MARKDOWN,
        SourceFormat.HTML,
        SourceFormat.TEX,
    }:
        raise ParseError(
            "rich_source_required",
            "rich document parsing requires Markdown, HTML, or flattened TeX",
            artifact=artifact,
        )
    if (
        len(payload) != artifact.size
        or hashlib.sha256(payload).hexdigest() != artifact.artifact_digest
    ):
        raise ParseError(
            "source_artifact_mismatch",
            "source bytes do not match the supplied artifact",
            artifact=artifact,
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(
            "source_encoding_invalid",
            f"{artifact.source_format.value} source must be UTF-8",
            artifact=artifact,
        ) from exc
    assets: dict[str, RichAsset] = {}
    warnings: list[str] = []

    def import_asset(target: str) -> RichAsset | None:
        if not _is_local_asset_target(target):
            return None
        if asset_importer is None:
            warnings.append(f"local asset was not imported: {target}")
            return None
        asset = asset_importer(target)
        if asset is None:
            warnings.append(f"local asset was not found: {target}")
            return None
        assets.setdefault(asset.artifact_digest, asset)
        return asset

    if artifact.source_format is SourceFormat.MARKDOWN:
        raw = _parse_markdown(text, artifact, import_asset)
    elif artifact.source_format is SourceFormat.HTML:
        raw = _parse_html(text, artifact, import_asset)
    else:
        raw = _parse_tex(text, artifact, import_asset)
    document = _finalize_document(
        artifact,
        raw,
        assets=tuple(assets.values()),
        metadata={
            "format": artifact.source_format.value,
            "single_file": artifact.source_format is SourceFormat.TEX,
        },
    )
    return RichSourceParseResult(document=document, warnings=tuple(_dedupe(warnings)))


def _parse_markdown(
    text: str,
    artifact: SourceArtifact,
    import_asset: AssetImporter,
) -> list[_RawBlock]:
    lines = text.splitlines()
    output: list[_RawBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.HEADING,
                    index + 1,
                    index + 1,
                    {
                        "text": _markdown_plain_text(heading.group(2)),
                        "level": len(heading.group(1)),
                    },
                )
            )
            index += 1
            continue
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker = fence.group(1)
            language = fence.group(2).strip().split(maxsplit=1)[0] if fence.group(2).strip() else ""
            start = index
            index += 1
            code: list[str] = []
            while index < len(lines):
                if re.match(
                    rf"^\s{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$",
                    lines[index],
                ):
                    break
                code.append(lines[index])
                index += 1
            end = index
            if index < len(lines):
                index += 1
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.CODE,
                    start + 1,
                    end + 1,
                    {"text": "\n".join(code), "language": language},
                )
            )
            continue
        equation = _markdown_display_equation(lines, index)
        if equation is not None:
            end, raw_tex = equation
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.EQUATION,
                    index + 1,
                    end + 1,
                    {
                        "tex": normalize_tex(raw_tex),
                        "display": True,
                        "label": _tex_label(raw_tex),
                    },
                )
            )
            index = end + 1
            continue
        table = _markdown_table(lines, index)
        if table is not None:
            end, headers, rows = table
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.TABLE,
                    index + 1,
                    end + 1,
                    {"headers": headers, "rows": rows, "caption": ""},
                )
            )
            index = end + 1
            continue
        list_match = re.match(r"^\s{0,3}([-+*]|\d+[.)])\s+(.+)$", line)
        if list_match:
            ordered = bool(re.match(r"\d", list_match.group(1)))
            start = index
            items: list[dict[str, Any]] = []
            while index < len(lines):
                item = re.match(r"^\s{0,3}([-+*]|\d+[.)])\s+(.+)$", lines[index])
                if not item or bool(re.match(r"\d", item.group(1))) != ordered:
                    break
                content = item.group(2).strip()
                items.append(_markdown_inline_payload(content))
                index += 1
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.LIST,
                    start + 1,
                    index,
                    {"ordered": ordered, "items": items},
                )
            )
            continue
        figure = _markdown_figure(line)
        if figure is not None:
            alt_text, target, caption = figure
            asset = import_asset(target)
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.FIGURE,
                    index + 1,
                    index + 1,
                    _figure_payload(
                        asset,
                        alt_text=alt_text,
                        caption=caption,
                        target=target,
                    ),
                )
            )
            index += 1
            continue
        start = index
        paragraph_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            candidate = lines[index]
            if index > start and _markdown_starts_block(lines, index):
                break
            paragraph_lines.append(candidate.strip())
            index += 1
        raw_text = "\n".join(paragraph_lines)
        output.append(
            _raw(
                artifact,
                RichBlockKind.PARAGRAPH,
                start + 1,
                index,
                _markdown_inline_payload(raw_text),
            )
        )
        for alt_text, target, caption in _markdown_figures(raw_text):
            asset = import_asset(target)
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.FIGURE,
                    start + 1,
                    index,
                    _figure_payload(
                        asset,
                        alt_text=alt_text,
                        caption=caption,
                        target=target,
                    ),
                )
            )
    return output


def _markdown_starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        re.match(r"^\s{0,3}(?:#{1,6})\s+", line)
        or re.match(r"^\s{0,3}(?:`{3,}|~{3,})", line)
        or re.match(r"^\s{0,3}(?:[-+*]|\d+[.)])\s+", line)
        or _markdown_figure(line)
        or _markdown_display_equation(lines, index)
        or _markdown_table(lines, index)
    )


def _markdown_display_equation(
    lines: list[str], index: int
) -> tuple[int, str] | None:
    stripped = lines[index].strip()
    delimiters = {"$$": "$$", r"\[": r"\]"}
    for opening, closing in delimiters.items():
        if not stripped.startswith(opening):
            continue
        if stripped != opening and stripped.endswith(closing):
            return index, stripped
        values = [lines[index]]
        current = index + 1
        while current < len(lines):
            values.append(lines[current])
            if lines[current].strip().endswith(closing):
                return current, "\n".join(values)
            current += 1
        return len(lines) - 1, "\n".join(values)
    environment = re.match(
        r"^\s*\\begin\{(equation|align|gather|multline|eqnarray)\*?\}",
        stripped,
    )
    if environment:
        values = [lines[index]]
        current = index
        closing = re.compile(
            rf"\\end\{{{re.escape(environment.group(1))}\*?\}}"
        )
        while current + 1 < len(lines) and not closing.search(values[-1]):
            current += 1
            values.append(lines[current])
        return current, "\n".join(values)
    return None


def _markdown_table(
    lines: list[str], index: int
) -> tuple[int, list[str], list[list[str]]] | None:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return None
    separator = _split_pipe_row(lines[index + 1])
    if not separator or any(
        re.fullmatch(r":?-{3,}:?", cell.strip()) is None for cell in separator
    ):
        return None
    headers = [_markdown_plain_text(cell.strip()) for cell in _split_pipe_row(lines[index])]
    rows: list[list[str]] = []
    current = index + 2
    while current < len(lines) and "|" in lines[current] and lines[current].strip():
        rows.append(
            [
                _markdown_plain_text(cell.strip())
                for cell in _split_pipe_row(lines[current])
            ]
        )
        current += 1
    return current - 1, headers, rows


def _split_pipe_row(value: str) -> list[str]:
    stripped = value.strip().strip("|")
    return [item.replace(r"\|", "|") for item in re.split(r"(?<!\\)\|", stripped)]


def _markdown_figure(value: str) -> tuple[str, str, str] | None:
    match = re.fullmatch(
        r'\s*!\[([^\]]*)\]\((\S+?)(?:\s+["\'](.*?)["\'])?\)\s*',
        value,
    )
    return match.groups(default="") if match else None


def _markdown_figures(value: str) -> list[tuple[str, str, str]]:
    return [
        match.groups(default="")
        for match in re.finditer(
            r'!\[([^\]]*)\]\((\S+?)(?:\s+["\'](.*?)["\'])?\)',
            value,
        )
    ]


def _markdown_inline_payload(value: str) -> dict[str, Any]:
    token = re.compile(
        r"(?P<link>(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\))"
        r"|(?P<dollar>(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$))"
        r"|(?P<paren>\\\((.+?)\\\))"
    )
    parts: list[dict[str, str]] = []
    cursor = 0
    for match in token.finditer(value):
        _append_inline_part(
            parts, "text", _markdown_text_segment(value[cursor : match.start()])
        )
        if match.lastgroup == "link":
            _append_inline_part(
                parts,
                "link",
                _markdown_plain_text(match.group(2)),
                target=match.group(3),
            )
        else:
            source = match.group(0)
            tex = normalize_tex(source)
            if tex:
                _append_inline_part(
                    parts, "math", source, tex=tex, source=source
                )
            else:
                _append_inline_part(parts, "text", source)
        cursor = match.end()
    _append_inline_part(parts, "text", _markdown_text_segment(value[cursor:]))
    return _inline_payload(parts)


def _markdown_text_segment(value: str) -> str:
    value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"(?<!\\)(?:\*\*|__)(.+?)(?:\*\*|__)", r"\1", value)
    value = re.sub(r"(?<!\\)(?:\*|_)(.+?)(?:\*|_)", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return re.sub(r"\s+", " ", value)


def _markdown_plain_text(value: str) -> str:
    value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"(?<!\\)(?:\*\*|__)(.+?)(?:\*\*|__)", r"\1", value)
    value = re.sub(r"(?<!\\)(?:\*|_)(.+?)(?:\*|_)", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return " ".join(value.split())


def _parse_html(
    text: str,
    artifact: SourceArtifact,
    import_asset: AssetImporter,
) -> list[_RawBlock]:
    soup = BeautifulSoup(text, "lxml")
    root = soup.select_one("article") or soup.body or soup
    candidates = root.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "pre", "table", "figure", "img", "math"]
    )
    output: list[_RawBlock] = []
    cursor = 0
    block_names = {
        "p",
        "ul",
        "ol",
        "pre",
        "table",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
    for ordinal, node in enumerate(candidates):
        if not isinstance(node, Tag):
            continue
        parent = node.find_parent(block_names)
        if isinstance(parent, Tag) and parent is not root:
            continue
        if node.name == "img" and isinstance(node.find_parent("figure"), Tag):
            continue
        rendered = str(node)
        offset = text.find(rendered, cursor)
        if offset < 0:
            offset = text.find(rendered)
        cursor = max(cursor, offset + len(rendered))
        line_start = text.count("\n", 0, max(offset, 0)) + 1
        line_end = line_start + rendered.count("\n")
        locator = SourceLocator(
            source_format=artifact.source_format,
            line_start=line_start,
            column_start=1,
            line_end=line_end,
            column_end=max(1, len(rendered.rsplit("\n", 1)[-1])),
            selector=_html_selector(node, ordinal),
            source_id=str(node.get("id") or ""),
        )
        if re.fullmatch(r"h[1-6]", node.name or ""):
            output.append(
                _RawBlock(
                    RichBlockKind.HEADING,
                    locator,
                    {"text": node.get_text(" ", strip=True), "level": int(node.name[1:])},
                )
            )
        elif node.name == "p":
            output.append(
                _RawBlock(
                    RichBlockKind.PARAGRAPH,
                    locator,
                    _html_inline_payload(node),
                )
            )
            for image in node.find_all("img"):
                target = str(image.get("src") or "")
                asset = import_asset(target)
                output.append(
                    _RawBlock(
                        RichBlockKind.FIGURE,
                        locator,
                        _figure_payload(
                            asset,
                            alt_text=str(image.get("alt") or ""),
                            caption="",
                            target=target,
                        ),
                    )
                )
        elif node.name in {"ul", "ol"}:
            output.append(
                _RawBlock(
                    RichBlockKind.LIST,
                    locator,
                    {
                        "ordered": node.name == "ol",
                        "items": [
                            _html_inline_payload(item)
                            for item in node.find_all("li", recursive=False)
                        ],
                    },
                )
            )
        elif node.name == "pre":
            code = node.find("code")
            language = ""
            if isinstance(code, Tag):
                for class_name in code.get("class") or ():
                    if str(class_name).startswith("language-"):
                        language = str(class_name)[9:]
                        break
            output.append(
                _RawBlock(
                    RichBlockKind.CODE,
                    locator,
                    {
                        "text": (code or node).get_text("", strip=False),
                        "language": language,
                    },
                )
            )
        elif node.name == "table":
            equation_math = (
                node.find("math")
                if any(
                    "equation" in str(class_name).casefold()
                    for class_name in node.get("class") or ()
                )
                else None
            )
            if isinstance(equation_math, Tag):
                tex = _html_math_tex(equation_math)
                if tex:
                    output.append(
                        _RawBlock(
                            RichBlockKind.EQUATION,
                            locator,
                            {
                                "tex": tex,
                                "display": True,
                                "label": str(
                                    node.get("id")
                                    or equation_math.get("id")
                                    or ""
                                ),
                            },
                        )
                    )
                continue
            caption = node.find("caption")
            rows = node.find_all("tr")
            header_cells = rows[0].find_all(["th", "td"]) if rows else []
            data_start = 1 if rows and rows[0].find("th") else 0
            output.append(
                _RawBlock(
                    RichBlockKind.TABLE,
                    locator,
                    {
                        "headers": [
                            cell.get_text(" ", strip=True) for cell in header_cells
                        ]
                        if data_start
                        else [],
                        "rows": [
                            [
                                cell.get_text(" ", strip=True)
                                for cell in row.find_all(["th", "td"])
                            ]
                            for row in rows[data_start:]
                        ],
                        "caption": caption.get_text(" ", strip=True)
                        if isinstance(caption, Tag)
                        else "",
                    },
                )
            )
        elif node.name in {"figure", "img"}:
            image = node.find("img") if node.name == "figure" else node
            if not isinstance(image, Tag):
                continue
            target = str(image.get("src") or "")
            asset = import_asset(target)
            caption = node.find("figcaption") if node.name == "figure" else None
            output.append(
                _RawBlock(
                    RichBlockKind.FIGURE,
                    locator,
                    _figure_payload(
                        asset,
                        alt_text=str(image.get("alt") or ""),
                        caption=(
                            caption.get_text(" ", strip=True)
                            if isinstance(caption, Tag)
                            else ""
                        ),
                        target=target,
                    ),
                )
            )
        elif node.name == "math" and str(node.get("display") or "").casefold() == "block":
            tex = _html_math_tex(node)
            if tex:
                output.append(
                    _RawBlock(
                        RichBlockKind.EQUATION,
                        locator,
                        {"tex": tex, "display": True, "label": str(node.get("id") or "")},
                    )
                )
    return output


def _html_selector(node: Tag, ordinal: int) -> str:
    if node.get("id"):
        return f"#{node['id']}"
    return f"{node.name}:nth-block({ordinal + 1})"


def _html_inline_payload(node: Tag) -> dict[str, Any]:
    parts: list[dict[str, str]] = []

    def visit(value: Tag | NavigableString) -> None:
        if isinstance(value, NavigableString):
            _append_inline_part(parts, "text", re.sub(r"\s+", " ", str(value)))
            return
        if value.name == "img":
            _append_inline_part(parts, "text", str(value.get("alt") or ""))
            return
        if value.name == "math":
            if str(value.get("display") or "").casefold() == "block":
                return
            tex = _html_math_tex(value)
            if tex:
                source = value.get_text(" ", strip=True) or tex
                _append_inline_part(
                    parts, "math", source, tex=tex, source=source
                )
            return
        if value.name == "a":
            text = value.get_text(" ", strip=True)
            _append_inline_part(
                parts,
                "link",
                text,
                target=str(value.get("href") or ""),
            )
            return
        for child in value.children:
            if isinstance(child, (Tag, NavigableString)):
                visit(child)

    visit(node)
    return _inline_payload(parts)


def _html_math_tex(node: Tag) -> str:
    tex = str(node.get("alttext") or node.get("alt") or "")
    if not tex:
        annotation = node.find(
            "annotation", attrs={"encoding": re.compile("tex", re.I)}
        )
        if isinstance(annotation, Tag):
            tex = annotation.get_text(" ", strip=True)
    return normalize_tex(tex or node.get_text(" ", strip=True))


def _parse_tex(
    text: str,
    artifact: SourceArtifact,
    import_asset: AssetImporter,
) -> list[_RawBlock]:
    active = _tex_without_comments(text)
    if re.search(
        r"\\(?:input|include)(?![A-Za-z@])\s*(?:\{|[^\s])", active
    ):
        raise ParseError(
            "unsupported_tex_project",
            "rich TeX parsing accepts one pre-flattened file; input/include is unsupported",
            artifact=artifact,
        )
    lines = active.splitlines()
    output: list[_RawBlock] = []
    levels = {"section": 1, "subsection": 2, "subsubsection": 3}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        heading = re.search(
            r"\\(section|subsection|subsubsection)\*?\s*\{([^{}]*)\}", line
        )
        if heading:
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.HEADING,
                    index + 1,
                    index + 1,
                    {
                        "text": _tex_plain_text(heading.group(2)),
                        "level": levels[heading.group(1)],
                    },
                )
            )
            index += 1
            continue
        figure_environment = re.search(r"\\begin\{figure\*?\}", line)
        if figure_environment:
            start = index
            values = [line]
            while index + 1 < len(lines) and not re.search(
                r"\\end\{figure\*?\}", values[-1]
            ):
                index += 1
                values.append(lines[index])
            joined = "\n".join(values)
            image = re.search(
                r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}", joined
            )
            target = image.group(1) if image else ""
            asset = import_asset(target) if target else None
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.FIGURE,
                    start + 1,
                    index + 1,
                    _figure_payload(
                        asset,
                        alt_text="",
                        caption=_tex_caption(joined),
                        target=target,
                    ),
                )
            )
            index += 1
            continue
        environment = re.search(
            r"\\begin\{(equation|align|gather|multline|eqnarray)\*?\}", line
        )
        if environment:
            start = index
            values = [line]
            closing = re.compile(
                rf"\\end\{{{re.escape(environment.group(1))}\*?\}}"
            )
            while index + 1 < len(lines) and not closing.search(values[-1]):
                index += 1
                values.append(lines[index])
            raw_tex = "\n".join(values)
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.EQUATION,
                    start + 1,
                    index + 1,
                    {
                        "tex": normalize_tex(raw_tex),
                        "display": True,
                        "label": _tex_label(raw_tex),
                    },
                )
            )
            index += 1
            continue
        display = _markdown_display_equation(lines, index)
        if display is not None:
            end, raw_tex = display
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.EQUATION,
                    index + 1,
                    end + 1,
                    {
                        "tex": normalize_tex(raw_tex),
                        "display": True,
                        "label": _tex_label(raw_tex),
                    },
                )
            )
            index = end + 1
            continue
        verbatim = re.search(r"\\begin\{(verbatim|lstlisting)\}", line)
        if verbatim:
            start = index
            values: list[str] = []
            closing = re.compile(rf"\\end\{{{re.escape(verbatim.group(1))}\}}")
            index += 1
            while index < len(lines) and not closing.search(lines[index]):
                values.append(lines[index])
                index += 1
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.CODE,
                    start + 1,
                    min(index + 1, len(lines)),
                    {"text": "\n".join(values), "language": "tex"},
                )
            )
            index += 1
            continue
        list_environment = re.search(r"\\begin\{(itemize|enumerate)\}", line)
        if list_environment:
            start = index
            values: list[str] = []
            index += 1
            while index < len(lines) and not re.search(
                rf"\\end\{{{list_environment.group(1)}\}}", lines[index]
            ):
                values.append(lines[index])
                index += 1
            joined = "\n".join(values)
            items = [
                _tex_inline_payload(value.strip())
                for value in re.split(r"\\item(?:\[[^\]]*\])?", joined)
                if value.strip()
            ]
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.LIST,
                    start + 1,
                    min(index + 1, len(lines)),
                    {
                        "ordered": list_environment.group(1) == "enumerate",
                        "items": items,
                    },
                )
            )
            index += 1
            continue
        tabular = re.search(r"\\begin\{tabular\}", line)
        if tabular:
            start = index
            values = [line]
            while index + 1 < len(lines) and r"\end{tabular}" not in values[-1]:
                index += 1
                values.append(lines[index])
            body = "\n".join(values)
            body = re.sub(r"\\begin\{tabular\}\{[^{}]*\}", "", body)
            body = body.replace(r"\end{tabular}", "")
            rows = [
                [_tex_plain_text(cell.strip()) for cell in row.split("&")]
                for row in re.split(r"\\\\", body)
                if _tex_plain_text(row.strip())
            ]
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.TABLE,
                    start + 1,
                    index + 1,
                    {"headers": [], "rows": rows, "caption": ""},
                )
            )
            index += 1
            continue
        figure = re.search(
            r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}", line
        )
        if figure:
            target = figure.group(1)
            asset = import_asset(target)
            output.append(
                _raw(
                    artifact,
                    RichBlockKind.FIGURE,
                    index + 1,
                    index + 1,
                    _figure_payload(
                        asset,
                        alt_text="",
                        caption=_tex_caption(line),
                        target=target,
                    ),
                )
            )
            index += 1
            continue
        start = index
        values: list[str] = []
        while index < len(lines) and lines[index].strip():
            if index > start and _tex_starts_block(lines[index]):
                break
            values.append(lines[index].strip())
            index += 1
        raw_text = " ".join(values)
        output.append(
            _raw(
                artifact,
                RichBlockKind.PARAGRAPH,
                start + 1,
                index,
                _tex_inline_payload(raw_text),
            )
        )
    return output


def _tex_starts_block(value: str) -> bool:
    return bool(
        re.search(
            r"\\(?:sub)*section|\\begin\{(?:equation|align|gather|multline|eqnarray|verbatim|lstlisting|itemize|enumerate|tabular)\}|\\includegraphics",
            value,
        )
        or value.strip().startswith((r"\[", "$$"))
    )


def _tex_without_comments(text: str) -> str:
    text = re.sub(
        r"\\begin\{comment\*?\}.*?\\end\{comment\*?\}",
        lambda match: "\n" * match.group(0).count("\n"),
        text,
        flags=re.DOTALL,
    )
    return "\n".join(
        re.sub(r"(?<!\\)%.*$", "", line) for line in text.splitlines()
    )


def _tex_plain_text(value: str) -> str:
    value = re.sub(r"\\(?:label|tag)\{[^{}]*\}", "", value)
    value = re.sub(
        r"\\(?:textbf|textit|emph|mathrm|mathbf|mathcal)\{([^{}]*)\}", r"\1", value
    )
    value = re.sub(r"\\(?:href|url)\{([^{}]*)\}(?:\{([^{}]*)\})?", lambda match: match.group(2) or match.group(1), value)
    value = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", "", value)
    value = value.replace("{", "").replace("}", "")
    return " ".join(value.split())


def _tex_inline_payload(value: str) -> dict[str, Any]:
    token = re.compile(
        r"(?P<href>\\href\{([^{}]+)\}\{([^{}]+)\})"
        r"|(?P<url>\\url\{([^{}]+)\})"
        r"|(?P<dollar>(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$))"
        r"|(?P<paren>\\\((.+?)\\\))"
    )
    parts: list[dict[str, str]] = []
    cursor = 0
    for match in token.finditer(value):
        _append_inline_part(parts, "text", _tex_text_segment(value[cursor : match.start()]))
        if match.lastgroup == "href":
            _append_inline_part(
                parts,
                "link",
                _tex_plain_text(match.group(3)),
                target=match.group(2),
            )
        elif match.lastgroup == "url":
            _append_inline_part(
                parts,
                "link",
                match.group(5),
                target=match.group(5),
            )
        else:
            source = match.group(0)
            tex = normalize_tex(source)
            if tex:
                _append_inline_part(
                    parts, "math", source, tex=tex, source=source
                )
            else:
                _append_inline_part(parts, "text", source)
        cursor = match.end()
    _append_inline_part(parts, "text", _tex_text_segment(value[cursor:]))
    return _inline_payload(parts)


def _tex_text_segment(value: str) -> str:
    leading = bool(re.match(r"\s", value))
    trailing = bool(re.search(r"\s$", value))
    text = _tex_plain_text(value)
    if not text:
        return " " if leading or trailing else ""
    return (" " if leading else "") + text + (" " if trailing else "")


def _tex_caption(value: str) -> str:
    match = re.search(r"\\caption\{([^{}]*)\}", value)
    return _tex_plain_text(match.group(1)) if match else ""


def _tex_label(value: str) -> str:
    match = re.search(r"\\(?:label|tag)\s*\{([^{}]+)\}", value)
    return match.group(1) if match else ""


def _append_inline_part(
    parts: list[dict[str, str]],
    kind: str,
    text: str,
    **metadata: str,
) -> None:
    if not text:
        return
    item = {"kind": kind, "text": text, **metadata}
    if kind == "text" and parts and parts[-1]["kind"] == "text":
        parts[-1]["text"] += text
    else:
        parts.append(item)


def _inline_payload(parts: list[dict[str, str]]) -> dict[str, Any]:
    normalized: list[dict[str, str]] = []
    for part in parts:
        item = dict(part)
        item["text"] = re.sub(r"\s+", " ", item["text"])
        if not item["text"]:
            continue
        if normalized and normalized[-1]["text"].endswith(" ") and item["text"].startswith(" "):
            item["text"] = item["text"][1:]
        if item["text"]:
            normalized.append(item)
    if normalized:
        normalized[0]["text"] = normalized[0]["text"].lstrip()
        normalized[-1]["text"] = normalized[-1]["text"].rstrip()
        normalized = [item for item in normalized if item["text"]]
    text = "".join(item["text"] for item in normalized)
    spans: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    inline_math: list[dict[str, str]] = []
    cursor = 0
    for part in normalized:
        end = cursor + len(part["text"])
        span: dict[str, Any] = {
            "kind": part["kind"],
            "start": cursor,
            "end": end,
            "text": part["text"],
        }
        if part["kind"] == "link":
            span["target"] = part["target"]
            links.append({"text": part["text"], "target": part["target"]})
        elif part["kind"] == "math":
            span["tex"] = part["tex"]
            span["source"] = part["source"]
            inline_math.append(
                {"tex": part["tex"], "source": part["source"]}
            )
        spans.append(span)
        cursor = end
    return {
        "text": text,
        "links": links,
        "inline_math": inline_math,
        "inline_spans": spans,
    }


def _figure_payload(
    asset: RichAsset | None,
    *,
    alt_text: str,
    caption: str,
    target: str,
) -> dict[str, Any]:
    return {
        "asset_digest": asset.artifact_digest if asset else "",
        "alt_text": alt_text,
        "caption": caption,
        "target": target,
        "media_type": asset.media_type if asset else "",
        "logical_name": asset.logical_name if asset else target,
        "size": asset.size if asset else 0,
    }


def _raw(
    artifact: SourceArtifact,
    kind: RichBlockKind,
    line_start: int,
    line_end: int,
    payload: Mapping[str, Any],
) -> _RawBlock:
    return _RawBlock(
        kind=kind,
        locator=SourceLocator(
            source_format=artifact.source_format,
            line_start=line_start,
            column_start=1,
            line_end=line_end,
            column_end=1,
        ),
        payload=payload,
    )


def _finalize_document(
    artifact: SourceArtifact,
    raw_blocks: list[_RawBlock],
    *,
    assets: tuple[RichAsset, ...],
    metadata: Mapping[str, Any],
) -> RichDocument:
    section_specs: list[dict[str, Any]] = []
    paths: list[tuple[str, ...]] = []
    stack: list[tuple[int, str]] = []
    synthetic_id = "sec-" + hashlib.sha256(
        json_bytes(
            {
                "source": artifact.content_identity,
                "role": "synthetic-document-section",
            }
        )
    ).hexdigest()[:20]
    for ordinal, raw in enumerate(raw_blocks):
        if raw.kind is RichBlockKind.HEADING:
            level = int(raw.payload["level"])
            title = str(raw.payload["text"])
            material = {
                "source": artifact.content_identity,
                "ordinal": ordinal,
                "level": level,
                "title": title,
            }
            section_id = (
                f"sec-{hashlib.sha256(json_bytes(material)).hexdigest()[:20]}"
            )
            while stack and stack[-1][0] >= level:
                stack.pop()
            path = tuple(item[1] for item in stack) + (section_id,)
            stack.append((level, section_id))
            section_specs.append(
                {
                    "section_id": section_id,
                    "title": title,
                    "level": level,
                    "path": path,
                    "block_start": ordinal,
                }
            )
        elif not stack:
            if not section_specs or section_specs[-1]["section_id"] != synthetic_id:
                section_specs.append(
                    {
                        "section_id": synthetic_id,
                        "title": "Document",
                        "level": 1,
                        "path": (synthetic_id,),
                        "block_start": ordinal,
                    }
                )
            stack = [(1, synthetic_id)]
        paths.append(tuple(item[1] for item in stack))
    blocks: list[RichBlock] = []
    for ordinal, (raw, section_path) in enumerate(zip(raw_blocks, paths, strict=True)):
        material = {
            "source": artifact.content_identity,
            "ordinal": ordinal,
            "kind": raw.kind.value,
            "locator": {
                "line_start": raw.locator.line_start,
                "line_end": raw.locator.line_end,
                "selector": raw.locator.selector,
                "source_id": raw.locator.source_id,
            },
            "payload": raw.payload,
        }
        block_id = "block-" + hashlib.sha256(
            json_bytes(material)
        ).hexdigest()[:24]
        blocks.append(
            RichBlock(
                block_id=block_id,
                ordinal=ordinal,
                kind=raw.kind,
                section_path=section_path,
                locator=raw.locator,
                payload=raw.payload,
            )
        )
    sections: list[RichSection] = []
    for ordinal, spec in enumerate(section_specs):
        following = [
            int(other["block_start"])
            for other in section_specs[ordinal + 1 :]
            if len(other["path"]) <= len(spec["path"])
        ]
        block_end = min(following) if following else len(blocks)
        sections.append(
            RichSection(
                section_id=str(spec["section_id"]),
                title=str(spec["title"]),
                level=int(spec["level"]),
                ordinal=ordinal,
                path=tuple(spec["path"]),
                block_start=int(spec["block_start"]),
                block_end=block_end,
            )
        )
    if not sections and blocks:
        sections.append(
            RichSection(
                section_id=synthetic_id,
                title="Document",
                level=1,
                ordinal=0,
                path=(synthetic_id,),
                block_start=0,
                block_end=len(blocks),
            )
        )
    return RichDocument(
        source=artifact,
        blocks=tuple(blocks),
        sections=tuple(sections),
        assets=assets,
        metadata=metadata,
    )


def json_bytes(value: Any) -> bytes:
    import json

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _is_local_asset_target(value: str) -> bool:
    parsed = urlparse(value)
    return bool(value) and not parsed.scheme and not parsed.netloc and not value.startswith(("#", "/"))


def resolve_local_asset_path(source_path: str | Path, target: str) -> Path | None:
    """Resolve a source-relative asset target without interpreting remote URLs."""

    if not _is_local_asset_target(target):
        return None
    clean_target = target.split("#", 1)[0].split("?", 1)[0]
    return Path(source_path).parent / clean_target


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


__all__ = [
    "AssetImporter",
    "RichSourceParseResult",
    "parse_rich_artifact_bytes",
    "resolve_local_asset_path",
]
