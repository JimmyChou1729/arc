from __future__ import annotations

import json
from pathlib import Path

from arc_llm import ModelSelection
from arc_paper import (
    RichBlockKind,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

import arc_companion.request_contracts_v1 as request_contracts
from arc_companion.build import CompanionBuildHandler
from arc_companion.prompts_v1 import (
    CHAPTER_PLAN_SCHEMA,
    LANGUAGE_SCHEMA,
    chapter_plan_prompt,
)
from arc_companion.request_contracts_v1 import (
    NEUTRAL_TEXTBOOK_INTENT,
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
    encode_handler_semantic_input,
)
from arc_companion.service import companion_run_id
from arc_companion.source_planning import (
    deterministic_language_samples,
    plan_source_chapters,
    same_primary_language,
)


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
    assert LANGUAGE_SCHEMA["properties"]["classification"]["type"] == "string"
    planned_unit = CHAPTER_PLAN_SCHEMA["properties"]["learning_units"]["items"]
    assert planned_unit["properties"]["kind"]["type"] == "string"
    evidence = CHAPTER_PLAN_SCHEMA["properties"]["evidence_requests"]["items"]
    assert evidence["properties"]["kind"]["type"] == "string"


def test_language_samples_are_stable_beginning_middle_end(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        "# Heading\n\n"
        + "a" * 30
        + "\n\n"
        + "b" * 30
        + "\n\n"
        + "c" * 30,
    )
    joined = "\n\n".join(
        (
            "Heading",
            "a" * 30,
            "b" * 30,
            "c" * 30,
        )
    )
    expected = (
        joined[:20],
        joined[(len(joined) - 20) // 2 : (len(joined) - 20) // 2 + 20],
        joined[-20:],
    )

    assert deterministic_language_samples(
        document, maximum_characters=60
    ) == expected
    assert deterministic_language_samples(
        document, maximum_characters=60
    ) == expected


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


def test_language_and_empty_intent_contracts_enter_prompt_and_identity(
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

    assert same_primary_language("zh", "zh-CN")
    assert not same_primary_language("und", "zh-CN")
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

    monkeypatch.setattr(
        request_contracts,
        "CHAPTER_REVIEW_PROMPT_VERSION",
        "arc.companion.chapter-review-prompt.test-next",
    )
    next_review = CompanionGenerationRecipe(
        chapter_review_prompt="arc.companion.chapter-review-prompt.test-next"
    )
    assert companion_run_id(request, next_review) != companion_run_id(
        request, auto
    )


def test_workers_and_cache_root_are_operational_not_content_identity(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path, "# Source\n\nBody.\n")
    request = CompanionBuildRequest(document)
    recipe = CompanionGenerationRecipe()
    first = CompanionExecutionOptions(
        workers=1,
        cache_root=tmp_path / "cache-one",
    )
    second = CompanionExecutionOptions(
        workers=24,
        cache_root=tmp_path / "cache-two",
    )

    assert first != second
    assert CompanionBuildHandler(
        request, recipe, execution=first
    ).semantic_input() == CompanionBuildHandler(
        request, recipe, execution=second
    ).semantic_input()
