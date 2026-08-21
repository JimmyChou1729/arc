"""Filesystem layout helpers for render publications and atomic fragments."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ._io import atomic_write_bytes
from ._json import canonical_json_bytes, strict_json_loads
from .contracts import (
    Layer,
    Publication,
    layer_from_document,
    layer_to_document,
    publication_from_document,
    publication_to_document,
)
from .markdown import (
    encode_fragment_revision,
    fragment_revision_filename,
)
from .contracts import FragmentRevision


class RenderWorkspaceError(ValueError):
    """A publication workspace is unreadable, inconsistent, or unsafe."""


def fragment_revision_storage_path(revision: FragmentRevision) -> str:
    """Return ARC's portable project-relative path for a new revision."""

    if not isinstance(revision, FragmentRevision):
        raise TypeError("revision must be a FragmentRevision")
    return f"fragments/{fragment_revision_filename(revision)}"


def read_publication(path: str | Path) -> Publication:
    """Read one strict publication JSON document."""

    value = _read_json(Path(path), "publication")
    try:
        return publication_from_document(value)
    except ValueError as exc:
        raise RenderWorkspaceError(f"invalid publication: {exc}") from exc


def write_publication(path: str | Path, publication: Publication) -> Path:
    """Atomically write one canonical publication JSON document."""

    target = Path(path).resolve()
    _atomic_write(target, canonical_json_bytes(publication_to_document(publication)))
    return target


def read_layer(path: str | Path) -> Layer:
    """Read one strict layer JSON document."""

    value = _read_json(Path(path), "layer")
    try:
        return layer_from_document(value)
    except ValueError as exc:
        raise RenderWorkspaceError(f"invalid layer: {exc}") from exc


def write_layer(path: str | Path, layer: Layer) -> Path:
    """Atomically write one canonical layer JSON document."""

    target = Path(path).resolve()
    _atomic_write(target, canonical_json_bytes(layer_to_document(layer)))
    return target


def write_fragment_revision(
    project_root: str | Path,
    revision: FragmentRevision,
) -> Path:
    """Create an immutable revision directly below ``fragments/``.

    Repeating the exact write is idempotent. Existing different bytes are
    never overwritten. Semantic fragment IDs remain inside the validated
    revision document and are never used as native path components.
    """

    root = Path(project_root).resolve()
    target = root.joinpath(*fragment_revision_storage_path(revision).split("/"))
    payload = encode_fragment_revision(revision).encode("utf-8")
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise RenderWorkspaceError(
                f"existing fragment revision is unreadable: {target}"
            ) from exc
        if existing != payload:
            raise RenderWorkspaceError(
                f"immutable fragment revision already exists with other bytes: {target}"
            )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes without an overwrite window. A cooperating
        # writer that won the race must have emitted exactly the same bytes.
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise RenderWorkspaceError(
                    "concurrent fragment publication produced conflicting bytes"
                )
        Path(temporary_name).unlink(missing_ok=True)
        return target
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def relative_fragment_path(
    project_root: str | Path,
    revision_path: str | Path,
) -> str:
    """Return a normalized project-relative fragment path."""

    root = Path(project_root).resolve()
    path = Path(revision_path).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RenderWorkspaceError("fragment revision is outside the project") from exc
    return relative.as_posix()


def _read_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        value = strict_json_loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RenderWorkspaceError(
            f"{description} JSON is unreadable or invalid: {path}"
        ) from exc
    if not isinstance(value, Mapping):
        raise RenderWorkspaceError(f"{description} JSON must be an object")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    atomic_write_bytes(path, payload)


__all__ = [
    "RenderWorkspaceError",
    "fragment_revision_storage_path",
    "read_layer",
    "read_publication",
    "relative_fragment_path",
    "write_fragment_revision",
    "write_layer",
    "write_publication",
]
