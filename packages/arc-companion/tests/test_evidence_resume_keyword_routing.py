from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arc_jobs import RunRepository, RunStatus
from arc_llm import LLMCompleted
from arc_paper import (
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)
from arc_paper.workflows.keywords import KEYWORD_CHAPTER_PROMPT_CONTRACT
from arc_translate.prompts import (
    GLOSSARY_PROMPT_VERSION,
    LANGUAGE_PROMPT_VERSION,
    REVIEW_PROMPT_VERSION,
    TRANSLATION_PROMPT_VERSION,
)

from arc_companion.prompts import (
    AUTHOR_IDENTITY_PROMPT_VERSION,
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
    CHAPTER_PLAN_PROMPT_VERSION,
    EVIDENCE_RESEARCH_PROMPT_VERSION,
    LITERATURE_REQUEST_PROMPT_VERSION,
    LITERATURE_SURVEY_PROMPT_VERSION,
)
from arc_companion.request_contracts import CompanionBuildRequest
from arc_companion.service import CompanionService
from arc_companion.translation_adapter import ArcTranslateAdapter


class _EvidenceResumeTasks:
    """Offline LLM fixture exercising Companion's real translation adapter."""

    def __init__(self) -> None:
        self.keyword_resume_inputs: list[Any] = []

    def execute_or_resume(self, _context, request, *, input=None, options=None):
        contract, payload = _prompt(request.prompt)
        if contract == AUTHOR_IDENTITY_PROMPT_VERSION:
            value = {
                "authors": [],
                "confidence": "low",
                "basis": "No author is confirmed by this fixture.",
                "anchor_block_ids": [],
            }
        elif contract == LITERATURE_REQUEST_PROMPT_VERSION:
            block_id = payload["blocks"][0]["block_id"]
            value = {
                "requests": [
                    {
                        "request_id": "supporting-paper",
                        "kind": "paper",
                        "query": "A bounded supporting result",
                        "purpose": "Provide a cited reading path.",
                        "anchor_block_ids": [block_id],
                    }
                ],
            }
        elif contract == LITERATURE_SURVEY_PROMPT_VERSION:
            block_id = payload["blocks"][0]["block_id"]
            value = {
                "themes": [
                    {
                        "theme_id": "support",
                        "title": "Supporting literature",
                        "synthesis": "The selected source adds context.",
                        "anchor_block_ids": [block_id],
                        "evidence_ids": ["support-1"],
                    }
                ],
                "limitations": [],
            }
        elif contract == CHAPTER_PLAN_PROMPT_VERSION:
            block_id = payload["blocks"][0]["block_id"]
            value = {
                "chapter_id": payload["chapter_id"],
                "reader_profile": {
                    "source_type": "research_paper",
                    "assumed_background": (
                        "A student with relevant foundational coursework."
                    ),
                    "basis": "The fixture requests supporting research.",
                },
                "reader_needs": [
                    {
                        "block_id": block["block_id"],
                        "needs_companion": index == 0,
                        "reason": (
                            "The first block needs the supporting result."
                            if index == 0
                            else "This block is simple and self-contained."
                        ),
                        "learning_unit_ids": (
                            ["reading"] if index == 0 else []
                        ),
                    }
                    for index, block in enumerate(payload["blocks"])
                ],
                "learning_units": [
                    {
                        "unit_id": "reading",
                        "anchor_block_ids": [block_id],
                        "placement": "inline",
                        "purpose": "Connects the source to supporting work.",
                        "evidence_ids": ["support-1"],
                    }
                ],
            }
        elif contract == LANGUAGE_PROMPT_VERSION:
            value = {
                "language_tag": "en",
                "classification": "known",
                "confidence": 1.0,
            }
        elif contract == KEYWORD_CHAPTER_PROMPT_CONTRACT:
            # A parent evidence response must never become an arc-llm input.
            self.keyword_resume_inputs.append(input)
            value = {"entries": [{"term": "field"}]}
        elif contract == GLOSSARY_PROMPT_VERSION:
            value = {
                "entries": [
                    {
                        "term_id": term["term_id"],
                        "preferred_translation": "场",
                        "target_definition": "A test glossary term.",
                    }
                    for term in payload["terms"]
                ]
            }
        elif contract == EVIDENCE_RESEARCH_PROMPT_VERSION:
            value = {
                "responses": [
                    {
                        "request_id": "supporting-paper",
                        "candidates": [
                            {
                                "evidence_id": f"support-{index}",
                                "title": f"Supporting result {index}",
                                "content": "A bounded offline evidence excerpt.",
                                "source": f"fixture:{index}",
                            }
                            for index in range(1, 21)
                        ],
                        "selected_evidence_ids": ["support-1"],
                        "selection_rationale": (
                            "Only the first source is directly relevant."
                        ),
                    }
                ]
            }
        elif contract == TRANSLATION_PROMPT_VERSION:
            value = {
                "translations": [
                    {
                        "block_id": block["block_id"],
                        "text": "已翻译的源文本。",
                    }
                    for block in payload["blocks"]
                ]
            }
        elif contract == REVIEW_PROMPT_VERSION:
            value = {"translation_patches": [], "summary": "No changes."}
        elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
            value = {
                "chapter_id": payload["plan"]["chapter_id"],
                "learning_units": [
                    {
                        "unit_id": "reading",
                        "title": "Supporting result",
                        "content_markdown": (
                            "Read the selected supporting result [@support-1]."
                        ),
                    }
                ],
            }
        elif contract == CHAPTER_GUIDE_REVIEW_PROMPT_VERSION:
            value = {
                "decisions": [
                    {
                        "unit_id": "reading",
                        "decision": "keep",
                        "replacement_title": None,
                        "replacement_markdown": None,
                        "reason": "The unit is directly grounded.",
                    }
                ],
            }
        else:  # pragma: no cover - contract drift guard
            raise AssertionError(contract)
        return LLMCompleted(value, "fake", "fake", None, None)


def _prompt(prompt: str) -> tuple[str, dict[str, Any]]:
    contract = prompt.splitlines()[0].removeprefix("Contract: ")
    if "\nInput JSON:\n" not in prompt:
        return contract, {}
    return contract, json.loads(prompt.split("\nInput JSON:\n", 1)[1])


def _document(tmp_path: Path):
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        b"# Introduction\n\nA field appears in this source paragraph.\n",
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT, locator="source.md"
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def test_direct_evidence_research_reaches_keyword_glossary_and_book(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    tasks = _EvidenceResumeTasks()
    service = CompanionService(RunRepository(tmp_path / "jobs"))
    translation = ArcTranslateAdapter(
        tasks,  # type: ignore[arg-type]
        paper_cache_root=tmp_path / "paper-cache",
    )

    completed = service.build(
        CompanionBuildRequest(document, target_language="zh-CN"),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.keyword_resume_inputs == [None]
    book = service.accepted_book(completed.run_id)
    assert [item.evidence_id for item in book.bibliography] == ["support-1"]
    assert book.bibliography[0].title == "Supporting result 1"
    assert book.translation_mode == "enabled"
