from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from bs4 import BeautifulSoup, Tag

from .._parsing import ParseError, normalize_tex
from .._parsing.html_source import (
    legacy_html_source_line,
    standard_html_root,
)
from .._parsing.markdown_lex import (
    markdown_column_width as _markdown_column_width,
    markdown_indent_width as _markdown_indent_width,
    markdown_math_end as _markdown_math_end,
    markdown_quote_content as _markdown_quote_content,
    match_atx_heading,
    match_fence,
)
from .._parsing.tex_lex import tex_without_comments as _tex_without_comments
from ..sources import SourceArtifact, SourceFormat
from .models import (
    MathSpan,
    MathSpanKind,
    PDFTextLayer,
    ParsedDocument,
    ParsedPage,
    ParsedSection,
)


class PDFTextExtractionError(RuntimeError):
    """A deterministic PDF extraction failure, distinct from a missing text layer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PDFTextExtractor(Protocol):
    contract_id: str

    def extract(self, payload: bytes) -> PDFTextLayer: ...


class PdftotextExtractor:
    """Narrow, replaceable adapter for deterministic PDF text extraction."""

    contract_id = "arc.paper.pdf_text.pdftotext.layout_utf8.v1"

    def __init__(self, *, executable: str = "pdftotext", timeout_seconds: float = 30):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def extract(self, payload: bytes) -> PDFTextLayer:
        try:
            with tempfile.TemporaryDirectory(prefix="arc-paper-pdf-") as directory:
                path = Path(directory) / "source.pdf"
                path.write_bytes(payload)
                completed = subprocess.run(
                    [
                        self.executable,
                        "-layout",
                        "-enc",
                        "UTF-8",
                        str(path),
                        "-",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout_seconds,
                )
        except FileNotFoundError as exc:
            raise PDFTextExtractionError(
                "pdf_text_extractor_unavailable",
                "pdftotext is unavailable; install it before parsing PDF full text",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PDFTextExtractionError(
                "pdf_text_extraction_timeout",
                "pdftotext timed out while extracting PDF full text",
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise PDFTextExtractionError(
                "pdf_invalid",
                "pdftotext rejected the PDF document",
            ) from exc
        except (subprocess.SubprocessError, OSError, UnicodeError) as exc:
            raise PDFTextExtractionError(
                "pdf_extraction_failed",
                "pdftotext could not extract the PDF document",
            ) from exc
        pages = completed.stdout.split("\f")
        if pages and not pages[-1]:
            pages.pop()
        if not pages:
            return PDFTextLayer((), "PDF contains no extractable text layer")
        if not any(page.strip() for page in pages):
            return PDFTextLayer(
                tuple(pages), "PDF contains no extractable text layer; partial parse retained"
            )
        return PDFTextLayer(tuple(page.strip() for page in pages))


def parse_artifact_bytes(
    artifact: SourceArtifact,
    payload: bytes,
    *,
    pdf_text_extractor: PDFTextExtractor | None = None,
) -> ParsedDocument:
    if len(payload) != artifact.size or hashlib.sha256(payload).hexdigest() != artifact.artifact_digest:
        raise ParseError(
            "source_artifact_mismatch",
            "source bytes do not match the supplied artifact",
            artifact=artifact,
        )
    if artifact.source_format is SourceFormat.PDF:
        return _parse_pdf(
            artifact,
            payload,
            extractor=pdf_text_extractor or PdftotextExtractor(),
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(
            "source_encoding_invalid",
            f"{artifact.source_format.value} source must be UTF-8",
            artifact=artifact,
        ) from exc
    if artifact.source_format is SourceFormat.HTML:
        return _parse_html(artifact, text)
    if artifact.source_format is SourceFormat.MARKDOWN:
        return _parse_markdown(artifact, text)
    if artifact.source_format is SourceFormat.TEX:
        return _parse_tex(artifact, text)
    raise ParseError("unsupported_source", "unsupported source format", artifact=artifact)


def _span_id(
    artifact: SourceArtifact,
    *,
    kind: MathSpanKind,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    tex: str,
) -> str:
    material = "\0".join(
        (
            artifact.artifact_digest,
            kind.value,
            str(start_line),
            str(start_column),
            str(end_line),
            str(end_column),
            tex,
        )
    )
    return f"math-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _make_span(
    artifact: SourceArtifact,
    lines: list[str],
    *,
    kind: MathSpanKind,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    raw: str,
) -> MathSpan | None:
    tex = normalize_tex(raw)
    if not tex:
        return None
    before = ""
    after = ""
    if kind is MathSpanKind.INLINE and start_line == end_line:
        source_line = lines[start_line - 1]
        before = re.sub(r"\s+", " ", source_line[: start_column - 1]).strip()
        after = re.sub(r"\s+", " ", source_line[end_column:]).strip()
    before = before or _nearest_context(lines, start_line - 2, -1)
    after = after or _nearest_context(lines, end_line, 1)
    label_match = re.search(r"\\(?:label|tag)\s*\{([^{}]+)\}", raw)
    return MathSpan(
        span_id=_span_id(
            artifact,
            kind=kind,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            tex=tex,
        ),
        kind=kind,
        source_line_start=start_line,
        source_column_start=start_column,
        source_line_end=end_line,
        source_column_end=end_column,
        normalized_tex=tex,
        context_before=before,
        context_after=after,
        source_label=label_match.group(1) if label_match else "",
    )


def _nearest_context(lines: list[str], index: int, direction: int) -> str:
    while 0 <= index < len(lines):
        candidate = lines[index].strip()
        if candidate and not _is_math_delimiter_line(candidate):
            return re.sub(r"\s+", " ", candidate)
        index += direction
    return ""


def _is_math_delimiter_line(value: str) -> bool:
    return value in {"$$", r"\[", r"\]", r"\(", r"\)"} or bool(
        re.fullmatch(r"\\(?:begin|end)\{[^{}]+\*?\}", value)
    )


def _sections_from_headings(
    headings: Iterable[tuple[int, int, str]], lines: list[str], artifact: SourceArtifact
) -> tuple[ParsedSection, ...]:
    values = list(headings)
    if not values and any(line.strip() for line in lines):
        values = [(1, 1, "Document")]
    output: list[ParsedSection] = []
    for ordinal, (line_number, level, title) in enumerate(values):
        end = values[ordinal + 1][0] - 1 if ordinal + 1 < len(values) else len(lines)
        text = "\n".join(lines[line_number - 1 : end]).strip()
        section_material = (
            f"{artifact.artifact_digest}\0{line_number}\0{level}\0{title}"
        )
        output.append(
            ParsedSection(
                section_id=f"sec-{hashlib.sha256(section_material.encode()).hexdigest()[:20]}",
                title=" ".join(title.split()),
                level=level,
                text=text,
                ordinal=ordinal,
            )
        )
    return tuple(output)


def _parse_markdown(artifact: SourceArtifact, text: str) -> ParsedDocument:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    fenced_lines: set[int] = set()
    active_fence: tuple[int, str] | None = None
    for index, line in enumerate(lines, 1):
        content, quote_depth = _markdown_quote_content(line)
        fence_match = match_fence(content)
        if active_fence is not None and quote_depth < active_fence[0]:
            active_fence = None
        if active_fence is not None:
            fenced_lines.add(index)
            if (
                quote_depth == active_fence[0]
                and fence_match
                and fence_match.group(1)[0] == active_fence[1][0]
                and len(fence_match.group(1)) >= len(active_fence[1])
                and not fence_match.group(2).strip()
            ):
                active_fence = None
            continue
        if fence_match:
            marker = fence_match.group(1)
            active_fence = (quote_depth, marker)
            fenced_lines.add(index)
            continue
        heading = match_atx_heading(content)
        if heading:
            headings.append((index, len(heading.group(1)), heading.group(2)))
    indented_code_lines = _markdown_indented_code_lines(lines, fenced_lines)
    spans = _scan_delimited_math(
        artifact,
        lines,
        excluded_lines=fenced_lines | indented_code_lines,
        include_tex_environments=True,
    )
    return ParsedDocument(
        source=artifact,
        sections=_sections_from_headings(headings, lines, artifact),
        math_spans=spans,
        metadata={"format": "markdown"},
    )


def _markdown_indented_code_lines(
    lines: list[str], fenced_lines: set[int]
) -> set[int]:
    """Identify indented code blocks without hiding ordinary indented content."""

    excluded: set[int] = set()
    code_active = False
    paragraph_active = False
    list_content_indent: int | None = None
    math_end: str | None = None
    quote_depth = 0

    for line_number, line in enumerate(lines, 1):
        content, current_quote_depth = _markdown_quote_content(line)
        if current_quote_depth != quote_depth:
            code_active = False
            paragraph_active = False
            list_content_indent = None
            math_end = None
            quote_depth = current_quote_depth
        if line_number in fenced_lines:
            code_active = False
            paragraph_active = False
            math_end = None
            continue
        stripped = content.strip()
        if not stripped:
            paragraph_active = False
            continue
        if math_end is not None:
            if math_end in content:
                math_end = None
            paragraph_active = True
            continue

        indent = _markdown_indent_width(content)
        list_match = re.match(
            r"^( {0,3})(?:[-+*]|\d+[.)])([ \t]+)", content
        )
        if list_match:
            prefix = list_match.group(0)
            list_content_indent = _markdown_column_width(prefix)
            code_active = False
            paragraph_active = True
            math_end = _markdown_math_end(content)
            continue

        if list_content_indent is not None and indent < list_content_indent:
            list_content_indent = None

        code_threshold = (
            list_content_indent + 4 if list_content_indent is not None else 4
        )
        if indent >= code_threshold and (code_active or not paragraph_active):
            excluded.add(line_number)
            code_active = True
            paragraph_active = False
            continue

        code_active = False
        paragraph_active = not bool(
            re.match(r"^\s{0,3}(?:#{1,6})(?:\s+|$)", content)
            or re.match(r"^\s{0,3}(?:[-*_]\s*){3,}$", content)
        )
        math_end = _markdown_math_end(content)

    return excluded


def _scan_delimited_math(
    artifact: SourceArtifact,
    lines: list[str],
    *,
    excluded_lines: set[int],
    include_tex_environments: bool,
) -> tuple[MathSpan, ...]:
    spans: list[MathSpan] = []
    occupied: set[tuple[int, int]] = set()
    environment_names = "equation|align|gather|multline|eqnarray"
    joined = "\n".join(lines)
    offsets = _line_offsets(lines)
    patterns: list[tuple[MathSpanKind, re.Pattern[str]]] = [
        (MathSpanKind.DISPLAY, re.compile(r"\$\$(.+?)\$\$", re.DOTALL)),
        (MathSpanKind.DISPLAY, re.compile(r"\\\[(.+?)\\\]", re.DOTALL)),
    ]
    if include_tex_environments:
        patterns.append(
            (
                MathSpanKind.DISPLAY,
                re.compile(
                    rf"\\begin\{{(?P<env>{environment_names})\*?\}}.*?"
                    rf"\\end\{{(?P=env)\*?\}}",
                    re.DOTALL,
                ),
            )
        )
    for kind, pattern in patterns:
        for match in pattern.finditer(joined):
            start_line, start_column = _offset_position(offsets, match.start())
            end_line, end_column = _offset_position(offsets, max(match.start(), match.end() - 1))
            if any(line in excluded_lines for line in range(start_line, end_line + 1)):
                continue
            cells = {
                (line, column)
                for line in range(start_line, end_line + 1)
                for column in range(
                    start_column if line == start_line else 1,
                    (end_column if line == end_line else len(lines[line - 1])) + 1,
                )
            }
            if cells.intersection(occupied):
                continue
            span = _make_span(
                artifact,
                lines,
                kind=kind,
                start_line=start_line,
                start_column=start_column,
                end_line=end_line,
                end_column=end_column,
                raw=match.group(0),
            )
            if span:
                spans.append(span)
                occupied.update(cells)

    inline_patterns = (
        re.compile(r"(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$)"),
        re.compile(r"\\\((.+?)\\\)"),
    )
    for line_number, line in enumerate(lines, 1):
        if line_number in excluded_lines:
            continue
        # Inline code is excluded without interpreting its contents.
        code_ranges = [
            range(match.start() + 1, match.end() + 1)
            for match in re.finditer(r"`+[^`]*`+", line)
        ]
        for pattern in inline_patterns:
            for match in pattern.finditer(line):
                columns = range(match.start() + 1, match.end() + 1)
                if any(
                    (line_number, column) in occupied
                    or any(column in code_range for code_range in code_ranges)
                    for column in columns
                ):
                    continue
                span = _make_span(
                    artifact,
                    lines,
                    kind=MathSpanKind.INLINE,
                    start_line=line_number,
                    start_column=match.start() + 1,
                    end_line=line_number,
                    end_column=match.end(),
                    raw=match.group(0),
                )
                if span:
                    spans.append(span)
                    occupied.update((line_number, column) for column in columns)
    return tuple(
        sorted(
            spans,
            key=lambda item: (
                item.source_line_start,
                item.source_column_start,
                item.source_line_end,
                item.source_column_end,
            ),
        )
    )


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1
    return offsets or [0]


def _offset_position(offsets: list[int], offset: int) -> tuple[int, int]:
    for index in range(len(offsets) - 1, -1, -1):
        if offset >= offsets[index]:
            return index + 1, offset - offsets[index] + 1
    return 1, 1


def _parse_tex(artifact: SourceArtifact, text: str) -> ParsedDocument:
    active = _tex_without_comments(text)
    if re.search(r"\\(?:input|include)(?![A-Za-z@])\s*(?:\{|[^\s])", active):
        raise ParseError(
            "unsupported_tex_project",
            "TeX parsing accepts one pre-flattened file; input/include is unsupported",
            artifact=artifact,
        )
    lines = active.splitlines()
    headings: list[tuple[int, int, str]] = []
    levels = {"section": 1, "subsection": 2, "subsubsection": 3}
    for index, line in enumerate(lines, 1):
        match = re.search(
            r"\\(section|subsection|subsubsection)\*?\s*\{([^{}]*)\}", line
        )
        if match:
            headings.append((index, levels[match.group(1)], match.group(2)))
    spans = _scan_delimited_math(
        artifact, lines, excluded_lines=set(), include_tex_environments=True
    )
    return ParsedDocument(
        source=artifact,
        sections=_sections_from_headings(headings, lines, artifact),
        math_spans=spans,
        metadata={"format": "tex", "single_file": True},
    )


def _parse_html(artifact: SourceArtifact, text: str) -> ParsedDocument:
    soup = BeautifulSoup(text, "lxml")
    root = standard_html_root(soup)
    headings = [
        tag for tag in root.find_all(re.compile(r"^h[1-6]$")) if isinstance(tag, Tag)
    ]
    sections: list[ParsedSection] = []
    if headings:
        for ordinal, heading in enumerate(headings):
            title = heading.get_text(" ", strip=True)
            content: list[str] = []
            for sibling in heading.next_siblings:
                if isinstance(sibling, Tag) and sibling.name in {
                    f"h{level}" for level in range(1, int(heading.name[1:]) + 1)
                }:
                    break
                if isinstance(sibling, Tag):
                    value = sibling.get_text(" ", strip=True)
                    if value:
                        content.append(value)
            source_key = str(heading.get("id") or f"{ordinal}:{title}")
            sections.append(
                ParsedSection(
                    section_id=f"sec-{hashlib.sha256((artifact.artifact_digest + source_key).encode()).hexdigest()[:20]}",
                    title=title,
                    level=int(heading.name[1:]),
                    text="\n".join(content),
                    ordinal=ordinal,
                )
            )
    elif root.get_text(" ", strip=True):
        sections.append(
            ParsedSection(
                section_id=f"sec-{artifact.artifact_digest[:20]}",
                title="Document",
                level=1,
                text=root.get_text(" ", strip=True),
                ordinal=0,
            )
        )
    spans: list[MathSpan] = []
    math_nodes = [
        node
        for node in root.select("math, .ltx_equation, .ltx_Math")
        if isinstance(node, Tag)
        and not (
            node.name == "math"
            and node.find_parent(class_=re.compile(r"(?:ltx_equation|ltx_Math)"))
        )
    ]
    for ordinal, node in enumerate(math_nodes):
        math = node if node.name == "math" else node.find("math")
        tex = ""
        if isinstance(math, Tag):
            tex = str(math.get("alttext") or math.get("alt") or "")
            if not tex:
                annotation = math.find("annotation", attrs={"encoding": re.compile("tex", re.I)})
                tex = annotation.get_text(" ", strip=True) if isinstance(annotation, Tag) else ""
        tex = normalize_tex(tex or node.get_text(" ", strip=True))
        if not tex:
            continue
        display = node.name != "math" or str(node.get("display") or "").casefold() == "block"
        line = legacy_html_source_line(text, node, ordinal)
        source_key = str(node.get("id") or ordinal)
        span_id = (
            f"math-{hashlib.sha256((artifact.artifact_digest + source_key + tex).encode()).hexdigest()[:24]}"
        )
        spans.append(
            MathSpan(
                span_id=span_id,
                kind=MathSpanKind.DISPLAY if display else MathSpanKind.INLINE,
                source_line_start=line,
                source_column_start=1,
                source_line_end=line,
                source_column_end=max(1, len(tex)),
                normalized_tex=tex,
                context_before=_html_neighbor_text(node, previous=True),
                context_after=_html_neighbor_text(node, previous=False),
                source_label=str(node.get("id") or ""),
            )
        )
    return ParsedDocument(
        source=artifact,
        sections=tuple(sections),
        math_spans=tuple(spans),
        metadata={"format": "html"},
    )


def _html_neighbor_text(node: Tag, *, previous: bool) -> str:
    method: Callable[..., Tag | None] = (
        node.find_previous if previous else node.find_next
    )
    candidate = method(["p", "li", "blockquote"])
    if not isinstance(candidate, Tag):
        return ""
    section = node.find_parent("section")
    if section is not None and candidate.find_parent("section") is not section:
        return ""
    return candidate.get_text(" ", strip=True)


def _parse_pdf(
    artifact: SourceArtifact,
    payload: bytes,
    *,
    extractor: PDFTextExtractor,
) -> ParsedDocument:
    if not payload.startswith(b"%PDF"):
        raise ParseError(
            "pdf_invalid",
            "PDF source does not contain a PDF header",
            artifact=artifact,
        )
    try:
        result = extractor.extract(payload)
    except PDFTextExtractionError as exc:
        raise ParseError(exc.code, exc.message, artifact=artifact) from exc
    pages = tuple(ParsedPage(index, text) for index, text in enumerate(result.pages, 1))
    sections = tuple(
        ParsedSection(
            section_id=f"pdf-page-{index:05d}",
            title=_first_nonempty_line(page) or f"Page {index}",
            level=1,
            text=page,
            ordinal=index - 1,
            page_start=index,
            page_end=index,
        )
        for index, page in enumerate(result.pages, 1)
    )
    spans: list[MathSpan] = []
    for page_number, page in enumerate(result.pages, 1):
        page_lines = page.splitlines()
        for line_number, line in enumerate(page_lines, 1):
            if not _looks_like_pdf_math(line):
                continue
            raw = re.sub(r"\s*\(([^()]*(?:\d|[ivxlcdm])[^()]*)\)\s*$", "", line).strip()
            if not raw:
                continue
            span = _make_span(
                artifact,
                page_lines,
                kind=MathSpanKind.DISPLAY,
                start_line=line_number,
                start_column=1,
                end_line=line_number,
                end_column=max(1, len(line)),
                raw=raw,
            )
            if span:
                spans.append(span)
    warnings = (result.warning,) if result.warning else ()
    return ParsedDocument(
        source=artifact,
        sections=sections,
        math_spans=tuple(spans),
        pages=pages,
        warnings=warnings,
        metadata={
            "format": "pdf",
            "page_count": len(result.pages),
            "text_layer": result.has_text,
        },
    )


def _first_nonempty_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _looks_like_pdf_math(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    has_operator = bool(re.search(r"[=+\-*/^_≤≥∑∫√]", stripped))
    printed_number = bool(re.search(r"\([^()]*\d[^()]*\)\s*$", stripped))
    return has_operator and (printed_number or len(stripped.split()) <= 24)


__all__ = [
    "PDFTextExtractor",
    "ParseError",
    "PdftotextExtractor",
    "normalize_tex",
    "parse_artifact_bytes",
]
