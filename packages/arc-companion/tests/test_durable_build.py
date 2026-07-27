from __future__ import annotations

import builtins
import json
from collections import Counter
from pathlib import Path
from threading import Event, Lock

import pytest

from arc_jobs import (
    ImmutableArtifactStore,
    ResumeReason,
    RunRepository,
    RunStatus,
)
from arc_llm import LLMCompleted, LLMPaused
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
    AUTHOR_IDENTITY_PROMPT_VERSION,
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
    CHAPTER_PLAN_PROMPT_VERSION,
    EVIDENCE_RESEARCH_PROMPT_VERSION,
    LITERATURE_REQUEST_PROMPT_VERSION,
    LITERATURE_SURVEY_PROMPT_VERSION,
)
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
)
from arc_companion.service import CompanionService
from arc_companion.source_planning import plan_source_chapters
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
        remove_second_unit: bool = False,
        malformed_evidence: bool = False,
        select_evidence: bool = False,
        semantic_invalid_contract: str | None = None,
        semantic_invalid_calls: frozenset[int] = frozenset({1}),
    ) -> None:
        self.guide_started = guide_started
        self.translation_started = translation_started
        self.remove_second_unit = remove_second_unit
        self.malformed_evidence = malformed_evidence
        self.select_evidence = select_evidence
        self.semantic_invalid_contract = semantic_invalid_contract
        self.semantic_invalid_calls = semantic_invalid_calls
        self.counts: Counter[str] = Counter()
        self.guide_glossaries: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, str, str]] = []
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
            contract_call = self.counts[contract]
            self.requests.append(
                (contract, request.task_id, request.prompt)
            )
        if contract == AUTHOR_IDENTITY_PROMPT_VERSION:
            value = {
                "authors": [],
                "confidence": "low",
                "basis": "The fixture contains no confirmed author.",
                "anchor_block_ids": [],
            }
        elif contract == LITERATURE_REQUEST_PROMPT_VERSION:
            value = {
                "requests": [
                    {
                        "request_id": "research-log",
                        "kind": "paper",
                        "query": "Inspect directly relevant literature.",
                        "purpose": "Test the frozen research-log boundary.",
                        "anchor_block_ids": [
                            payload["blocks"][0]["block_id"]
                        ],
                    }
                ]
            }
        elif contract == EVIDENCE_RESEARCH_PROMPT_VERSION:
            value = {
                "responses": [
                    {
                        "request_id": "research-log",
                        "candidates": [
                            {
                                "evidence_id": f"candidate-{index}",
                                "title": f"Candidate {index}",
                                "content": "Inspected but not selected.",
                                "source": f"fixture:{index}",
                            }
                            for index in range(
                                1, 20 if self.malformed_evidence else 21
                            )
                        ],
                        "selected_evidence_ids": (
                            ["candidate-1"]
                            if self.select_evidence
                            else []
                        ),
                        "selection_rationale": (
                            "None adds value beyond this self-contained fixture."
                        ),
                    }
                ]
            }
        elif contract == LITERATURE_SURVEY_PROMPT_VERSION:
            block_id = payload["blocks"][0]["block_id"]
            evidence_id = payload["selected_evidence"][0][
                "evidence_id"
            ]
            value = {
                "themes": [
                    {
                        "theme_id": "direct-context",
                        "title": "Direct context",
                        "synthesis": "One selected source adds context.",
                        "anchor_block_ids": [block_id],
                        "evidence_ids": [evidence_id],
                    }
                ],
                "limitations": [],
            }
        elif contract == CHAPTER_PLAN_PROMPT_VERSION:
            block_id = payload["blocks"][0]["block_id"]
            units = [
                {
                    "unit_id": "intuition",
                    "anchor_block_ids": [block_id],
                    "placement": "inline",
                    "purpose": "Makes one implicit connection explicit.",
                    "evidence_ids": [],
                }
            ]
            if self.remove_second_unit:
                units.append(
                    {
                        "unit_id": "redundant",
                        "anchor_block_ids": [block_id],
                        "placement": "chapter",
                        "purpose": "Claims to repeat the source.",
                        "evidence_ids": [],
                    }
                )
            value = {
                "chapter_id": "model-supplied-title-not-routing-identity",
                "reader_profile": {
                    "source_type": "popular_or_directional",
                    "assumed_background": (
                        "An adult reader without specialist training."
                    ),
                    "basis": "The short fixture is explanatory prose.",
                },
                "reader_needs": [
                    {
                        "block_id": block["block_id"],
                        "needs_companion": index == 0,
                        "reason": (
                            "The first block benefits from one connection."
                            if index == 0
                            else "This block is simple and self-contained."
                        ),
                        "learning_unit_ids": (
                            ["intuition"] if index == 0 else []
                        ),
                    }
                    for index, block in enumerate(payload["blocks"])
                ],
                "learning_units": units,
            }
        elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
            self.guide_glossaries[str(payload["plan"]["chapter_id"])] = list(
                payload["glossary"]
            )
            if self.guide_started is not None:
                self.guide_started.set()
            if self.translation_started is not None:
                assert self.translation_started.wait(timeout=5)
            value = {
                "chapter_id": payload["plan"]["chapter_id"],
                "learning_units": [
                    {
                        "unit_id": item["unit_id"],
                        "title": (
                            f"Question for {payload['plan']['chapter_id']}"
                            if item["unit_id"] == "intuition"
                            else "Restatement"
                        ),
                        "content_markdown": (
                            "A focused source-anchored explanation."
                            if item["unit_id"] == "intuition"
                            else "The source says the same thing again."
                        ),
                    }
                    for item in payload["plan"]["learning_units"]
                ],
            }
        elif contract == CHAPTER_GUIDE_REVIEW_PROMPT_VERSION:
            assert "translations" not in payload["draft"]
            value = {
                "decisions": [
                    {
                        "unit_id": item["unit_id"],
                        "decision": (
                            "remove"
                            if item["unit_id"] == "redundant"
                            else "keep"
                        ),
                        "replacement_title": None,
                        "replacement_markdown": None,
                        "reason": (
                            "It merely restates the source."
                            if item["unit_id"] == "redundant"
                            else "It adds a focused explanation."
                        ),
                    }
                    for item in payload["draft"]["learning_units"]
                ],
            }
        else:
            raise AssertionError(f"unexpected guide contract: {contract}")
        if (
            contract == self.semantic_invalid_contract
            and contract_call in self.semantic_invalid_calls
        ):
            value = _semantically_invalid_value(contract, value)
        completed = LLMCompleted(value, "fake", "fake", None, None)
        with self._lock:
            self._completed[request.task_id] = completed
        return completed


def _semantically_invalid_value(contract: str, value: dict) -> dict:
    invalid = json.loads(json.dumps(value))
    if contract == AUTHOR_IDENTITY_PROMPT_VERSION:
        invalid.update(
            {
                "authors": [],
                "confidence": "high",
                "anchor_block_ids": [],
            }
        )
    elif contract == LITERATURE_REQUEST_PROMPT_VERSION:
        invalid["requests"][0]["anchor_block_ids"] = ["unknown-block"]
    elif contract == EVIDENCE_RESEARCH_PROMPT_VERSION:
        invalid["responses"][0]["candidates"].pop()
    elif contract == LITERATURE_SURVEY_PROMPT_VERSION:
        invalid["themes"][0]["anchor_block_ids"] = ["unknown-block"]
    elif contract == CHAPTER_PLAN_PROMPT_VERSION:
        invalid["reader_needs"][0]["block_id"] = "unknown-block"
    elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
        invalid["learning_units"][0]["unit_id"] = "unknown-unit"
    elif contract == CHAPTER_GUIDE_REVIEW_PROMPT_VERSION:
        invalid["decisions"] = []
    else:
        raise AssertionError(f"unsupported invalid contract: {contract}")
    return invalid


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


class _HostTurnEvidenceTasks(FakeGuideTasks):
    def execute_or_resume(self, context, request, **kwargs):
        contract, _payload = _request_payload(request.prompt)
        if contract == EVIDENCE_RESEARCH_PROMPT_VERSION:
            request_ref = context.artifacts.publish_json(
                "test/host-turn-request",
                {
                    "schema_version": "arc.llm.host_turn.v1",
                    "state": "request_host",
                },
            )
            return LLMPaused(
                ResumeReason.INTERACTION_REQUIRED,
                "arc-llm-host-turn",
                {"code": "host_broker_required"},
                request_ref=request_ref,
                input_required=True,
                response_contract="arc.llm.resume_input.v3",
            )
        return super().execute_or_resume(context, request, **kwargs)


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
    for chapter in plan_source_chapters(document):
        candidate = json.loads(
            (
                service.repository.run_directory(completed.run_id)
                / "working/candidates/chapters"
                / chapter.chapter_id
                / "plan.json"
            ).read_text(encoding="utf-8")
        )
        assert candidate["chapter_id"] == chapter.chapter_id
    assert [
        item["term"]
        for item in next(iter(tasks.guide_glossaries.values()))
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


def test_review_remove_publishes_ordered_subset_without_retry(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    tasks = FakeGuideTasks(remove_second_unit=True)
    translation = FakeTranslationAdapter(mode="skipped")
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 2
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 2
    assert [
        [unit.unit_id for unit in chapter.learning_units]
        for chapter in service.accepted_book(completed.run_id).chapters
    ] == [["intuition"], ["intuition"]]


@pytest.mark.parametrize(
    ("contract", "candidate_kind", "expected_calls"),
    [
        (AUTHOR_IDENTITY_PROMPT_VERSION, "author", 2),
        (LITERATURE_REQUEST_PROMPT_VERSION, "requests", 2),
        (EVIDENCE_RESEARCH_PROMPT_VERSION, "evidence", 2),
        (LITERATURE_SURVEY_PROMPT_VERSION, "survey", 2),
        (CHAPTER_PLAN_PROMPT_VERSION, "plan", 3),
        (CHAPTER_GUIDE_PROMPT_VERSION, "guide", 3),
        (CHAPTER_GUIDE_REVIEW_PROMPT_VERSION, "review", 3),
    ],
)
def test_schema_valid_semantic_error_gets_one_fresh_retry(
    tmp_path: Path,
    contract: str,
    candidate_kind: str,
    expected_calls: int,
) -> None:
    document = _document(tmp_path)
    chapter_id = plan_source_chapters(document)[0].chapter_id
    candidate_ids = {
        "author": "identity/author.json",
        "requests": "planning/literature-requests.json",
        "evidence": "planning/evidence-research.json",
        "survey": "planning/literature-survey.json",
        "plan": f"chapters/{chapter_id}/plan.json",
        "guide": f"chapters/{chapter_id}/guide-draft.json",
        "review": f"chapters/{chapter_id}/guide-review.json",
    }
    tasks = FakeGuideTasks(
        select_evidence=contract == LITERATURE_SURVEY_PROMPT_VERSION,
        semantic_invalid_contract=contract,
    )
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.counts[contract] == expected_calls
    candidate_id = candidate_ids[candidate_kind]
    retry_candidate_id = candidate_id.removesuffix(
        ".json"
    ) + ".semantic-retry.json"
    candidate_root = (
        service.repository.run_directory(completed.run_id)
        / "working/candidates"
    )
    first_path = candidate_root / candidate_id
    retry_path = candidate_root / retry_candidate_id
    assert first_path.is_file()
    assert retry_path.is_file()
    assert json.loads(first_path.read_text(encoding="utf-8")) != json.loads(
        retry_path.read_text(encoding="utf-8")
    )
    retry_requests = [
        (task_id_value, prompt)
        for prompt_contract, task_id_value, prompt in tasks.requests
        if prompt_contract == contract
        and "-semantic-retry-" in task_id_value
    ]
    assert len(retry_requests) == 1
    assert "Semantic retry feedback:" in retry_requests[0][1]
    assert "Validation code:" in retry_requests[0][1]


def test_invalid_review_retry_keeps_valid_draft_with_warning(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    first_chapter = plan_source_chapters(document)[0]
    tasks = FakeGuideTasks(
        semantic_invalid_contract=CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        semantic_invalid_calls=frozenset({1, 2}),
    )
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 3
    book = service.accepted_book(completed.run_id)
    assert [
        item.unit_id for item in book.chapters[0].learning_units
    ] == ["intuition"]
    store = ImmutableArtifactStore(
        service.repository.run_directory(completed.run_id),
        repository_root=service.repository.root,
    )
    diagnostics_ref = store.find(
        (
            f"diagnostics/chapters/{first_chapter.chapter_id}"
            "/guide-review"
        )
    )
    assert diagnostics_ref is not None
    diagnostics = json.loads(
        store.read_bytes(diagnostics_ref).decode("utf-8")
    )
    assert diagnostics["status"] == "discarded_invalid_review"
    assert len(diagnostics["candidate_paths"]) == 2


def test_semantic_pause_publishes_completed_chapter_and_reuses_it(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    chapters = plan_source_chapters(document)
    tasks = FakeGuideTasks(
        semantic_invalid_contract=CHAPTER_GUIDE_PROMPT_VERSION,
        # Chapter one succeeds. Chapter two exhausts its fixed retry.
        semantic_invalid_calls=frozenset({2, 3}),
    )
    translation = FakeTranslationAdapter(mode="skipped")
    service = CompanionService(tmp_path / "jobs")

    paused = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    store = ImmutableArtifactStore(
        service.repository.run_directory(paused.run_id),
        repository_root=service.repository.root,
    )
    first_ref = store.find(
        f"chapters/{chapters[0].chapter_id}/accepted"
    )
    assert first_ref is not None
    assert store.find(
        f"chapters/{chapters[1].chapter_id}/accepted"
    ) is None
    retry_path = Path(
        str(paused.awaiting.details["active_candidate_path"])
    )
    repaired = json.loads(retry_path.read_text(encoding="utf-8"))
    repaired["learning_units"][0]["unit_id"] = "intuition"
    retry_path.write_text(json.dumps(repaired), encoding="utf-8")
    guide_calls = tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION]
    first_digest = first_ref.digest

    recovered = service.resume(
        paused.run_id,
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert recovered.status is RunStatus.SUCCEEDED
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == guide_calls
    recovered_store = ImmutableArtifactStore(
        service.repository.run_directory(recovered.run_id),
        repository_root=service.repository.root,
    )
    recovered_first = recovered_store.find(
        f"chapters/{chapters[0].chapter_id}/accepted"
    )
    assert recovered_first is not None
    assert recovered_first.digest == first_digest
    assert recovered_store.find(
        f"chapters/{chapters[1].chapter_id}/accepted"
    ) is not None


def test_malformed_evidence_retries_once_then_pauses_for_candidate_edit(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    tasks = FakeGuideTasks(malformed_evidence=True)
    translation = FakeTranslationAdapter(mode="skipped")
    service = CompanionService(tmp_path / "jobs")

    paused = service.build(
        CompanionBuildRequest(document, target_language="en"),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    assert paused.awaiting.reason is ResumeReason.SUPERVISION_REQUIRED
    assert paused.awaiting.input_required is False
    assert paused.awaiting.details["automatic_retry_exhausted"] is True
    assert paused.awaiting.details["output_attempts"] == 2
    candidate_paths = [
        Path(str(item))
        for item in paused.awaiting.details["candidate_paths"]
    ]
    assert candidate_paths == [
        service.repository.run_directory(paused.run_id)
        / "working/candidates/planning/evidence-research.json",
        service.repository.run_directory(paused.run_id)
        / (
            "working/candidates/planning/"
            "evidence-research.semantic-retry.json"
        ),
    ]
    first_raw = json.loads(
        candidate_paths[0].read_text(encoding="utf-8")
    )
    raw = json.loads(candidate_paths[1].read_text(encoding="utf-8"))
    raw["responses"][0]["candidates"].append(
        {
            "evidence_id": "candidate-20",
            "title": "Candidate 20",
            "content": "Inspected but not selected.",
            "source": "fixture:20",
        }
    )
    candidate_paths[1].write_text(
        json.dumps(raw), encoding="utf-8"
    )
    evidence_calls = tasks.counts[EVIDENCE_RESEARCH_PROMPT_VERSION]
    assert evidence_calls == 2

    recovered = service.resume(
        paused.run_id,
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )

    assert recovered.status is RunStatus.SUCCEEDED
    assert tasks.counts[EVIDENCE_RESEARCH_PROMPT_VERSION] == evidence_calls
    assert json.loads(
        candidate_paths[0].read_text(encoding="utf-8")
    ) == first_raw


def test_evidence_research_propagates_native_arc_llm_host_turn_pause(
    tmp_path: Path,
) -> None:
    service = CompanionService(tmp_path / "jobs")

    paused = service.build(
        CompanionBuildRequest(_document(tmp_path), target_language="en"),
        task_service=_HostTurnEvidenceTasks(),  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    assert paused.awaiting.resume_key == "arc-llm-host-turn"
    assert paused.awaiting.response_contract == "arc.llm.resume_input.v3"
    assert paused.awaiting.details == {"code": "host_broker_required"}


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
