"""Standalone HTML publication rendering for rich sources and atomic overlays."""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
from html import escape
from html.parser import HTMLParser
from importlib.resources import files
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any

from arc_paper import RichDocument

from .contracts import (
    FragmentRevision,
    Layer,
    Publication,
    anchor_block_from_rich_block,
    fragment_revision_to_document,
    publication_to_document,
    source_identity_to_document,
)
from .markdown import read_fragment_revision
from .resolver import RevisionDiagnostic, resolve_fragment_revision_files
from .standalone_html import StandaloneHtmlError, write_standalone_html
from .workspace import RenderWorkspaceError, read_layer, read_publication


HTML_RENDER_RECIPE = "arc.render.standalone_html.v1"
READER_PAYLOAD_SCHEMA = "arc.render.reader_payload.v1"
AssetLoader = Callable[[str], bytes | None]


class HTMLRenderError(RuntimeError):
    """A publication cannot be rendered as a verified standalone HTML file."""


@dataclass(frozen=True)
class RenderedHTML:
    publication_digest: str
    html_path: Path
    selected_revision_digests: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    renderer_recipe: str = HTML_RENDER_RECIPE


def render_publication_html(
    publication_path: str | Path,
    output_path: str | Path,
    *,
    asset_loader: AssetLoader | None = None,
) -> RenderedHTML:
    """Load a publication workspace and write one portable HTML reader."""

    source_path = Path(publication_path).resolve()
    try:
        publication = read_publication(source_path)
    except RenderWorkspaceError as exc:
        raise HTMLRenderError(str(exc)) from exc
    return render_html(
        publication,
        output_path,
        project_root=source_path.parent,
        asset_loader=asset_loader,
    )


def render_html(
    publication: Publication,
    output_path: str | Path,
    *,
    project_root: str | Path,
    asset_loader: AssetLoader | None = None,
) -> RenderedHTML:
    """Render ``publication`` and discovered project revisions to one HTML."""

    if not isinstance(publication, Publication):
        raise TypeError("publication must be a Publication")
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise HTMLRenderError(f"publication project root is missing: {root}")
    output = Path(output_path).resolve()
    revisions, selected, diagnostics = _load_revisions(publication, root)
    _validate_selected(publication, selected)
    resources = _embedded_resources(
        publication,
        root=root,
        asset_loader=asset_loader,
    )
    payload = _reader_payload(
        publication,
        revisions=revisions,
        resources=resources,
        diagnostics=diagnostics,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".arc-render-html-", dir=output.parent
    ) as raw:
        bundle = Path(raw)
        assets = bundle / "assets"
        _copy_web_assets(assets)
        index = bundle / "index.html"
        index.write_text(_html_shell(publication, payload), encoding="utf-8")
        try:
            write_standalone_html(index, output)
        except StandaloneHtmlError as exc:
            raise HTMLRenderError(str(exc)) from exc
    validate_standalone_html(publication, output)
    warnings = tuple(
        _diagnostic_text(item)
        for item in diagnostics
    )
    return RenderedHTML(
        publication_digest=publication.publication_digest,
        html_path=output,
        selected_revision_digests=tuple(
            item.semantic_digest for item in selected
        ),
        warnings=warnings,
    )


def validate_publication_workspace(
    publication_path: str | Path,
    *,
    asset_loader: AssetLoader | None = None,
) -> tuple[str, ...]:
    """Validate one publication, its layers, revisions, and declared assets."""

    source_path = Path(publication_path).resolve()
    try:
        publication = read_publication(source_path)
    except RenderWorkspaceError as exc:
        raise HTMLRenderError(str(exc)) from exc
    _revisions, selected, diagnostics = _load_revisions(
        publication, source_path.parent
    )
    _validate_selected(publication, selected)
    _embedded_resources(
        publication,
        root=source_path.parent,
        asset_loader=asset_loader,
    )
    return tuple(_diagnostic_text(item) for item in diagnostics)


def validate_standalone_html(
    publication: Publication,
    html_path: str | Path,
) -> None:
    """Validate identity, source coverage, and automatic-resource portability."""

    path = Path(html_path).resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HTMLRenderError("standalone HTML is unreadable") from exc
    if (
        f'data-publication-digest="{publication.publication_digest}"'
        not in text
    ):
        raise HTMLRenderError("standalone HTML is not bound to its publication")
    payload = _extract_reader_payload(text)
    if payload.get("schema_version") != READER_PAYLOAD_SCHEMA:
        raise HTMLRenderError("standalone HTML contains an invalid reader payload")
    encoded_publication = payload.get("publication")
    if not isinstance(encoded_publication, Mapping):
        raise HTMLRenderError("standalone HTML has no encoded publication")
    if (
        encoded_publication.get("publication_digest")
        != publication.publication_digest
    ):
        raise HTMLRenderError("standalone HTML publication digest is inconsistent")
    source = encoded_publication.get("source_document")
    if not isinstance(source, Mapping):
        raise HTMLRenderError("standalone HTML has no rich source")
    blocks = source.get("blocks")
    if not isinstance(blocks, list):
        raise HTMLRenderError("standalone HTML source blocks are invalid")
    expected = [item.block_id for item in publication.source_document.blocks]
    actual = [
        item.get("block_id")
        for item in blocks
        if isinstance(item, Mapping)
    ]
    if actual != expected:
        raise HTMLRenderError("standalone HTML source block order is invalid")
    portability = _PortabilityValidator()
    portability.feed(text)
    portability.close()
    if portability.errors:
        raise HTMLRenderError(
            "standalone HTML contains an external automatic resource: "
            + portability.errors[0]
        )
    if "window.markdownit" not in text or "window.katex" not in text:
        raise HTMLRenderError("standalone HTML is missing embedded reader libraries")
    if "@media print" not in text:
        raise HTMLRenderError("standalone HTML is missing its print stylesheet")


def _load_revisions(
    publication: Publication,
    root: Path,
) -> tuple[
    tuple[FragmentRevision, ...],
    tuple[FragmentRevision, ...],
    tuple[RevisionDiagnostic, ...],
]:
    source = publication.source
    paths_by_fragment: dict[str, list[Path]] = defaultdict(list)
    fragment_order: list[str] = []
    claimed_paths: dict[Path, str] = {}
    seen_layer_producers: set[str] = set()

    for layer_reference in publication.layers:
        layer_path = _project_path(root, layer_reference.path, "layer")
        try:
            layer = read_layer(layer_path)
        except RenderWorkspaceError as exc:
            raise HTMLRenderError(str(exc)) from exc
        _validate_layer_reference(layer, layer_reference)
        if layer.producer in seen_layer_producers:
            raise HTMLRenderError(
                f"publication contains duplicate producer layer: {layer.producer}"
            )
        seen_layer_producers.add(layer.producer)
        for revision_reference in layer.initial_revisions:
            revision_path = _project_path(
                root, revision_reference.path, "fragment revision"
            )
            try:
                initial = read_fragment_revision(revision_path)
            except ValueError as exc:
                raise HTMLRenderError(
                    f"initial fragment revision is invalid: {revision_reference.path}"
                ) from exc
            if (
                initial.fragment_id != revision_reference.fragment_id
                or initial.revision != revision_reference.revision
                or initial.semantic_digest != revision_reference.semantic_digest
                or initial.source != source
            ):
                raise HTMLRenderError(
                    "initial fragment revision does not match its layer reference: "
                    f"{revision_reference.path}"
                )
            existing = claimed_paths.get(revision_path)
            if existing is not None and existing != revision_reference.fragment_id:
                raise HTMLRenderError(
                    "two fragment identities claim the same revision path"
                )
            claimed_paths[revision_path] = revision_reference.fragment_id
            paths_by_fragment[revision_reference.fragment_id].append(revision_path)
            if revision_reference.fragment_id not in fragment_order:
                fragment_order.append(revision_reference.fragment_id)

    fragments_root = root / "fragments"
    if fragments_root.exists():
        if not fragments_root.is_dir():
            raise HTMLRenderError("project fragments path is not a directory")
        for candidate in sorted(fragments_root.rglob("*.md")):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(fragments_root.resolve())
            except ValueError as exc:
                raise HTMLRenderError("fragment path escapes the project") from exc
            fragment_id = claimed_paths.get(resolved, candidate.parent.name)
            if resolved not in paths_by_fragment[fragment_id]:
                paths_by_fragment[fragment_id].append(resolved)
            if fragment_id not in fragment_order:
                fragment_order.append(fragment_id)

    revisions: list[FragmentRevision] = []
    selected: list[FragmentRevision] = []
    diagnostics: list[RevisionDiagnostic] = []
    selected_ids: set[str] = set()
    for fragment_id in fragment_order:
        resolution = resolve_fragment_revision_files(
            paths_by_fragment[fragment_id],
            fragment_id=fragment_id,
        )
        diagnostics.extend(resolution.diagnostics)
        revisions.extend(resolution.revisions)
        if resolution.selected is None:
            continue
        if resolution.selected.source != source:
            raise HTMLRenderError(
                f"fragment binds another rich source: {fragment_id}"
            )
        if fragment_id in selected_ids:
            raise HTMLRenderError(f"duplicate selected fragment: {fragment_id}")
        selected_ids.add(fragment_id)
        selected.append(resolution.selected)

    order_index = {
        fragment_id: index for index, fragment_id in enumerate(fragment_order)
    }
    selected.sort(
        key=lambda item: (item.priority, order_index[item.fragment_id])
    )
    return tuple(revisions), tuple(selected), tuple(diagnostics)


def _validate_layer_reference(layer: Layer, reference: Any) -> None:
    if (
        layer.source != reference.source
        or layer.producer != reference.producer
        or layer.layer_digest != reference.layer_digest
    ):
        raise HTMLRenderError(
            f"layer does not match its publication reference: {reference.path}"
        )


def _validate_selected(
    publication: Publication,
    selected: Sequence[FragmentRevision],
) -> None:
    document = publication.source_document
    blocks = {item.block_id: item for item in document.blocks}
    sections = {item.section_id for item in document.sections}
    bibliography_ids = {
        str(
            item.get("evidence_id")
            or item.get("citation_id")
            or item.get("id")
            or ""
        )
        for item in publication.bibliography
    }
    bibliography_ids.discard("")
    for revision in selected:
        anchor = revision.anchor
        if anchor.kind.value == "block" and anchor.target_id not in blocks:
            raise HTMLRenderError(
                f"fragment anchor block is absent from the source: {revision.fragment_id}"
            )
        if anchor.kind.value == "section" and anchor.target_id not in sections:
            raise HTMLRenderError(
                f"fragment anchor section is absent from the source: {revision.fragment_id}"
            )
        for frozen in anchor.related_blocks:
            current = blocks.get(frozen.block_id)
            if current is None:
                raise HTMLRenderError(
                    f"fragment provenance block is absent: {revision.fragment_id}"
                )
            if anchor_block_from_rich_block(current) != frozen:
                raise HTMLRenderError(
                    f"fragment provenance differs from the rich source: {revision.fragment_id}"
                )
        unknown = next(
            (item for item in revision.citation_ids if item not in bibliography_ids),
            None,
        )
        if unknown is not None:
            raise HTMLRenderError(
                f"fragment citation is absent from the bibliography: {unknown}"
            )


def _embedded_resources(
    publication: Publication,
    *,
    root: Path,
    asset_loader: AssetLoader | None,
) -> tuple[dict[str, Any], ...]:
    declarations: dict[str, Mapping[str, Any]] = {}
    for item in publication.resources:
        digest = item.get("artifact_digest") or item.get("digest")
        if isinstance(digest, str):
            declarations[digest.casefold()] = item
    values: list[dict[str, Any]] = []
    for asset in publication.source_document.assets:
        payload: bytes | None = None
        if asset_loader is not None:
            payload = asset_loader(asset.artifact_digest)
        if payload is None:
            declaration = declarations.get(asset.artifact_digest)
            if declaration is not None:
                relative = declaration.get("path")
                if not isinstance(relative, str):
                    raise HTMLRenderError(
                        f"resource path is missing: {asset.artifact_digest}"
                    )
                path = _project_path(root, relative, "resource")
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    raise HTMLRenderError(
                        f"resource is unreadable: {relative}"
                    ) from exc
        if payload is None:
            raise HTMLRenderError(
                f"source asset is unavailable: {asset.artifact_digest}"
            )
        actual = hashlib.sha256(payload).hexdigest()
        if actual != asset.artifact_digest or len(payload) != asset.size:
            raise HTMLRenderError(
                f"source asset bytes do not match metadata: {asset.artifact_digest}"
            )
        values.append(
            {
                "artifact_digest": asset.artifact_digest,
                "media_type": asset.media_type,
                "logical_name": asset.logical_name,
                "size": asset.size,
                "data_uri": (
                    f"data:{asset.media_type};base64,"
                    f"{base64.b64encode(payload).decode('ascii')}"
                ),
            }
        )
    return tuple(values)


def _reader_payload(
    publication: Publication,
    *,
    revisions: Sequence[FragmentRevision],
    resources: Sequence[Mapping[str, Any]],
    diagnostics: Sequence[RevisionDiagnostic],
) -> dict[str, Any]:
    return {
        "schema_version": READER_PAYLOAD_SCHEMA,
        "renderer_recipe": HTML_RENDER_RECIPE,
        "publication": publication_to_document(publication),
        "source_identity": source_identity_to_document(publication.source),
        "block_fingerprints": {
            item.block_id: anchor_block_from_rich_block(item).content_fingerprint
            for item in publication.source_document.blocks
        },
        "revisions": [
            {
                "metadata": fragment_revision_to_document(item),
                "markdown_body": item.markdown_body,
                "semantic_digest": item.semantic_digest,
            }
            for item in revisions
        ],
        "resources": list(resources),
        "diagnostics": [_diagnostic_text(item) for item in diagnostics],
    }


def _html_shell(publication: Publication, payload: Mapping[str, Any]) -> str:
    profile = publication.reader_profile
    title = str(
        profile.get("title")
        or publication.labels.get("document_title")
        or _source_title(publication.source_document)
        or publication.labels.get("untitled_document")
        or "Untitled document"
    )
    language = str(
        profile.get("target_language")
        or profile.get("source_language")
        or "und"
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("</script", r"<\/script")
    digest = publication.publication_digest
    return f"""<!doctype html>
<html lang="{escape(language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="assets/katex/katex.min.css">
  <link rel="stylesheet" href="assets/reader.css">
  <script defer src="assets/markdown-it/markdown-it.min.js"></script>
  <script defer src="assets/katex/katex.min.js"></script>
  <script defer src="assets/reader.js"></script>
</head>
<body data-publication-digest="{digest}">
  <div class="arc-fixed-tools">
    <button id="arc-contents-toggle" class="arc-tool-button" type="button"
      aria-controls="arc-contents" aria-expanded="true">☰</button>
    <button id="arc-connect" class="arc-tool-button" type="button"></button>
  </div>
  <p id="arc-storage-status" class="arc-storage-status" hidden></p>
  <div id="arc-shell" class="arc-shell">
    <aside id="arc-contents" class="arc-contents">
      <nav aria-labelledby="arc-contents-heading">
        <h2 id="arc-contents-heading">Contents</h2>
        <ol id="arc-contents-list"></ol>
      </nav>
    </aside>
    <div class="arc-reader">
      <header id="arc-book-header" class="arc-book-header"></header>
      <main id="arc-document" class="arc-document"></main>
    </div>
  </div>
  <div id="arc-tooltip" class="arc-tooltip" role="tooltip" hidden></div>
  <dialog id="arc-editor-dialog" class="arc-dialog">
    <div class="arc-dialog-form">
      <header class="arc-dialog-header">
        <h2 id="arc-editor-heading">Edit overlay</h2>
        <button id="arc-editor-close" type="button" aria-label="Close">×</button>
      </header>
      <div class="arc-dialog-fields">
        <label>Title<input id="arc-editor-title" type="text"></label>
        <label>Role
          <select id="arc-editor-role">
            <option value="translation">Translation</option>
            <option value="companion">Companion</option>
            <option value="guide">Guide</option>
            <option value="note">Note</option>
          </select>
        </label>
        <label>Priority<input id="arc-editor-priority" type="number" min="1"></label>
      </div>
      <div class="arc-editor-grid">
        <div class="arc-editor-pane">
          <label>Markdown
            <textarea id="arc-editor-markdown" spellcheck="true"></textarea>
          </label>
        </div>
        <div class="arc-editor-pane">
          <label>Preview</label>
          <div id="arc-editor-preview" class="arc-editor-preview"></div>
        </div>
      </div>
      <div id="arc-editor-history" class="arc-history"></div>
      <footer class="arc-dialog-footer">
        <button id="arc-editor-cancel" type="button">Close</button>
        <button id="arc-editor-save" type="button">Save as new revision</button>
      </footer>
    </div>
  </dialog>
  <script id="arc-render-payload" type="application/json">{encoded}</script>
</body>
</html>
"""


def _source_title(document: RichDocument) -> str:
    for block in document.blocks:
        if (
            block.kind.value == "heading"
            and int(block.payload["level"]) == 1
        ):
            return str(block.payload["text"])
    return ""


def _copy_web_assets(output: Path) -> None:
    root = files("arc_render").joinpath("web_assets")
    _copy_traversable(root, output)
    katex_css = output / "katex" / "katex.min.css"
    css = katex_css.read_text(encoding="utf-8")
    css = re.sub(
        r'(src:url\((?P<woff2>[^)]+[.]woff2)\) format\("woff2"\))'
        r',url\([^)]+[.]woff\) format\("woff"\)'
        r',url\([^)]+[.]ttf\) format\("truetype"\)',
        r"\1",
        css,
    )
    katex_css.write_text(css, encoding="utf-8")


def _copy_traversable(source: Any, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = output / child.name
        if child.is_dir():
            _copy_traversable(child, target)
        else:
            target.write_bytes(child.read_bytes())


def _project_path(root: Path, relative: str, description: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise HTMLRenderError(f"{description} path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != relative:
        raise HTMLRenderError(f"{description} path is unsafe: {relative}")
    path = (root / Path(*pure.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTMLRenderError(f"{description} path escapes the project") from exc
    if not path.is_file():
        raise HTMLRenderError(f"{description} is missing: {relative}")
    return path


def _extract_reader_payload(text: str) -> Mapping[str, Any]:
    match = re.search(
        r'<script id="arc-render-payload" type="application/json">'
        r"(?P<value>.*?)</script>",
        text,
        re.DOTALL,
    )
    if match is None:
        raise HTMLRenderError("standalone HTML has no reader payload")
    try:
        value = json.loads(match.group("value").replace(r"<\/script", "</script"))
    except json.JSONDecodeError as exc:
        raise HTMLRenderError("standalone HTML reader payload is invalid") from exc
    if not isinstance(value, Mapping):
        raise HTMLRenderError("standalone HTML reader payload must be an object")
    return value


def _diagnostic_text(item: RevisionDiagnostic) -> str:
    detail = f" ({', '.join(item.paths)})" if item.paths else ""
    return f"{item.code}: {item.message}{detail}"


class _PortabilityValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.errors: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._check(tag, attrs)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._check(tag, attrs)

    def _check(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {
            key.casefold(): value
            for key, value in attrs
            if value is not None
        }
        lower = tag.casefold()
        for name in ("src", "poster"):
            value = values.get(name)
            if value is not None and not value.startswith("data:"):
                self.errors.append(f"{lower}[{name}]={value}")
        if lower == "object":
            value = values.get("data")
            if value is not None and not value.startswith("data:"):
                self.errors.append(f"object[data]={value}")
        if lower == "link":
            value = values.get("href")
            if value is not None and not (
                value.startswith("data:") or value.startswith("#")
            ):
                self.errors.append(f"link[href]={value}")


__all__ = [
    "AssetLoader",
    "HTML_RENDER_RECIPE",
    "HTMLRenderError",
    "READER_PAYLOAD_SCHEMA",
    "RenderedHTML",
    "render_html",
    "render_publication_html",
    "validate_publication_workspace",
    "validate_standalone_html",
]
