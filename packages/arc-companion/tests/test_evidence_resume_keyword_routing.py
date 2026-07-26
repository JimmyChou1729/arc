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
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
    CHAPTER_PLAN_PROMPT_VERSION,
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
        if contract == CHAPTER_PLAN_PROMPT_VERSION:
            block_id = payload["blocks"][0]["block_id"]
            value = {
                "chapter_id": payload["chapter_id"],
                "guide": "A source-anchored guide.",
                "learning_units": [],
                "evidence_requests": [
                    {
                        "request_id": "supporting-paper",
                        "kind": "paper",
                        "query": "A bounded supporting result",
                        "purpose": "Provide a cited reading path.",
                        "anchor_block_ids": [block_id],
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
                        **term,
                        "preferred_translation": "场",
                        "target_definition": "A test glossary term.",
                    }
                    for term in payload["terms"]
                ]
            }
        elif contract == TRANSLATION_PROMPT_VERSION:
            value = {
                "translations": [
                    {
                        "block_id": block["block_id"],
                        "text": "已翻译的源文本。",
                        "source_identity": block["source_identity"],
                    }
                    for block in payload["blocks"]
                ]
            }
        elif contract == REVIEW_PROMPT_VERSION:
            value = {"translation_patches": [], "summary": "No changes."}
        elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
            value = {
                "chapter_id": payload["plan"]["chapter_id"],
                "guide": payload["plan"]["guide"],
                "learning_units": [],
            }
        elif contract == CHAPTER_GUIDE_REVIEW_PROMPT_VERSION:
            value = {
                "guide_replacement": None,
                "learning_unit_patches": [],
                "summary": "No changes.",
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


def test_companion_evidence_resume_reaches_keyword_glossary_and_book(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    tasks = _EvidenceResumeTasks()
    service = CompanionService(RunRepository(tmp_path / "jobs"))
    translation = ArcTranslateAdapter(
        tasks,  # type: ignore[arg-type]
        paper_cache_root=tmp_path / "paper-cache",
    )

    first = service.build(
        CompanionBuildRequest(document, target_language="zh-CN"),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert first.status is RunStatus.PAUSED
    assert first.awaiting is not None
    assert first.awaiting.response_contract == "arc.companion.evidence_response.v1"

    resumed = service.resume(
        first.run_id,
        input={
            "schema_version": "arc.companion.evidence_response.v1",
            "resume_key": first.awaiting.resume_key,
            "responses": [
                {
                    "request_id": "supporting-paper",
                    "evidence_id": "support-1",
                    "title": "Supporting result",
                    "content": "A bounded offline evidence excerpt.",
                    "source": "fixture",
                }
            ],
        },
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert tasks.keyword_resume_inputs == [None]
    book = service.accepted_book(resumed.run_id)
    assert book.bibliography[0].evidence_id == "support-1"
    assert book.translation_mode == "enabled"
