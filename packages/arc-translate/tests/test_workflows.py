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
from arc_paper import ParsedDocument, SourceArtifact, SourceFormat, SourceOrigin, SourceOriginKind

from arc_translate import (
    BlocksRequest,
    BlocksResult,
    GenerationRecipe,
    GlossaryRequest,
    GlossaryResult,
    LanguageRequest,
    LanguageResult,
    TranslationService,
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
from arc_translate.source import TranslationSourceError, resolve_translation_source
from arc_translate.workflow import REVIEW_SUPERVISION_SCHEMA


class FakeTasks:
    def __init__(
        self,
        *,
        language: str = "en",
        classification: str = "known",
        invalid_review: bool = False,
    ) -> None:
        self.language = language
        self.classification = classification
        self.invalid_review = invalid_review
        self.calls: list[str] = []
        self.translation_glossaries: list[list[str]] = []
        self.prompt_glossary_fields: list[list[set[str]]] = []

    def execute_or_resume(
        self, _context, request, *, input=None, options=None
    ):
        contract, payload = _prompt(request.prompt)
        self.calls.append(contract)
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
            self.translation_glossaries.append(
                [item["term"] for item in payload["glossary"]]
            )
            self.prompt_glossary_fields.append(
                [set(item) for item in payload["glossary"]]
            )
            translations = []
            for block in payload["blocks"]:
                identity = block["source_identity"]
                text = (
                    identity["code_text"]
                    if identity["code_text"] is not None
                    else f"translated:{block_text(block)}"
                )
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
    parsed = paper.parser.parse_source(artifact)
    rich = RichDocumentParserService(paper.repository).parse_source(artifact)
    return TranslationSource(parsed=parsed, rich=rich)


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
    assert isinstance(blocks, BlocksResult)
    assert [item["block_id"] for item in blocks.translations] == [
        item["block_id"] for item in source_blocks(source)
    ]


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


def test_invalid_glossary_candidate_is_editable_and_reused_without_provider(
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

    failed = workflow.build_glossary(
        context,
        source,
        language=language,
        target_language="fr",
        approx_count=1,
    )

    assert isinstance(failed, RunError)
    candidate_path = Path(str(failed.details["candidate_path"]))
    assert candidate_path.is_file()
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
    assert tasks.calls == 1
    assert recovered.entries[0]["term"] == term["term"]
    assert recovered.entries[0]["matched_sentences"] == term["matched_sentences"]


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
    assert isinstance(result, BlocksResult)
    assert [item["block_id"] for item in result.translations] == [
        entropy_block["block_id"]
    ]
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
    assert isinstance(resumed, BlocksResult)
    assert len(resumed.translations) == len(source_blocks(source))


def test_review_prompt_obeys_the_same_complete_input_budget(tmp_path):
    markdown = tmp_path / "long.md"
    markdown.write_text("# Long\n\n" + ("source prose " * 100), encoding="utf-8")
    paper = ArcPaperService(cache_root=tmp_path / "long-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        parsed=paper.parser.parse_source(artifact),
        rich=RichDocumentParserService(paper.repository).parse_source(artifact),
    )
    result = TranslationWorkflowService(FakeTasks()).translate_blocks(
        _context(tmp_path, "review-budget"),
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
        input_budget_bytes=4096,
    )
    assert isinstance(result, RunError)
    assert result.code == "translation_review_exceeds_input_budget"


def test_translation_windows_reserve_space_for_review(tmp_path):
    markdown = tmp_path / "review-windows.md"
    markdown.write_text(
        "# Long\n\n" + "\n\n".join(["source prose " * 40] * 6),
        encoding="utf-8",
    )
    paper = ArcPaperService(cache_root=tmp_path / "review-windows-cache")
    artifact = paper.import_source(markdown)
    source = TranslationSource(
        parsed=paper.parser.parse_source(artifact),
        rich=RichDocumentParserService(paper.repository).parse_source(artifact),
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

    assert isinstance(result, BlocksResult)
    assert tasks.calls.count(TRANSLATION_PROMPT_VERSION) == 4
    assert tasks.calls.count(REVIEW_PROMPT_VERSION) == 4


def test_pdf_without_text_layer_is_a_typed_source_failure(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-fake")
    artifact = SourceArtifact(
        SourceFormat.PDF,
        hashlib.sha256(b"%PDF-fake").hexdigest(),
        len(b"%PDF-fake"),
        "application/pdf",
        SourceOrigin(SourceOriginKind.LOCAL_IMPORT, locator=str(path)),
    )
    parsed = ParsedDocument(
        source=artifact,
        metadata={"text_layer": False},
    )

    class Parser:
        def parse_source(self, _artifact):
            return parsed

    class Paper:
        parser = Parser()

        def import_source(self, _path):
            return artifact

    try:
        resolve_translation_source(Paper(), path)  # type: ignore[arg-type]
    except TranslationSourceError as exc:
        assert exc.code == "pdf_text_layer_missing"
    else:  # pragma: no cover
        raise AssertionError("missing PDF text layer was accepted")
