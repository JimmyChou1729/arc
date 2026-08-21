"""Command-line entry point for atomic publications and standalone HTML."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from arc_document import rich_document_from_document

from ._json import strict_json_loads
from .contracts import Publication, source_identity_from_rich_document
from .browser_validation import validate_reader_in_browser
from .html import (
    HTMLRenderError,
    render_publication_html,
    validate_publication_workspace,
    validate_standalone_html,
)
from .standalone_html import StandaloneHtmlError, write_standalone_html
from .workspace import (
    RenderWorkspaceError,
    read_layer,
    read_publication,
    write_publication,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "compose":
            result = _compose(args)
        elif args.command == "render":
            result = _render(args)
        elif args.command == "validate":
            result = _validate(args)
        elif args.command == "standalone-html":
            write_standalone_html(args.input, args.output)
            result = {"html": str(args.output.resolve())}
        else:  # pragma: no cover - argparse enforces the subcommand
            parser.error("a command is required")
    except (
        HTMLRenderError,
        RenderWorkspaceError,
        StandaloneHtmlError,
        OSError,
        UnicodeError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc-render",
        description=(
            "Compose atomic source overlays and render standalone HTML. "
            "PDF generation is intentionally not provided."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compose = subparsers.add_parser(
        "compose", help="compose a publication JSON document"
    )
    compose.add_argument(
        "--source",
        type=Path,
        required=True,
        help="RichDocument JSON document",
    )
    compose.add_argument(
        "--layer",
        type=Path,
        action="append",
        default=[],
        help="layer JSON path; repeat for ordered layers",
    )
    compose.add_argument(
        "--metadata",
        type=Path,
        help=(
            "optional JSON object with glossary, bibliography, labels, "
            "resources, and reader_profile"
        ),
    )
    compose.add_argument("--output", type=Path, required=True)

    render = subparsers.add_parser(
        "render", help="render a publication to standalone HTML"
    )
    render.add_argument("--publication", type=Path, required=True)
    render.add_argument("--html", type=Path, required=True)

    validate = subparsers.add_parser(
        "validate", help="validate a publication and optionally its HTML"
    )
    validate.add_argument("--publication", type=Path, required=True)
    validate.add_argument("--html", type=Path)
    validate.add_argument(
        "--browser",
        action="store_true",
        help="run optional local Chromium reader checks (requires --html)",
    )
    validate.add_argument(
        "--browser-executable",
        help="local Chromium-family executable for --browser",
    )
    validate.add_argument(
        "--browser-timeout",
        type=int,
        default=60,
        help="browser validation timeout in seconds (default: 60)",
    )

    standalone = subparsers.add_parser(
        "standalone-html",
        help="inline one existing local HTML bundle",
    )
    standalone.add_argument("input", type=Path)
    standalone.add_argument("output", type=Path)
    return parser


def _compose(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output.resolve()
    root = output.parent
    metadata = _publication_metadata(args.metadata)
    value = _read_json(args.source, "rich source")
    document = rich_document_from_document(value)

    source = source_identity_from_rich_document(document)
    layer_refs = []
    for path in args.layer:
        resolved = path.resolve()
        layer = read_layer(resolved)
        if layer.source != source:
            raise ValueError(f"layer binds another rich source: {path}")
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"layer must be below the publication directory: {path}"
            ) from exc
        layer_refs.append(layer.reference(relative))

    publication = Publication(
        source_document=document,
        layers=tuple(layer_refs),
        glossary=tuple(metadata["glossary"]),
        bibliography=tuple(metadata["bibliography"]),
        labels=metadata["labels"],
        resources=tuple(metadata["resources"]),
        reader_profile=metadata["reader_profile"],
    )
    write_publication(output, publication)
    return {
        "publication": str(output),
        "publication_digest": publication.publication_digest,
        "source_document_digest": document.document_digest,
        "layer_count": len(layer_refs),
    }


def _render(args: argparse.Namespace) -> dict[str, Any]:
    result = render_publication_html(args.publication, args.html)
    return {
        "html": str(result.html_path),
        "publication_digest": result.publication_digest,
        "selected_revision_digests": list(result.selected_revision_digests),
        "warnings": list(result.warnings),
    }


def _validate(args: argparse.Namespace) -> dict[str, Any]:
    warnings = validate_publication_workspace(args.publication)
    publication = read_publication(args.publication)
    result: dict[str, Any] = {
        "publication": str(args.publication.resolve()),
        "publication_digest": publication.publication_digest,
        "warnings": list(warnings),
    }
    if args.html is not None:
        validate_standalone_html(publication, args.html)
        result["html"] = str(args.html.resolve())
    if args.browser:
        if args.html is None:
            raise ValueError("--browser requires --html")
        browser = validate_reader_in_browser(
            args.html,
            browser_executable=args.browser_executable,
            timeout_seconds=args.browser_timeout,
        )
        result["browser"] = {
            "executable": browser.executable,
            "timeout_seconds": browser.timeout_seconds,
        }
    return result


def _publication_metadata(path: Path | None) -> dict[str, Any]:
    fields = {
        "glossary",
        "bibliography",
        "labels",
        "resources",
        "reader_profile",
    }
    if path is None:
        return {
            "glossary": [],
            "bibliography": [],
            "labels": {},
            "resources": [],
            "reader_profile": {},
        }
    value = _read_json(path, "publication metadata")
    if set(value) != fields:
        raise ValueError("publication metadata has invalid fields")
    if (
        not isinstance(value["glossary"], list)
        or not isinstance(value["bibliography"], list)
        or not isinstance(value["resources"], list)
        or not isinstance(value["labels"], Mapping)
        or not isinstance(value["reader_profile"], Mapping)
    ):
        raise ValueError("publication metadata collections are invalid")
    return dict(value)


def _read_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{description} JSON is unreadable or invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} JSON must be an object")
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
