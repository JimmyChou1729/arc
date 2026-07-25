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
import unicodedata
from urllib.parse import quote, unquote, urlparse

from .contracts import AcceptedBook, LearningUnit, SourceAnchor
from .validation import require_valid_accepted_book


WEB_RENDER_RECIPE = "arc.companion.web.source_anchored.v4"
PDF_RENDER_RECIPE = "arc.companion.pdf.source_anchored.v5"
_SOURCE_DATE_EPOCH = "946684800"
_AssetLoader = Callable[[str], bytes | None]


class CompanionRenderError(RuntimeError):
    """A deterministic render or output validation failed."""


@dataclass(frozen=True)
class RenderedCompanion:
    accepted_book_digest: str
    web_index: Path | None = None
    pdf_path: Path | None = None


@dataclass(frozen=True)
class _SourceAssetReference:
    display_path: str
    original_path: str
    media_type: str


@dataclass(frozen=True)
class _ReleaseLinks:
    fragment_blocks: Mapping[str, str]

    def html_target(self, target: str) -> str:
        block_id = self._fragment_block(target)
        return (
            f"#anchor-{_anchor_token(block_id)}"
            if block_id is not None
            else target
        )

    def tex_link(self, target: str, text: str) -> str:
        block_id = self._fragment_block(target)
        label = _tex_escape(text)
        if block_id is not None:
            return (
                rf"\hyperlink{{anchor-{_anchor_token(block_id)}}}"
                rf"{{{label}}}"
            )
        return rf"\href{{{_tex_url(target)}}}{{{label}}}"

    def _fragment_block(self, target: str) -> str | None:
        parsed = urlparse(target)
        if parsed.scheme or parsed.netloc:
            return None
        if not parsed.path and not parsed.params and not parsed.query:
            fragment = unquote(parsed.fragment)
            if fragment and fragment in self.fragment_blocks:
                return self.fragment_blocks[fragment]
            if target.startswith("#"):
                raise CompanionRenderError(
                    f"source fragment does not resolve in this book: {target}"
                )
        raise CompanionRenderError(
            f"relative source link is not included in the release: {target}"
        )


def _release_links(book: AcceptedBook) -> _ReleaseLinks:
    fragment_blocks: dict[str, str] = {}
    for chapter in book.chapters:
        for anchor in chapter.source_anchors:
            source_id = str(anchor.locator.get("source_id") or "")
            if not source_id:
                continue
            existing = fragment_blocks.get(source_id)
            if existing is not None and existing != anchor.block_id:
                raise CompanionRenderError(
                    f"source fragment is ambiguous in this book: #{source_id}"
                )
            fragment_blocks[source_id] = anchor.block_id

    links = _ReleaseLinks(fragment_blocks)
    for chapter in book.chapters:
        for anchor in chapter.source_anchors:
            payload = anchor.payload
            span_groups: list[Sequence[Mapping[str, Any]]] = []
            if anchor.kind == "paragraph":
                span_groups.append(payload["inline_spans"])
            elif anchor.kind == "list":
                span_groups.extend(
                    item["inline_spans"] for item in payload["items"]
                )
            for spans in span_groups:
                for span in spans:
                    if span["kind"] == "link":
                        links.html_target(str(span["target"]))
            if (
                anchor.kind == "figure"
                and not payload.get("asset_digest")
                and payload.get("target")
            ):
                links.html_target(str(payload["target"]))
    return links


class CompanionRenderer:
    """Render one validated accepted book without loading an LLM runtime."""

    def __init__(self, *, asset_loader: _AssetLoader | None = None) -> None:
        self._asset_loader = asset_loader

    def render_web(self, book: AcceptedBook, output_dir: Path) -> Path:
        require_valid_accepted_book(book)
        release_links = _release_links(book)
        root = output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        assets = root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        _write_if_changed(assets / "reader.css", _WEB_CSS.encode("utf-8"))
        _write_if_changed(assets / "reader.js", _WEB_JS.encode("utf-8"))
        _copy_katex_assets(assets / "katex")
        source_assets = {
            block_id: _SourceAssetReference(
                display_path=f"assets/{reference.display_path}",
                original_path=f"assets/{reference.original_path}",
                media_type=reference.media_type,
            )
            for block_id, reference in self._write_source_assets(
                book, assets / "source", pdf_compatible=False
            ).items()
        }
        html = _render_html(
            book,
            source_assets=source_assets,
            release_links=release_links,
        )
        index = root / "index.html"
        _write_if_changed(index, html.encode("utf-8"))
        self.validate_web(book, index)
        return index

    def render_pdf(self, book: AcceptedBook, output_path: Path) -> Path:
        require_valid_accepted_book(book)
        release_links = _release_links(book)
        target = output_path.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".arc-companion-render-", dir=target.parent
        ) as temporary:
            workspace = Path(temporary)
            source_assets = self._write_source_assets(
                book, workspace / "source", pdf_compatible=True
            )
            tex = _render_tex(
                book,
                source_paths={
                    block_id: reference.display_path
                    for block_id, reference in source_assets.items()
                },
                release_links=release_links,
            )
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
        for evidence in book.bibliography:
            if any(
                escape_html(value) not in text
                for value in (
                    evidence.evidence_id,
                    evidence.title,
                    evidence.source,
                )
            ):
                raise CompanionRenderError(
                    "Web reader bibliography is incomplete"
                )
        for relative in (
            "assets/reader.css",
            "assets/reader.js",
            "assets/katex/katex.min.css",
            "assets/katex/katex.min.js",
            "assets/katex/LICENSE",
        ):
            if not (index_path.parent / relative).is_file():
                raise CompanionRenderError(f"Web reader asset is missing: {relative}")
        for chapter in book.chapters:
            for anchor in chapter.source_anchors:
                if anchor.kind != "figure":
                    continue
                digest = str(anchor.payload.get("asset_digest") or "")
                if not digest:
                    continue
                media_type = str(anchor.payload.get("media_type") or "")
                original = (
                    index_path.parent
                    / "assets"
                    / "source"
                    / f"{digest}{_asset_suffix(media_type)}"
                )
                if not original.is_file() or original.stat().st_size == 0:
                    raise CompanionRenderError(
                        f"Web reader source asset is missing: {digest}"
                    )
                if _requires_web_preview(media_type):
                    preview = original.with_name(f"{digest}.preview.png")
                    if not preview.is_file() or preview.stat().st_size == 0:
                        raise CompanionRenderError(
                            f"Web reader source preview is missing: {digest}"
                        )

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
            page_match = re.search(r"^Pages:\s+([1-9][0-9]*)\s*$", info, re.MULTILINE)
            if page_match is None:
                raise CompanionRenderError("PDF does not report a positive page count")
            page_count = int(page_match.group(1))
            text_path = workspace / "content.txt"
            _run([str(tools["pdftotext"]), str(pdf_path), str(text_path)])
            text = text_path.read_text(encoding="utf-8", errors="replace")
            if not _normalize_pdf_search_text(text) or not _pdf_text_contains(
                text, book.title
            ):
                raise CompanionRenderError("PDF searchable text is incomplete")
            for chapter in book.chapters:
                for anchor in chapter.source_anchors:
                    if anchor.kind != "table" or not anchor.payload["rows"]:
                        continue
                    for cell in anchor.payload["rows"][-1]:
                        if str(cell).strip() and not _pdf_text_contains(
                            text, str(cell)
                        ):
                            raise CompanionRenderError(
                                "PDF searchable table content is incomplete"
                            )
            for evidence in book.bibliography:
                if any(
                    not _pdf_text_contains(text, value)
                    for value in (
                        evidence.evidence_id,
                        evidence.title,
                        evidence.source,
                    )
                ):
                    raise CompanionRenderError(
                        "PDF searchable bibliography is incomplete"
                    )
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
                    "-png",
                    "-r",
                    "120",
                    str(pdf_path),
                    str(raster_prefix),
                ]
            )
            rasters = sorted(workspace.glob("page-*.png"))
            if len(rasters) != page_count or any(
                item.stat().st_size == 0 for item in rasters
            ):
                raise CompanionRenderError("PDF raster validation failed")

    def _write_source_assets(
        self,
        book: AcceptedBook,
        output_dir: Path,
        *,
        pdf_compatible: bool,
    ) -> dict[str, _SourceAssetReference]:
        resolved: dict[str, _SourceAssetReference] = {}
        for chapter in book.chapters:
            for anchor in chapter.source_anchors:
                if anchor.kind != "figure":
                    continue
                digest = str(anchor.payload.get("asset_digest") or "")
                target = str(anchor.payload.get("target") or "")
                if not digest:
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
                expected_size = anchor.payload.get("size")
                if expected_size != len(data):
                    raise CompanionRenderError(
                        f"source asset size mismatch: {digest}"
                    )
                media_type = str(anchor.payload.get("media_type") or "")
                suffix = _asset_suffix(media_type)
                relative = f"{digest}{suffix}"
                output_dir.mkdir(parents=True, exist_ok=True)
                original = output_dir / relative
                _write_if_changed(original, data)
                if pdf_compatible:
                    rendered = _prepare_pdf_asset(
                        original,
                        media_type=media_type,
                        digest=digest,
                    )
                else:
                    rendered = _prepare_web_asset(
                        original,
                        media_type=media_type,
                        digest=digest,
                    )
                resolved[anchor.block_id] = _SourceAssetReference(
                    display_path=f"source/{rendered.name}",
                    original_path=f"source/{original.name}",
                    media_type=media_type,
                )
        return resolved


def _render_html(
    book: AcceptedBook,
    *,
    source_assets: Mapping[str, _SourceAssetReference],
    release_links: _ReleaseLinks,
) -> str:
    chapters = "\n".join(
        _render_html_chapter(
            book,
            chapter,
            source_assets=source_assets,
            release_links=release_links,
        )
        for chapter in book.chapters
    )
    glossary = _render_html_glossary(book)
    bibliography = _render_html_bibliography(book)
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
  <main>{chapters}{glossary}{bibliography}</main>
</body>
</html>
"""


def _render_html_chapter(
    book: AcceptedBook,
    chapter: Any,
    *,
    source_assets: Mapping[str, _SourceAssetReference],
    release_links: _ReleaseLinks,
) -> str:
    translation_by_id = {item.block_id: item for item in chapter.translations}
    units_by_anchor = _units_by_first_anchor(chapter.learning_units)
    anchors: list[str] = []
    for anchor in chapter.source_anchors:
        source = _render_html_source(
            anchor,
            source_assets=source_assets,
            release_links=release_links,
        )
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
            f'<article class="source-anchor" '
            f'id="anchor-{_anchor_token(anchor.block_id)}" '
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
    anchor: SourceAnchor,
    *,
    source_assets: Mapping[str, _SourceAssetReference],
    release_links: _ReleaseLinks,
) -> str:
    payload = anchor.payload
    if anchor.kind == "heading":
        level = max(3, min(6, int(payload["level"]) + 2))
        return f"<h{level}>{escape_html(str(payload['text']))}</h{level}>"
    if anchor.kind == "paragraph":
        return (
            f"<p>{_render_html_inline(payload['inline_spans'], release_links)}</p>"
        )
    if anchor.kind == "list":
        tag = "ol" if payload["ordered"] else "ul"
        items = "".join(
            f"<li>{_render_html_inline(item['inline_spans'], release_links)}</li>"
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
        provenance = anchor.locator.get("equation_label_provenance")
        source_note = ""
        if (
            isinstance(provenance, Mapping)
            and provenance.get("source_label") != payload["label"]
            and isinstance(provenance.get("source_label"), str)
        ):
            source_note = (
                '<span class="equation-source-label">'
                f'Rich-source label: {escape_html(provenance["source_label"])}</span>'
            )
        return (
            f'<div class="math math-display" data-tex="{escape_html(str(payload["tex"]))}">'
            f'{escape_html(str(payload["tex"]))}</div>{label}{source_note}'
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
        head = f"<thead><tr>{headers}</tr></thead>" if headers else ""
        return f"<table>{caption}{head}<tbody>{rows}</tbody></table>"
    source = source_assets.get(anchor.block_id)
    caption = escape_html(str(payload["caption"]))
    alt = escape_html(str(payload["alt_text"]))
    if source is None:
        target = str(payload["target"])
        link = (
            f' <a href="{escape_html(release_links.html_target(target))}">'
            "Original figure URL</a>"
            if target
            else ""
        )
        description = alt or "Figure asset was not frozen with the source."
        return (
            '<figure class="figure-unfrozen">'
            f"<p>{description}{link}</p><figcaption>{caption}</figcaption></figure>"
        )
    original_link = ""
    if source.display_path != source.original_path:
        original_link = (
            f' <a class="figure-original" href="{escape_html(source.original_path)}" '
            f'type="{escape_html(source.media_type)}">Open original figure</a>'
        )
    return (
        f'<figure><img src="{escape_html(source.display_path)}" alt="{alt}">'
        f"<figcaption>{caption}{original_link}</figcaption></figure>"
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


def _paper_landing_url(identifier: str) -> str | None:
    # Keep the deterministic renderer importable without loading arc-paper's
    # optional execution facade and its LLM runtime.
    from arc_paper import paper_landing_url

    return paper_landing_url(identifier)


def _render_html_bibliography(book: AcceptedBook) -> str:
    if not book.bibliography:
        return ""
    rows: list[str] = []
    for item in book.bibliography:
        title = f"<strong>{escape_html(item.title)}</strong>"
        landing_url = _paper_landing_url(item.source)
        if landing_url is not None:
            title = f'<a href="{escape_html(landing_url)}">{title}</a>'
        rows.append(
            f'<li id="reference-{escape_html(item.evidence_id)}">'
            f"{title} — {escape_html(item.source)} "
            f'<code>{escape_html(item.evidence_id)}</code></li>'
        )
    return (
        '<section class="bibliography" id="references">'
        f"<h2>References</h2><ol>{''.join(rows)}</ol></section>"
    )


def _render_html_inline(
    spans: Sequence[Mapping[str, Any]], release_links: _ReleaseLinks
) -> str:
    values: list[str] = []
    for item in spans:
        kind = str(item["kind"])
        if kind == "link":
            target = release_links.html_target(str(item["target"]))
            values.append(
                f'<a href="{escape_html(target)}">'
                f'{escape_html(str(item["text"]))}</a>'
            )
        elif kind == "math":
            values.append(
                f'<span class="math math-inline" '
                f'data-tex="{escape_html(str(item["tex"]))}">'
                f'{escape_html(str(item["source"]))}</span>'
            )
        else:
            values.append(_html_text(str(item["text"])))
    return "".join(values)


def _html_citations(citations: Sequence[str]) -> str:
    if not citations:
        return ""
    return '<p class="citations">' + " · ".join(
        escape_html(item) for item in citations
    ) + "</p>"


def _html_text(value: str) -> str:
    return escape_html(value).replace("\n", "<br>")


def _render_tex(
    book: AcceptedBook,
    *,
    source_paths: Mapping[str, str],
    release_links: _ReleaseLinks | None = None,
) -> str:
    exact_links = release_links or _release_links(book)
    chapters = "\n".join(
        _render_tex_chapter(
            book,
            chapter,
            source_paths=source_paths,
            release_links=exact_links,
        )
        for chapter in book.chapters
    )
    glossary = _render_tex_glossary(book)
    bibliography = _render_tex_bibliography(book)
    return rf"""\documentclass[10pt]{{article}}
\usepackage[margin=21mm]{{geometry}}
\usepackage{{fontspec}}
\usepackage{{xeCJK}}
\usepackage{{amsmath,amssymb}}
\usepackage[table]{{xcolor}}
\usepackage{{graphicx}}
\usepackage{{longtable,booktabs,array}}
\usepackage[breakable]{{tcolorbox}}
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
{bibliography}
\end{{document}}
"""


def _render_tex_chapter(
    book: AcceptedBook,
    chapter: Any,
    *,
    source_paths: Mapping[str, str],
    release_links: _ReleaseLinks,
) -> str:
    translations = {item.block_id: item for item in chapter.translations}
    units = _units_by_first_anchor(chapter.learning_units)
    values = [
        rf"\section{{{_tex_escape(chapter.title)}}}",
        rf"\textbf{{Chapter guide.}} {_render_tex_prose(chapter.guide)}",
    ]
    for anchor in chapter.source_anchors:
        page = (
            rf"\par\noindent\hfill{{\footnotesize source p. {anchor.page_number}}}\par"
            if anchor.page_number is not None
            else ""
        )
        anchor_target = (
            rf"\par\noindent"
            rf"\hypertarget{{anchor-{_anchor_token(anchor.block_id)}}}{{}}"
            rf"{page}"
        )
        source = _render_tex_source(
            anchor,
            source_paths=source_paths,
            release_links=release_links,
        )
        if anchor.kind == "table":
            # longtable must remain in the main vertical list to split across
            # pages.  Nesting it in a tcolorbox/tabular silently clips rows.
            values.append(
                anchor_target
                + r"\textbf{Source}\par"
                + source
            )
        else:
            values.append(
                anchor_target
                + rf"\begin{{tcolorbox}}[breakable,colback=SourceBg,colframe=SourceBg,"
                rf"boxrule=0pt,arc=1mm,left=2mm,right=2mm,top=1.5mm,bottom=1.5mm]"
                rf"\textbf{{Source}}\par {source}"
                rf"\end{{tcolorbox}}"
            )
        translation = translations.get(anchor.block_id)
        if translation is not None:
            values.append(
                rf"\begin{{tcolorbox}}[breakable,colback=TranslationBg,colframe=TranslationBg,"
                rf"boxrule=0pt,arc=1mm,left=2mm,right=2mm,top=1.5mm,bottom=1.5mm]"
                rf"\textbf{{Translation}}\par {_render_tex_prose(translation.text)}"
                rf"\end{{tcolorbox}}"
            )
        for unit in units.get(anchor.block_id, ()):
            citations = (
                rf"\par{{\footnotesize\itshape Evidence: {_tex_escape('; '.join(unit.citations))}}}"
                if unit.citations
                else ""
            )
            values.append(
                rf"\begin{{tcolorbox}}[breakable,colback=LearningBg,colframe=LearningBg,"
                rf"boxrule=0pt,arc=1mm,left=2mm,right=2mm,top=1.5mm,bottom=1.5mm]"
                rf"\textbf{{{_tex_escape(unit.title)}}}\par "
                rf"{_render_tex_prose(unit.content)}{citations}\end{{tcolorbox}}"
            )
        values.append(r"\medskip")
    return "\n".join(values)


def _render_tex_source(
    anchor: SourceAnchor,
    *,
    source_paths: Mapping[str, str],
    release_links: _ReleaseLinks,
) -> str:
    payload = anchor.payload
    if anchor.kind == "heading":
        return rf"\textbf{{{_tex_escape(payload['text'])}}}"
    if anchor.kind == "paragraph":
        return _render_tex_inline(payload["inline_spans"], release_links)
    if anchor.kind == "list":
        environment = "enumerate" if payload["ordered"] else "itemize"
        items = "\n".join(
            rf"\item {_render_tex_inline(item['inline_spans'], release_links)}"
            for item in payload["items"]
        )
        return rf"\begin{{{environment}}}[leftmargin=*]{items}\end{{{environment}}}"
    if anchor.kind == "code":
        return _render_tex_code(str(payload["text"]))
    if anchor.kind == "equation":
        label = (
            rf"\tag{{{_tex_escape(payload['label'])}}}" if payload["label"] else ""
        )
        provenance = anchor.locator.get("equation_label_provenance")
        source_note = ""
        if (
            isinstance(provenance, Mapping)
            and provenance.get("source_label") != payload["label"]
            and isinstance(provenance.get("source_label"), str)
        ):
            source_note = (
                rf"\par{{\footnotesize\itshape Rich-source label: "
                rf"{_tex_escape(provenance['source_label'])}}}"
            )
        return rf"\[{_sanitize_math(payload['tex'])}{label}\]" + source_note
    if anchor.kind == "table":
        headers = payload["headers"]
        rows_value = payload["rows"]
        width = len(headers) or (len(rows_value[0]) if rows_value else 1)
        columns = " ".join([r">{\raggedright\arraybackslash}p{" + f"{0.88 / width:.3f}" + r"\linewidth}"] * width)
        header = " & ".join(_tex_escape(item) for item in headers) + r" \\"
        rows = "\n".join(
            " & ".join(_tex_escape(cell) for cell in row) + r" \\"
            for row in rows_value
        )
        caption = (
            rf"\textit{{{_tex_escape(payload['caption'])}}}\par"
            if payload["caption"]
            else ""
        )
        repeated_header = (
            rf"\toprule {header}\midrule\endfirsthead"
            "\n"
            rf"\toprule {header}\midrule\endhead"
            "\n"
            if headers
            else "\n".join(
                (r"\toprule\endfirsthead", r"\toprule\endhead", "")
            )
        )
        return (
            caption
            + rf"\begin{{longtable}}{{{columns}}}"
            + repeated_header
            + rows
            + r"\bottomrule\end{longtable}"
        )
    source = source_paths.get(anchor.block_id)
    caption = _tex_escape(payload["caption"])
    if source and not urlparse(source).scheme:
        image = rf"\includegraphics[width=0.82\linewidth]{{\detokenize{{{source}}}}}"
    else:
        target = str(payload["target"])
        link = (
            rf"\par {release_links.tex_link(target, target)}"
            if target
            else ""
        )
        image = (
            rf"\textit{{Figure asset was not frozen with the source.}} "
            rf"{_tex_escape(payload['alt_text'])}{link}"
        )
    return rf"\begin{{center}}{image}\par{{\footnotesize {caption}}}\end{{center}}"


def _render_tex_glossary(book: AcceptedBook) -> str:
    if not book.glossary:
        return ""
    rows = "\n".join(
        rf"\textbf{{{_tex_escape(item.term)}}} & "
        rf"{_tex_escape(item.translated_term)} & {_render_tex_prose(item.definition)}"
        + (
            rf"\par{{\footnotesize\itshape Evidence: "
            rf"{_tex_escape('; '.join(item.citations))}}}"
            if item.citations
            else ""
        )
        + r" \\"
        for item in book.glossary
    )
    return (
        r"\section{Glossary}"
        r"\begin{longtable}{p{0.2\linewidth}p{0.2\linewidth}p{0.5\linewidth}}"
        r"\toprule Source term & Translation & Definition \\\midrule "
        + rows
        + r"\bottomrule\end{longtable}"
    )


def _render_tex_bibliography(book: AcceptedBook) -> str:
    if not book.bibliography:
        return ""
    rows: list[str] = []
    for item in book.bibliography:
        title = rf"\textbf{{{_tex_escape(item.title)}}}"
        landing_url = _paper_landing_url(item.source)
        if landing_url is not None:
            title = rf"\href{{{_tex_url(landing_url)}}}{{{title}}}"
        rows.append(
            rf"\item {title} --- {_tex_escape(item.source)} "
            rf"[{_tex_escape(item.evidence_id)}]"
        )
    return (
        r"\section{References}\begin{enumerate}"
        + "\n".join(rows)
        + r"\end{enumerate}"
    )


def _render_tex_inline(
    spans: Sequence[Mapping[str, Any]], release_links: _ReleaseLinks
) -> str:
    values: list[str] = []
    for item in spans:
        kind = str(item["kind"])
        if kind == "link":
            values.append(
                release_links.tex_link(
                    str(item["target"]),
                    str(item["text"]),
                )
            )
        elif kind == "math":
            values.append(rf"\({_sanitize_math(item['tex'])}\)")
        else:
            values.append(_tex_escape(item["text"]))
    return "".join(values)


def _render_tex_code(value: str) -> str:
    lines = value.splitlines() or [""]
    return (
        r"{\ttfamily\small\raggedright "
        + r"\par ".join(_tex_escape(line) or r"\strut" for line in lines)
        + "}"
    )


def _render_tex_prose(value: Any) -> str:
    """Render plain prose while preserving authored line and paragraph breaks."""

    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [
        paragraph
        for paragraph in re.split(r"\n[ \t]*\n+", normalized)
        if paragraph.strip()
    ]
    return r"\par ".join(
        r"\newline{} ".join(_tex_escape(line) for line in paragraph.split("\n"))
        for paragraph in paragraphs
    )


def _normalize_pdf_search_text(value: Any) -> str:
    """Normalize Unicode and whitespace while retaining lexical boundaries."""

    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\N{SOFT HYPHEN}", "")
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) != "Cf"
    )
    return re.sub(r"\s+", " ", text).strip()


def _pdf_search_alternatives(
    extracted: Any, expected: str
) -> tuple[str, ...]:
    """Return projections for explicit extractor line-wrap behaviors."""

    text = unicodedata.normalize("NFKC", str(extracted))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\N{SOFT HYPHEN}", "")
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) != "Cf"
    )
    line_break = r"(?:\n|\f|\v|\u2028|\u2029)"
    values = [_normalize_pdf_search_text(text)]
    hyphen_preserved = re.sub(
        rf"(?<=\w)-[ \t]*{line_break}[ \t]*(?=\w)",
        "-",
        text,
    )
    values.append(_normalize_pdf_search_text(hyphen_preserved))
    typeset_dehyphenated = re.sub(
        rf"(?P<left>\w+)-[ \t]*{line_break}[ \t]*"
        rf"(?P<right>\w+)",
        lambda match: (
            match.group("left") + match.group("right")
            if match.group("left") + match.group("right") in expected
            and (
                match.group("left") + "-" + match.group("right")
                not in expected
            )
            else match.group(0)
        ),
        text,
    )
    values.append(_normalize_pdf_search_text(typeset_dehyphenated))
    if _allows_pdf_line_concat(expected):
        line_unwrapped = re.sub(
            rf"(?<=\S)[ \t]*{line_break}[ \t]*(?=\S)",
            "",
            text,
        )
        values.append(_normalize_pdf_search_text(line_unwrapped))
    return tuple(dict.fromkeys(value for value in values if value))


def _allows_pdf_line_concat(expected: str) -> bool:
    value = expected.strip()
    if not value or any(character.isspace() for character in value):
        return False
    return value.casefold().startswith(
        ("http://", "https://", "mailto:", "doi:", "arxiv:", "inspire:")
    )


def _pdf_text_contains(extracted_text: str, expected: Any) -> bool:
    normalized_expected = _normalize_pdf_search_text(expected)
    return bool(normalized_expected) and any(
        normalized_expected in alternative
        for alternative in _pdf_search_alternatives(
            extracted_text, normalized_expected
        )
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


def _asset_suffix(media_type: str) -> str:
    suffixes = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "application/pdf": ".pdf",
        "application/postscript": ".eps",
        "application/eps": ".eps",
        "image/eps": ".eps",
        "image/x-eps": ".eps",
    }
    if media_type in suffixes:
        return suffixes[media_type]
    return ".bin"


def _requires_web_preview(media_type: str) -> bool:
    return media_type == "application/pdf" or _is_eps(media_type)


def _is_eps(media_type: str) -> bool:
    return media_type in {
        "application/postscript",
        "application/eps",
        "image/eps",
        "image/x-eps",
    }


def _prepare_web_asset(
    source: Path,
    *,
    media_type: str,
    digest: str,
) -> Path:
    if not _requires_web_preview(media_type):
        return source
    target = source.with_name(f"{digest}.preview.png")
    if media_type == "application/pdf":
        converter = shutil.which("pdftocairo")
        if converter is None:
            raise CompanionRenderError(
                "pdftocairo is required to preview PDF source figures in Web"
            )
        output_prefix = target.with_suffix("")
        _run(
            [
                converter,
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-png",
                "-r",
                "144",
                str(source),
                str(output_prefix),
            ]
        )
    else:
        _convert_eps_to_png(
            source,
            target=target,
            purpose="preview EPS source figures in Web",
        )
    _require_converted_asset(target, media_type)
    return target


def _prepare_pdf_asset(
    source: Path,
    *,
    media_type: str,
    digest: str,
) -> Path:
    if media_type in {"image/png", "image/jpeg", "application/pdf"}:
        return source
    target = source.with_name(f"{digest}.png")
    if media_type == "image/svg+xml":
        converter = shutil.which("rsvg-convert")
        if converter is None:
            raise CompanionRenderError(
                "rsvg-convert is required to render SVG source figures in PDF"
            )
        _run([converter, "--format=png", "--output", str(target), str(source)])
    elif media_type in {"image/gif", "image/webp"}:
        converter = shutil.which("magick") or shutil.which("convert")
        if converter is None:
            raise CompanionRenderError(
                "ImageMagick is required to render GIF/WebP source figures in PDF"
            )
        _run(
            [
                converter,
                f"{source}[0]",
                "-strip",
                "-define",
                "png:exclude-chunk=time,date",
                str(target),
            ]
        )
    elif _is_eps(media_type):
        _convert_eps_to_png(
            source,
            target=target,
            purpose="render EPS source figures in PDF",
        )
    else:
        raise CompanionRenderError(
            f"unsupported source figure media type for PDF: {media_type or 'unknown'}"
        )
    _require_converted_asset(target, media_type)
    return target


def _convert_eps_to_png(source: Path, *, target: Path, purpose: str) -> None:
    converter = shutil.which("gs")
    if converter is None:
        raise CompanionRenderError(f"Ghostscript is required to {purpose}")
    _run(
        [
            converter,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-dEPSCrop",
            "-sDEVICE=pngalpha",
            "-r144",
            f"-sOutputFile={target}",
            str(source),
        ]
    )


def _require_converted_asset(target: Path, media_type: str) -> None:
    if not target.is_file() or target.stat().st_size == 0:
        raise CompanionRenderError(
            f"source figure conversion produced no output: {media_type}"
        )


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
    encoded = quote(
        value,
        safe=":/?#[]@!$&'()*+,;=%-._~",
    )
    replacements = {
        "%": r"\%",
        "&": r"\&",
        "#": r"\#",
        "$": r"\$",
        "_": r"\_",
        "~": r"\string~",
    }
    return "".join(replacements.get(char, char) for char in encoded)


def _anchor_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
.equation-source-label { display: block; color: var(--muted); text-align: right; font-size: .8rem; }
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
