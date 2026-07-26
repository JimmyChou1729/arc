from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from arc_llm import ModelSelection
from arc_paper import (
    RichBlockKind,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

import arc_companion.request_contracts as request_contracts
from arc_companion import __all__ as public_names
from arc_companion.build import CompanionBuildHandler
from arc_companion.contracts import AcceptedBook, AcceptedChapter, SourceAnchor
from arc_companion.generation_validation import (
    CompanionContentError,
    validate_chapter_plan,
    validate_literature_request_plan,
)
from arc_companion.prompts import (
    CHAPTER_PLAN_SCHEMA,
    LITERATURE_REQUEST_PLAN_SCHEMA,
    VALUE_DIMENSIONS,
    chapter_plan_prompt,
    literature_request_prompt,
)
from arc_companion.request_contracts import (
    NEUTRAL_TEXTBOOK_INTENT,
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_handler_semantic_input,
)
from arc_companion.service import companion_run_id
from arc_companion.source_planning import (
    block_prompt_document,
    plan_source_chapters,
)
from arc_companion.renderer import CompanionRenderer


def test_public_build_surface_is_current_only() -> None:
    assert CompanionBuildHandler.name == "arc.companion.build.v3"
    assert not any(name.startswith("Legacy") for name in public_names)
    for module_name in (
        "arc_companion.build_v2",
        "arc_companion.generation_validation_v2",
        "arc_companion.prompts_v1",
        "arc_companion.prompts_v2",
        "arc_companion.request_contracts_v1",
        "arc_companion.request_contracts_v2",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)


def _document(tmp_path: Path, text: str, *, name: str = "source.md"):
    repository = SourceRepository(tmp_path / "paper")
    payload = text.encode("utf-8")
    artifact = repository.store_bytes(
        payload,
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT, locator=name),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def _prompt_payload(prompt: str) -> dict:
    marker = "\n\nInput JSON:\n"
    _prefix, found, payload = prompt.partition(marker)
    assert found
    return json.loads(payload)


def test_companion_provider_enum_nodes_declare_string_types() -> None:
    planned_unit = CHAPTER_PLAN_SCHEMA["properties"]["learning_units"]["items"]
    assert planned_unit["properties"]["kind"]["type"] == "string"
    evidence = LITERATURE_REQUEST_PLAN_SCHEMA["properties"]["requests"]["items"]
    assert evidence["properties"]["kind"]["type"] == "string"
    assert LITERATURE_REQUEST_PLAN_SCHEMA["properties"]["requests"][
        "minItems"
    ] == 1


def test_evidence_first_prompts_encode_selective_value_contract() -> None:
    request = literature_request_prompt(
        blocks=[{"block_id": "b1", "text": "source"}],
        intent="Explain only useful omissions.",
    )
    plan = chapter_plan_prompt(
        chapter_id="chapter",
        title="Chapter",
        blocks=[{"block_id": "b1", "text": "source"}],
        target_language="en",
        intent="Explain only useful omissions.",
    )

    assert "at least 20 distinct candidates" in request
    for category in (
        "sources explicitly named by the document",
        "important prior history",
        "later work central to the main debates",
    ):
        assert category in request
    assert "not an inclusion quota" in request
    assert "Do not write a chapter summary or guide" in plan
    for prohibited in (
        "Paraphrase",
        "same-meaning rewrite",
        "repeated reasoning",
        "generic summary",
    ):
        assert prohibited in plan
    assert "no placement quota" in plan
    assert tuple(
        CHAPTER_PLAN_SCHEMA["properties"]["learning_units"]["items"][
            "properties"
        ]["value_dimensions"]["items"]["enum"]
    ) == VALUE_DIMENSIONS


def test_chapter_plan_rejects_unknown_value_dimension() -> None:
    with pytest.raises(
        CompanionContentError, match="value dimension is unsupported"
    ):
        validate_chapter_plan(
            {
                "chapter_id": "chapter",
                "learning_units": [
                    {
                        "unit_id": "unit",
                        "kind": "intuition",
                        "title": "A useful addition",
                        "anchor_block_ids": ["b1"],
                        "placement": "inline",
                        "reader_question": "What is missing?",
                        "added_value": "Adds a distinct physical interpretation.",
                        "value_dimensions": ["generic_summary"],
                        "evidence_ids": [],
                    }
                ],
            },
            chapter_id="chapter",
            block_ids=("b1",),
        )


def test_literature_plan_rejects_empty_research_log() -> None:
    with pytest.raises(
        CompanionContentError,
        match="must inspect candidate evidence",
    ):
        validate_literature_request_plan(
            {"requests": []},
            block_ids=("b1",),
        )


def test_source_chapters_cover_front_matter_and_mixed_headings_exactly(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        "Preface.\n\n"
        "## First\n\n"
        "First body.\n\n"
        "### Detail\n\n"
        "Detail body.\n\n"
        "## Second\n\n"
        "Second body.\n",
    )

    chapters = plan_source_chapters(document)

    assert [chapter.title for chapter in chapters] == ["First", "Second"]
    assert tuple(
        block_id for chapter in chapters for block_id in chapter.block_ids
    ) == tuple(block.block_id for block in document.blocks)
    assert chapters[0].block_ids[0] == document.blocks[0].block_id
    detail = next(
        block
        for block in document.blocks
        if block.kind is RichBlockKind.HEADING
        and block.payload["text"] == "Detail"
    )
    assert detail.block_id in chapters[0].block_ids


def test_source_chapter_without_heading_uses_document_title(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "Only body text.\n")

    chapters = plan_source_chapters(document)

    assert len(chapters) == 1
    assert chapters[0].title == "Document"
    assert chapters[0].block_ids == tuple(
        block.block_id for block in document.blocks
    )


def test_equation_label_provenance_projects_to_prompts_anchors_and_reader(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\n$$\nx = 1\n$$\n")
    equation = next(
        block for block in document.blocks if block.kind is RichBlockKind.EQUATION
    )
    provenance = {
        "source_label": "22",
        "pdf_label": "19",
        "effective_label": "19",
        "page_number": 3,
        "matching_method": "strict_complete_pdf_sequence",
    }

    prompt_block = block_prompt_document(
        equation, equation_label_provenance=provenance
    )
    anchor = SourceAnchor.from_rich_block(
        equation, page_number=3, equation_label_provenance=provenance
    )
    book = AcceptedBook(
        document_digest=document.document_digest,
        title="Companion",
        source_language="en",
        target_language="zh-CN",
        translation_mode="skipped",
        chapters=(
            AcceptedChapter(
                chapter_id="chapter",
                title="Source",
                guide="Guide.",
                source_anchors=(anchor,),
            ),
        ),
    )

    assert prompt_block["payload"]["label"] == "19"
    assert prompt_block["equation_label_provenance"] == provenance
    assert anchor.payload["label"] == "19"
    assert anchor.locator["equation_label_provenance"] == provenance
    reader = CompanionRenderer().render_web(book, tmp_path / "reader")
    html = reader.read_text(encoding="utf-8")
    assert 'class="equation-label">19' in html
    assert "Rich-source label: 22" in html


def test_empty_intent_contract_enters_prompt_and_identity(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    request = CompanionBuildRequest(
        document,
        target_language="zh-CN",
        user_intent="",
    )
    semantic_input = encode_handler_semantic_input(
        request, CompanionGenerationRecipe()
    )
    prompt = chapter_plan_prompt(
        chapter_id="chapter",
        title="Source",
        blocks=[],
        target_language=request.target_language,
        intent=request.effective_intent,
    )

    assert request.effective_intent == NEUTRAL_TEXTBOOK_INTENT
    assert semantic_input["request"]["user_intent"] == NEUTRAL_TEXTBOOK_INTENT
    assert _prompt_payload(prompt)["intent"] == NEUTRAL_TEXTBOOK_INTENT
    assert companion_run_id(
        request, CompanionGenerationRecipe()
    ) == companion_run_id(
        CompanionBuildRequest(
            document,
            target_language="zh-CN",
            user_intent=NEUTRAL_TEXTBOOK_INTENT,
        ),
        CompanionGenerationRecipe(),
    )


def test_provider_model_and_prompt_contract_change_run_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    request = CompanionBuildRequest(document)
    auto = CompanionGenerationRecipe()
    pinned_provider = CompanionGenerationRecipe(
        model=ModelSelection(provider="codex", tier="medium")
    )
    pinned_model = CompanionGenerationRecipe(
        model=ModelSelection(
            provider="codex",
            model="gpt-5.6-codex",
            tier="medium",
        )
    )

    assert len(
        {
            companion_run_id(request, auto),
            companion_run_id(request, pinned_provider),
            companion_run_id(request, pinned_model),
        }
    ) == 3
    semantic_input = encode_handler_semantic_input(request, auto)
    recipe_input = semantic_input["generation_recipe"]
    assert (
        recipe_input["schema_version"]
        == "arc.companion.generation_recipe.v5"
    )
    assert (
        recipe_input["literature_request_prompt"]
        == auto.literature_request_prompt
    )
    assert (
        recipe_input["literature_survey_prompt"]
        == auto.literature_survey_prompt
    )

    with monkeypatch.context() as patch:
        patch.setattr(
            request_contracts,
            "LITERATURE_REQUEST_PROMPT_VERSION",
            "arc.companion.literature-request-prompt.test-next",
        )
        next_literature_request = CompanionGenerationRecipe(
            literature_request_prompt=(
                "arc.companion.literature-request-prompt.test-next"
            )
        )
        assert companion_run_id(
            request, next_literature_request
        ) != companion_run_id(request, auto)

    with monkeypatch.context() as patch:
        patch.setattr(
            request_contracts,
            "LITERATURE_SURVEY_PROMPT_VERSION",
            "arc.companion.literature-survey-prompt.test-next",
        )
        next_literature_survey = CompanionGenerationRecipe(
            literature_survey_prompt=(
                "arc.companion.literature-survey-prompt.test-next"
            )
        )
        assert companion_run_id(
            request, next_literature_survey
        ) != companion_run_id(request, auto)

    with monkeypatch.context() as patch:
        patch.setattr(
            request_contracts,
            "CHAPTER_GUIDE_REVIEW_PROMPT_VERSION",
            "arc.companion.chapter-guide-review-prompt.test-next",
        )
        next_review = CompanionGenerationRecipe(
            chapter_guide_review_prompt=(
                "arc.companion.chapter-guide-review-prompt.test-next"
            )
        )
        assert companion_run_id(request, next_review) != companion_run_id(
            request, auto
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            request_contracts,
            "EQUATION_LABEL_VISUAL_PROMPT_VERSION",
            "arc.paper.equation-label-visual-prompt.test-next",
        )
        next_visual = CompanionGenerationRecipe(
            equation_label_visual_prompt=(
                "arc.paper.equation-label-visual-prompt.test-next"
            )
        )
        assert companion_run_id(request, next_visual) != companion_run_id(
            request, auto
        )


def test_workers_and_paper_cache_root_are_operational_not_content_identity(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    request = CompanionBuildRequest(document)
    recipe = CompanionGenerationRecipe()
    first = CompanionExecutionOptions(
        workers=1,
        paper_cache_root=tmp_path / "cache-one",
    )
    second = CompanionExecutionOptions(
        workers=24,
        paper_cache_root=tmp_path / "cache-two",
    )

    assert first != second
    assert CompanionBuildHandler(
        request, recipe, execution=first
    ).semantic_input() == CompanionBuildHandler(
        request, recipe, execution=second
    ).semantic_input()
