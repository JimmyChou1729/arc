from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from arc_paper import (
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

import arc_companion.cli as cli_module
from arc_companion.cli import _parser, _reader_labels_file
from arc_companion.project import CompanionProjectPaths
from arc_companion.reader_labels import reader_labels
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionGenerationRecipe,
    decode_build_request,
    decode_generation_recipe,
    encode_build_request,
    encode_generation_recipe,
)
from arc_companion.service import companion_run_id
from arc_companion.source_identity import resolve_document_identity
from arc_companion.source_planning import plan_source_chapters


def _document(
    tmp_path: Path,
    text: str,
    *,
    name: str = "source.md",
):
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        text.encode("utf-8"),
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT,
            locator=name,
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def test_document_identity_prefers_metadata_then_shallowest_earliest_heading(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        "## Earlier deep heading\n\nBody.\n\n"
        "# First shallow heading\n\nMore.\n\n"
        "# Second shallow heading\n",
    )

    assert (
        resolve_document_identity(document).title
        == "First shallow heading"
    )
    with_metadata = replace(
        document,
        metadata={**document.metadata, "title": "Metadata title"},
    )
    assert resolve_document_identity(with_metadata).title == "Metadata title"


def test_document_identity_falls_back_to_usable_source_name(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        "Body without a heading.\n",
        name="notes_from_school.tex",
    )

    identity = resolve_document_identity(document)

    assert identity.title == "notes from school"
    assert plan_source_chapters(document)[0].title == "notes from school"


def test_document_identity_returns_empty_for_content_addressed_locator(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "Body without a heading.\n")
    digest = document.source.artifact_digest
    source = replace(
        document.source,
        origin=SourceOrigin(
            SourceOriginKind.REPOSITORY,
            locator=f"markdown/sha256/{digest}",
        ),
    )
    content_addressed = replace(document, source=source)

    identity = resolve_document_identity(content_addressed)

    assert identity.title == ""
    assert identity.candidate_authors == ()
    assert identity.author_basis == "none"


def test_flattened_front_matter_extracts_only_actual_author_candidate(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        "# The Death of the Author\n\n"
        "Author: Roland Barthes English translation: Stephen Heath "
        "Source edition: Image, Music, Text.\n\n"
        "Body.\n",
    )

    identity = resolve_document_identity(document)

    assert identity.title == "The Death of the Author"
    assert identity.candidate_authors == ("Roland Barthes",)
    assert identity.author_basis == "document.byline"


def test_metadata_authors_are_candidates_but_request_authors_stay_explicit(
    tmp_path: Path,
) -> None:
    document = replace(
        _document(tmp_path, "# Source\n\nBy Automatic Candidate\n"),
        metadata={"author": "Metadata Candidate"},
    )
    identity = resolve_document_identity(document)
    request = CompanionBuildRequest(
        document,
        authors=("User Supplied",),
    )

    assert identity.candidate_authors == ("Metadata Candidate",)
    assert identity.author_basis == "metadata.author"
    assert request.authors == ("User Supplied",)

    plural = replace(
        document,
        metadata={
            "authors": ["First Candidate", "Second Candidate"],
            "author": "Ignored Candidate",
        },
    )
    plural_identity = resolve_document_identity(plural)
    assert plural_identity.candidate_authors == (
        "First Candidate",
        "Second Candidate",
    )
    assert plural_identity.author_basis == "metadata.authors"


def test_short_explicit_byline_produces_an_unconfirmed_candidate(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        "# Source\n\nBy Example Author\n\nBody.\n",
    )

    identity = resolve_document_identity(document)

    assert identity.candidate_authors == ("Example Author",)
    assert identity.author_basis == "document.byline"


def test_request_identity_encodes_authors_and_complete_reader_labels(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    plain = CompanionBuildRequest(document)
    labels = reader_labels("zh-CN")
    labels["notes"] = "自定义伴读"
    supplied = CompanionBuildRequest(
        document,
        authors=("First Author", "Second Author"),
        reader_labels=labels,
    )
    author_only = CompanionBuildRequest(
        document,
        authors=supplied.authors,
    )
    labels_only = CompanionBuildRequest(
        document,
        reader_labels=labels,
    )

    encoded = encode_build_request(supplied)
    decoded = decode_build_request(encoded)

    assert encoded["schema_version"] == "arc.companion.build_request.v5"
    assert encoded["authors"] == ["First Author", "Second Author"]
    assert encoded["reader_labels"]["notes"] == "自定义伴读"
    assert decoded.authors == supplied.authors
    assert dict(decoded.reader_labels or {}) == dict(
        supplied.reader_labels or {}
    )
    recipe = CompanionGenerationRecipe()
    assert len(
        {
            companion_run_id(plain, recipe),
            companion_run_id(author_only, recipe),
            companion_run_id(labels_only, recipe),
            companion_run_id(supplied, recipe),
        }
    ) == 4


def test_request_decoder_accepts_v2_and_v3_defaults(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    current = encode_build_request(CompanionBuildRequest(document))
    legacy_fields = {
        key: value
        for key, value in current.items()
        if key not in {"authors", "reader_labels"}
    }
    v3 = {
        **legacy_fields,
        "schema_version": "arc.companion.build_request.v3",
    }
    v2 = {
        key: value
        for key, value in legacy_fields.items()
        if key != "translation_reuse_digest"
    }
    v2["schema_version"] = "arc.companion.build_request.v2"

    for legacy in (v2, v3):
        decoded = decode_build_request(legacy)
        assert decoded.authors == ()
        assert dict(decoded.reader_labels or {}) == reader_labels("zh-CN")


def test_request_decoder_accepts_v4_source_page_label(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    labels = reader_labels("zh-CN")
    current = encode_build_request(
        CompanionBuildRequest(document, reader_labels=labels)
    )
    current["schema_version"] = "arc.companion.build_request.v4"
    current["reader_labels"]["source_page"] = "原文第 {page} 页"

    decoded = decode_build_request(current)

    assert "source_page" not in (decoded.reader_labels or {})
    assert dict(decoded.reader_labels or {}) == labels


def test_recipe_identity_reserves_author_prompt_and_decodes_v5(
    tmp_path: Path,
) -> None:
    del tmp_path
    recipe = CompanionGenerationRecipe()
    encoded = encode_generation_recipe(recipe)
    legacy = {
        key: value
        for key, value in encoded.items()
        if key
        not in {
            "author_identity_prompt",
            "evidence_research_prompt",
            "chapter_guide_max_rounds",
            "chapter_guide_review_final_round",
        }
    }
    legacy["schema_version"] = "arc.companion.generation_recipe.v5"

    decoded = decode_generation_recipe(legacy)

    assert encoded["schema_version"] == "arc.companion.generation_recipe.v8"
    assert encoded["author_identity_prompt"] == recipe.author_identity_prompt
    assert decoded.author_identity_prompt == recipe.author_identity_prompt


def test_recipe_identity_decodes_v6_without_evidence_prompt() -> None:
    recipe = CompanionGenerationRecipe()
    legacy = {
        key: value
        for key, value in encode_generation_recipe(recipe).items()
        if key
        not in {
            "evidence_research_prompt",
            "chapter_guide_max_rounds",
            "chapter_guide_review_final_round",
        }
    }
    legacy["schema_version"] = "arc.companion.generation_recipe.v6"

    decoded = decode_generation_recipe(legacy)

    assert decoded.evidence_research_prompt == recipe.evidence_research_prompt


def test_recipe_identity_decodes_v7_with_terminal_revision_defaults() -> None:
    recipe = CompanionGenerationRecipe()
    legacy = {
        key: value
        for key, value in encode_generation_recipe(recipe).items()
        if key
        not in {
            "chapter_guide_max_rounds",
            "chapter_guide_review_final_round",
        }
    }
    legacy["schema_version"] = "arc.companion.generation_recipe.v7"

    decoded = decode_generation_recipe(legacy)

    assert decoded.chapter_guide_max_rounds == 3
    assert decoded.chapter_guide_review_final_round is False


def test_cli_parses_repeatable_authors_and_strict_reader_label_file(
    tmp_path: Path,
) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text('{"source":"Original","guide":"Guide"}', encoding="utf-8")
    args = _parser().parse_args(
        [
            "build",
            "source.md",
            "--project-dir",
            str(tmp_path / "project"),
            "--author",
            "First Author",
            "--author",
            "Second Author",
            "--reader-labels",
            str(labels),
        ]
    )

    assert args.author == ["First Author", "Second Author"]
    assert _reader_labels_file(str(labels)) == {
        "source": "Original",
        "guide": "Guide",
    }

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"source":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="string keys and values"):
        _reader_labels_file(str(invalid))
    with pytest.raises(ValueError, match="existing JSON file"):
        _reader_labels_file(str(tmp_path / "missing.json"))


def test_cli_build_places_explicit_identity_inputs_in_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    labels = reader_labels("zh-CN")
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(labels, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: list[CompanionBuildRequest] = []

    class FakeService:
        def __init__(self, _root: Path) -> None:
            pass

        def prepare(self, request, **_kwargs):
            captured.append(request)
            return SimpleNamespace(run_id="companion-test")

        def execute(self, _run_id, **_kwargs):
            return object()

    monkeypatch.setattr(cli_module, "require_translation_runtime", lambda: None)
    monkeypatch.setattr(cli_module, "ArcPaperService", lambda **_kwargs: object())
    monkeypatch.setattr(
        cli_module,
        "_resolve_source",
        lambda *_args, **_kwargs: (document, (), ()),
    )
    monkeypatch.setattr(cli_module, "CompanionService", FakeService)
    monkeypatch.setattr(
        cli_module,
        "companion_run_id",
        lambda *_args: "companion-test",
    )
    monkeypatch.setattr(
        cli_module,
        "_snapshot_result",
        lambda *_args, **_kwargs: "completed",
    )
    args = _parser().parse_args(
        [
            "build",
            "source.md",
            "--project-dir",
            str(tmp_path / "project"),
            "--author",
            "First Author",
            "--author",
            "Second Author",
            "--reader-labels",
            str(labels_path),
        ]
    )

    assert cli_module._build(args) == "completed"
    assert captured[0].authors == ("First Author", "Second Author")
    assert dict(captured[0].reader_labels or {}) == labels


def test_project_exposes_stable_run_scoped_operator_paths(
    tmp_path: Path,
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")

    assert paths.diagnostics_visual_run_path("companion-run") == (
        paths.diagnostics_visual_root / "companion-run"
    )
    assert paths.operator_inputs_run_path("companion-run") == (
        paths.operator_inputs_root / "companion-run"
    )
    assert not paths.diagnostics_visual_root.exists()
    assert not paths.operator_inputs_root.exists()
    with pytest.raises(ValueError, match="local identifier"):
        paths.operator_inputs_run_path("../outside")
