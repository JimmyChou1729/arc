"""Command-line entry point for atomic publications and standalone HTML."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from arc_paper import (
    cached_rich_document_ref_from_document,
    open_cached_rich_document,
    read_cached_rich_asset,
    rich_document_from_document,
)

from .contracts import Publication, source_identity_from_rich_document
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
    source = compose.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--source",
        type=Path,
        help="RichDocument JSON document",
    )
    source.add_argument(
        "--source-ref",
        type=Path,
        help="CachedRichDocumentRef JSON document",
    )
    compose.add_argument(
        "--cache-root",
        type=Path,
        help="arc-paper cache root for --source-ref",
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
    generated_resources: list[Mapping[str, Any]] = []
    if args.source is not None:
        value = _read_json(args.source, "rich source")
        document = rich_document_from_document(value)
    else:
        reference_value = _read_json(args.source_ref, "cached rich source reference")
        reference = cached_rich_document_ref_from_document(reference_value)
        document = open_cached_rich_document(
            reference,
            cache_root=args.cache_root,
        )
        for asset in document.assets:
            payload = read_cached_rich_asset(
                reference,
                asset.artifact_digest,
                cache_root=args.cache_root,
            )
            suffix = Path(asset.logical_name).suffix or (
                mimetypes.guess_extension(asset.media_type) or ""
            )
            relative = (
                Path("resources")
                / "source"
                / f"{asset.artifact_digest}{suffix}"
            )
            _write_immutable_bytes(root / relative, payload)
            generated_resources.append(
                {
                    "artifact_digest": asset.artifact_digest,
                    "media_type": asset.media_type,
                    "size": asset.size,
                    "path": relative.as_posix(),
                }
            )

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
        resources=tuple((*metadata["resources"], *generated_resources)),
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
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{description} JSON is unreadable or invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} JSON must be an object")
    return value


def _write_immutable_bytes(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"immutable resource path has conflicting bytes: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError(
                    f"concurrent resource publication conflicted: {path}"
                )
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _unique_object(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
