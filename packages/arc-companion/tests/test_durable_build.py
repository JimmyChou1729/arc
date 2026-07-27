from __future__ import annotations

import builtins
import json
from collections import Counter
from pathlib import Path
from threading import Event, Lock

import pytest

from arc_jobs import (
    ImmutableArtifactStore,
    RunRepository,
    RunStatus,
)
from arc_llm import LLMCompleted
from arc_paper import (
    ReferenceIdentity,
    ReferenceMaterialCache,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    cached_reference_material_to_document,
)

from arc_companion.build import (
    COMPANION_BUILD_HANDLER,
    CompanionBuildHandler,
    _verify_cached_reference_materials,
)
from arc_companion.contracts import CompanionContentCodec
from arc_companion.generation_validation import CompanionContentError
from arc_companion.prompts import (
    AUTHOR_IDENTITY_PROMPT_VERSION,
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
        reviewer_stop_round: int | None = None,
        empty_seed: bool = False,
        with_reference: bool = False,
        invalid_cached_material: bool = False,
        semantic_invalid_contract: str | None = None,
        semantic_invalid_calls: frozenset[int] = frozenset({1}),
    ) -> None:
        self.guide_started = guide_started
        self.translation_started = translation_started
        self.remove_second_unit = remove_second_unit
        self.reviewer_stop_round = reviewer_stop_round
        self.empty_seed = empty_seed
        self.with_reference = with_reference
        self.invalid_cached_material = invalid_cached_material
        self.semantic_invalid_contract = semantic_invalid_contract
        self.semantic_invalid_calls = semantic_invalid_calls
        self.counts: Counter[str] = Counter()
        self.guide_glossaries: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.request_input_ids: list[tuple[str, tuple[str, ...]]] = []
        self.runtime_environments: list[dict[str, str | None]] = []
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
            self.request_input_ids.append(
                (
                    contract,
                    tuple(item.input_id for item in request.inputs),
                )
            )
            options = _kwargs.get("options")
            if options is not None:
                self.runtime_environments.append(
                    dict(options.runtime_environment.values)
                )
        if contract == AUTHOR_IDENTITY_PROMPT_VERSION:
            value = {
                "authors": [],
                "confidence": "low",
                "basis": "The fixture contains no confirmed author.",
                "anchor_block_ids": [],
            }
        elif contract == CHAPTER_PLAN_PROMPT_VERSION:
            block_id = payload["block_ids"][0]
            units = [
                {
                    "unit_id": "intuition",
                    "anchor_block_ids": [block_id],
                    "placement": "inline",
                    "purpose": "Makes one implicit connection explicit.",
                }
            ]
            if self.remove_second_unit:
                units.append(
                    {
                        "unit_id": "redundant",
                        "anchor_block_ids": [block_id],
                        "placement": "chapter",
                        "purpose": "Claims to repeat the source.",
                    }
                )
            if self.empty_seed:
                units = []
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
                        "block_id": block_id,
                        "needs_companion": index == 0,
                        "reason": (
                            "The first block benefits from one connection."
                            if index == 0
                            else "This block is simple and self-contained."
                        ),
                        "learning_unit_ids": (
                            (
                                []
                                if self.empty_seed
                                else ["intuition"]
                            )
                            if index == 0
                            else []
                        ),
                    }
                    for index, block_id in enumerate(payload["block_ids"])
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
                assert self.translation_started.is_set()
            round_task = payload["_round_task"]
            revised = round_task["kind"] == "revised_proposal"
            proposal_units = payload["plan"]["learning_units"] or [
                {
                    "unit_id": "proposer-added",
                    "anchor_block_ids": [
                        payload["plan"]["reader_needs"][0]["block_id"]
                    ],
                    "placement": "inline",
                    "purpose": "Supply the missing connection.",
                }
            ]
            value = {
                "learning_units": [
                    {
                        "unit_id": item["unit_id"],
                        "title": (
                            f"Question for {payload['plan']['chapter_id']}"
                            if item["unit_id"] == "intuition"
                            else "Restatement"
                        ),
                        "anchor_block_ids": list(
                            item["anchor_block_ids"]
                        ),
                        "placement": item["placement"],
                        "purpose": item["purpose"],
                        "content_markdown": (
                            "A focused source-anchored explanation "
                            "[@fixture-reference]."
                            if self.with_reference
                            else "A focused source-anchored explanation."
                            if item["unit_id"] == "intuition"
                            else "The source says the same thing again."
                        ),
                    }
                    for item in proposal_units
                    if not (
                        revised
                        and self.remove_second_unit
                        and item["unit_id"] == "redundant"
                    )
                ],
                "references": (
                    [
                        {
                            "reference_id": "fixture-reference",
                            "title": "An English Reference",
                            "source": "https://example.test/reference",
                            "dois": ["10.1000/FIXTURE"],
                            "arxiv_ids": ["2401.00001"],
                            "cached_document": None,
                            "cached_material": None,
                            **(
                                {
                                    "cached_material": {
                                        "identity": {
                                            "arxiv_id": "2401.00001",
                                            "dois": ["10.1000/fixture"],
                                            "urls": [
                                                "https://example.test/reference"
                                            ],
                                            "title": "An English Reference",
                                            "inspire_recid": "",
                                        },
                                        "resources": [
                                            {
                                                "resource_sha256": "f" * 64,
                                                "resource_size": 10,
                                                "media_type": "text/plain",
                                                "source_locator": (
                                                    "https://example.test/reference"
                                                ),
                                                "filename": "reference.txt",
                                            }
                                        ],
                                        "readable_resource": None,
                                    }
                                }
                                if self.invalid_cached_material
                                else {}
                            ),
                        }
                    ]
                    if self.with_reference
                    else []
                ),
            }
        elif contract == CHAPTER_GUIDE_REVIEW_PROMPT_VERSION:
            assert "translations" not in payload["draft"]
            round_number = payload["_round_task"]["round"]
            action = (
                "stop"
                if self.reviewer_stop_round is not None
                and round_number >= self.reviewer_stop_round
                else "continue"
            )
            value = {
                "schema_version": "arc.proposer_reviewer.review.v1",
                "action": action,
                "reason": (
                    "The proposal satisfies the reader needs."
                    if action == "stop"
                    else "One concrete revision remains."
                ),
                "feedback": {
                    "guide-proposer": (
                        "Preserve the grounded explanation and remove the "
                        "redundant unit; keep the same source anchor."
                    )
                },
                "payload": {
                    "reader_needs_satisfied": action == "stop",
                    "grounding_sufficient": True,
                    "remaining_issues": (
                        []
                        if action == "stop"
                        else ["Remove any redundant restatement."]
                    ),
                    "suggested_learning_units": [],
                    "suggested_references": [],
                },
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

    def execute(self, context, request, **kwargs):
        return self.execute_or_resume(context, request, **kwargs)


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
    elif contract == CHAPTER_PLAN_PROMPT_VERSION:
        invalid["reader_needs"][0]["block_id"] = "unknown-block"
    elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
        invalid["learning_units"][0]["anchor_block_ids"] = ["unknown-block"]
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
    if prompt.startswith("## Package protocol\n"):
        sections: dict[str, str] = {}
        for raw_section in prompt.removeprefix("## ").split("\n\n## "):
            heading, separator, body = raw_section.partition("\n")
            assert separator
            sections[heading] = body
        instructions = sections["Worker instructions"]
        contract_line = instructions.splitlines()[0]
        assert contract_line.startswith("Contract: ")
        payload = json.loads(sections["Caller context"])
        round_task = json.loads(sections["Round task"])
        payload["_round_task"] = round_task
        if round_task["kind"] == "independent_review":
            payload["draft"] = round_task["current_proposals"][
                "guide-proposer"
            ]
        return contract_line.removeprefix("Contract: "), payload
    first, _blank, rest = prompt.partition("\n\n")
    _instruction, marker, payload = rest.partition("\n\nInput JSON:\n")
    assert marker
    return first.removeprefix("Contract: "), json.loads(payload)


def test_translation_precedes_reviewed_guides_and_uses_local_glossary(
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
        execution=CompanionExecutionOptions(
            workers=2,
            paper_cache_root=tmp_path / "paper",
        ),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert translation.calls[0:2] == ["language", "glossary"]
    assert translation.approx_counts == [73]
    assert tasks.counts[CHAPTER_PLAN_PROMPT_VERSION] == 2
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 6
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 4
    assert all(
        "A quantum field appears here." not in prompt
        for _contract, _task_id, prompt in tasks.requests
    )
    for contract, input_ids in tasks.request_input_ids:
        assert input_ids[:2] == (
            "companion-source-index",
            "companion-source",
        ), contract
    plan_inputs = next(
        inputs
        for contract, inputs in tasks.request_input_ids
        if contract == CHAPTER_PLAN_PROMPT_VERSION
    )
    assert "literature-survey" not in plan_inputs
    assert "selected-evidence" not in plan_inputs
    assert tasks.runtime_environments
    assert {
        item["ARC_PAPER_CACHE"] for item in tasks.runtime_environments
    } == {str(tmp_path / "paper")}
    run_store = ImmutableArtifactStore(
        service.repository.run_directory(completed.run_id),
        repository_root=service.repository.root,
    )
    source_index_ref = run_store.find("source/model-index")
    assert source_index_ref is not None
    source_index = json.loads(run_store.read_bytes(source_index_ref))
    assert source_index["cache_relationship"] == "exact"
    assert source_index["cached_document"]["source_sha256"] == (
        document.source.artifact_digest
    )
    planned_chapters = plan_source_chapters(document)
    for chapter in planned_chapters:
        candidate = json.loads(
            (
                service.repository.run_directory(completed.run_id)
                / "working/candidates/chapters"
                / chapter.chapter_id
                / "plan.json"
            ).read_text(encoding="utf-8")
        )
        assert candidate["chapter_id"] == chapter.chapter_id
        final_guide = json.loads(
            (
                service.repository.run_directory(completed.run_id)
                / "working/candidates/chapters"
                / chapter.chapter_id
                / "guide-final.json"
            ).read_text(encoding="utf-8")
        )
        assert final_guide["chapter_id"] == chapter.chapter_id
    assert {
        chapter_id: [item["term"] for item in values]
        for chapter_id, values in tasks.guide_glossaries.items()
    } == {
        planned_chapters[0].chapter_id: ["quantum field"],
        planned_chapters[1].chapter_id: ["relativity"],
    }

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


def test_empty_seed_still_runs_three_proposals_and_covers_reader_need(
    tmp_path: Path,
) -> None:
    tasks = FakeGuideTasks(empty_seed=True)
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(_document(tmp_path), target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 6
    store = ImmutableArtifactStore(
        service.repository.run_directory(completed.run_id),
        repository_root=service.repository.root,
    )
    book_ref = store.find("book/accepted")
    assert book_ref is not None
    book = CompanionContentCodec.loads(store.read_bytes(book_ref))
    assert all(chapter.learning_units for chapter in book.chapters)


def test_program_names_and_publishes_only_cited_chapter_references(
    tmp_path: Path,
) -> None:
    tasks = FakeGuideTasks(with_reference=True)
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(_document(tmp_path), target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert completed.status is RunStatus.SUCCEEDED
    store = ImmutableArtifactStore(
        service.repository.run_directory(completed.run_id),
        repository_root=service.repository.root,
    )
    book_ref = store.find("book/accepted")
    assert book_ref is not None
    book = CompanionContentCodec.loads(store.read_bytes(book_ref))
    assert len(book.bibliography) == 1
    reference = book.bibliography[0]
    assert reference.evidence_id.startswith("reference-")
    assert reference.dois == ("10.1000/fixture",)
    assert reference.arxiv_ids == ("2401.00001",)
    assert all(
        unit.citations == (reference.evidence_id,)
        for chapter in book.chapters
        for unit in chapter.learning_units
    )


def test_forged_cached_material_reports_program_owned_candidate(
    tmp_path: Path,
) -> None:
    tasks = FakeGuideTasks(
        with_reference=True,
        invalid_cached_material=True,
    )
    service = CompanionService(tmp_path / "jobs")

    failed = service.build(
        CompanionBuildRequest(_document(tmp_path), target_language="en"),
        execution=CompanionExecutionOptions(
            workers=1,
            paper_cache_root=tmp_path / "paper-cache",
        ),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert failed.status is RunStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "chapter_reference_cache_invalid"
    assert Path(failed.error.details["candidate_path"]).is_file()


def test_cached_material_rejects_mismatched_identity_and_resources(
    tmp_path: Path,
) -> None:
    cache = ReferenceMaterialCache(tmp_path / "paper-cache")
    first_resource = cache.store_resource(
        b"first",
        media_type="text/plain",
    )
    second_resource = cache.store_resource(
        b"second",
        media_type="text/plain",
    )
    first = cache.store_material(
        ReferenceIdentity(dois=("10.1000/first",)),
        (first_resource,),
        readable_resource=first_resource,
    )
    cache.store_material(
        ReferenceIdentity(dois=("10.1000/second",)),
        (second_resource,),
        readable_resource=second_resource,
    )
    forged = cached_reference_material_to_document(first)
    forged["resources"] = [
        {
            "resource_sha256": second_resource.resource_sha256,
            "resource_size": second_resource.resource_size,
            "media_type": second_resource.media_type,
            "source_locator": second_resource.source_locator,
            "filename": second_resource.filename,
        }
    ]
    forged["readable_resource"] = forged["resources"][0]

    with pytest.raises(
        CompanionContentError,
        match="does not match",
    ):
        _verify_cached_reference_materials(
            {
                "references": [
                    {"cached_material": forged}
                ]
            },
            cache_root=tmp_path / "paper-cache",
        )


def test_cached_document_parse_failure_is_not_downgraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(tmp_path)
    monkeypatch.setattr(
        "arc_companion.build.ArcPaperService.cache_document",
        lambda _self, _source: (_ for _ in ()).throw(
            ValueError("parsed document contains duplicate math span IDs")
        ),
    )
    service = CompanionService(tmp_path / "jobs")

    failed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(
            workers=1,
            paper_cache_root=tmp_path / "paper",
        ),
        task_service=FakeGuideTasks(),  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert failed.status is RunStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "companion_content_invalid"
    assert "duplicate math span IDs" in failed.error.message


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
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 6
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 4
    assert [
        [unit.unit_id for unit in chapter.learning_units]
        for chapter in service.accepted_book(completed.run_id).chapters
    ] == [["intuition"], ["intuition"]]


@pytest.mark.parametrize(
        ("contract", "candidate_kind", "expected_calls"),
        [
            (AUTHOR_IDENTITY_PROMPT_VERSION, "author", 2),
            (CHAPTER_PLAN_PROMPT_VERSION, "plan", 3),
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
        "plan": f"chapters/{chapter_id}/plan.json",
    }
    tasks = FakeGuideTasks(
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


def test_reviewer_can_accept_without_forcing_an_extra_revision(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    chapters = plan_source_chapters(document)
    tasks = FakeGuideTasks(reviewer_stop_round=1)
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 2
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 2
    store = ImmutableArtifactStore(
        service.repository.run_directory(completed.run_id),
        repository_root=service.repository.root,
    )
    for chapter in chapters:
        assert store.find(
            "proposer-reviewer/loops/"
            f"{chapter.chapter_id}/rounds/001/reviews/guide-reviewer"
        ) is not None
        assert store.find(
            "proposer-reviewer/loops/"
            f"{chapter.chapter_id}/rounds/002/proposals/guide-proposer"
        ) is None


def test_invalid_terminal_revision_reports_program_owned_candidate(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    chapters = plan_source_chapters(document)
    tasks = FakeGuideTasks(
        semantic_invalid_contract=CHAPTER_GUIDE_PROMPT_VERSION,
        # Each chapter receives P-R-P-R-P; corrupt only chapter two's
        # terminal proposal so final deterministic validation owns the error.
        semantic_invalid_calls=frozenset({6}),
    )
    service = CompanionService(tmp_path / "jobs")

    failed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert failed.status is RunStatus.FAILED
    assert failed.error is not None
    candidate_path = (
        service.repository.run_directory(failed.run_id)
        / "working/candidates/chapters"
        / chapters[1].chapter_id
        / "guide-final.json"
    )
    assert failed.error.details["candidate_path"] == str(candidate_path)
    assert failed.error.details["chapter_id"] == chapters[1].chapter_id
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["chapter_id"] == chapters[1].chapter_id
    assert candidate["learning_units"][0]["anchor_block_ids"] == [
        "unknown-block"
    ]
    store = ImmutableArtifactStore(
        service.repository.run_directory(failed.run_id),
        repository_root=service.repository.root,
    )
    assert store.find(
        f"chapters/{chapters[0].chapter_id}/guide-accepted"
    ) is not None
    assert store.find(
        f"chapters/{chapters[1].chapter_id}/guide-accepted"
    ) is None

    candidate["learning_units"][0]["anchor_block_ids"] = [
        chapters[1].block_ids[0]
    ]
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    guide_calls = tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION]

    recovered = service.resume(
        failed.run_id,
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert recovered.status is RunStatus.SUCCEEDED
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == guide_calls


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
