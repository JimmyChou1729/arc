from __future__ import annotations

import builtins
import json
from collections import Counter
from pathlib import Path
from threading import Event, Lock

import pytest

from arc_jobs import RunRepository, RunStatus
from arc_llm import LLMCompleted
from arc_paper import (
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

from arc_companion.build import (
    COMPANION_BUILD_HANDLER,
    CompanionBuildHandler,
)
from arc_companion.prompts import (
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
    CHAPTER_PLAN_PROMPT_VERSION,
)
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
)
from arc_companion.service import CompanionService
from arc_companion.translation_adapter import (
    ArcTranslateAdapter,
    CompanionTranslationRuntimeError,
    require_translation_runtime,
)


class FakeGuideTasks:
    def __init__(
        self,
        *,
        guide_started: Event | None = None,
        translation_started: Event | None = None,
    ) -> None:
        self.guide_started = guide_started
        self.translation_started = translation_started
        self.counts: Counter[str] = Counter()
        self.guide_glossaries: dict[str, list[dict]] = {}
        self._completed = {}
        self._lock = Lock()

    def execute_or_resume(self, _context, request, **_kwargs):
        with self._lock:
            existing = self._completed.get(request.task_id)
        if existing is not None:
            return existing
        contract, payload = _request_payload(request.prompt)
        with self._lock:
            self.counts[contract] += 1
        if contract == CHAPTER_PLAN_PROMPT_VERSION:
            value = {
                "chapter_id": payload["chapter_id"],
                "guide": f"Guide for {payload['title']}",
                "learning_units": [],
                "evidence_requests": [],
            }
        elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
            self.guide_glossaries[str(payload["plan"]["guide"])] = list(
                payload["glossary"]
            )
            if self.guide_started is not None:
                self.guide_started.set()
            if self.translation_started is not None:
                assert self.translation_started.wait(timeout=5)
            value = {
                "chapter_id": payload["plan"]["chapter_id"],
                "guide": payload["plan"]["guide"],
                "learning_units": [],
            }
        elif contract == CHAPTER_GUIDE_REVIEW_PROMPT_VERSION:
            assert "translations" not in payload["draft"]
            value = {
                "guide_replacement": None,
                "learning_unit_patches": [],
                "summary": "The guide is source faithful.",
            }
        else:
            raise AssertionError(f"unexpected guide contract: {contract}")
        completed = LLMCompleted(value, "fake", "fake", None, None)
        with self._lock:
            self._completed[request.task_id] = completed
        return completed


class FakeTranslationAdapter:
    def __init__(
        self,
        *,
        mode: str,
        translation_started: Event | None = None,
        guide_started: Event | None = None,
    ) -> None:
        self.mode = mode
        self.translation_started = translation_started
        self.guide_started = guide_started
        self.calls: list[str] = []
        self.approx_counts: list[int] = []

    def detect_language(self, _context, source, **kwargs):
        self.calls.append("language")
        return {
            "schema_version": "arc.translate.language_result.v1",
            "document_digest": source.document_digest,
            "source_digest": source.source.artifact_digest,
            "language_tag": "en",
            "classification": "known",
            "confidence": 0.99,
            "target_language": kwargs["target_language"],
            "mode": self.mode,
        }

    def build_glossary(self, _context, source, **kwargs):
        self.calls.append("glossary")
        self.approx_counts.append(kwargs["approx_count"])
        return {
            "schema_version": "arc.translate.glossary_result.v1",
            "document_digest": source.document_digest,
            "source_digest": source.source.artifact_digest,
            "target_language": kwargs["target_language"],
            "approx_count": kwargs["approx_count"],
            "entries": [
                {
                    "term_id": "term-quantum-field",
                    "term": "quantum field",
                    "aliases": [],
                    "occurrence_count": 1,
                    "source_refs": [],
                    "matched_sentences": [],
                    "preferred_translation": "量子场",
                    "target_definition": "量子场的定义",
                },
                {
                    "term_id": "term-relativity",
                    "term": "relativity",
                    "aliases": [],
                    "occurrence_count": 1,
                    "source_refs": [],
                    "matched_sentences": [],
                    "preferred_translation": "相对论",
                    "target_definition": "只在另一章出现",
                },
                {
                    "term_id": "term-unanchored",
                    "term": "unanchored concept",
                    "aliases": [],
                    "occurrence_count": 0,
                    "source_refs": [],
                    "matched_sentences": [],
                    "preferred_translation": "无锚概念",
                    "target_definition": "不属于源文本的补充候选。",
                },
            ],
        }

    def translate_blocks(self, _context, source, **kwargs):
        self.calls.append(f"translation:{kwargs['artifact_prefix']}")
        if self.translation_started is not None:
            self.translation_started.set()
        if self.guide_started is not None:
            assert self.guide_started.wait(timeout=5)
        by_id = {item.block_id: item for item in source.blocks}
        return {
            "schema_version": "arc.translate.blocks_result.v1",
            "document_digest": source.document_digest,
            "source_digest": source.source.artifact_digest,
            "target_language": kwargs["target_language"],
            "translations": [
                {
                    "block_id": block_id,
                    "text": f"translated {by_id[block_id].kind.value}",
                }
                for block_id in kwargs["block_ids"]
            ],
        }


def _document(tmp_path: Path):
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        (
            b"# Chapter\n\nA quantum field appears here.\n\n"
            b"# Relativity\n\nRelativity appears there.\n"
        ),
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT, locator="fixture.md"
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def _request_payload(prompt: str) -> tuple[str, dict]:
    first, _blank, rest = prompt.partition("\n\n")
    _instruction, marker, payload = rest.partition("\n\nInput JSON:\n")
    assert marker
    return first.removeprefix("Contract: "), json.loads(payload)


def test_translation_and_guide_share_post_glossary_group(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    translation_started = Event()
    guide_started = Event()
    tasks = FakeGuideTasks(
        guide_started=guide_started,
        translation_started=translation_started,
    )
    translation = FakeTranslationAdapter(
        mode="enabled",
        translation_started=translation_started,
        guide_started=guide_started,
    )
    request = CompanionBuildRequest(document, target_language="zh-CN")
    recipe = CompanionGenerationRecipe(approx_term_count=73)
    service = CompanionService(RunRepository(tmp_path / "jobs"))

    prepared = service.prepare(request, recipe=recipe)
    assert service.repository.read_spec(
        prepared.run_id
    ).handler == COMPANION_BUILD_HANDLER
    completed = service.execute(
        prepared.run_id,
        execution=CompanionExecutionOptions(workers=2),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert translation.calls[0:2] == ["language", "glossary"]
    assert translation.approx_counts == [73]
    assert tasks.counts[CHAPTER_PLAN_PROMPT_VERSION] == 2
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 2
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 2
    assert [
        item["term"]
        for item in tasks.guide_glossaries["Guide for Chapter"]
    ] == ["quantum field"]

    book = service.accepted_book(completed.run_id)
    assert book.translation_mode == "enabled"
    assert [
        item.block_id
        for chapter in book.chapters
        for item in chapter.translations
    ] == [
        item.block_id for item in document.blocks
    ]
    assert book.glossary[0].term == "quantum field"
    assert book.glossary[0].translated_term == "量子场"
    assert book.glossary[0].definition == "量子场的定义"
    assert [item.term for item in book.glossary] == [
        "quantum field",
        "relativity",
    ]


def test_same_language_skips_all_translation_owned_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(tmp_path)
    tasks = FakeGuideTasks()
    translation = FakeTranslationAdapter(mode="skipped")
    service = CompanionService(tmp_path / "jobs")
    monkeypatch.setattr(
        "arc_companion.service.require_translation_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("injected adapter must skip runtime preflight")
        ),
    )

    completed = service.build(
        CompanionBuildRequest(document, target_language="en-US"),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert translation.calls == ["language"]
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 2
    book = service.accepted_book(completed.run_id)
    assert book.translation_mode == "skipped"
    assert book.glossary == ()
    assert book.chapters[0].translations == ()


@pytest.mark.parametrize("value", [0, 201, True])
def test_approx_term_count_range(value: int) -> None:
    with pytest.raises(ValueError, match="approx_term_count"):
        CompanionGenerationRecipe(approx_term_count=value)


def test_guide_identity_does_not_depend_on_translation_output(
    tmp_path: Path,
) -> None:
    request = CompanionBuildRequest(_document(tmp_path))
    first = CompanionBuildHandler(
        request,
        translation_adapter=FakeTranslationAdapter(mode="enabled"),
    )
    second = CompanionBuildHandler(
        request,
        translation_adapter=FakeTranslationAdapter(mode="enabled"),
    )

    assert first.semantic_input() == second.semantic_input()


def test_default_adapter_wires_keyword_provider_to_companion_cache(
    tmp_path: Path,
) -> None:
    from arc_paper import KeywordInventoryService, TermInventoryStore
    from arc_translate import TranslationWorkflowService

    tasks = FakeGuideTasks()
    adapter = ArcTranslateAdapter(
        tasks,  # type: ignore[arg-type]
        paper_cache_root=tmp_path / "paper-cache",
    )

    service, source = adapter._service_and_source(_document(tmp_path))

    assert isinstance(service, TranslationWorkflowService)
    assert service.task_service is tasks
    assert isinstance(service.keyword_provider, KeywordInventoryService)
    assert isinstance(service.keyword_provider.store, TermInventoryStore)
    assert service.keyword_provider.store.root == tmp_path / "paper-cache"
    assert source.rich is not None
    assert source.parsed is None


def test_default_adapter_preflight_requires_public_translate_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "arc_translate":
            raise ImportError("incomplete arc-translate facade")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(
        CompanionTranslationRuntimeError,
        match="complete compatible arc-translate runtime",
    ) as exc_info:
        require_translation_runtime()
    assert exc_info.value.code == "runtime_dependency_missing"
