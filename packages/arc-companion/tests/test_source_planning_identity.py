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
    validate_author_identity,
    validate_chapter_guide,
    validate_chapter_plan,
)
from arc_companion.prompts import (
    AUTHOR_IDENTITY_SCHEMA,
    CHAPTER_PLAN_SCHEMA,
    author_identity_prompt,
    chapter_guide_prompt,
    chapter_guide_review_prompt,
    chapter_plan_prompt,
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
    assert CompanionBuildHandler.name == "arc.companion.build.v9"
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
    assert set(planned_unit["properties"]) == {
        "unit_id",
        "anchor_block_ids",
        "placement",
        "purpose",
    }
    assert planned_unit["properties"]["placement"]["type"] == "string"
    reader_profile = CHAPTER_PLAN_SCHEMA["properties"]["reader_profile"]
    assert reader_profile["properties"]["source_type"]["type"] == "string"
    reader_need = CHAPTER_PLAN_SCHEMA["properties"]["reader_needs"]["items"]
    assert reader_need["properties"]["needs_companion"]["type"] == "boolean"


def test_chapter_prompt_encodes_selective_value_contract() -> None:
    plan = chapter_plan_prompt(
        chapter_id="chapter",
        title="Chapter",
        block_ids=["b1"],
        target_language="en",
        intent="Explain only useful omissions.",
    )

    assert "Do not add" in plan
    for prohibited in (
        "paraphrase",
        "same-meaning rewrite",
        "repeated reasoning",
        "generic summary",
    ):
        assert prohibited in plan
    for phrase in (
        "does not prescribe a creative or pedagogical form",
        "non-exhaustive inspirations",
        "no default or quota",
        "paragraph-local and cross-paragraph work",
    ):
        assert phrase in plan
    for phrase in (
        "adult with average general literacy and no specialist training",
        "research paper",
        "relevant discipline's foundational courses",
        "textbook",
        "standard prerequisite courses",
        "do not assume difficult prerequisite concepts",
        "Audit every supplied source block exactly once",
        "Zero units are valid only when every block",
        "plot, narrative levels, or mistaken-identity context",
        "missing context behind an isolated quotation or named work",
        "make skipped derivation steps explicit",
        "bridge a real logical gap",
        "later corrections",
        "unexpectedly important developments",
        "historical significance",
        "Never invent a belief for the reader",
    ):
        assert phrase in plan
    assert "reader question" not in plan.casefold()


def test_prior_companion_is_reference_context_not_a_template() -> None:
    prior = {
        "schema_version": "arc.companion.prior_reference.v1",
        "chapters": [
            {
                "chapter_id": "chapter",
                "reader_profile": {
                    "source_type": "research_paper",
                    "assumed_background": "A prepared student.",
                    "basis": "The source is a research paper.",
                },
                "reader_needs": [
                    {
                        "block_id": "b1",
                        "needs_companion": True,
                        "reason": "The interpretation is implicit.",
                        "learning_unit_ids": ["unit"],
                    }
                ],
                "learning_units": [
                    {
                        "title": "旧伴读",
                        "content_markdown": "一个值得继续深化的旧洞见。",
                    }
                ],
            }
        ],
    }
    prompt = chapter_plan_prompt(
        chapter_id="chapter",
        title="Chapter",
        block_ids=["b1"],
        target_language="zh-CN",
        intent="Improve the reading companion.",
        has_prior_companion=True,
    )

    assert "prior-companion" in _prompt_payload(prompt)["source_inputs"][
        "input_ids"
    ]
    assert "旧伴读" not in prompt
    assert "optional reference" in prompt
    assert "never copy its repeated format" in prompt


def test_chapter_plan_rejects_legacy_presentation_fields() -> None:
    with pytest.raises(
        CompanionContentError, match="invalid fields"
    ):
        validate_chapter_plan(
            {
                "chapter_id": "chapter",
                "learning_units": [
                    {
                        "unit_id": "unit",
                        "anchor_block_ids": ["b1"],
                        "placement": "inline",
                        "purpose": "Adds a distinct physical interpretation.",
                        "evidence_ids": [],
                        "kind": "intuition",
                    }
                ],
            },
            chapter_id="chapter",
            block_ids=("b1",),
        )


def _reader_profile(source_type: str = "research_paper") -> dict:
    return {
        "source_type": source_type,
        "assumed_background": "A student with the applicable preparation.",
        "basis": "The fixture states its source type.",
    }


def _planned_unit(
    unit_id: str,
    anchors: list[str],
) -> dict:
    return {
        "unit_id": unit_id,
        "anchor_block_ids": anchors,
        "placement": "inline",
        "purpose": "Supplies missing context.",
    }


def test_reader_needs_cover_blocks_in_order_and_allow_cross_block_units() -> None:
    value = validate_chapter_plan(
        {
            "chapter_id": "chapter",
            "reader_profile": _reader_profile(),
            "reader_needs": [
                {
                    "block_id": block_id,
                    "needs_companion": True,
                    "reason": "The shared context is not in the source.",
                    "learning_unit_ids": ["shared"],
                }
                for block_id in ("b1", "b2")
            ],
            "learning_units": [
                _planned_unit("shared", ["b1", "b2"])
            ],
        },
        chapter_id="chapter",
        block_ids=("b1", "b2"),
    )

    assert [
        need["block_id"] for need in value["reader_needs"]
    ] == ["b1", "b2"]
    assert len(value["learning_units"]) == 1


@pytest.mark.parametrize(
    "reader_needs",
    [
        [],
        [
            {
                "block_id": "b2",
                "needs_companion": False,
                "reason": "Simple.",
                "learning_unit_ids": [],
            },
            {
                "block_id": "b1",
                "needs_companion": False,
                "reason": "Simple.",
                "learning_unit_ids": [],
            },
        ],
        [
            {
                "block_id": "b1",
                "needs_companion": False,
                "reason": "Simple.",
                "learning_unit_ids": [],
            },
            {
                "block_id": "b1",
                "needs_companion": False,
                "reason": "Simple.",
                "learning_unit_ids": [],
            },
        ],
    ],
)
def test_reader_needs_require_exact_source_order(reader_needs: list[dict]) -> None:
    with pytest.raises(
        CompanionContentError, match="exactly once in source order"
    ):
        validate_chapter_plan(
            {
                "chapter_id": "chapter",
                "reader_profile": _reader_profile(),
                "reader_needs": reader_needs,
                "learning_units": [],
            },
            chapter_id="chapter",
            block_ids=("b1", "b2"),
        )


def test_simple_source_allows_zero_learning_units() -> None:
    value = validate_chapter_plan(
        {
            "chapter_id": "chapter",
            "reader_profile": _reader_profile(
                "popular_or_directional"
            ),
            "reader_needs": [
                {
                    "block_id": "b1",
                    "needs_companion": False,
                    "reason": "The sentence is simple and self-contained.",
                    "learning_unit_ids": [],
                }
            ],
            "learning_units": [],
        },
        chapter_id="chapter",
        block_ids=("b1",),
    )

    assert value["learning_units"] == []


def test_reader_need_requires_unit_anchored_to_covered_block() -> None:
    with pytest.raises(
        CompanionContentError, match="anchor the covered block"
    ):
        validate_chapter_plan(
            {
                "chapter_id": "chapter",
                "reader_profile": _reader_profile(),
                "reader_needs": [
                    {
                        "block_id": "b1",
                        "needs_companion": True,
                        "reason": "Context is missing.",
                        "learning_unit_ids": ["unit"],
                    },
                    {
                        "block_id": "b2",
                        "needs_companion": False,
                        "reason": "Simple.",
                        "learning_unit_ids": [],
                    },
                ],
                "learning_units": [
                    _planned_unit("unit", ["b2"])
                ],
            },
            chapter_id="chapter",
            block_ids=("b1", "b2"),
        )


def test_final_guide_must_cover_required_reader_need() -> None:
    plan = validate_chapter_plan(
        {
            "chapter_id": "chapter",
            "reader_profile": _reader_profile(),
            "reader_needs": [
                {
                    "block_id": "b1",
                    "needs_companion": True,
                    "reason": "Context is missing.",
                    "learning_unit_ids": ["unit"],
                }
            ],
            "learning_units": [_planned_unit("unit", ["b1"])],
        },
        chapter_id="chapter",
        block_ids=("b1",),
    )
    with pytest.raises(
        CompanionContentError, match="cover every required"
    ):
        validate_chapter_guide(
            {
                "chapter_id": "chapter",
                "learning_units": [],
                "references": [],
            },
            plan=plan,
        )


def test_guide_and_review_prompts_reject_invented_misconceptions() -> None:
    plan = {
        "chapter_id": "chapter",
        "reader_profile": _reader_profile("textbook"),
        "reader_needs": [],
        "learning_units": [],
    }
    guide = chapter_guide_prompt(
        plan=plan,
        block_ids=[],
        glossary=[],
        target_language="zh-CN",
        language_result={"language_tag": "en"},
    )
    review = chapter_guide_review_prompt(
        plan=plan,
        draft={
            "chapter_id": "chapter",
            "learning_units": [],
            "references": [],
        },
        block_ids=[],
        glossary=[],
    )

    assert "never manufacture a prior reader belief" in guide.casefold()
    assert "Translate English excerpts" in guide
    assert "Never recommend removing the final useful unit" in review
    assert "Treat unsupported corrective framing as a material defect" in review
    assert "Do not criticize merely to demonstrate reviewer activity" in review
    assert "accept it by choosing `stop`" in review
    assert "valuable new Companion idea" in review
    assert "add a new unit" in review
    assert "minimum or maximum reference count" in guide
    assert "minimum or maximum reference count" in review
    assert "capability-matching download tool" in guide
    assert "Actively consider" in review
    assert "merely to make the review look more thorough" in review


def test_guide_validation_decodes_model_escaped_paragraphs() -> None:
    plan = {
        "chapter_id": "chapter",
        "reader_needs": [
            {
                "block_id": "b1",
                "needs_companion": True,
                "reason": "A connection is omitted.",
                "learning_unit_ids": ["unit"],
            }
        ],
        "learning_units": [
            {
                "unit_id": "unit",
                "anchor_block_ids": ["b1"],
                "placement": "inline",
                "purpose": "Adds an omitted conceptual connection.",
            }
        ],
    }
    guide = {
        "chapter_id": "chapter",
        "learning_units": [
            {
                "unit_id": "unit",
                "title": "A distinction rather than a question",
                "anchor_block_ids": ["b1"],
                "placement": "inline",
                "purpose": "Adds an omitted conceptual connection.",
                "content_markdown": r"First paragraph.\n\nSecond paragraph.",
            }
        ],
        "references": [],
    }

    validated = validate_chapter_guide(guide, plan=plan)

    assert (
        validated["learning_units"][0]["content_markdown"]
        == "First paragraph.\n\nSecond paragraph."
    )
    assert validated["learning_units"][0]["purpose"] == (
        "Adds an omitted conceptual connection."
    )


def test_chapter_references_allow_only_english_wikipedia() -> None:
    plan = {
        "chapter_id": "chapter",
        "reader_needs": [
            {
                "block_id": "b1",
                "needs_companion": True,
                "reason": "Context is missing.",
                "learning_unit_ids": [],
            }
        ],
        "learning_units": [],
    }

    def guide(source: str) -> dict:
        return {
            "chapter_id": "chapter",
            "learning_units": [
                {
                    "unit_id": "added",
                    "title": "Context",
                    "anchor_block_ids": ["b1"],
                    "placement": "inline",
                    "purpose": "Supplies missing context.",
                    "content_markdown": "Context [@wiki].",
                }
            ],
            "references": [
                {
                    "reference_id": "wiki",
                    "title": "English page title",
                    "source": source,
                    "dois": [],
                    "arxiv_ids": [],
                    "cached_document": None,
                    "cached_material": None,
                }
            ],
        }

    assert validate_chapter_guide(
        guide("https://en.wikipedia.org/wiki/Test"),
        plan=plan,
    )["references"]
    with pytest.raises(CompanionContentError, match="English Wikipedia"):
        validate_chapter_guide(
            guide("zh.wikipedia.org/wiki/Test"),
            plan=plan,
        )


def test_author_identity_requires_high_confidence_for_authors() -> None:
    prompt = author_identity_prompt(
        title="A source title",
        auto_candidates=[
            {"author": "Example Author", "basis": "source byline"}
        ],
        block_access=[
            {
                "block_id": "b1",
                "start_line": 1,
                "end_line": 2,
            }
        ],
    )
    assert "publication identity" in prompt
    assert "Do not guess" in prompt
    assert "Prefer the bounded front-matter line ranges" in prompt
    assert "complete relevant chapter" in prompt
    assert "Avoid reading the whole book" in prompt
    assert "Use only" not in prompt
    assert _prompt_payload(prompt)["block_access"][0]["block_id"] == "b1"
    assert set(AUTHOR_IDENTITY_SCHEMA["properties"]) == {
        "authors",
        "confidence",
        "basis",
        "anchor_block_ids",
    }
    assert validate_author_identity(
        {
            "authors": ["Example Author"],
            "confidence": "high",
            "basis": "The byline identifies the author.",
            "anchor_block_ids": ["b1"],
        },
        block_ids=("b1",),
    )["authors"] == ["Example Author"]
    with pytest.raises(CompanionContentError, match="must be empty"):
        validate_author_identity(
            {
                "authors": ["Possibly Someone"],
                "confidence": "medium",
                "basis": "The source is ambiguous.",
                "anchor_block_ids": ["b1"],
            },
            block_ids=("b1",),
        )
    with pytest.raises(CompanionContentError, match="existing block IDs"):
        validate_author_identity(
            {
                "authors": ["Example Author"],
                "confidence": "high",
                "basis": "Unsupported anchor.",
                "anchor_block_ids": ["missing"],
            },
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
    assert chapters[0].title == "source"
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
    assert "原文公式编号：22" in html


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
        block_ids=[],
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
            == "arc.companion.generation_recipe.v12"
    )
    assert recipe_input["chapter_guide_max_rounds"] == 3
    assert recipe_input["chapter_guide_review_final_round"] is False
    assert (
        recipe_input["author_identity_prompt"]
        == auto.author_identity_prompt
    )
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
