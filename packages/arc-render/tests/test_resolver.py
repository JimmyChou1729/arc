from __future__ import annotations

import hashlib
from dataclasses import replace

from arc_document import (
    RichDocument,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
)
from arc_render import (
    AnchorBlock,
    FragmentAnchor,
    FragmentRevision,
    encode_fragment_revision,
    fragment_revision_filename,
    resolve_fragment_revision_files,
    resolve_fragment_revisions,
    source_identity_from_rich_document,
)


def initial_revision() -> FragmentRevision:
    payload = b"# Source\n"
    document = RichDocument(
        SourceArtifact(
            SourceFormat.MARKDOWN,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "text/markdown",
            SourceOrigin(SourceOriginKind.REPOSITORY),
        ),
        (),
    )
    return FragmentRevision(
        source_identity_from_rich_document(document),
        "fragment-1",
        1,
        None,
        FragmentAnchor(
            "block",
            "b1",
            (
                AnchorBlock(
                    "b1",
                    "paragraph",
                    0,
                    {"line_start": 1},
                    "c" * 64,
                ),
            ),
        ),
        10,
        "note",
        "en",
        None,
        (),
        {"created_by": "test"},
        "revision one",
    )


def child(
    parent: FragmentRevision, body: str, *, parent_digest: str | None = None
) -> FragmentRevision:
    return replace(
        parent,
        revision=parent.revision + 1,
        parent_semantic_digest=parent_digest or parent.semantic_digest,
        markdown_body=body,
    )


def test_linear_history_selects_latest_revision() -> None:
    first = initial_revision()
    second = child(first, "revision two")
    third = child(second, "revision three")
    resolution = resolve_fragment_revisions((third, first, second))
    assert resolution.selected == third
    assert resolution.diagnostics == ()


def test_dangling_revision_is_ignored_with_diagnostic() -> None:
    first = initial_revision()
    second = child(first, "revision two")
    dangling = child(second, "dangling", parent_digest="d" * 64)
    resolution = resolve_fragment_revisions((first, second, dangling))
    assert resolution.selected == second
    assert {item.code for item in resolution.diagnostics} == {
        "dangling_revision"
    }


def test_fork_falls_back_to_common_parent_without_winner() -> None:
    first = initial_revision()
    second = child(first, "revision two")
    left = child(second, "left branch")
    right = child(second, "right branch")
    resolution = resolve_fragment_revisions((first, second, left, right))
    assert resolution.selected == second
    assert resolution.has_conflict
    assert [item.code for item in resolution.diagnostics] == [
        "revision_fork"
    ]


def test_malformed_newest_file_falls_back_to_latest_valid_ancestor(
    tmp_path,
) -> None:
    first = initial_revision()
    second = child(first, "revision two")
    paths = []
    for revision in (first, second):
        path = tmp_path / fragment_revision_filename(revision)
        path.write_text(encode_fragment_revision(revision), encoding="utf-8")
        paths.append(path)
    malformed = tmp_path / f"revision-000003-{'0' * 64}.md"
    malformed.write_text("not a fragment", encoding="utf-8")
    paths.append(malformed)

    resolution = resolve_fragment_revision_files(
        paths, fragment_id=first.fragment_id
    )
    assert resolution.selected == second
    assert [item.code for item in resolution.diagnostics] == [
        "malformed_revision"
    ]
