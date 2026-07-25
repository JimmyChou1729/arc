from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from arc_jobs import RunStatus
from arc_llm import LLMCompleted, ModelSelection

from arc_paper import (
    ArcPaperService,
    KeywordExtractionService,
    KeywordExtractionRunner,
    KeywordTextUnit,
    ParsedDocument,
    ParsedSection,
    RichBlock,
    RichBlockKind,
    RichDocument,
    RichSection,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceLocator,
    TermCandidate,
    TermInventoryLineage,
    TermInventoryStore,
    TermInventoryStoreError,
    build_keyword_terms,
    keyword_result_from_document,
    parse_rich_artifact_bytes,
    validate_approx_count,
)
from arc_paper.cli import main
from arc_paper.parse.parser import parse_artifact_bytes
from arc_paper.workflows.keywords import (
    KEYWORD_CHAPTER_PROMPT_CONTRACT,
    KeywordExtractionError,
    _chapter_allocations,
    _llm_resume_input,
    _lineage as workflow_lineage,
)


def test_keyword_service_has_no_deprecated_extract_alias() -> None:
    assert not hasattr(KeywordExtractionService, "extract")


class FakeKeywordTasks:
    def __init__(self, *, unusable: bool = False) -> None:
        self.unusable = unusable
        self.requests = []

    def execute_or_resume(self, context, request, *, input=None, options=None):
        self.requests.append(request)
        if request.task_id.startswith("keyword-review-"):
            entries = json.loads(request.prompt.split("Entries:\n", 1)[1])
            if self.unusable:
                value = {
                    "status": "unusable",
                    "reason": "not a term list",
                    "entries": [],
                    "discarded_source_entry_ids": [
                        item["source_entry_id"] for item in entries
                    ],
                }
            else:
                value = {
                    "status": "usable",
                    "reason": "",
                    "entries": [
                        {
                            "term": item["text"],
                            "source_entry_ids": [item["source_entry_id"]],
                        }
                        for item in entries
                    ],
                    "discarded_source_entry_ids": [],
                }
        else:
            section_id = _prompt_value(request.prompt, "Section ID")
            value = {
                "entries": [
                    {"term": f"chapter concept {section_id}"},
                    {"term": "shared concept"},
                ]
            }
        return LLMCompleted(value, "fake", "fake-model", None, None)


def _prompt_value(prompt: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}: (.+)$", prompt, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _parsed(
    seed: str = "paper",
    *,
    explicit_entries: tuple[str, ...] = (),
    repeated_sentences: int = 1,
) -> ParsedDocument:
    payload = seed.encode()
    source = SourceArtifact(
        SourceFormat.MARKDOWN,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "text/markdown",
        SourceOrigin(SourceOriginKind.REPOSITORY, locator=seed),
    )
    keyword_section = ParsedSection(
        "keywords",
        "Keywords",
        1,
        "\n".join(explicit_entries),
        0,
    )
    repeated = " ".join(
        "Shared concept appears in the body." for _ in range(repeated_sentences)
    )
    body = ParsedSection(
        "body",
        "Body",
        1,
        (
            f"First chapter concept body appears. {repeated} "
            "A distinct chapter concept body closes the text."
        ),
        1,
    )
    metadata = (
        {
            "explicit_term_fields": [
                {
                    "kind": "keywords",
                    "label": "Keywords",
                    "entries": list(explicit_entries),
                }
            ]
        }
        if explicit_entries
        else {}
    )
    return ParsedDocument(
        source=source,
        sections=(keyword_section, body),
        metadata=metadata,
    )


def _lineage(document: ParsedDocument, *, model: str = "fake") -> TermInventoryLineage:
    return TermInventoryLineage(
        document.document_digest,
        document.source.artifact_digest,
        document.source.source_format.value,
        document.source.media_type,
        document.source.size,
        document.schema_version,
        "discovery.v1",
        "review.v1",
        "normalize.v1",
        "count.v1",
        {"provider": "fake", "model": model, "tier": "medium"},
    )


@pytest.mark.parametrize("value", [0, 201, True, 1.5, "50"])
def test_approx_count_is_closed(value: object) -> None:
    with pytest.raises(ValueError):
        validate_approx_count(value)


def _allocation_counts(
    lengths: tuple[int, ...], requested_total: int
) -> tuple[int, ...]:
    sections = tuple(
        KeywordTextUnit(str(index), f"Chapter {index}", "x" * length)
        for index, length in enumerate(lengths)
    )
    return tuple(
        count
        for _, count in _chapter_allocations(sections, requested_total)
    )


def test_chapter_quotas_weight_unequal_lengths() -> None:
    assert _allocation_counts((1, 2, 3), 7) == (2, 2, 3)


def test_chapter_quotas_honor_minimum_one_for_tiny_budget() -> None:
    assert _allocation_counts((1, 2, 3), 1) == (1, 1, 1)


def test_chapter_quota_equal_remainder_ties_prefer_source_order() -> None:
    assert _allocation_counts((10, 10, 10), 5) == (2, 2, 1)


def test_chapter_quota_total_and_order_are_exact_and_deterministic() -> None:
    first = _chapter_allocations(
        (
            KeywordTextUnit("empty", "Empty", ""),
            KeywordTextUnit("whitespace", "Whitespace", " \n"),
            KeywordTextUnit("a", "A", "x" * 3),
            KeywordTextUnit("b", "B", "x" * 5),
            KeywordTextUnit("c", "C", "x" * 7),
        ),
        17,
    )
    second = _chapter_allocations(
        (
            KeywordTextUnit("empty", "Empty", ""),
            KeywordTextUnit("whitespace", "Whitespace", " \n"),
            KeywordTextUnit("a", "A", "x" * 3),
            KeywordTextUnit("b", "B", "x" * 5),
            KeywordTextUnit("c", "C", "x" * 7),
        ),
        17,
    )
    assert tuple(section.section_id for section, _ in first) == ("a", "b", "c")
    assert tuple(count for _, count in first) == (4, 6, 7)
    assert sum(count for _, count in first) == 17
    assert first == second


def test_weighted_chapter_recipe_uses_distinct_cache_lineage() -> None:
    document = _parsed()
    lineage = workflow_lineage(
        document, ModelSelection(provider="fake", model="fake-model")
    )
    old_lineage = replace(
        lineage,
        discovery_contract="arc.paper.keyword_chapter_prompt.v1",
    )
    assert (
        KEYWORD_CHAPTER_PROMPT_CONTRACT
        == "arc.paper.keyword_chapter_prompt.v2"
    )
    assert lineage.key != old_lineage.key


def test_explicit_windows_are_auditable_and_result_is_frequency_projection(
    tmp_path: Path,
) -> None:
    entries = tuple(f"Explicit term {index}" for index in range(81))
    document = _parsed(explicit_entries=entries, repeated_sentences=12)
    fake = FakeKeywordTasks()
    runner = KeywordExtractionRunner(
        tmp_path / "jobs",
        store=TermInventoryStore(tmp_path / "cache"),
        task_service=fake,
    )

    snapshot = runner.execute(document, approx_count=2)
    result = runner.read_result(snapshot)

    review_requests = [
        item for item in fake.requests if item.task_id.startswith("keyword-review-")
    ]
    assert [len(json.loads(item.prompt.split("Entries:\n", 1)[1])) for item in review_requests] == [
        80,
        1,
    ]
    assert result.planned_count == 3
    assert result.returned_count == 3
    assert len(result.terms) == 3
    assert all(
        source_ref.startswith("explicit:field-")
        for term in result.terms
        for source_ref in term.source_refs
    )
    assert all(len(term.matched_sentences) <= 10 for term in result.terms)
    assert keyword_result_from_document(result.to_document()) == result
    batches = list(
        (tmp_path / "cache" / "term-inventory" / "v1" / "lineages").glob(
            "*/*/batches/*/*.json"
        )
    )
    assert len(batches) == 1
    cached = json.loads(batches[0].read_text(encoding="utf-8"))
    cached_explicit_refs = {
        source_ref
        for term in cached["terms"]
        for source_ref in term["source_refs"]
        if source_ref.startswith("explicit:")
    }
    assert len(cached_explicit_refs) == 81


def test_small_reuse_and_larger_growth_use_high_water_and_existing_terms(
    tmp_path: Path,
) -> None:
    document = _parsed()
    fake = FakeKeywordTasks()
    runner = KeywordExtractionRunner(
        tmp_path / "jobs",
        store=TermInventoryStore(tmp_path / "cache"),
        task_service=fake,
    )
    model = ModelSelection(provider="fake")

    first = runner.execute(document, approx_count=2, model=model)
    assert first.status is RunStatus.SUCCEEDED
    first_calls = len(fake.requests)
    smaller = runner.execute(document, approx_count=1, model=model)
    assert smaller.status is RunStatus.SUCCEEDED
    assert len(fake.requests) == first_calls

    larger = runner.execute(document, approx_count=10, model=model)
    assert larger.status is RunStatus.SUCCEEDED
    growth_prompts = [
        item.prompt
        for item in fake.requests[first_calls:]
        if item.task_id.startswith("keyword-chapter-")
    ]
    assert growth_prompts
    assert "Existing normalized inventory:" in growth_prompts[0]
    assert "shared concept" in growth_prompts[0]


def test_unusable_explicit_list_pauses_without_cache_then_discards_or_aborts(
    tmp_path: Path,
) -> None:
    document = _parsed(explicit_entries=("bad index",))
    cache = TermInventoryStore(tmp_path / "cache")
    fake = FakeKeywordTasks(unusable=True)
    runner = KeywordExtractionRunner(
        tmp_path / "jobs", store=cache, task_service=fake
    )

    paused = runner.execute(document, approx_count=2, run_id="discard-case")
    assert paused.status is RunStatus.PAUSED
    assert cache.admin_entries() == ()
    assert paused.awaiting is not None
    resumed = runner.execute(
        document,
        approx_count=2,
        run_id="discard-case",
        resume_input={
            "resume_key": paused.awaiting.resume_key,
            "action": "discard_index_and_continue",
        },
    )
    assert resumed.status is RunStatus.SUCCEEDED
    assert cache.admin_entries()

    other_cache = TermInventoryStore(tmp_path / "other-cache")
    abort_runner = KeywordExtractionRunner(
        tmp_path / "other-jobs",
        store=other_cache,
        task_service=FakeKeywordTasks(unusable=True),
    )
    paused = abort_runner.execute(document, approx_count=2, run_id="abort-case")
    assert paused.awaiting is not None
    aborted = abort_runner.execute(
        document,
        approx_count=2,
        run_id="abort-case",
        resume_input={
            "resume_key": paused.awaiting.resume_key,
            "action": "abort",
        },
    )
    assert aborted.status is RunStatus.FAILED
    assert aborted.error is not None
    assert aborted.error.code == "explicit_term_list_unusable"
    assert other_cache.admin_entries() == ()


def test_keyword_llm_resume_ignores_foreign_parent_response() -> None:
    assert _llm_resume_input(
        {
            "schema_version": "arc.companion.evidence_response.v1",
            "resume_key": "evidence-example",
            "responses": [],
        }
    ) is None


def test_keyword_llm_resume_rejects_malformed_claimed_arc_llm_input() -> None:
    with pytest.raises(KeywordExtractionError) as exc_info:
        _llm_resume_input(
            {
                "schema_version": "arc.llm.resume_input.v2",
                "resume_key": "missing-action",
            }
        )
    assert exc_info.value.code == "keyword_resume_input_invalid"


def test_current_rebuilds_from_batch_but_batch_damage_is_hard(
    tmp_path: Path,
) -> None:
    document = _parsed()
    store = TermInventoryStore(tmp_path)
    lineage = _lineage(document)
    stored = store.merge(
        document,
        lineage,
        high_water=3,
        explicit_disposition="absent",
        candidates=(TermCandidate("shared concept", source_refs=("section:body",)),),
    )
    current_path = (
        tmp_path
        / "term-inventory"
        / "v1"
        / "lineages"
        / lineage.key[:2]
        / lineage.key
        / "current.json"
    )
    current_path.write_bytes(b"broken")
    rebuilt, warnings = store.load(document, lineage)
    assert rebuilt == stored
    assert warnings

    current = json.loads(current_path.read_text(encoding="utf-8"))
    digest = current["current_batch_digest"]
    batch_path = current_path.parent / "batches" / digest[:2] / f"{digest}.json"
    batch_path.write_bytes(b"broken")
    with pytest.raises(TermInventoryStoreError) as exc_info:
        store.load(document, lineage)
    assert exc_info.value.code == "term_inventory_batch_corrupt"


def test_cache_admin_lists_removes_inventory_and_update_never_calls_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _parsed()
    paper = ArcPaperService(cache_root=tmp_path)
    paper.term_inventory_store.merge(
        document,
        _lineage(document),
        high_water=3,
        explicit_disposition="absent",
        candidates=(TermCandidate("shared concept"),),
    )
    entry = next(
        item
        for item in paper.list_cache().entries
        if item.components[0].name == "term-inventory"
    )
    assert entry.updateable is False
    assert paper.update_cache(entry_ids=(entry.entry_id,)).records[0].status == "skipped"
    removed = paper.remove_cache(entry_ids=(entry.entry_id,), dry_run=False)
    assert removed.removed_entry_ids == (entry.entry_id,)
    assert paper.term_inventory_store.admin_entries() == ()


def test_inventory_merge_is_atomic_and_rich_lists_use_visible_text(
    tmp_path: Path,
) -> None:
    payload = b"rich"
    source = SourceArtifact(
        SourceFormat.MARKDOWN,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "text/markdown",
        SourceOrigin(SourceOriginKind.REPOSITORY, locator="rich"),
    )
    section = RichSection("sec", "Chapter", 1, 0, ("sec",), 0, 1)
    block = RichBlock(
        "block",
        0,
        RichBlockKind.LIST,
        ("sec",),
        SourceLocator(SourceFormat.MARKDOWN, line_start=1, line_end=1),
        {
            "ordered": False,
            "items": [
                {
                    "text": "Visible concept appears.",
                    "inline_spans": [
                        {
                            "kind": "text",
                            "start": 0,
                            "end": 24,
                            "text": "Visible concept appears.",
                        }
                    ],
                }
            ],
        },
    )
    document = RichDocument(source, (block,), (section,))
    store = TermInventoryStore(tmp_path)
    lineage = _lineage_for_rich(document)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                store.merge,
                document,
                lineage,
                high_water=high_water,
                explicit_disposition="absent",
                candidates=(TermCandidate(term),),
            )
            for high_water, term in ((3, "Visible concept"), (8, "Other term"))
        )
        tuple(future.result() for future in futures)

    stored, _ = store.load(document, lineage)
    assert stored is not None
    assert stored.high_water == 8
    assert {item.term for item in stored.terms} == {
        "Visible concept",
        "Other term",
    }
    visible = next(item for item in stored.terms if item.term == "Visible concept")
    assert visible.occurrence_count == 1
    assert all("inline_math" not in item.text for item in visible.matched_sentences)


def _lineage_for_rich(document: RichDocument) -> TermInventoryLineage:
    return TermInventoryLineage(
        document.document_digest,
        document.source.artifact_digest,
        document.source.source_format.value,
        document.source.media_type,
        document.source.size,
        document.schema_version,
        "discovery.v1",
        "review.v1",
        "normalize.v1",
        "count.v1",
        {"provider": "fake", "model": "fake", "tier": "medium"},
    )


@pytest.mark.parametrize(
    ("source_format", "payload", "expected"),
    [
        (
            SourceFormat.MARKDOWN,
            b"---\nkeywords: [alpha, beta]\n---\n# Body\nalpha.\n",
            ["alpha", "beta"],
        ),
        (
            SourceFormat.HTML,
            b"<html><head><meta name='keywords' content='alpha; beta'></head><body><p>alpha.</p></body></html>",
            ["alpha", "beta"],
        ),
        (
            SourceFormat.TEX,
            br"\keywords{alpha, beta}\section{Body}alpha.",
            ["alpha", "beta"],
        ),
    ],
)
def test_standard_and_rich_parsers_preserve_explicit_term_fields(
    source_format: SourceFormat,
    payload: bytes,
    expected: list[str],
) -> None:
    source = SourceArtifact(
        source_format,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        {
            SourceFormat.MARKDOWN: "text/markdown",
            SourceFormat.HTML: "text/html",
            SourceFormat.TEX: "application/x-tex",
        }[source_format],
        SourceOrigin(SourceOriginKind.REPOSITORY, locator="fixture"),
    )

    standard = parse_artifact_bytes(source, payload)
    rich = parse_rich_artifact_bytes(source, payload).document

    assert standard.metadata["explicit_term_fields"][0]["entries"] == expected
    assert tuple(rich.metadata["explicit_term_fields"][0]["entries"]) == tuple(
        expected
    )


def test_explicit_yaml_list_is_excluded_from_frequency_and_search_context() -> None:
    payload = (
        b"---\nkeywords:\n- alpha\n- beta\n---\n"
        b"Alpha appears once in the scientific body.\n"
    )
    source = SourceArtifact(
        SourceFormat.MARKDOWN,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "text/markdown",
        SourceOrigin(SourceOriginKind.REPOSITORY, locator="yaml"),
    )
    documents = (
        parse_artifact_bytes(source, payload),
        parse_rich_artifact_bytes(source, payload).document,
    )

    for document in documents:
        by_term = {
            item.term.casefold(): item
            for item in build_keyword_terms(
                document,
                (
                    TermCandidate("alpha", source_refs=("explicit:alpha",)),
                    TermCandidate("beta", source_refs=("explicit:beta",)),
                ),
            )
        }
        assert by_term["alpha"].occurrence_count == 1
        assert len(by_term["alpha"].matched_sentences) == 1
        assert "scientific body" in by_term["alpha"].matched_sentences[0].text
        assert by_term["beta"].occurrence_count == 0
        assert by_term["beta"].matched_sentences == ()


def test_cli_routes_keyword_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = {}

    def dispatch(name, parameters):
        captured.update(name=name, parameters=parameters)
        document = _parsed()
        terms = build_keyword_terms(
            document, (TermCandidate("shared concept"),)
        )
        store = TermInventoryStore(tmp_path / "cache")
        stored = store.merge(
            document,
            _lineage(document),
            high_water=75,
            explicit_disposition="absent",
            candidates=(TermCandidate("shared concept"),),
        )
        from arc_paper.terms import result_from_inventory

        return result_from_inventory(
            stored, approx_count=50, planned_count=75
        )

    monkeypatch.setattr("arc_paper.cli.dispatch_operation", dispatch)
    assert (
        main(
            [
                "extract-keywords",
                "arXiv:1234.5678",
                "--project-dir",
                str(tmp_path / "project"),
            ]
        )
        == 0
    )
    assert captured["name"] == "extract-keywords"
    assert captured["parameters"]["approx_count"] == 50
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
