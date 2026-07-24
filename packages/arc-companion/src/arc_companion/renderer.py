"""Deterministic PDF and responsive Web rendering from ``AcceptedBook``."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
from html import escape as escape_html
from importlib import resources
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse

from .contracts import AcceptedBook, LearningUnit, SourceAnchor
from .validation import require_valid_accepted_book


WEB_RENDER_RECIPE = "arc.companion.web.source_anchored.v1"
PDF_RENDER_RECIPE = "arc.companion.pdf.source_anchored.v1"
_SOURCE_DATE_EPOCH = "946684800"
_AssetLoader = Callable[[str], bytes | None]


class CompanionRenderError(RuntimeError):
    """A deterministic render or output validation failed."""


@dataclass(frozen=True)
class RenderedCompanion:
    accepted_book_digest: str
    web_index: Path | None = None
    pdf_path: Path | None = None


class CompanionRenderer:
    """Render one validated accepted book without loading an LLM runtime."""

    def __init__(self, *, asset_loader: _AssetLoader | None = None) -> None:
        self._asset_loader = asset_loader

    def render_web(self, book: AcceptedBook, output_dir: Path) -> Path:
        require_valid_accepted_book(book)
        root = output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        assets = root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        _write_if_changed(assets / "reader.css", _WEB_CSS.encode("utf-8"))
        _write_if_changed(assets / "reader.js", _WEB_JS.encode("utf-8"))
        _copy_katex_assets(assets / "katex")
        source_urls = self._write_source_assets(book, assets / "source")
        html = _render_html(book, source_urls=source_urls)
        index = root / "index.html"
        _write_if_changed(index, html.encode("utf-8"))
        self.validate_web(book, index)
        return index

    def render_pdf(self, book: AcceptedBook, output_path: Path) -> Path:
        require_valid_accepted_book(book)
        target = output_path.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".arc-companion-render-", dir=target.parent
        ) as temporary:
            workspace = Path(temporary)
            source_paths = self._write_source_assets(book, workspace / "source")
            tex = _render_tex(book, source_paths=source_paths)
            tex_path = workspace / "companion.tex"
            tex_path.write_text(tex, encoding="utf-8")
            built = _compile_tex(tex_path, book.content_digest)
            staged = workspace / target.name
            shutil.copy2(built, staged)
            os.replace(staged, target)
        self.validate_pdf(book, target)
        return target

    def render_all(
        self, book: AcceptedBook, *, web_dir: Path, pdf_path: Path
    ) -> RenderedCompanion:
        web_index = self.render_web(book, web_dir)
        rendered_pdf = self.render_pdf(book, pdf_path)
        return RenderedCompanion(
            accepted_book_digest=book.content_digest,
            web_index=web_index,
            pdf_path=rendered_pdf,
        )

    def validate_web(self, book: AcceptedBook, index_path: Path) -> None:
        if not index_path.is_file():
            raise CompanionRenderError("Web reader index is missing")
        text = index_path.read_text(encoding="utf-8")
        if f'data-book-digest="{book.content_digest}"' not in text:
            raise CompanionRenderError("Web reader is not bound to its accepted book")
        expected = [
            anchor.block_id
            for chapter in book.chapters
            for anchor in chapter.source_anchors
        ]
        positions = [text.find(f'data-source-anchor="{escape_html(item)}"') for item in expected]
        if any(item < 0 for item in positions) or positions != sorted(positions):
            raise CompanionRenderError("Web reader source-anchor order is invalid")
        for relative in (
            "assets/reader.css",
            "assets/reader.js",
            "assets/katex/katex.min.css",
            "assets/katex/katex.min.js",
            "assets/katex/LICENSE",
        ):
            if not (index_path.parent / relative).is_file():
                raise CompanionRenderError(f"Web reader asset is missing: {relative}")

    def validate_pdf(self, book: AcceptedBook, pdf_path: Path) -> None:
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise CompanionRenderError("PDF is missing or empty")
        required = ("pdfinfo", "pdftotext", "pdffonts", "pdftoppm")
        tools = {name: shutil.which(name) for name in required}
        missing = [name for name, path in tools.items() if path is None]
        if missing:
            raise CompanionRenderError(
                "PDF validation tools are required: " + ", ".join(missing)
            )
        with tempfile.TemporaryDirectory(prefix="arc-companion-pdf-check-") as raw:
            workspace = Path(raw)
            info = _run([str(tools["pdfinfo"]), str(pdf_path)])
            if not re.search(r"^Pages:\s+[1-9][0-9]*\s*$", info, re.MULTILINE):
                raise CompanionRenderError("PDF does not report a positive page count")
            text_path = workspace / "content.txt"
            _run([str(tools["pdftotext"]), str(pdf_path), str(text_path)])
            text = text_path.read_text(encoding="utf-8", errors="replace")
            if not text.strip() or book.title not in text:
                raise CompanionRenderError("PDF searchable text is incomplete")
            fonts = _run([str(tools["pdffonts"]), str(pdf_path)])
            rows = [row for row in fonts.splitlines()[2:] if row.strip()]
            if not rows or any(
                (
                    re.search(
                        r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$",
                        row,
                        re.IGNORECASE,
                    )
                    is None
                    or re.search(
                        r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$",
                        row,
                        re.IGNORECASE,
                    ).group(1).casefold()
                    != "yes"
                )
                for row in rows
            ):
                raise CompanionRenderError("PDF contains a non-embedded font")
            raster_prefix = workspace / "page"
            _run(
                [
                    str(tools["pdftoppm"]),
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-singlefile",
                    "-png",
                    "-r",
                    "120",
                    str(pdf_path),
                    str(raster_prefix),
                ]
            )
            raster = workspace / "page.png"
            if not raster.is_file() or raster.stat().st_size == 0:
                raise CompanionRenderError("PDF raster validation failed")

    def _write_source_assets(
        self, book: AcceptedBook, output_dir: Path
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for chapter in book.chapters:
            for anchor in chapter.source_anchors:
                if anchor.kind != "figure":
                    continue
                digest = str(anchor.payload.get("asset_digest") or "")
                target = str(anchor.payload.get("target") or "")
                if not digest:
                    if target:
                        resolved[anchor.block_id] = target
                    continue
                if self._asset_loader is None:
                    raise CompanionRenderError(
                        f"source asset loader is required for {anchor.block_id}"
                    )
                data = self._asset_loader(digest)
                if data is None:
                    raise CompanionRenderError(
                        f"source asset is unavailable: {digest}"
                    )
                actual = hashlib.sha256(data).hexdigest()
                if actual != digest:
                    raise CompanionRenderError(
                        f"source asset digest mismatch: {digest}"
                    )
                suffix = _safe_asset_suffix(target)
                relative = f"{digest}{suffix}"
                output_dir.mkdir(parents=True, exist_ok=True)
                _write_if_changed(output_dir / relative, data)
                resolved[anchor.block_id] = f"source/{relative}"
        return resolved


def _render_html(book: AcceptedBook, *, source_urls: Mapping[str, str]) -> str:
    chapters = "\n".join(
        _render_html_chapter(book, chapter, source_urls=source_urls)
        for chapter in book.chapters
    )
    glossary = _render_html_glossary(book)
    language = escape_html(book.target_language)
    return f"""<!doctype html>
<html lang="{language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{escape_html(book.title)}</title>
  <link rel="stylesheet" href="assets/katex/katex.min.css">
  <link rel="stylesheet" href="assets/reader.css">
  <script defer src="assets/katex/katex.min.js"></script>
  <script defer src="assets/reader.js"></script>
</head>
<body data-book-digest="{book.content_digest}">
  <header class="book-header">
    <p class="eyebrow">Source-anchored textbook companion</p>
    <h1>{escape_html(book.title)}</h1>
  </header>
  <main>{chapters}{glossary}</main>
</body>
</html>
"""


def _render_html_chapter(
    book: AcceptedBook, chapter: Any, *, source_urls: Mapping[str, str]
) -> str:
    translation_by_id = {item.block_id: item for item in chapter.translations}
    units_by_anchor = _units_by_first_anchor(chapter.learning_units)
    anchors: list[str] = []
    for anchor in chapter.source_anchors:
        source = _render_html_source(anchor, source_urls=source_urls)
        translation = translation_by_id.get(anchor.block_id)
        translated = (
            f'<section class="translation-layer" lang="{escape_html(book.target_language)}">'
            f"<h3>Translation</h3><p>{_html_text(translation.text)}</p></section>"
            if translation is not None
            else ""
        )
        units = "".join(
            _render_html_learning(unit)
            for unit in units_by_anchor.get(anchor.block_id, ())
        )
        learning = (
            f'<aside class="learning-layer"><h3>Textbook notes</h3>{units}</aside>'
            if units
            else '<aside class="learning-layer learning-empty" aria-hidden="true"></aside>'
        )
        page = (
            f'<span class="source-page">source p. {anchor.page_number}</span>'
            if anchor.page_number is not None
            else ""
        )
        anchors.append(
            f'<article class="source-anchor" id="anchor-{escape_html(anchor.block_id)}" '
            f'data-source-anchor="{escape_html(anchor.block_id)}">'
            f"{page}<div class=\"anchor-grid\">"
            f'<section class="source-layer" lang="{escape_html(book.source_language)}">'
            f"<h3>Source</h3>{source}</section>{translated}{learning}</div></article>"
        )
    return (
        f'<section class="chapter" id="chapter-{escape_html(chapter.chapter_id)}">'
        f"<h2>{escape_html(chapter.title)}</h2>"
        f'<p class="chapter-guide">{_html_text(chapter.guide)}</p>'
        f'{"".join(anchors)}</section>'
    )


def _render_html_source(
    anchor: SourceAnchor, *, source_urls: Mapping[str, str]
) -> str:
    payload = anchor.payload
    if anchor.kind == "heading":
        level = max(3, min(6, int(payload["level"]) + 2))
        return f"<h{level}>{escape_html(str(payload['text']))}</h{level}>"
    if anchor.kind == "paragraph":
        links = _html_links(payload["links"])
        math = "".join(
            f'<span class="math math-inline" data-tex="{escape_html(str(item["tex"]))}">'
            f'{escape_html(str(item["source"]))}</span>'
            for item in payload["inline_math"]
        )
        return f"<p>{_html_text(str(payload['text']))}</p>{math}{links}"
    if anchor.kind == "list":
        tag = "ol" if payload["ordered"] else "ul"
        items = "".join(
            f"<li>{_html_text(str(item['text']))}{_html_links(item['links'])}</li>"
            for item in payload["items"]
        )
        return f"<{tag}>{items}</{tag}>"
    if anchor.kind == "code":
        language = escape_html(str(payload["language"]))
        return f'<pre><code data-language="{language}">{escape_html(str(payload["text"]))}</code></pre>'
    if anchor.kind == "equation":
        label = (
            f'<span class="equation-label">{escape_html(str(payload["label"]))}</span>'
            if payload["label"]
            else ""
        )
        return (
            f'<div class="math math-display" data-tex="{escape_html(str(payload["tex"]))}">'
            f'{escape_html(str(payload["tex"]))}</div>{label}'
        )
    if anchor.kind == "table":
        headers = "".join(f"<th>{_html_text(item)}</th>" for item in payload["headers"])
        rows = "".join(
            "<tr>" + "".join(f"<td>{_html_text(cell)}</td>" for cell in row) + "</tr>"
            for row in payload["rows"]
        )
        caption = (
            f"<caption>{_html_text(str(payload['caption']))}</caption>"
            if payload["caption"]
            else ""
        )
        return f"<table>{caption}<thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>"
    source = source_urls.get(anchor.block_id, str(payload["target"]))
    caption = escape_html(str(payload["caption"]))
    alt = escape_html(str(payload["alt_text"]))
    return (
        f'<figure><img src="{escape_html(source)}" alt="{alt}">'
        f"<figcaption>{caption}</figcaption></figure>"
    )


def _render_html_learning(unit: LearningUnit) -> str:
    citations = _html_citations(unit.citations)
    return (
        f'<section class="learning-unit learning-{escape_html(unit.kind)}" '
        f'data-learning-unit="{escape_html(unit.unit_id)}">'
        f"<h4>{escape_html(unit.title)}</h4>"
        f"<p>{_html_text(unit.content)}</p>{citations}</section>"
    )


def _render_html_glossary(book: AcceptedBook) -> str:
    if not book.glossary:
        return ""
    rows = "".join(
        "<div class=\"glossary-row\">"
        f"<dt>{escape_html(item.term)}</dt>"
        f'<dd class="translated-term">{escape_html(item.translated_term)}</dd>'
        f"<dd>{_html_text(item.definition)}{_html_citations(item.citations)}</dd>"
        "</div>"
        for item in book.glossary
    )
    return f'<section class="glossary" id="glossary"><h2>Glossary</h2><dl>{rows}</dl></section>'


def _html_links(links: Sequence[Mapping[str, Any]]) -> str:
    if not links:
        return ""
    values = " ".join(
        f'<a href="{escape_html(str(item["target"]))}">{escape_html(str(item["text"]))}</a>'
        for item in links
    )
    return f'<p class="source-links">{values}</p>'


def _html_citations(citations: Sequence[str]) -> str:
    if not citations:
        return ""
    return '<p class="citations">' + " · ".join(
        escape_html(item) for item in citations
    ) + "</p>"


def _html_text(value: str) -> str:
    return escape_html(value).replace("\n", "<br>")


def _render_tex(book: AcceptedBook, *, source_paths: Mapping[str, str]) -> str:
    chapters = "\n".join(
        _render_tex_chapter(book, chapter, source_paths=source_paths)
        for chapter in book.chapters
    )
    glossary = _render_tex_glossary(book)
    return rf"""\documentclass[10pt]{{article}}
\usepackage[margin=21mm]{{geometry}}
\usepackage{{fontspec}}
\usepackage{{xeCJK}}
\usepackage{{amsmath,amssymb}}
\usepackage[table]{{xcolor}}
\usepackage{{graphicx}}
\usepackage{{longtable,booktabs,array}}
\usepackage[colorlinks=true,linkcolor=blue!45!black,urlcolor=blue!45!black]{{hyperref}}
\usepackage{{enumitem}}
\setmainfont{{Noto Sans}}
\setsansfont{{Noto Sans}}
\setmonofont{{Noto Sans Mono CJK SC}}
\setCJKmainfont{{Noto Sans CJK SC}}
\setCJKsansfont{{Noto Sans CJK SC}}
\definecolor{{SourceBg}}{{HTML}}{{F6F8FA}}
\definecolor{{TranslationBg}}{{HTML}}{{EFF6FF}}
\definecolor{{LearningBg}}{{HTML}}{{FFF7E6}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{5pt}}
\hypersetup{{pdftitle={{{_tex_escape(book.title)}}},pdfauthor={{ARC Companion}}}}
\begin{{document}}
\begin{{center}}
{{\LARGE\bfseries {_tex_escape(book.title)}}}\\[4pt]
{{\small Source-anchored textbook companion}}
\end{{center}}
{chapters}
{glossary}
\end{{document}}
"""


def _render_tex_chapter(
    book: AcceptedBook, chapter: Any, *, source_paths: Mapping[str, str]
) -> str:
    translations = {item.block_id: item for item in chapter.translations}
    units = _units_by_first_anchor(chapter.learning_units)
    values = [
        rf"\section{{{_tex_escape(chapter.title)}}}",
        rf"\textbf{{Chapter guide.}} {_tex_escape(chapter.guide)}",
    ]
    for anchor in chapter.source_anchors:
        page = (
            rf"\par\noindent\hfill{{\footnotesize source p. {anchor.page_number}}}\par"
            if anchor.page_number is not None
            else ""
        )
        values.append(
            rf"\par\noindent\hypertarget{{anchor-{_tex_id(anchor.block_id)}}}{{}}{page}"
            rf"\colorbox{{SourceBg}}{{\begin{{minipage}}{{\dimexpr\linewidth-2\fboxsep\relax}}"
            rf"\textbf{{Source}}\par {_render_tex_source(anchor, source_paths=source_paths)}"
            rf"\end{{minipage}}}}\par"
        )
        translation = translations.get(anchor.block_id)
        if translation is not None:
            values.append(
                rf"\par\noindent\colorbox{{TranslationBg}}{{\begin{{minipage}}{{\dimexpr\linewidth-2\fboxsep\relax}}"
                rf"\textbf{{Translation}}\par {_tex_escape(translation.text)}"
                rf"\end{{minipage}}}}\par"
            )
        for unit in units.get(anchor.block_id, ()):
            citations = (
                rf"\par{{\footnotesize\itshape Evidence: {_tex_escape('; '.join(unit.citations))}}}"
                if unit.citations
                else ""
            )
            values.append(
                rf"\par\noindent\colorbox{{LearningBg}}{{\begin{{minipage}}{{\dimexpr\linewidth-2\fboxsep\relax}}"
                rf"\textbf{{{_tex_escape(unit.title)}}}\par "
                rf"{_tex_escape(unit.content)}{citations}\end{{minipage}}}}\par"
            )
        values.append(r"\medskip")
    return "\n".join(values)


def _render_tex_source(
    anchor: SourceAnchor, *, source_paths: Mapping[str, str]
) -> str:
    payload = anchor.payload
    if anchor.kind == "heading":
        return rf"\textbf{{{_tex_escape(payload['text'])}}}"
    if anchor.kind == "paragraph":
        links = _tex_links(payload["links"])
        math = " ".join(
            rf"\({_sanitize_math(item['tex'])}\)"
            for item in payload["inline_math"]
        )
        return " ".join(
            item for item in (_tex_escape(payload["text"]), math, links) if item
        )
    if anchor.kind == "list":
        environment = "enumerate" if payload["ordered"] else "itemize"
        items = "\n".join(
            rf"\item {_tex_escape(item['text'])} {_tex_links(item['links'])}"
            for item in payload["items"]
        )
        return rf"\begin{{{environment}}}[leftmargin=*]{items}\end{{{environment}}}"
    if anchor.kind == "code":
        return rf"{{\ttfamily\small {_tex_escape(payload['text'])}}}"
    if anchor.kind == "equation":
        label = (
            rf"\tag{{{_tex_escape(payload['label'])}}}" if payload["label"] else ""
        )
        return rf"\[{_sanitize_math(payload['tex'])}{label}\]"
    if anchor.kind == "table":
        width = max(1, len(payload["headers"]))
        columns = " ".join([r">{\raggedright\arraybackslash}p{" + f"{0.88 / width:.3f}" + r"\linewidth}"] * width)
        header = " & ".join(_tex_escape(item) for item in payload["headers"]) + r" \\"
        rows = "\n".join(
            " & ".join(_tex_escape(cell) for cell in row) + r" \\"
            for row in payload["rows"]
        )
        caption = (
            rf"\textit{{{_tex_escape(payload['caption'])}}}\par"
            if payload["caption"]
            else ""
        )
        return (
            caption
            + rf"\begin{{tabular}}{{{columns}}}\toprule {header}\midrule "
            + rows
            + r"\bottomrule\end{tabular}"
        )
    source = source_paths.get(anchor.block_id)
    caption = _tex_escape(payload["caption"])
    if source and not urlparse(source).scheme:
        image = rf"\includegraphics[width=0.82\linewidth]{{\detokenize{{{source}}}}}"
    else:
        image = rf"\fbox{{\parbox{{0.8\linewidth}}{{{_tex_escape(payload['alt_text'])}}}}}"
    return rf"\begin{{center}}{image}\par{{\footnotesize {caption}}}\end{{center}}"


def _render_tex_glossary(book: AcceptedBook) -> str:
    if not book.glossary:
        return ""
    rows = "\n".join(
        rf"\textbf{{{_tex_escape(item.term)}}} & "
        rf"{_tex_escape(item.translated_term)} & {_tex_escape(item.definition)} \\"
        for item in book.glossary
    )
    return (
        r"\section{Glossary}"
        r"\begin{longtable}{p{0.2\linewidth}p{0.2\linewidth}p{0.5\linewidth}}"
        r"\toprule Source term & Translation & Definition \\\midrule "
        + rows
        + r"\bottomrule\end{longtable}"
    )


def _tex_links(links: Sequence[Mapping[str, Any]]) -> str:
    return " ".join(
        rf"\href{{{_tex_url(str(item['target']))}}}{{{_tex_escape(item['text'])}}}"
        for item in links
    )


def _units_by_first_anchor(
    units: Sequence[LearningUnit],
) -> dict[str, tuple[LearningUnit, ...]]:
    values: dict[str, list[LearningUnit]] = {}
    for unit in units:
        values.setdefault(unit.anchor_ids[0], []).append(unit)
    return {key: tuple(items) for key, items in values.items()}


def _compile_tex(tex_path: Path, content_digest: str) -> Path:
    latexmk = shutil.which("latexmk")
    if latexmk is None:
        raise CompanionRenderError("latexmk is required to render a companion PDF")
    jobname = f"arc-companion-{content_digest[:16]}"
    command = [
        latexmk,
        "-xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-jobname={jobname}",
        tex_path.name,
    ]
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = _SOURCE_DATE_EPOCH
    completed = subprocess.run(
        command,
        cwd=tex_path.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    built = tex_path.parent / f"{jobname}.pdf"
    if completed.returncode != 0 or not built.is_file():
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-30:])
        raise CompanionRenderError(f"XeLaTeX compilation failed:\n{tail}")
    return built


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command, text=True, capture_output=True, timeout=120, check=False
    )
    if completed.returncode != 0:
        raise CompanionRenderError(
            f"{Path(command[0]).name} failed: {completed.stderr[-1200:]}"
        )
    return completed.stdout


def _copy_katex_assets(target: Path) -> None:
    source = resources.files("arc_companion").joinpath("web_assets", "katex")
    for item in sorted(source.rglob("*"), key=lambda value: str(value)):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        _write_if_changed(target / relative, item.read_bytes())


def _write_if_changed(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _safe_asset_suffix(target: str) -> str:
    suffix = Path(urlparse(target).path).suffix.casefold()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf"}:
        return suffix
    return ".bin"


def _tex_escape(value: Any) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in str(value))


def _sanitize_math(value: Any) -> str:
    text = str(value).strip()
    if "\x00" in text or r"\write18" in text or r"\input" in text:
        raise CompanionRenderError("source equation contains unsupported TeX commands")
    return text


def _tex_url(value: str) -> str:
    return value.replace("\\", "").replace("{", r"\{").replace("}", r"\}")


def _tex_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9:.-]+", "-", value).strip("-") or "source"


_WEB_CSS = """\
:root {
  color-scheme: light;
  --ink: #20262e;
  --muted: #68717c;
  --line: #dfe4e9;
  --source: #f7f9fb;
  --translation: #eef5ff;
  --learning: #fff7e7;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: #f3f5f7;
  font: 1rem/1.65 Inter, ui-sans-serif, system-ui, "Noto Sans CJK SC", sans-serif;
}
a { color: #235b83; text-underline-offset: .15em; }
.book-header, main { width: min(100% - 2rem, 94rem); margin-inline: auto; }
.book-header { padding: 3rem 0 1.5rem; border-bottom: 1px solid var(--line); }
.book-header h1 { margin: .2rem 0 0; font-size: clamp(1.8rem, 4vw, 3.1rem); }
.eyebrow { margin: 0; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
.chapter { margin: 3rem 0 5rem; }
.chapter > h2 { font-size: clamp(1.45rem, 3vw, 2.25rem); }
.chapter-guide { max-width: 70rem; padding: 1rem 1.2rem; border-left: .25rem solid #86a3ba; background: #edf3f7; }
.source-anchor { position: relative; margin: 1.3rem 0; scroll-margin-top: 1rem; }
.source-page { display: block; margin-bottom: .25rem; color: var(--muted); font-size: .8rem; }
.anchor-grid { display: grid; gap: .8rem; }
.source-layer, .translation-layer, .learning-layer {
  min-width: 0;
  padding: 1rem 1.1rem;
  border: 1px solid var(--line);
  border-radius: .65rem;
  overflow-wrap: anywhere;
}
.source-layer { background: var(--source); }
.translation-layer { background: var(--translation); border-color: #d5e3f5; }
.learning-layer { background: var(--learning); border-color: #e9ddbd; }
.learning-empty { display: none; }
.source-layer > h3, .translation-layer > h3, .learning-layer > h3 {
  margin: 0 0 .55rem; color: #52616e; font-size: .78rem; letter-spacing: .06em; text-transform: uppercase;
}
.learning-unit + .learning-unit { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #e2d4b4; }
.learning-unit h4 { margin: 0 0 .25rem; }
pre { overflow-x: auto; padding: .7rem; background: #eef1f4; border-radius: .35rem; }
.math-display { overflow-x: auto; padding: .4rem 0; text-align: center; }
.equation-label { display: block; color: var(--muted); text-align: right; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
caption { padding: .4rem; color: var(--muted); }
th, td { padding: .4rem .5rem; border: 1px solid #ccd4dc; text-align: left; vertical-align: top; }
figure { margin: .7rem 0; text-align: center; }
figure img { max-width: 100%; max-height: 38rem; object-fit: contain; }
figcaption, .citations, .source-links { color: var(--muted); font-size: .84rem; }
.glossary { margin: 5rem 0; padding-top: 1.5rem; border-top: 1px solid var(--line); }
.glossary dl { margin: 0; background: white; border: 1px solid var(--line); border-radius: .6rem; overflow: hidden; }
.glossary-row { display: grid; grid-template-columns: minmax(8rem,.6fr) minmax(8rem,.6fr) minmax(14rem,1.4fr); }
.glossary-row > * { margin: 0; padding: .55rem .7rem; border-top: 1px solid #edf0f2; }
.glossary-row dt { font-weight: 700; }
@media (min-width: 900px) {
  .anchor-grid { grid-template-columns: minmax(0,1fr) minmax(0,1fr) minmax(18rem,.85fr); align-items: start; }
  .anchor-grid:has(.translation-layer):not(:has(.learning-unit)) { grid-template-columns: minmax(0,1fr) minmax(0,1fr); }
}
@media (max-width: 899px) {
  .anchor-grid { grid-template-columns: 1fr; }
  .source-layer { order: 1; }
  .translation-layer { order: 2; }
  .learning-layer { order: 3; }
  .glossary-row { grid-template-columns: 1fr 1fr; }
  .glossary-row > dd:last-child { grid-column: 1 / -1; }
}
"""


_WEB_JS = """\
(function () {
  "use strict";
  function typeset() {
    if (!window.katex || typeof window.katex.render !== "function") return;
    document.querySelectorAll(".math[data-tex]").forEach(function (node) {
      try {
        window.katex.render(node.dataset.tex || "", node, {
          displayMode: node.classList.contains("math-display"),
          throwOnError: false,
          strict: "warn"
        });
      } catch (_) {
        node.textContent = node.dataset.tex || "";
        node.classList.add("math-error");
      }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", typeset, {once: true});
  } else {
    typeset();
  }
}());
"""


__all__ = [
    "CompanionRenderError",
    "CompanionRenderer",
    "PDF_RENDER_RECIPE",
    "RenderedCompanion",
    "WEB_RENDER_RECIPE",
]
