from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from arc_paper import (
    RichDocument,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
)

from arc_render import (
    FragmentAnchor,
    FragmentRevision,
    Publication,
    RenderWorkspaceError,
    source_identity_from_rich_document,
)
from arc_render.workspace import (
    read_publication,
    write_fragment_revision,
    write_publication,
)


def _document() -> RichDocument:
    payload = b"source"
    return RichDocument(
        SourceArtifact(
            SourceFormat.MARKDOWN,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "text/markdown",
            SourceOrigin(SourceOriginKind.REPOSITORY),
        ),
        (),
    )


def test_publication_workspace_json_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "publication.json"
    publication = Publication(_document())
    write_publication(path, publication)

    assert read_publication(path) == publication

    text = path.read_text(encoding="utf-8")
    path.write_text(text[:-1] + ',"layers":[]}', encoding="utf-8")
    with pytest.raises(RenderWorkspaceError, match="unreadable or invalid"):
        read_publication(path)


def test_fragment_write_is_idempotent_and_never_replaces_bytes(
    tmp_path: Path,
) -> None:
    document = _document()
    revision = FragmentRevision(
        source_identity_from_rich_document(document),
        "translation:block-1",
        1,
        None,
        FragmentAnchor("section", "section-1", ()),
        110,
        "note",
        "en",
        None,
        (),
        {"producer": "test"},
        "Body",
    )
    path = write_fragment_revision(tmp_path, revision)

    assert path.parent == tmp_path / "fragments"
    assert ":" not in path.name
    assert write_fragment_revision(tmp_path, revision) == path
    path.write_text("conflict", encoding="utf-8")
    with pytest.raises(RenderWorkspaceError, match="other bytes"):
        write_fragment_revision(tmp_path, revision)
