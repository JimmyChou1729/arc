from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import (
    ArtifactDigest,
    ArtifactSourceRef,
    Paused,
    RunContext,
    RunError,
    RunRepository,
    RunSpec,
    RunStatus,
    canonical_json_bytes,
)
from arc_llm import LLMCompleted
from arc_paper import ArcPaperService, RichDocumentParserService
from arc_render import decode_fragment_revision

from arc_translate import (
    BlocksRequest,
    GenerationRecipe,
    GlossaryRequest,
    GlossaryResult,
    LanguageRequest,
    LanguageResult,
    TranslationService,
    TranslationResult,
    TranslationSource,
    TranslationWorkflowService,
    source_blocks,
)
from arc_translate.prompts import (
    GLOSSARY_SCHEMA,
    GLOSSARY_PROMPT_VERSION,
    LANGUAGE_PROMPT_VERSION,
    REVIEW_PROMPT_VERSION,
    TRANSLATION_SCHEMA,
    TRANSLATION_PROMPT_VERSION,
)
from arc_translate.source import block_text
from arc_translate.source import (
    STRUCTURAL_FIGURE_PLACEHOLDER,
    TranslationSourceError,
    resolve_translation_source,
)
from arc_translate.workflow import (
    OUTPUT_SUPERVISION_SCHEMA,
    REVIEW_SUPERVISION_SCHEMA,
    TranslationWorkflowError,
    _output_supervision,
)


class FakeTasks:
    def __init__(
        self,
        *,
        language: str = "en",
        classification: str = "known",
        invalid_review: bool = False,
        translation_prefix: str = "translated:",
        translation_prefix_by_text: dict[str, str] | None = None,
    ) -> None:
        self.language = language
        self.classification = classification
        self.invalid_review = invalid_review
        self.translation_prefix = translation_prefix
        self.translation_prefix_by_text = translation_prefix_by_text or {}
        self.calls: list[str] = []
        self.translation_glossaries: list[list[str]] = []
        self.prompt_glossary_fields: list[list[set[str]]] = []
        self.translation_blocks: list[list[dict[str, Any]]] = []
        self.review_blocks: list[list[dict[str, Any]]] = []
        self.prompt_sizes: list[tuple[str, int]] = []

    def execute_or_resume(
        self, _context, request, *, input=None, options=None
    ):
        contract, payload = _prompt(request.prompt)
        self.calls.append(contract)
        self.prompt_sizes.append((contract, len(request.prompt.encode("utf-8"))))
        if contract == LANGUAGE_PROMPT_VERSION:
            value = {
                "language_tag": self.language,
                "classification": self.classification,
                "confidence": 0.9,
            }
        elif contract == GLOSSARY_PROMPT_VERSION:
            value = {
                "entries": [
                    {
                        "term_id": term["term_id"],
                        "preferred_translation": f"target:{term['term']}",
                        "target_definition": f"definition:{term['term']}",
                    }
                    for term in payload["terms"]
                ]
            }
        elif contract == TRANSLATION_PROMPT_VERSION:
            self.translation_blocks.append(payload["blocks"])
            self.translation_glossaries.append(
                [item["term"] for item in payload["glossary"]]
            )
            self.prompt_glossary_fields.append(
                [set(item) for item in payload["glossary"]]
            )
            translations = []
            for block in payload["blocks"]:
                identity = block["source_identity"]
                if identity["code_text"] is not None:
                    text = identity["code_text"]
                elif block["kind"] == "equation":
                    text = block_text(block)
                else:
                    source_text = block_text(block)
                    prefix = next(
                        (
                            value
                            for marker, value in (
                                self.translation_prefix_by_text.items()
                            )
                            if marker in source_text
                        ),
                        self.translation_prefix,
                    )
                    text = f"{prefix}{source_text}"
                for token in [
                    *identity["equations"],
                    *identity["link_targets"],
                ]:
                    if token not in text:
                        text += f" {token}"
                if not text.strip():
                    text = "translated:block"
                translations.append(
                    {
                        "block_id": block["block_id"],
                        "text": text,
                    }
                )
            value = {"translations": translations}
        elif contract == REVIEW_PROMPT_VERSION:
            self.review_blocks.append(payload["blocks"])
            self.prompt_glossary_fields.append(
                [set(item) for item in payload["glossary"]]
            )
            patches = (
                [{"block_id": "missing-block", "replacement": "unsafe"}]
                if self.invalid_review
                else []
            )
            value = {
                "translation_patches": patches,
                "summary": "reviewed",
            }
        else:  # pragma: no cover - guards contract drift
            raise AssertionError(contract)
        return LLMCompleted(value, "fake", "fake", None, None)


class InvalidGlossaryTasks:
    def __init__(self):
        self.calls = 0

    def execute_or_resume(self, _context, request, *, input=None, options=None):
        contract, _payload = _prompt(request.prompt)
        assert contract == GLOSSARY_PROMPT_VERSION
        self.calls += 1
        return LLMCompleted(
            {
                "entries": [
                    {
                        "term_id": "wrong-id",
                        "preferred_translation": "wrong",
                        "target_definition": "wrong",
                    }
                ]
            },
            "fake",
            "fake",
            None,
            None,
        )


class InvalidOnceTasks(FakeTasks):
    def __init__(self, invalid_contract: str):
        super().__init__()
        self.invalid_contract = invalid_contract
        self.invalid_attempts = 0
        self.task_ids: list[str] = []

    def execute_or_resume(self, context, request, *, input=None, options=None):
        contract, _payload = _prompt(request.prompt)
        self.task_ids.append(request.task_id)
        if contract == self.invalid_contract and self.invalid_attempts == 0:
            self.invalid_attempts += 1
            self.calls.append(contract)
            if contract == LANGUAGE_PROMPT_VERSION:
                value = {
                    "language_tag": " ",
                    "classification": "known",
                    "confidence": 0.9,
                }
            elif contract == TRANSLATION_PROMPT_VERSION:
                value = {
                    "translations": [
                        {"block_id": "wrong-block", "text": "translated"}
                    ]
                }
            else:  # pragma: no cover - guards fixture scope
                raise AssertionError(contract)
            return LLMCompleted(value, "fake", "fake", None, None)
        return super().execute_or_resume(
            context, request, input=input, options=options
        )


@dataclass
class FakeKeywords:
    terms: list[dict[str, Any]]
    calls: int = 0

    def extract_keywords(
        self,
        _context,
        source,
        *,
        approx_count=50,
        model=None,
        resume_input=None,
        options=None,
    ):
        self.calls += 1
        payload = canonical_json_bytes(self.terms)
        return {
            "schema_version": "arc.paper.keyword_result.v1",
            "document_digest": source.document_digest,
            "source_digest": source.source.artifact_digest,
            "approx_count": approx_count,
            "planned_count": (3 * approx_count + 1) // 2,
            "returned_count": len(self.terms),
            "terms": self.terms,
            "inventory_digest": hashlib.sha256(payload).hexdigest(),
            "warnings": [],
        }


def _term(
    term_id: str, term: str, *, sentence: str | None = None
) -> dict[str, Any]:
    return {
        "term_id": term_id,
        "term": term,
        "aliases": [],
        "occurrence_count": 2,
        "source_refs": ["section:intro"],
        "matched_sentences": [
            {
                "text": sentence or f"{term} occurs here.",
                "section_id": "intro",
                "page_number": None,
                "matched_surface": term,
                "clipped": False,
            }
        ],
    }


def _source(tmp_path: Path) -> TranslationSource:
    markdown = tmp_path / "source.md"
    markdown.write_text(
        "# Intro\n\nEntropy appears in this paragraph.\n\n"
        "# Methods\n\nA tensor appears in this paragraph.\n\n"
        "```python\nprint('fixed')\n```\n",
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "paper-cache")
    artifact = paper.import_source(markdown)
    rich = RichDocumentParserService(paper.repository).parse_source(artifact)
    return TranslationSource(rich)


def _context(tmp_path: Path, run_id: str = "parent-run") -> RunContext:
    repository = RunRepository(tmp_path / "jobs")
    snapshot = repository.create(RunSpec(run_id, "test.parent", {}))
    return RunContext(
        repository,
        snapshot,
        resume_input=None,
    )


def _prompt(prompt: str) -> tuple[str, dict[str, Any]]:
    contract = prompt.splitlines()[0].removeprefix("Contract: ")
    payload = json.loads(prompt.split("Input JSON:\n", 1)[1])
    return contract, payload


def test_equation_translation_round_trips_through_fragment_markdown(
    tmp_path: Path,
) -> None:
    tex = r"\left[p_\mu\right](p'-p)"
    markdown = tmp_path / "equation.md"
    markdown.write_text(
        f"# Equation\n\n$$\n{tex}\n$$\n",
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "equation-cache")
    rich = RichDocumentParserService(paper.repository).parse_source(
        paper.import_source(markdown)
    )
    source = TranslationSource(rich)
    equation = next(
        block
        for block in source_blocks(source)
        if block["kind"] == "equation"
    )
    tasks = FakeTasks()
    context = _context(tmp_path, "equation-round-trip")

    result = TranslationWorkflowService(tasks).translate_blocks(
        context,
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "zh-CN",
            "enabled",
        ),
        glossary=GlossaryResult(
            source.document_digest,
            source.source_digest,
            "zh-CN",
            1,
            "d" * 64,
            (),
        ),
        target_language="zh-CN",
    )

    assert isinstance(result, TranslationResult)
    assert all(
        block["block_id"] != equation["block_id"]
        for window in tasks.translation_blocks
        for block in window
    )
    assert all(
        block["block_id"] != equation["block_id"]
        for window in tasks.review_blocks
        for block in window
    )
    revisions = [
        decode_fragment_revision(
            context.artifacts.read_bytes(item.artifact).decode("utf-8"),
            filename=Path(item.revision.path).name,
        )
        for item in result.revision_artifacts
    ]
    revision = next(
        item
        for item in revisions
        if item.anchor.target_id == equation["block_id"]
    )
    assert revision.markdown_body == f"$$\n{tex}\n$$\n"


def test_equation_is_reinjected_after_review_supervision(tmp_path: Path) -> None:
    tex = r"E = mc^2"
    markdown = tmp_path / "equation-supervision.md"
    markdown.write_text(
        f"# Intro\n\nSource prose.\n\n$$\n{tex}\n$$\n",
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "equation-supervision-cache")
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(
            paper.import_source(markdown)
        )
    )
    equation = next(
        block for block in source_blocks(source) if block["kind"] == "equation"
    )
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1,
        "fr",
        "enabled",
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "fr",
        1,
        "d" * 64,
        (),
    )
    context = _context(tmp_path, "equation-supervision")
    tasks = FakeTasks(invalid_review=True)
    workflow = TranslationWorkflowService(tasks)

    paused = workflow.translate_blocks(
        context,
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )

    assert isinstance(paused, Paused)
    resumed = workflow.translate_blocks(
        RunContext(
            context.repository,
            context.repository.inspect("equation-supervision").snapshot,
            resume_input={
                "schema_version": REVIEW_SUPERVISION_SCHEMA,
                "resume_key": paused.awaiting.resume_key,
                "action": "accept_pre_review",
            },
        ),
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )

    assert isinstance(resumed, TranslationResult)
    assert all(
        block["block_id"] != equation["block_id"]
        for window in tasks.translation_blocks
        for block in window
    )
    assert all(
        block["block_id"] != equation["block_id"]
        for window in tasks.review_blocks
        for block in window
    )
    revisions = [
        decode_fragment_revision(
            context.artifacts.read_bytes(item.artifact).decode("utf-8"),
            filename=Path(item.revision.path).name,
        )
        for item in resumed.revision_artifacts
    ]
    revision = next(
        item
        for item in revisions
        if item.anchor.target_id == equation["block_id"]
    )
    assert revision.markdown_body == f"$$\n{tex}\n$$\n"


def test_glossary_schema_only_requests_reasoned_content_and_join_id():
    entry = GLOSSARY_SCHEMA["properties"]["entries"]["items"]
    assert entry["additionalProperties"] is False
    assert entry["required"] == [
        "term_id",
        "preferred_translation",
        "target_definition",
    ]
    assert set(entry["properties"]) == set(entry["required"])


def test_translation_schema_only_requests_block_id_and_text():
    entry = TRANSLATION_SCHEMA["properties"]["translations"]["items"]
    assert entry["additionalProperties"] is False
    assert entry["required"] == ["block_id", "text"]
    assert set(entry["properties"]) == {"block_id", "text"}


def test_language_same_primary_skips_but_mixed_stays_enabled(tmp_path):
    source = _source(tmp_path)
    context = _context(tmp_path, "known-language")
    known = TranslationWorkflowService(FakeTasks(language="zh")).detect_language(
        context, source, target_language="zh-CN"
    )
    assert isinstance(known, LanguageResult)
    assert known.mode == "skipped"

    mixed_context = _context(tmp_path, "mixed-language")
    mixed = TranslationWorkflowService(
        FakeTasks(language="zh", classification="mixed")
    ).detect_language(mixed_context, source, target_language="zh-CN")
    assert isinstance(mixed, LanguageResult)
    assert mixed.mode == "enabled"


def test_outer_handler_round_trips_companion_rich_only_source(tmp_path):
    source = _source(tmp_path)
    assert source.rich is not None
    rich_only = TranslationSource(rich=source.rich)
    service = TranslationService(tmp_path / "rich-only-jobs")
    snapshot = service.prepare_language(LanguageRequest(rich_only, "fr"))
    snapshot = service.execute(snapshot.run_id, task_service=FakeTasks())
    assert snapshot.status is RunStatus.SUCCEEDED
    result = service.result(snapshot.run_id)
    assert isinstance(result, LanguageResult)
    assert result.document_digest == source.rich.document_digest


def test_standalone_steps_use_verified_cross_run_results(tmp_path):
    source = _source(tmp_path)
    service = TranslationService(tmp_path / "jobs")
    tasks = FakeTasks()
    language_snapshot = service.prepare_language(
        LanguageRequest(source, "fr")
    )
    language_snapshot = service.execute(
        language_snapshot.run_id, task_service=tasks
    )
    assert language_snapshot.status is RunStatus.SUCCEEDED

    keywords = FakeKeywords([_term("term-1", "Entropy")])
    glossary_snapshot = service.prepare_glossary(
        GlossaryRequest(
            source,
            "fr",
            50,
            service.result_source(language_snapshot.run_id),
        )
    )
    glossary_snapshot = service.execute(
        glossary_snapshot.run_id,
        task_service=tasks,
        keyword_provider=keywords,
    )
    assert glossary_snapshot.status is RunStatus.SUCCEEDED
    glossary = service.result(glossary_snapshot.run_id)
    assert isinstance(glossary, GlossaryResult)
    assert [item["term_id"] for item in glossary.entries] == ["term-1"]
    assert keywords.calls == 1

    blocks_snapshot = service.prepare_blocks(
        BlocksRequest(
            source,
            "fr",
            service.result_source(language_snapshot.run_id),
            service.result_source(glossary_snapshot.run_id),
        )
    )
    blocks_snapshot = service.execute(
        blocks_snapshot.run_id, task_service=tasks
    )
    assert blocks_snapshot.status is RunStatus.SUCCEEDED
    blocks = service.result(blocks_snapshot.run_id)
    assert isinstance(blocks, TranslationResult)
    assert blocks.coverage == "document"
    assert len(blocks.revision_artifacts) == len(source_blocks(source))
    revisions = [
        decode_fragment_revision(
            payload.decode("utf-8"),
            filename=Path(item.revision.path).name,
        )
        for item, payload in zip(
            blocks.revision_artifacts,
            service.revision_payloads(blocks_snapshot.run_id, blocks),
            strict=True,
        )
    ]
    assert [item.anchor.target_id for item in revisions] == [
        item["block_id"] for item in source_blocks(source)
    ]
    assert all(
        item.priority == 10
        and item.role == "translation"
        and item.anchor.related_block_ids == (item.anchor.target_id,)
        for item in revisions
    )
    manifest = blocks.to_document()
    assert manifest["schema_version"] == "arc.translate.translation_result.v1"
    assert set(manifest) == {
        "schema_version",
        "source_language",
        "target_language",
        "mode",
        "coverage",
        "layer",
        "revision_artifacts",
    }
    assert TranslationResult.from_document(manifest) == blocks


def test_missing_or_unverified_prerequisite_never_runs_keyword_step(tmp_path):
    source = _source(tmp_path)
    service = TranslationService(tmp_path / "jobs")
    keywords = FakeKeywords([_term("term-1", "Entropy")])
    missing = ArtifactSourceRef(
        "missing-run",
        "language/result",
        ArtifactDigest("sha256", "0" * 64, 1),
    )
    snapshot = service.prepare_glossary(
        GlossaryRequest(source, "fr", 50, missing)
    )
    snapshot = service.execute(
        snapshot.run_id,
        task_service=FakeTasks(),
        keyword_provider=keywords,
    )
    assert snapshot.status is RunStatus.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "prerequisite_not_verified"
    assert keywords.calls == 0


def test_glossary_windows_preserve_every_term_identity_and_order(tmp_path):
    source = _source(tmp_path)
    terms = [
        _term(f"term-{index}", f"Term {index}", sentence="x" * 1100)
        for index in range(3)
    ]
    tasks = FakeTasks()
    result = TranslationWorkflowService(
        tasks, FakeKeywords(terms)
    ).build_glossary(
        _context(tmp_path, "glossary-windows"),
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1.0,
            "fr",
            "enabled",
        ),
        target_language="fr",
        approx_count=3,
        term_input_budget_bytes=4096,
    )
    assert isinstance(result, GlossaryResult)
    assert [item["term_id"] for item in result.entries] == [
        "term-0",
        "term-1",
        "term-2",
    ]
    assert tasks.calls.count(GLOSSARY_PROMPT_VERSION) >= 2


def test_invalid_glossary_retries_once_then_pauses_with_editable_candidate(
    tmp_path,
):
    source = _source(tmp_path)
    term = _term("term-1", "Entropy")
    context = _context(tmp_path, "editable-glossary")
    tasks = InvalidGlossaryTasks()
    workflow = TranslationWorkflowService(tasks, FakeKeywords([term]))
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1.0,
        "fr",
        "enabled",
    )

    paused = workflow.build_glossary(
        context,
        source,
        language=language,
        target_language="fr",
        approx_count=1,
    )

    assert isinstance(paused, Paused)
    assert paused.awaiting.details["automatic_retry_exhausted"] is True
    assert paused.awaiting.details["output_attempts"] == 2
    candidate_path = Path(str(paused.awaiting.details["candidate_path"]))
    assert candidate_path.is_file()
    assert tasks.calls == 2
    candidate_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "term_id": "term-1",
                        "preferred_translation": "entropie",
                        "target_definition": "Une grandeur thermodynamique.",
                    }
                ]
            }
        )
    )

    recovered = workflow.build_glossary(
        context,
        source,
        language=language,
        target_language="fr",
        approx_count=1,
    )

    assert isinstance(recovered, GlossaryResult)
    assert tasks.calls == 2
    assert recovered.entries[0]["term"] == term["term"]
    assert recovered.entries[0]["matched_sentences"] == term["matched_sentences"]


def test_invalid_language_output_gets_one_fresh_retry(tmp_path):
    source = _source(tmp_path)
    tasks = InvalidOnceTasks(LANGUAGE_PROMPT_VERSION)
    context = _context(tmp_path, "language-retry")

    result = TranslationWorkflowService(tasks).detect_language(
        context,
        source,
        target_language="fr",
    )

    assert isinstance(result, LanguageResult)
    assert tasks.calls == [
        LANGUAGE_PROMPT_VERSION,
        LANGUAGE_PROMPT_VERSION,
    ]
    assert context.working.find_candidate(
        "language/language/result.json"
    ) is not None
    assert len(set(tasks.task_ids)) == 2


def test_language_second_invalid_output_pauses_and_resumes_without_third_call(
    tmp_path,
):
    source = _source(tmp_path)
    service = TranslationService(tmp_path / "language-recovery-jobs")
    tasks = FakeTasks(language=" ")
    snapshot = service.prepare_language(
        LanguageRequest(source, "fr"),
        run_id="language-output-recovery",
    )

    paused = service.execute(snapshot.run_id, task_service=tasks)

    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    assert paused.awaiting.input_required is False
    assert paused.awaiting.details["output_attempts"] == 2
    candidate_path = Path(
        str(paused.awaiting.details["candidate_path"])
    )
    candidate_path.write_text(
        json.dumps(
            {
                "language_tag": "en",
                "classification": "known",
                "confidence": 0.9,
            }
        ),
        encoding="utf-8",
    )

    resumed = service.resume(snapshot.run_id, task_service=tasks)

    assert resumed.status is RunStatus.SUCCEEDED
    assert tasks.calls.count(LANGUAGE_PROMPT_VERSION) == 2
    assert service.result(snapshot.run_id).language_tag == "en"


def test_invalid_translation_draft_gets_one_fresh_retry(tmp_path):
    source = _source(tmp_path)
    tasks = InvalidOnceTasks(TRANSLATION_PROMPT_VERSION)

    result = TranslationWorkflowService(tasks).translate_blocks(
        _context(tmp_path, "draft-retry"),
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "fr",
            "enabled",
        ),
        glossary=GlossaryResult(
            source.document_digest,
            source.source_digest,
            "fr",
            1,
            "d" * 64,
            (),
        ),
        target_language="fr",
    )

    assert isinstance(result, TranslationResult)
    assert result.coverage == "document"
    assert tasks.calls.count(TRANSLATION_PROMPT_VERSION) == 2


def test_output_supervision_request_tracks_current_error(tmp_path):
    context = _context(tmp_path, "output-supervision-identity")
    candidate = tmp_path / "candidate.json"
    first = _output_supervision(
        context,
        artifact_prefix="translation",
        stage="draft-0001",
        error=TranslationWorkflowError("invalid", "first error"),
        candidate_path=candidate,
    )
    second = _output_supervision(
        context,
        artifact_prefix="translation",
        stage="draft-0001",
        error=TranslationWorkflowError("invalid", "second error"),
        candidate_path=candidate,
    )

    assert first.awaiting.response_contract is None
    assert first.awaiting.resume_key != second.awaiting.resume_key
    assert first.awaiting.request_ref != second.awaiting.request_ref
    assert second.awaiting.request_ref is not None
    request = json.loads(
        context.artifacts.read_bytes(second.awaiting.request_ref)
    )
    assert request["schema_version"] == OUTPUT_SUPERVISION_SCHEMA
    assert request["message"] == "second error"


def test_changed_translation_gets_a_distinct_fragment_identity(tmp_path) -> None:
    source = _source(tmp_path)
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1,
        "fr",
        "enabled",
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "fr",
        1,
        "d" * 64,
        (),
    )
    first = TranslationWorkflowService(
        FakeTasks(translation_prefix="first:")
    ).translate_blocks(
        _context(tmp_path, "first-generation"),
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )
    second = TranslationWorkflowService(
        FakeTasks(translation_prefix="second:")
    ).translate_blocks(
        _context(tmp_path, "second-generation"),
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )

    assert isinstance(first, TranslationResult)
    assert isinstance(second, TranslationResult)
    assert first.layer.initial_revisions[0].fragment_id != (
        second.layer.initial_revisions[0].fragment_id
    )


def test_block_selector_normalizes_order_and_filters_window_glossary(tmp_path):
    source = _source(tmp_path)
    blocks = source_blocks(source)
    entropy_block = next(
        item for item in blocks if "Entropy" in block_text(item)
    )
    tensor_block = next(
        item for item in blocks if "tensor" in block_text(item)
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "fr",
        2,
        "a" * 64,
        (
            {
                **_term("entropy", "Entropy"),
                "preferred_translation": "entropie",
                "target_definition": "definition entropy",
            },
            {
                **_term("tensor", "tensor"),
                "preferred_translation": "tenseur",
                "target_definition": "definition tensor",
            },
        ),
    )
    tasks = FakeTasks()
    result = TranslationWorkflowService(tasks).translate_blocks(
        _context(tmp_path, "selected-block"),
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "fr",
            "enabled",
        ),
        glossary=glossary,
        target_language="fr",
        block_ids=[entropy_block["block_id"]],
    )
    assert isinstance(result, TranslationResult)
    assert result.coverage == "selection"
    assert len(result.revision_artifacts) == 1
    assert tasks.translation_glossaries == [["Entropy"]]
    assert tasks.prompt_glossary_fields == [
        [
            {
                "term_id",
                "term",
                "aliases",
                "preferred_translation",
                "target_definition",
            }
        ],
        [
            {
                "term_id",
                "term",
                "aliases",
                "preferred_translation",
                "target_definition",
            }
        ],
    ]

    invalid = TranslationWorkflowService(FakeTasks()).translate_blocks(
        _context(tmp_path, "invalid-selector"),
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "fr",
            "enabled",
        ),
        glossary=glossary,
        target_language="fr",
        block_ids=[tensor_block["block_id"], tensor_block["block_id"]],
    )
    assert isinstance(invalid, RunError)
    assert invalid.code == "block_selector_invalid"


def test_structural_figures_bypass_models_and_keep_ordered_coverage(tmp_path):
    assets = tmp_path / "images"
    assets.mkdir()
    (assets / "structural.png").write_bytes(b"\x89PNG structural")
    (assets / "captioned.png").write_bytes(b"\x89PNG captioned")
    (assets / "alt.png").write_bytes(b"\x89PNG alt")
    markdown = tmp_path / "figures.md"
    markdown.write_text(
        "# Figures\n\n"
        "![](images/structural.png)\n\n"
        "<details>\n<summary>natural_image</summary>\n\n"
        "Extractor-only sidecar text.\n</details>\n\n"
        "![private alt](images/captioned.png \"Visible scientific caption\")\n\n"
        "![Accessibility language](images/alt.png)\n\n"
        "The surrounding prose remains translatable.\n",
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "figure-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(artifact)
    )
    blocks = source_blocks(source)
    structural = next(
        item
        for item in blocks
        if item["kind"] == "figure"
        and not str(item["payload"]["caption"]).strip()
    )
    captioned = next(
        item
        for item in blocks
        if item["kind"] == "figure"
        and str(item["payload"]["caption"]).strip()
    )
    alt_only = next(
        item
        for item in blocks
        if item["kind"] == "figure"
        and str(item["payload"]["alt_text"]).strip()
        == "Accessibility language"
    )
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1,
        "fr",
        "enabled",
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "fr",
        1,
        "e" * 64,
        (),
    )
    tasks = FakeTasks()

    context = _context(tmp_path, "figures")
    result = TranslationWorkflowService(tasks).translate_blocks(
        context,
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )

    assert isinstance(result, TranslationResult)
    revision_block_ids = [
        decode_fragment_revision(
            context.artifacts.read_bytes(item.artifact).decode("utf-8"),
            filename=Path(item.revision.path).name,
        ).anchor.target_id
        for item in result.revision_artifacts
    ]
    assert structural["block_id"] not in revision_block_ids
    assert captioned["block_id"] in revision_block_ids
    assert alt_only["block_id"] in revision_block_ids
    prompted = [
        item for window in tasks.translation_blocks for item in window
    ]
    reviewed = [item for window in tasks.review_blocks for item in window]
    assert structural["block_id"] not in {
        item["block_id"] for item in [*prompted, *reviewed]
    }
    prompted_caption = next(
        item for item in prompted if item["block_id"] == captioned["block_id"]
    )
    assert prompted_caption["payload"] == {
        "caption": "Visible scientific caption",
        "alt_text": "private alt",
    }
    prompted_alt = next(
        item for item in prompted if item["block_id"] == alt_only["block_id"]
    )
    assert prompted_alt["payload"] == {
        "caption": "",
        "alt_text": "Accessibility language",
    }
    assert set(prompted_caption) == {
        "block_id",
        "ordinal",
        "kind",
        "section_path",
        "payload",
        "source_identity",
    }
    figure_prompts = json.dumps(
        [
            item
            for item in [*prompted, *reviewed]
            if item["kind"] == "figure"
        ],
        ensure_ascii=False,
    )
    for private_value in (
            "images/structural.png",
            "images/captioned.png",
            "Extractor-only sidecar text",
            "asset_digest",
            "asset_target",
            "logical_name",
        '"target"',
    ):
        assert private_value not in figure_prompts

    structural_only_tasks = FakeTasks()
    structural_only = TranslationWorkflowService(
        structural_only_tasks
    ).translate_blocks(
        _context(tmp_path, "structural-only"),
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
        block_ids=[structural["block_id"]],
    )
    assert isinstance(structural_only, TranslationResult)
    assert structural_only.coverage == "selection"
    assert structural_only.revision_artifacts == ()
    assert structural_only_tasks.calls == []


def test_failed_review_can_accept_validated_pre_review_translation(tmp_path):
    source = _source(tmp_path)
    first_context = _context(tmp_path, "review-supervision")
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1,
        "fr",
        "enabled",
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "fr",
        1,
        "b" * 64,
        (),
    )
    tasks = FakeTasks(invalid_review=True)
    workflow = TranslationWorkflowService(tasks)
    first = workflow.translate_blocks(
        first_context,
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )
    assert isinstance(first, Paused)
    assert first.awaiting.response_contract == REVIEW_SUPERVISION_SCHEMA
    assert tasks.calls.count(REVIEW_PROMPT_VERSION) == 2

    resumed_context = RunContext(
        first_context.repository,
        first_context.repository.inspect("review-supervision").snapshot,
        resume_input={
            "schema_version": REVIEW_SUPERVISION_SCHEMA,
            "resume_key": first.awaiting.resume_key,
            "action": "accept_pre_review",
        },
    )
    resumed = workflow.translate_blocks(
        resumed_context,
        source,
        language=language,
        glossary=glossary,
        target_language="fr",
    )
    assert isinstance(resumed, TranslationResult)
    assert len(resumed.revision_artifacts) == len(source_blocks(source))
    assert tasks.calls.count(REVIEW_PROMPT_VERSION) == 2


def test_oversized_single_block_review_requests_supervision(tmp_path):
    markdown = tmp_path / "long.md"
    markdown.write_text("# Long\n\n" + ("source prose " * 100), encoding="utf-8")
    paper = ArcPaperService(cache_root=tmp_path / "long-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(artifact)
    )
    context = _context(tmp_path, "review-budget")
    workflow = TranslationWorkflowService(FakeTasks())
    result = workflow.translate_blocks(
        context,
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "fr",
            "enabled",
        ),
        glossary=GlossaryResult(
            source.document_digest,
            source.source_digest,
            "fr",
            1,
            "c" * 64,
            (),
        ),
        target_language="fr",
        input_budget_bytes=4500,
    )
    assert isinstance(result, Paused)
    assert result.awaiting.response_contract == REVIEW_SUPERVISION_SCHEMA

    resumed_context = RunContext(
        context.repository,
        context.repository.inspect("review-budget").snapshot,
        resume_input={
            "schema_version": REVIEW_SUPERVISION_SCHEMA,
            "resume_key": result.awaiting.resume_key,
            "action": "accept_pre_review",
        },
    )
    resumed = workflow.translate_blocks(
        resumed_context,
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "fr",
            "enabled",
        ),
        glossary=GlossaryResult(
            source.document_digest,
            source.source_digest,
            "fr",
            1,
            "c" * 64,
            (),
        ),
        target_language="fr",
        input_budget_bytes=4500,
    )
    assert isinstance(resumed, TranslationResult)


def test_oversized_review_block_does_not_skip_neighbor_reviews(tmp_path):
    markdown = tmp_path / "mixed-review.md"
    markdown.write_text(
        "small before\n\nmiddle expands\n\nsmall after",
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "mixed-review-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(artifact)
    )
    tasks = FakeTasks(
        translation_prefix_by_text={"middle expands": "译" * 2_000}
    )
    workflow = TranslationWorkflowService(tasks)
    context = _context(tmp_path, "mixed-review")
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1,
        "zh-CN",
        "enabled",
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "zh-CN",
        1,
        "d" * 64,
        (),
    )

    paused = workflow.translate_blocks(
        context,
        source,
        language=language,
        glossary=glossary,
        target_language="zh-CN",
        input_budget_bytes=4_800,
    )
    assert isinstance(paused, Paused)
    assert len(tasks.review_blocks) == 1
    assert all(
        "middle expands" not in block_text(block)
        for block in tasks.review_blocks[0]
    )

    resumed_context = RunContext(
        context.repository,
        context.repository.inspect("mixed-review").snapshot,
        resume_input={
            "schema_version": REVIEW_SUPERVISION_SCHEMA,
            "resume_key": paused.awaiting.resume_key,
            "action": "accept_pre_review",
        },
    )
    resumed = workflow.translate_blocks(
        resumed_context,
        source,
        language=language,
        glossary=glossary,
        target_language="zh-CN",
        input_budget_bytes=4_800,
    )

    assert isinstance(resumed, TranslationResult)
    assert len(tasks.review_blocks) == 2
    assert all(
        "middle expands" not in block_text(block)
        for blocks in tasks.review_blocks
        for block in blocks
    )
    assert max(
        size
        for contract, size in tasks.prompt_sizes
        if contract == REVIEW_PROMPT_VERSION
    ) <= 4_800


def test_translation_windows_reserve_space_for_review(tmp_path):
    markdown = tmp_path / "review-windows.md"
    markdown.write_text(
        "# Long\n\n" + "\n\n".join(["source prose " * 40] * 6),
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "review-windows-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(artifact)
    )
    tasks = FakeTasks()
    result = TranslationWorkflowService(tasks).translate_blocks(
        _context(tmp_path, "review-windows"),
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "fr",
            "enabled",
        ),
        glossary=GlossaryResult(
            source.document_digest,
            source.source_digest,
            "fr",
            1,
            "d" * 64,
            (),
        ),
        target_language="fr",
        input_budget_bytes=4_800,
    )

    assert isinstance(result, TranslationResult)
    assert tasks.calls.count(TRANSLATION_PROMPT_VERSION) == 4
    assert tasks.calls.count(REVIEW_PROMPT_VERSION) == 4


def test_actual_translation_expansion_splits_review_windows(tmp_path):
    markdown = tmp_path / "expanded-review.md"
    markdown.write_text(
        "# Long\n\n" + "\n\n".join(["source prose " * 8] * 6),
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "expanded-review-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(artifact)
    )
    tasks = FakeTasks(translation_prefix="译" * 600)
    result = TranslationWorkflowService(tasks).translate_blocks(
        _context(tmp_path, "expanded-review"),
        source,
        language=LanguageResult(
            source.document_digest,
            source.source_digest,
            "en",
            "known",
            1,
            "zh-CN",
            "enabled",
        ),
        glossary=GlossaryResult(
            source.document_digest,
            source.source_digest,
            "zh-CN",
            1,
            "e" * 64,
            (),
        ),
        target_language="zh-CN",
        input_budget_bytes=4_800,
    )

    assert isinstance(result, TranslationResult)
    translation_count = tasks.calls.count(TRANSLATION_PROMPT_VERSION)
    review_count = tasks.calls.count(REVIEW_PROMPT_VERSION)
    assert review_count > translation_count
    assert max(
        size
        for contract, size in tasks.prompt_sizes
        if contract == REVIEW_PROMPT_VERSION
    ) <= 4_800


def test_split_review_supervision_progresses_across_subwindows(tmp_path):
    markdown = tmp_path / "split-review-supervision.md"
    markdown.write_text(
        "# Long\n\n" + "\n\n".join(["source prose " * 8] * 4),
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "split-supervision-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        RichDocumentParserService(paper.repository).parse_source(artifact)
    )
    language = LanguageResult(
        source.document_digest,
        source.source_digest,
        "en",
        "known",
        1,
        "zh-CN",
        "enabled",
    )
    glossary = GlossaryResult(
        source.document_digest,
        source.source_digest,
        "zh-CN",
        1,
        "f" * 64,
        (),
    )
    tasks = FakeTasks(
        invalid_review=True,
        translation_prefix="译" * 600,
    )
    workflow = TranslationWorkflowService(tasks)
    context = _context(tmp_path, "split-review-supervision")
    resume_keys: list[str] = []

    for _ in range(10):
        result = workflow.translate_blocks(
            context,
            source,
            language=language,
            glossary=glossary,
            target_language="zh-CN",
            input_budget_bytes=4_800,
        )
        if not isinstance(result, Paused):
            break
        resume_keys.append(result.awaiting.resume_key)
        context = RunContext(
            context.repository,
            context.repository.inspect("split-review-supervision").snapshot,
            resume_input={
                "schema_version": REVIEW_SUPERVISION_SCHEMA,
                "resume_key": result.awaiting.resume_key,
                "action": "accept_pre_review",
            },
        )

    assert isinstance(result, TranslationResult)
    assert len(set(resume_keys)) > 1


def test_non_rich_pdf_source_is_rejected(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-fake")
    paper = ArcPaperService(cache_root=tmp_path / "pdf-cache")

    try:
        resolve_translation_source(paper, path)
    except TranslationSourceError as exc:
        assert exc.code == "rich_source_required"
    else:  # pragma: no cover
        raise AssertionError("non-rich PDF source was accepted")
