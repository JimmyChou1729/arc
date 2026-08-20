from __future__ import annotations

from pathlib import Path

import pytest
from arc_jobs import ImmutableArtifactStore, RunContext, RunRepository, RunSpec
from arc_document import (
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)
from arc_render import (
    FragmentAppearance,
    FragmentRevision,
    read_publication_workspace_state,
    write_fragment_revision,
)

from arc_companion.project import CompanionProjectPaths
from arc_companion.publication import (
    materialize_published_companion,
    publish_companion,
)
from arc_companion.publication_revisions import (
    CompanionFragmentReplacement,
    CompanionPublicationRevisionError,
    CompanionPublicationRevisionRequest,
    commit_publication_revision,
    materialize_operator_revisions,
)
import arc_companion.publication_revisions as revision_module


def _fixture(tmp_path: Path):
    paper = SourceRepository(tmp_path / "paper")
    artifact = paper.store_bytes(
        b"# Title\n\nFirst body.\n\nSecond body.\n",
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT, locator="source.md"
        ),
    )
    source = RichDocumentParserService(paper).parse_source(artifact)
    repository = RunRepository(tmp_path / "jobs")
    snapshot = repository.create(RunSpec("run", "handler", {"test": True}))
    context = RunContext(repository, snapshot, resume_input=None)
    published = publish_companion(
        context,
        source=source,
        title="Title",
        authors=(),
        source_language="en",
        target_language="en",
        translation_mode="skipped",
        reader_labels={},
        chapters=(
            {
                "chapter_id": "chapter",
                "title": "Title",
                "block_ids": [item.block_id for item in source.blocks],
                "display_anchor_block_id": source.blocks[0].block_id,
                "section_block_ids": [],
                "section_titles": [],
                "section_levels": [],
                "translation_result": None,
                "learning_units": [
                    {
                        "unit_id": "one",
                        "title": "One",
                        "anchor_block_ids": [source.blocks[1].block_id],
                        "purpose": "companion",
                        "content_markdown": "Original one [@ref-1].",
                        "citations": ["ref-1"],
                    },
                    {
                        "unit_id": "two",
                        "title": "Two",
                        "anchor_block_ids": [source.blocks[2].block_id],
                        "purpose": "companion",
                        "content_markdown": "Original two.",
                        "citations": [],
                    },
                ],
            },
        ),
        glossary=(),
        bibliography=({"evidence_id": "ref-1", "title": "Reference"},),
        document_cache_root=tmp_path / "paper",
    )
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run")
    store = ImmutableArtifactStore(
        repository.run_directory("run"), repository_root=repository.root
    )
    publication_path = materialize_published_companion(
        store, published, paths.publication_workspace("run")
    )
    return paths, store, published, publication_path


def _request(
    paths: CompanionProjectPaths,
    publication_path: Path,
    *,
    review_id: str = "review-1",
    indexes: tuple[int, ...] = (0,),
    suffix: str = " revised",
) -> CompanionPublicationRevisionRequest:
    state = read_publication_workspace_state(publication_path)
    replacements = tuple(
        CompanionFragmentReplacement(
            fragment_id=state.selected_revisions[index].fragment_id,
            base_semantic_digest=state.selected_revisions[index].semantic_digest,
            title=f"{state.selected_revisions[index].title}{suffix}",
            markdown_body=(
                state.selected_revisions[index].markdown_body.rstrip()
                + suffix
                + "\n"
            ),
        )
        for index in indexes
    )
    return CompanionPublicationRevisionRequest(
        run_id=paths.current_run_id or "",
        publication_digest=state.publication_digest,
        review_id=review_id,
        reason="terminology correction",
        reviewer="operator",
        replacements=replacements,
    )


def test_revision_commit_is_idempotent_and_rebuilds_workspace(tmp_path: Path):
    paths, store, published, publication_path = _fixture(tmp_path)
    request = _request(paths, publication_path)

    first = commit_publication_revision(paths, request, publication_path)
    assert first.idempotent_replay is False
    assert len(first.revision_digests) == 1
    state = read_publication_workspace_state(publication_path)
    assert state.selected_revisions[0].revision == 2
    assert state.selected_revision_digests == first.selected_revision_digests
    assert state.edition_digest == first.edition_digest

    replay = commit_publication_revision(paths, request, publication_path)
    assert replay.idempotent_replay is True
    assert replay.revision_digests == first.revision_digests

    workspace = publication_path.parent
    for item in sorted(workspace.rglob("*"), reverse=True):
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            item.rmdir()
    publication_path = materialize_published_companion(
        store, published, workspace
    )
    materialize_operator_revisions(paths, "run", workspace)
    rebuilt = read_publication_workspace_state(publication_path)
    assert rebuilt.selected_revision_digests == first.selected_revision_digests


def test_multi_revision_rejects_stale_conflicting_and_invalid_requests(
    tmp_path: Path,
):
    paths, _store, _published, publication_path = _fixture(tmp_path)
    request = _request(paths, publication_path, indexes=(0, 1))
    result = commit_publication_revision(paths, request, publication_path)
    assert len(result.revision_digests) == 2

    stale = CompanionPublicationRevisionRequest(
        run_id=request.run_id,
        publication_digest=request.publication_digest,
        review_id="review-stale",
        reason="stale",
        replacements=request.replacements,
    )
    with pytest.raises(
        CompanionPublicationRevisionError, match="base is stale"
    ):
        commit_publication_revision(paths, stale, publication_path)

    conflict = _request(
        paths, publication_path, review_id=request.review_id, suffix=" again"
    )
    with pytest.raises(
        CompanionPublicationRevisionError, match="already bound"
    ):
        commit_publication_revision(paths, conflict, publication_path)

    state = read_publication_workspace_state(publication_path)
    base = state.selected_revisions[0]
    invalid_citation = CompanionPublicationRevisionRequest(
        run_id="run",
        publication_digest=state.publication_digest,
        review_id="review-citation",
        reason="bad citation",
        replacements=(
            CompanionFragmentReplacement(
                base.fragment_id,
                base.semantic_digest,
                base.title,
                "Unknown [@not-in-bibliography].\n",
            ),
        ),
    )
    with pytest.raises(
        CompanionPublicationRevisionError, match="unknown bibliography"
    ):
        commit_publication_revision(paths, invalid_citation, publication_path)

    no_change = CompanionPublicationRevisionRequest(
        run_id="run",
        publication_digest=state.publication_digest,
        review_id="review-no-change",
        reason="no visible change",
        replacements=(
            CompanionFragmentReplacement(
                base.fragment_id,
                base.semantic_digest,
                base.title,
                base.markdown_body,
            ),
        ),
    )
    with pytest.raises(CompanionPublicationRevisionError, match="no visible change"):
        commit_publication_revision(paths, no_change, publication_path)

    unknown = CompanionPublicationRevisionRequest(
        run_id="run",
        publication_digest=state.publication_digest,
        review_id="review-unknown",
        reason="unknown fragment",
        replacements=(
            CompanionFragmentReplacement(
                "missing-fragment",
                base.semantic_digest,
                "Missing",
                "Replacement.\n",
            ),
        ),
    )
    with pytest.raises(CompanionPublicationRevisionError, match="unknown selected"):
        commit_publication_revision(paths, unknown, publication_path)


def test_revision_adopts_unmanaged_browser_ancestor(tmp_path: Path):
    paths, store, published, publication_path = _fixture(tmp_path)
    state = read_publication_workspace_state(publication_path)
    base = state.selected_revisions[0]
    browser = FragmentRevision(
        source=base.source,
        fragment_id=base.fragment_id,
        revision=2,
        parent_semantic_digest=base.semantic_digest,
        anchor=base.anchor,
        priority=base.priority,
        role=base.role,
        language=base.language,
        title="Browser edit",
        citation_ids=base.citation_ids,
        provenance={**dict(base.provenance), "last_editor": "arc-render-browser"},
        markdown_body=base.markdown_body + "Browser.\n",
        appearance=FragmentAppearance(
            foreground="#f9fafb",
            background="#111827",
        ),
    )
    write_fragment_revision(publication_path.parent, browser)
    request = _request(paths, publication_path, suffix=" formal")

    result = commit_publication_revision(paths, request, publication_path)
    selected = read_publication_workspace_state(
        publication_path
    ).selected_revisions[0]
    assert selected.revision == 3
    assert selected.appearance == browser.appearance

    workspace = publication_path.parent
    for item in sorted(workspace.rglob("*"), reverse=True):
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            item.rmdir()
    publication_path = materialize_published_companion(
        store, published, workspace
    )
    materialize_operator_revisions(paths, "run", workspace)
    rebuilt = read_publication_workspace_state(publication_path)
    assert rebuilt.selected_revision_digests == result.selected_revision_digests
    assert (
        rebuilt.selected_revisions[0].parent_semantic_digest
        == browser.semantic_digest
    )


def test_revision_adopts_browser_created_fragment_root(tmp_path: Path):
    paths, store, published, publication_path = _fixture(tmp_path)
    state = read_publication_workspace_state(publication_path)
    template = state.selected_revisions[0]
    browser_root = FragmentRevision(
        source=template.source,
        fragment_id="browser-note",
        revision=1,
        parent_semantic_digest=None,
        anchor=template.anchor,
        priority=template.priority,
        role=template.role,
        language=template.language,
        title="Browser note",
        citation_ids=(),
        provenance={"producer": "arc-render-browser"},
        markdown_body="Browser-created note.\n",
    )
    write_fragment_revision(publication_path.parent, browser_root)
    state = read_publication_workspace_state(publication_path)
    assert browser_root.semantic_digest in state.selected_revision_digests
    request = CompanionPublicationRevisionRequest(
        run_id="run",
        publication_digest=state.publication_digest,
        review_id="review-browser-root",
        reason="formalize browser note",
        replacements=(
            CompanionFragmentReplacement(
                browser_root.fragment_id,
                browser_root.semantic_digest,
                "Formal browser note",
                "Browser-created note, formally reviewed.\n",
            ),
        ),
    )
    result = commit_publication_revision(paths, request, publication_path)

    workspace = publication_path.parent
    for item in sorted(workspace.rglob("*"), reverse=True):
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            item.rmdir()
    publication_path = materialize_published_companion(
        store, published, workspace
    )
    materialize_operator_revisions(paths, "run", workspace)
    rebuilt = read_publication_workspace_state(publication_path)
    assert rebuilt.selected_revision_digests == result.selected_revision_digests
    browser_head = next(
        item
        for item in rebuilt.selected_revisions
        if item.fragment_id == "browser-note"
    )
    assert browser_head.revision == 2
    assert browser_head.parent_semantic_digest == browser_root.semantic_digest


def test_revision_recovers_when_failure_happens_after_bundle_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths, _store, _published, publication_path = _fixture(tmp_path)
    request = _request(paths, publication_path)
    original = revision_module._fsync_directory
    calls = 0

    def fail_after_rename(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated post-rename crash")
        original(path)

    monkeypatch.setattr(revision_module, "_fsync_directory", fail_after_rename)
    with pytest.raises(OSError, match="post-rename"):
        commit_publication_revision(paths, request, publication_path)

    bundle = paths.operator_revisions_run_path("run") / (
        f"review-{request.request_digest}"
    )
    assert bundle.is_dir()
    monkeypatch.setattr(revision_module, "_fsync_directory", original)
    materialize_operator_revisions(paths, "run", publication_path.parent)
    replay = commit_publication_revision(paths, request, publication_path)
    assert replay.idempotent_replay is True


def test_revision_cleans_staging_when_rename_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths, _store, _published, publication_path = _fixture(tmp_path)
    request = _request(paths, publication_path)
    original = Path.rename

    def interrupted(source: Path, target: Path) -> Path:
        if source.name.startswith(".staging-"):
            raise OSError("simulated pre-rename crash")
        return original(source, target)

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(OSError, match="pre-rename"):
        commit_publication_revision(paths, request, publication_path)

    run_root = paths.operator_revisions_run_path("run")
    assert not tuple(run_root.glob("review-*"))
    assert not tuple(run_root.glob(".staging-*"))
