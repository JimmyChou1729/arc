from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from threading import Lock

from arc_jobs import ResumeReason, RunRepository, RunStatus
from arc_llm import LLMCompleted, LLMPaused
from arc_paper import (
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
)
from arc_companion.service import CompanionService, companion_run_id


class FakeCompanionTasks:
    def __init__(
        self,
        *,
        language: str = "en",
        classification: str = "known",
        confidence: float = 0.99,
        pause_chapter_title: str | None = None,
        unsafe_review_title: str | None = None,
    ) -> None:
        self.language = language
        self.classification = classification
        self.confidence = confidence
        self.pause_chapter_title = pause_chapter_title
        self.unsafe_review_title = unsafe_review_title
        self.counts: Counter[str] = Counter()
        self.draft_titles: Counter[str] = Counter()
        self.review_titles: Counter[str] = Counter()
        self.draft_language_results: list[dict] = []
        self._paused = False
        self._completed = {}
        self._lock = Lock()

    def execute_or_resume(self, context, request, *, input=None, options=None):
        with self._lock:
            cached = self._completed.get(request.task_id)
        if cached is not None:
            return cached
        contract, payload = _request_payload(request.prompt)
        with self._lock:
            self.counts[contract] += 1
        if contract == "arc.companion.language-prompt.v1":
            value = {
                "language_tag": self.language,
                "classification": self.classification,
                "confidence": self.confidence,
            }
        elif contract == "arc.companion.chapter-plan-prompt.v1":
            value = {
                "chapter_id": payload["chapter_id"],
                "guide": f"Guide for {payload['title']}",
                "learning_units": [],
                "glossary_candidates": [],
                "evidence_requests": [],
            }
        elif contract == "arc.companion.glossary-prompt.v1":
            value = {"entries": []}
        elif contract == "arc.companion.chapter-draft-prompt.v1":
            title = str(payload["plan"]["guide"]).removeprefix("Guide for ")
            with self._lock:
                self.draft_titles[title] += 1
                self.draft_language_results.append(
                    dict(payload["language_result"])
                )
                should_pause = (
                    title == self.pause_chapter_title and not self._paused
                )
                if should_pause:
                    self._paused = True
            if should_pause:
                return LLMPaused(
                    ResumeReason.EXTERNAL_CONDITION,
                    "fake-provider-ready",
                    input_required=False,
                )
            translations = (
                [
                    {
                        "block_id": item["block_id"],
                        "text": f"Translated {item['block_id']}",
                    }
                    for item in payload["blocks"]
                ]
                if payload["translation_required"]
                else []
            )
            value = {
                "chapter_id": payload["plan"]["chapter_id"],
                "guide": payload["plan"]["guide"],
                "translations": translations,
                "learning_units": [],
            }
        elif contract == "arc.companion.chapter-review-prompt.v1":
            title = str(payload["draft"]["guide"]).removeprefix("Guide for ")
            with self._lock:
                self.review_titles[title] += 1
            value = (
                {
                    "guide_replacement": None,
                    "translation_patches": [
                        {"id": "unknown-block", "replacement": "unsafe"}
                    ],
                    "learning_unit_patches": [],
                    "summary": "This patch is intentionally unsafe.",
                }
                if title == self.unsafe_review_title
                else {
                    "guide_replacement": None,
                    "translation_patches": [],
                    "learning_unit_patches": [],
                    "summary": "Source and anchors are preserved.",
                }
            )
        else:
            raise AssertionError(f"unexpected contract: {contract}")
        completed = LLMCompleted(value, "fake", "fake", None, None)
        with self._lock:
            self._completed[request.task_id] = completed
        return completed


def _request_payload(prompt: str) -> tuple[str, dict]:
    first, _blank, rest = prompt.partition("\n\n")
    marker = "\n\nInput JSON:\n"
    _instruction, found, payload = rest.partition(marker)
    assert found
    return first.removeprefix("Contract: "), json.loads(payload)


def _rich_document(tmp_path: Path):
    repository = SourceRepository(tmp_path / "paper")
    payload = b"""# First

The first chapter has $x=1$.

## Detail

- one
- two

# Second

The second chapter has

$$
y=2
$$
"""
    artifact = repository.store_bytes(
        payload,
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT, locator="fixture.md"),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def test_durable_build_detects_language_once_and_skips_same_base_translation(
    tmp_path: Path,
) -> None:
    document = _rich_document(tmp_path)
    request = CompanionBuildRequest(
        document,
        target_language="en-US",
    )
    tasks = FakeCompanionTasks()
    service = CompanionService(RunRepository(tmp_path / "jobs"))

    snapshot = service.build(
        request,
        execution=CompanionExecutionOptions(workers=2),
        task_service=tasks,
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    book = service.accepted_book(snapshot.run_id)
    assert book.translation_mode == "skipped"
    assert all(not chapter.translations for chapter in book.chapters)
    assert tasks.counts["arc.companion.language-prompt.v1"] == 1
    assert tuple(
        anchor.block_id
        for chapter in book.chapters
        for anchor in chapter.source_anchors
    ) == tuple(item.block_id for item in document.blocks)

    replay_tasks = FakeCompanionTasks()
    replay = service.build(
        request,
        execution=CompanionExecutionOptions(workers=1),
        task_service=replay_tasks,
    )
    assert replay.status is RunStatus.SUCCEEDED
    assert not replay_tasks.counts
    assert companion_run_id(request, service_request_recipe()) == snapshot.run_id


def service_request_recipe():
    from arc_companion.request_contracts import CompanionGenerationRecipe

    return CompanionGenerationRecipe()


def test_mixed_language_translates_every_block_and_replays_accepted_chapter(
    tmp_path: Path,
) -> None:
    document = _rich_document(tmp_path)
    request = CompanionBuildRequest(document, target_language="en")
    tasks = FakeCompanionTasks(
        language="und",
        classification="mixed",
        pause_chapter_title="Second",
    )
    service = CompanionService(tmp_path / "jobs")

    paused = service.build(
        request,
        execution=CompanionExecutionOptions(workers=2),
        task_service=tasks,
    )
    assert paused.status is RunStatus.PAUSED
    first_before = tasks.draft_titles["First"]

    completed = service.resume(
        paused.run_id,
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.draft_titles["First"] == first_before
    book = service.accepted_book(completed.run_id)
    assert book.translation_mode == "enabled"
    for chapter in book.chapters:
        assert [item.block_id for item in chapter.translations] == [
            item.block_id for item in chapter.source_anchors
        ]


def test_unsafe_review_patch_pauses_once_and_can_be_discarded(
    tmp_path: Path,
) -> None:
    document = _rich_document(tmp_path)
    request = CompanionBuildRequest(document, target_language="en")
    tasks = FakeCompanionTasks(unsafe_review_title="First")
    service = CompanionService(tmp_path / "jobs")

    paused = service.build(
        request,
        execution=CompanionExecutionOptions(workers=2),
        task_service=tasks,
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    assert paused.awaiting.reason is ResumeReason.SUPERVISION_REQUIRED
    assert tasks.review_titles["First"] == 1

    completed = service.resume(
        paused.run_id,
        input={
            "schema_version": "arc.companion.review_supervision.v1",
            "resume_key": paused.awaiting.resume_key,
            "action": "discard_review",
        },
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.review_titles["First"] == 1


def _accepted_chapter_keys(
    service: CompanionService, run_id: str
) -> tuple[str, ...]:
    state = json.loads(
        (
            service.repository.run_directory(run_id)
            / "groups"
            / "accepted-chapters"
            / "state.json"
        ).read_text(encoding="utf-8")
    )
    return tuple(
        item["semantic_key_sha256"] for item in state["units"]
    )


def test_chapter_identity_uses_frozen_language_result_not_execution_options(
    tmp_path: Path,
) -> None:
    document = _rich_document(tmp_path)
    request = CompanionBuildRequest(document, target_language="en")

    first_tasks = FakeCompanionTasks(confidence=0.99)
    first_service = CompanionService(tmp_path / "jobs-first")
    first = first_service.build(
        request,
        execution=CompanionExecutionOptions(
            workers=1,
            cache_root=tmp_path / "cache-first",
        ),
        task_service=first_tasks,
    )

    second_tasks = FakeCompanionTasks(confidence=0.99)
    second_service = CompanionService(tmp_path / "jobs-second")
    second = second_service.build(
        request,
        execution=CompanionExecutionOptions(
            workers=3,
            cache_root=tmp_path / "cache-second",
        ),
        task_service=second_tasks,
    )

    changed_tasks = FakeCompanionTasks(confidence=0.51)
    changed_service = CompanionService(tmp_path / "jobs-changed")
    changed = changed_service.build(
        request,
        execution=CompanionExecutionOptions(workers=2),
        task_service=changed_tasks,
    )

    assert all(
        item.status is RunStatus.SUCCEEDED
        for item in (first, second, changed)
    )
    assert _accepted_chapter_keys(
        first_service, first.run_id
    ) == _accepted_chapter_keys(second_service, second.run_id)
    assert _accepted_chapter_keys(
        first_service, first.run_id
    ) != _accepted_chapter_keys(changed_service, changed.run_id)
    assert first_tasks.draft_language_results
    assert all(
        item
        == {
            "language_tag": "en",
            "classification": "known",
            "confidence": 0.99,
        }
        for item in first_tasks.draft_language_results
    )


def test_malformed_arc_llm_resume_input_fails_strictly(
    tmp_path: Path,
) -> None:
    document = _rich_document(tmp_path)
    request = CompanionBuildRequest(document, target_language="en")
    tasks = FakeCompanionTasks(pause_chapter_title="Second")
    service = CompanionService(tmp_path / "jobs")

    paused = service.build(
        request,
        execution=CompanionExecutionOptions(workers=2),
        task_service=tasks,
    )
    assert paused.status is RunStatus.PAUSED

    failed = service.resume(
        paused.run_id,
        input={
            "schema_version": "arc.llm.resume_input.v1",
            "resume_key": "fake-provider-ready",
        },
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,
    )

    assert failed.status is RunStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "companion_llm_resume_input_invalid"
