from __future__ import annotations

import builtins
import json
from collections import Counter
from pathlib import Path
from threading import Event, Lock

import pytest

import arc_companion.build as companion_build

from arc_jobs import (
    ImmutableArtifactStore,
    RunContext,
    RunEngine,
    RunRepository,
    RunSpec,
    RunStatus,
    semantic_key,
)
from arc_llm import LLMCompleted
from arc_paper import (
    ArcPaperService,
    CachedDocumentRef,
    DocumentStructureCache,
    DocumentStructureEntry,
    DocumentStructureNodeKind,
    DocumentStructureOverlay,
    ReferenceIdentity,
    ReferenceMaterialCache,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    cached_document_ref_from_document,
    cached_reference_material_to_document,
)
from arc_render import (
    AnchorKind,
    FragmentAnchor,
    FragmentRevision,
    Layer,
    anchor_block_from_rich_block,
    block_text_to_markdown,
    decode_fragment_revision,
    encode_fragment_revision,
    fragment_revision_filename,
    fragment_revision_ref,
    source_identity_from_rich_document,
)
from arc_translate import TranslationResult, TranslationRevisionArtifact

from arc_companion.build import (
    COMPANION_BUILD_HANDLER,
    CompanionBuildHandler,
    _attach_cached_reference_materials,
    _verify_cached_reference_materials,
)
from arc_companion.generation_validation import CompanionContentError
from arc_companion.prompts import (
    AUTHOR_IDENTITY_PROMPT_VERSION,
    CHAPTER_GUIDE_PROMPT_VERSION,
    CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
)
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
)
from arc_companion.service import CompanionService, CompanionServiceError
from arc_companion.source_planning import (
    plan_source_chapters,
    plan_structured_source_chapters,
)
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
        checked_part_numbers: tuple[int, ...] | None = None,
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
        self.checked_part_numbers = checked_part_numbers
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
        elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
            self.guide_glossaries[request.task_id] = list(payload["glossary"])
            if self.guide_started is not None:
                self.guide_started.set()
            if self.translation_started is not None:
                assert self.translation_started.is_set()
            round_task = payload["_round_task"]
            revised = round_task["kind"] == "revised_proposal"
            text = (
                "A focused source-anchored explanation [@1]."
                if self.with_reference
                else "A focused source-anchored explanation."
            )
            value = {
                "chapter_guide": {
                    "title": f"Guide to {payload['chapter']['title']}",
                    "content_markdown": text,
                },
                "section_guides": [],
                "companions": (
                    [{
                        "after_part": 1,
                        "title": "Local companion",
                        "content_markdown": "A useful local explanation.",
                    }]
                    if self.remove_second_unit and not revised
                    else []
                ),
                "references": (
                    [
                        {
                            "title": "An English Reference",
                            "source": (
                                "https://example.test/reference "
                                "doi:10.1000/FIXTURE arXiv:2401.00001"
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
                    "checked_complete_chapter": True,
                    "checked_part_numbers": (
                        list(self.checked_part_numbers)
                        if self.checked_part_numbers is not None
                        else [
                            int(item["part_number"])
                            for item in payload["chapter"]["parts"]
                        ]
                    ),
                    "checked_section_numbers": [
                        int(item["section_number"])
                        for item in payload["chapter"]["sections"]
                    ],
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
    elif contract == CHAPTER_GUIDE_PROMPT_VERSION:
        invalid["companions"] = [
            {
                "after_part": 9999,
                "title": "Bad anchor",
                "content_markdown": "Bad.",
            }
        ]
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
        self.glossary_kwargs: list[dict] = []

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
        self.glossary_kwargs.append(dict(kwargs))
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

    def translate_blocks(self, context, source, **kwargs):
        self.calls.append(f"translation:{kwargs['artifact_prefix']}")
        if self.translation_started is not None:
            self.translation_started.set()
        by_id = {item.block_id: item for item in source.blocks}
        source_identity = source_identity_from_rich_document(source)
        revisions = []
        artifacts = []
        for block_id in kwargs["block_ids"]:
            block = by_id[block_id]
            if (
                block.kind.value == "figure"
                and not str(block.payload["caption"]).strip()
                and not str(block.payload["alt_text"]).strip()
            ):
                continue
            text = (
                str(block.payload["text"])
                if block.kind.value == "code"
                else str(block.payload["tex"])
                if block.kind.value == "equation"
                else f"translated {block.kind.value}"
            )
            revision = FragmentRevision(
                source=source_identity,
                fragment_id=f"fake-translation-{block.ordinal}",
                revision=1,
                parent_semantic_digest=None,
                anchor=FragmentAnchor(
                    AnchorKind.BLOCK,
                    block_id,
                    (anchor_block_from_rich_block(block),),
                ),
                priority=10,
                role="translation",
                language=kwargs["target_language"],
                title=None,
                citation_ids=(),
                provenance={"producer": "arc-translate"},
                markdown_body=block_text_to_markdown(block, text),
            )
            relative = (
                f"fragments/{fragment_revision_filename(revision)}"
            )
            reference = fragment_revision_ref(relative, revision)
            artifact = context.artifacts.publish_bytes(
                (
                    f"{kwargs['artifact_prefix']}/fragments/"
                    f"{revision.fragment_id}/revision-000001"
                ),
                encode_fragment_revision(revision).encode("utf-8"),
                media_type="text/markdown",
            )
            revisions.append(reference)
            artifacts.append(
                TranslationRevisionArtifact(reference, artifact)
            )
        return TranslationResult(
            source_language="en",
            target_language=kwargs["target_language"],
            mode="enabled",
            coverage="selection",
            layer=Layer(
                source_identity, "arc-translate", tuple(revisions)
            ),
            revision_artifacts=tuple(artifacts),
        ).to_document()


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


class StrictLegacyTranslationAdapter(FakeTranslationAdapter):
    """Adapter using the pre-structure keyword-only signature."""

    def build_glossary(
        self,
        context,
        source,
        *,
        language,
        target_language,
        approx_count,
        model,
        execution,
        resume_input,
    ):
        return super().build_glossary(
            context,
            source,
            language=language,
            target_language=target_language,
            approx_count=approx_count,
            model=model,
            execution=execution,
            resume_input=resume_input,
        )


def test_unstructured_build_preserves_legacy_translation_adapter_signature(
    tmp_path: Path,
) -> None:
    completed = CompanionService(tmp_path / "jobs").build(
        CompanionBuildRequest(_document(tmp_path), target_language="zh-CN"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=FakeGuideTasks(),  # type: ignore[arg-type]
        translation_adapter=StrictLegacyTranslationAdapter(mode="enabled"),
    )

    assert completed.status is RunStatus.SUCCEEDED


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
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 6
    assert tasks.counts[CHAPTER_GUIDE_REVIEW_PROMPT_VERSION] == 4
    assert all(
        "A quantum field appears here." not in prompt
        for _contract, _task_id, prompt in tasks.requests
    )
    for contract, input_ids in tasks.request_input_ids:
        assert input_ids[0] == "companion-source-index", contract
        assert "companion-source" not in input_ids, contract
        if contract in {
            CHAPTER_GUIDE_PROMPT_VERSION,
            CHAPTER_GUIDE_REVIEW_PROMPT_VERSION,
        }:
            assert "companion-translation-index" in input_ids, contract
    guide_payloads = [
        _request_payload(prompt)[1]
        for contract, _task_id, prompt in tasks.requests
        if contract == CHAPTER_GUIDE_PROMPT_VERSION
    ]
    assert guide_payloads
    assert all("arc_commands" in item for item in guide_payloads)
    assert all("block_ids" not in item for item in guide_payloads)
    for payload in guide_payloads:
        commands = payload["arc_commands"]
        assert commands["availability"] == "exact"
        assert any(
            item["command_id"] == "complete-current-chapter"
            for item in commands["source"]
        )
        assert all(
            item["shell"] and item["argv"][0] == "arc-paper"
            for item in commands["source"]
        )
        ranged = [
            item
            for item in commands["source"]
            if item["argv"][1] == "read-cached-source-range"
        ]
        assert ranged
        assert all("--text-only" in item["argv"] for item in ranged)
        assert all(
            item["command_id"] != "current-section"
            for item in commands["source"]
        )
        search_examples = commands["full_document_search_examples"]
        assert search_examples["availability"] == "exact"
        assert (
            search_examples["single_term"]["argv"][1]
            == "search-cached-document"
        )
        assert len(search_examples["alternative_terms"]) == 2
        assert all(
            item["argv"][1] == "search-cached-document"
            for item in search_examples["alternative_terms"]
        )
        assert {
            item["argv"][-1]
            for item in search_examples["alternative_terms"]
        } == {"<term-A>", "<term-B>"}
        assert all(
            "A quantum field appears here." not in item["shell"]
            for item in commands["source"]
        )
        translated = commands["translation"]
        assert translated["availability"] == "exact"
        assert translated["target_language"] == "zh-CN"
        assert any(
            item["command_id"] == "complete-current-chapter"
            for item in translated["parts"]
        )
        assert all(
            item["shell"]
            and item["argv"][0:2]
            == ["arc-paper", "read-cached-source-range"]
            and "--text-only" in item["argv"]
            for item in translated["parts"]
        )
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
    assert "blocks" not in source_index
    assert "chapters" not in source_index
    assert source_index["cached_document"]["source_sha256"] == (
        document.source.artifact_digest
    )
    translation_index_ref = run_store.find("translation/model-index")
    assert translation_index_ref is not None
    translation_index = json.loads(
        run_store.read_bytes(translation_index_ref)
    )
    assert translation_index["source_document_sha256"] == (
        document.document_digest
    )
    assert translation_index["target_language"] == "zh-CN"
    assert "translated paragraph" not in json.dumps(
        translation_index, ensure_ascii=False
    )
    translation_view_ref = run_store.find("translation/model-view")
    assert translation_view_ref is not None
    assert "translated paragraph" in run_store.read_bytes(
        translation_view_ref
    ).decode("utf-8")
    first_part = translation_index["chapters"][0]["parts"][0]
    translated_range = ArcPaperService(
        cache_root=tmp_path / "paper"
    ).read_cached_source_range(
        cached_document_ref_from_document(
            translation_index["cached_document"]
        ),
        first_part["line_start"],
        first_part["line_end"],
        text_only=True,
    )
    assert translated_range.text.startswith("# translated ")
    planned_chapters = plan_source_chapters(document)
    assert not (
        service.repository.run_directory(completed.run_id)
        / "working/candidates/chapters"
        / planned_chapters[0].chapter_id
        / "plan.json"
    ).exists()
    for chapter in planned_chapters:
        final_guide = json.loads(
            (
                service.repository.run_directory(completed.run_id)
                / "working/candidates/chapters"
                / chapter.chapter_id
                / "guide-final.json"
            ).read_text(encoding="utf-8")
        )
        assert set(final_guide) == {
            "chapter_guide",
            "section_guides",
            "companions",
            "references",
        }
    assert {
        tuple(item["term"] for item in values)
        for values in tasks.guide_glossaries.values()
    } == {("quantum field",), ("relativity",)}

    publication = service.publication(completed.run_id)
    assert publication.reader_profile["translation_mode"] == "enabled"
    published = service.published_companion(completed.run_id)
    store = ImmutableArtifactStore(
        service.repository.run_directory(completed.run_id),
        repository_root=service.repository.root,
    )
    translations = [
        revision
        for ref in published.fragment_refs
        for revision in (
            decode_fragment_revision(store.read_bytes(ref).decode("utf-8")),
        )
        if revision.role == "translation"
    ]
    assert [item.anchor.target_id for item in translations] == [
        item.block_id for item in document.blocks
    ]


def test_translation_durable_units_freeze_the_lane_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CompanionService(tmp_path / "jobs")
    request = CompanionBuildRequest(
        _document(tmp_path),
        target_language="zh-CN",
    )
    prepared = service.prepare(request)
    old_group = (
        service.repository.run_directory(prepared.run_id)
        / "groups"
        / "chapter-translations-v2"
    )
    old_group.mkdir(parents=True)
    (old_group / "state.json").write_text("{}", encoding="utf-8")
    captured: list[tuple[str, tuple]] = []
    original_run_group = RunContext.run_group

    def capture_run_group(self, group_id, units, worker, **kwargs):
        if group_id.startswith("chapter-translations-"):
            captured.append((group_id, units))
        return original_run_group(
            self,
            group_id,
            units,
            worker,
            **kwargs,
        )

    monkeypatch.setattr(RunContext, "run_group", capture_run_group)

    completed = service.execute(
        prepared.run_id,
        execution=CompanionExecutionOptions(workers=1),
        task_service=FakeGuideTasks(),  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="enabled"),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert [group_id for group_id, _units in captured] == [
        "chapter-translations-v3"
    ]
    assert all(
        unit.semantic_input["translation_lane_contract"]
        == "arc.companion.translation_lane.v1"
        for _group_id, units in captured
        for unit in units
    )
    for _group_id, units in captured:
        for unit in units:
            legacy_input = dict(unit.semantic_input)
            legacy_input.pop("translation_lane_contract")
            assert (
                semantic_key(legacy_input).sha256
                != semantic_key(unit.semantic_input).sha256
            )
    assert json.loads(
        (old_group / "state.json").read_text(encoding="utf-8")
    ) == {}


def test_structural_display_chapter_skips_loop_but_translates_and_augments(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    paper = ArcPaperService(cache_root=tmp_path / "paper")
    cached = paper.cache_document(document.source)
    pdf = CachedDocumentRef(
        SourceFormat.PDF,
        "f" * 64,
        1,
        "application/pdf",
        "test.pdf",
        "e" * 64,
    )
    overlay = DocumentStructureOverlay(
        cached,
        pdf,
        (
            DocumentStructureEntry(
                "real-chapter",
                "Relativity",
                1,
                None,
                0,
                5,
                5,
                7,
                1,
                1,
                DocumentStructureNodeKind.CONTENT,
                "fixture",
            ),
        ),
    )
    structure_ref = DocumentStructureCache(tmp_path / "paper").store(overlay)
    request = CompanionBuildRequest(
        document,
        target_language="zh-CN",
        structure_ref=structure_ref,
        companion_section_ids=("real-chapter",),
    )
    tasks = FakeGuideTasks()
    translation = FakeTranslationAdapter(mode="enabled")
    service = CompanionService(tmp_path / "jobs")
    prepared = service.prepare(request)

    class NoteHandler(CompanionBuildHandler):
        def _augment_chapter_candidate(self, chapter, candidate):
            value = super()._augment_chapter_candidate(chapter, candidate)
            if not chapter.generate_guide:
                value["companions"] = [
                    {
                        "after_part": 1,
                        "title": "译者注",
                        "content_markdown": "译者注：固定说明。",
                    }
                ]
            return value

    spec = service.repository.read_spec(prepared.run_id)
    snapshot = RunEngine(service.repository).execute(
        spec,
        NoteHandler(
            request,
            execution=CompanionExecutionOptions(
                workers=1,
                paper_cache_root=tmp_path / "paper",
            ),
            task_service=tasks,  # type: ignore[arg-type]
            translation_adapter=translation,
        ),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert translation.glossary_kwargs[0]["structure_ref"] == structure_ref
    assert translation.glossary_kwargs[0]["section_ids"] == ("real-chapter",)
    assert tasks.counts[CHAPTER_GUIDE_PROMPT_VERSION] == 3
    publication = service.publication(snapshot.run_id)
    assert len(publication.outline) >= 2
    structured_chapter = next(
        item for item in publication.outline if item.title == "Relativity"
    )
    expected_chapter = next(
        item
        for item in plan_structured_source_chapters(
            document,
            overlay,
            companion_section_ids=("real-chapter",),
        )
        if item.structure_section_id == "real-chapter"
    )
    assert (
        structured_chapter.anchor_block_id
        == expected_chapter.display_anchor_block_id
    )
    assert publication.glossary[0]["term"] == "quantum field"
    assert publication.glossary[0]["translated_term"] == "量子场"
    assert publication.glossary[0]["definition"] == "量子场的定义"
    assert [item["term"] for item in publication.glossary] == [
        "quantum field",
        "relativity",
    ]


def test_review_audit_excludes_verified_program_additions(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    request = CompanionBuildRequest(
        document,
        target_language="zh-CN",
    )
    service = CompanionService(tmp_path / "jobs")
    prepared = service.prepare(request)

    class NoteHandler(CompanionBuildHandler):
        def _augment_chapter_candidate(self, chapter, candidate):
            value = super()._augment_chapter_candidate(chapter, candidate)
            value["companions"] = [
                *value.get("companions", []),
                {
                    "after_part": 2,
                    "title": "译者注",
                    "content_markdown": "译者注：固定说明。",
                },
            ]
            return value

    spec = service.repository.read_spec(prepared.run_id)
    snapshot = RunEngine(service.repository).execute(
        spec,
        NoteHandler(
            request,
            execution=CompanionExecutionOptions(workers=1),
            task_service=FakeGuideTasks(
                checked_part_numbers=(1,),
            ),  # type: ignore[arg-type]
            translation_adapter=FakeTranslationAdapter(mode="enabled"),
        ),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    published = service.published_companion(snapshot.run_id)
    store = ImmutableArtifactStore(
        service.repository.run_directory(snapshot.run_id),
        repository_root=service.repository.root,
    )
    bodies = [
        decode_fragment_revision(store.read_bytes(ref).decode("utf-8")).markdown_body
        for ref in published.fragment_refs
    ]
    assert any("固定说明" in body for body in bodies)


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
    assert store.find("publication/publication.json") is not None
    assert service.published_companion(completed.run_id).fragment_refs


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
    publication = service.publication(completed.run_id)
    assert len(publication.bibliography) == 1
    reference = publication.bibliography[0]
    assert str(reference["evidence_id"]).startswith("reference-")
    assert reference["dois"] == ("10.1000/fixture",)
    assert reference["arxiv_ids"] == ("2401.00001",)


def test_minimal_reference_contract_does_not_accept_model_cache_handles(
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

    assert failed.status is RunStatus.SUCCEEDED


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


def test_program_attaches_already_admitted_reference_material(
    tmp_path: Path,
) -> None:
    cache = ReferenceMaterialCache(tmp_path / "paper-cache")
    resource = cache.store_resource(
        b"reference",
        media_type="text/plain",
        source_locator="https://example.test/reference",
    )
    cache.store_material(
        ReferenceIdentity(
            dois=("10.1000/fixture",),
            title="Fixture",
        ),
        (resource,),
        readable_resource=resource,
    )

    attached = _attach_cached_reference_materials(
        {
            "references": [
                {
                    "reference_id": "reference-fixture",
                    "title": "Fixture",
                    "source": "doi:10.1000/fixture",
                    "dois": ["10.1000/fixture"],
                    "arxiv_ids": [],
                    "cached_document": None,
                    "cached_material": None,
                }
            ]
        },
        cache_root=tmp_path / "paper-cache",
    )

    assert attached["references"][0]["cached_material"] is not None


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
    published = service.published_companion(completed.run_id)
    assert len(published.fragment_refs) == len(plan_source_chapters(document))
    assert all(
        ref.artifact_id.startswith(
            "publication/fragments/revision-000001-"
        )
        for ref in published.fragment_refs
    )


@pytest.mark.parametrize(
        ("contract", "candidate_kind", "expected_calls"),
        [(AUTHOR_IDENTITY_PROMPT_VERSION, "author", 2)],
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
    assert candidate["companions"][0]["after_part"] == 9999
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

    first_candidate_path = (
        service.repository.run_directory(failed.run_id)
        / "working/candidates/chapters"
        / chapters[0].chapter_id
        / "guide-final.json"
    )
    first_candidate = json.loads(
        first_candidate_path.read_text(encoding="utf-8")
    )
    first_candidate["chapter_guide"]["content_markdown"] = (
        "Recovered guide content."
    )
    first_candidate_path.write_text(
        json.dumps(first_candidate), encoding="utf-8"
    )
    candidate["companions"][0]["after_part"] = 1
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
    recovered_store = ImmutableArtifactStore(
        service.repository.run_directory(recovered.run_id),
        repository_root=service.repository.root,
    )
    recovered_guide_ref = recovered_store.find(
        "recovery-1/chapters/"
        f"{chapters[0].chapter_id}/guide-accepted"
    )
    assert recovered_guide_ref is not None
    recovered_guide = json.loads(
        recovered_store.read_bytes(recovered_guide_ref)
    )
    assert (
        recovered_guide["learning_units"][0]["content_markdown"]
        == "Recovered guide content."
    )


def test_resume_rebuilds_joined_chapter_after_guide_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(tmp_path)
    chapters = plan_source_chapters(document)
    tasks = FakeGuideTasks()
    service = CompanionService(tmp_path / "jobs")
    publish = companion_build.publish_companion
    calls = 0

    def fail_publication_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise companion_build.CompanionPublicationError(
                "fixture publication failure"
            )
        return publish(*args, **kwargs)

    monkeypatch.setattr(
        companion_build, "publish_companion", fail_publication_once
    )
    failed = service.build(
        CompanionBuildRequest(document, target_language="en"),
        execution=CompanionExecutionOptions(workers=1),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=FakeTranslationAdapter(mode="skipped"),
    )

    assert failed.status is RunStatus.FAILED
    store = ImmutableArtifactStore(
        service.repository.run_directory(failed.run_id),
        repository_root=service.repository.root,
    )
    assert store.find(
        f"chapters/{chapters[0].chapter_id}/accepted"
    ) is not None

    candidate_path = (
        service.repository.run_directory(failed.run_id)
        / "working/candidates/chapters"
        / chapters[0].chapter_id
        / "guide-final.json"
    )
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["chapter_guide"]["content_markdown"] = (
        "Recovered joined content."
    )
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
    recovered_store = ImmutableArtifactStore(
        service.repository.run_directory(recovered.run_id),
        repository_root=service.repository.root,
    )
    joined_ref = recovered_store.find(
        "recovery-1/chapters/"
        f"{chapters[0].chapter_id}/accepted"
    )
    assert joined_ref is not None
    joined = json.loads(recovered_store.read_bytes(joined_ref))
    assert (
        joined["learning_units"][0]["content_markdown"]
        == "Recovered joined content."
    )


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


def test_default_adapter_resolves_shared_cache_for_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arc_paper
    from arc_llm import LLMExecutionOptions, ModelSelection

    captured = {}

    class FakeStructureCache:
        def __init__(self, root):
            captured["root"] = root

        def read(self, ref):
            captured["ref"] = ref
            return "overlay"

    class FakeTermInventoryStore:
        root = tmp_path / "shared-cache"

    class FakeResult:
        def to_document(self):
            return {"result": "ok"}

    class FakeWorkflow:
        def build_glossary(self, _context, _source, **kwargs):
            captured["kwargs"] = kwargs
            return FakeResult()

    adapter = ArcTranslateAdapter()
    monkeypatch.setattr(arc_paper, "DocumentStructureCache", FakeStructureCache)
    monkeypatch.setattr(arc_paper, "TermInventoryStore", FakeTermInventoryStore)
    monkeypatch.setattr(
        adapter,
        "_service_and_source",
        lambda source: (FakeWorkflow(), object()),
    )
    document = _document(tmp_path)
    language = {
        "schema_version": "arc.translate.language_result.v1",
        "document_digest": document.document_digest,
        "source_digest": document.source.artifact_digest,
        "language_tag": "en",
        "classification": "known",
        "confidence": 1.0,
        "target_language": "zh-CN",
        "mode": "enabled",
    }

    result = adapter.build_glossary(
        None,  # type: ignore[arg-type]
        document,
        language=language,
        target_language="zh-CN",
        structure_ref=object(),  # type: ignore[arg-type]
        section_ids=("chapter",),
        approx_count=5,
        model=ModelSelection(),
        execution=LLMExecutionOptions(),
        resume_input=None,
    )

    assert result == {"result": "ok"}
    assert captured["root"] == tmp_path / "shared-cache"
    assert captured["kwargs"]["keyword_structure"] == "overlay"
    assert captured["kwargs"]["keyword_section_ids"] == ("chapter",)


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


@pytest.mark.parametrize(
    "handler",
    (
        "arc.companion.build.v6",
        "arc.companion.build.v10",
        "arc.companion.build.v11",
    ),
)
def test_unfinished_legacy_handlers_require_a_new_build(
    tmp_path: Path,
    handler: str,
) -> None:
    service = CompanionService(tmp_path / "jobs")
    spec = RunSpec("legacy-run", handler, {})

    with pytest.raises(CompanionServiceError) as exc_info:
        service._handler(
            spec,
            execution=CompanionExecutionOptions(),
            task_service=None,
            translation_adapter=None,
        )

    assert exc_info.value.code == "run_handler_invalid"
