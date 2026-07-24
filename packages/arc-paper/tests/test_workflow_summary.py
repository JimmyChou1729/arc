from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from arc_jobs import (
    ArtifactDigest,
    ArtifactSourceRef,
    FailureMode,
    ImmutableArtifactStore,
    RunContext,
    RunRepository,
    RunSpec,
    RunStatus,
)
from arc_llm import (
    InvalidRequestError,
    JsonOutput,
    LLMCompleted,
    LLMFailed,
    LLMPaused,
    ModelSelection,
    ProviderUsage,
    ResumeAction,
    ResumeInput,
    ResumeReason,
    resume_input_to_document,
)
from arc_llm.identity import semantic_key as llm_semantic_key

from arc_paper.parse import ParsedDocument, ParsedSection, parsed_document_to_document
from arc_paper.sources import (
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
)
from arc_paper.workflows.summary import (
    PaperSummaryCompleted,
    PaperSummaryService,
    PaperWorkflowError,
    SummaryBatchItem,
    SummaryBatchRunner,
)


class FakeTaskService:
    def __init__(
        self,
        *,
        pause_document_digest: str | None = None,
        pause_document_digests: set[str] | None = None,
        fail_document_digest: str | None = None,
        section_id_override: str | None = None,
    ) -> None:
        self.pause_document_digests = set(pause_document_digests or ())
        if pause_document_digest is not None:
            self.pause_document_digests.add(pause_document_digest)
        self.fail_document_digest = fail_document_digest
        self.section_id_override = section_id_override
        self.provider_calls: list[str] = []
        self.invocations: list[tuple[str, str | None]] = []
        self.models: list[ModelSelection] = []
        self.requests: list[object] = []
        self.completed: dict[str, LLMCompleted] = {}
        self.pause_keys: dict[str, str] = {}

    def execute_or_resume(self, context, request, *, input=None, options=None):
        self.invocations.append(
            (request.task_id, None if input is None else input.resume_key)
        )
        self.models.append(request.model)
        self.requests.append(request)
        if request.task_id in self.completed:
            return self.completed[request.task_id]
        document_digest = _prompt_value(request.prompt, "Document digest")
        if document_digest == self.fail_document_digest:
            return LLMFailed(InvalidRequestError("synthetic failure"))
        if (
            document_digest in self.pause_document_digests
            and request.task_id.startswith("summary-section-")
        ):
            key = self.pause_keys.setdefault(
                request.task_id,
                f"resume-{llm_semantic_key(request).sha256[:24]}-1",
            )
            if input is None:
                return LLMPaused(
                    ResumeReason.EXTERNAL_CONDITION,
                    key,
                    input_required=False,
                )
            assert input.resume_key == key
            self.pause_document_digests.remove(document_digest)
        self.provider_calls.append(request.task_id)
        if request.task_id.startswith("summary-section-"):
            section_id = _prompt_value(request.prompt, "Section ID")
            value = {
                "section_id": self.section_id_override or section_id,
                "summary": f"Summary for {section_id}",
                "warnings": [],
            }
        else:
            value = {
                "title": "Synthetic paper",
                "high_value_summary": ["Result"],
                "reading_guide": [],
                "warnings": [],
            }
        outcome = LLMCompleted(
            value,
            "codex",
            "fake-model",
            None,
            ProviderUsage(10, 2, 1),
        )
        self.completed[request.task_id] = outcome
        return outcome


def _prompt_value(prompt: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}: (.+)$", prompt, flags=re.MULTILINE)
    return "" if match is None else match.group(1)


def _parsed(seed: str, *, section_count: int = 2) -> ParsedDocument:
    source = SourceArtifact(
        SourceFormat.MARKDOWN,
        seed * 64,
        100,
        "text/markdown",
        SourceOrigin(SourceOriginKind.REPOSITORY, locator=f"markdown/{seed}"),
    )
    return ParsedDocument(
        source,
        sections=tuple(
            ParsedSection(
                f"section-{index}",
                f"Section {index}",
                1,
                f"Text {seed} {index}",
                index,
            )
            for index in range(section_count)
        ),
    )


def _publish(
    repository: RunRepository,
    parsed: ParsedDocument,
    *,
    run_id: str,
) -> ArtifactSourceRef:
    repository.create(RunSpec(run_id, "test.parse.v1", {"seed": parsed.document_digest}))
    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    ref = store.publish_json("parsed/document", parsed_document_to_document(parsed))
    return ArtifactSourceRef(run_id, ref.artifact_id, ref.digest)


def _context(repository: RunRepository, run_id: str = "parent") -> RunContext:
    snapshot = repository.create(RunSpec(run_id, "test.parent.v1", {"case": run_id}))
    return RunContext(
        repository, snapshot, resume_input=None, execution_slice=None
    )


def _result_document(repository: RunRepository, run_id: str) -> dict:
    snapshot = repository.inspect(run_id).snapshot
    assert snapshot.result_ref is not None
    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    return json.loads(store.read_bytes(snapshot.result_ref))


def test_summary_accepts_arbitrary_parsed_document_artifact_and_replays(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    parsed = _parsed("a")
    source = _publish(repository, parsed, run_id="parse-a")
    fake = FakeTaskService()
    service = PaperSummaryService(fake)
    context = _context(repository)

    first = service.summarize(
        context,
        source,
        expected_document_digest=parsed.document_digest,
        model=ModelSelection("auto", tier="low"),
    )
    second = service.summarize(
        context,
        source,
        expected_document_digest=parsed.document_digest,
        model=ModelSelection("auto", tier="low"),
    )

    assert isinstance(first, PaperSummaryCompleted)
    assert isinstance(second, PaperSummaryCompleted)
    assert first.result.document_digest == parsed.document_digest
    assert len(first.result.sections) == 2
    assert len(fake.provider_calls) == 3
    assert fake.models == [ModelSelection("auto", tier="low")] * 6
    assert all(isinstance(request.output, JsonOutput) for request in fake.requests)
    assert [
        tuple(request.output.schema["required"])
        for request in fake.requests[:3]
    ] == [
        ("section_id", "summary", "warnings"),
        ("section_id", "summary", "warnings"),
        ("title", "high_value_summary", "reading_guide", "warnings"),
    ]
    assert all(item.provider == "codex" for item in first.result.provenance)
    assert all(item.model == "fake-model" for item in first.result.provenance)


def test_summary_rejects_a_section_result_for_a_different_section(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    parsed = _parsed("a", section_count=1)
    source = _publish(repository, parsed, run_id="parse-a")
    service = PaperSummaryService(
        FakeTaskService(section_id_override="different-section")
    )

    with pytest.raises(PaperWorkflowError) as error:
        service.summarize(
            _context(repository),
            source,
            expected_document_digest=parsed.document_digest,
        )

    assert error.value.code == "summary_output_invalid"


def test_summary_rejects_mismatched_document_identity_before_llm(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    parsed = _parsed("a", section_count=1)
    source = _publish(repository, parsed, run_id="parse-a")
    fake = FakeTaskService()

    with pytest.raises(PaperWorkflowError) as error:
        PaperSummaryService(fake).summarize(
            _context(repository),
            source,
            expected_document_digest="0" * 64,
        )

    assert error.value.code == "document_identity_mismatch"
    assert fake.invocations == []


def test_summary_batch_replays_successful_unit_and_routes_child_pause(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    first = _parsed("a", section_count=1)
    second = _parsed("b", section_count=1)
    items = (
        SummaryBatchItem(
            "first", first, _publish(repository, first, run_id="parse-first")
        ),
        SummaryBatchItem(
            "second", second, _publish(repository, second, run_id="parse-second")
        ),
    )
    fake = FakeTaskService(pause_document_digest=second.document_digest)
    runner = SummaryBatchRunner(repository)

    paused = runner.execute(
        "summary-run",
        items,
        service=PaperSummaryService(fake),
        max_workers=1,
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.awaiting is not None
    first_calls = tuple(fake.provider_calls)
    assert len(first_calls) == 2

    resumed = runner.resume(
        "summary-run",
        items,
        input=resume_input_to_document(
            ResumeInput(paused.awaiting.resume_key, ResumeAction.CONTINUE)
        ),
        service=PaperSummaryService(fake),
        max_workers=1,
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert fake.provider_calls[:2] == list(first_calls)
    assert fake.provider_calls.count(first_calls[0]) == 1
    routed = [
        item
        for item in fake.invocations
        if item[1] == paused.awaiting.resume_key
    ]
    assert len(routed) == 1
    result = _result_document(repository, "summary-run")
    assert result["complete"] is True
    assert [unit["status"] for unit in result["units"]] == [
        "succeeded",
        "succeeded",
    ]


def test_fail_fast_publishes_partial_and_failed_retry_needs_new_run(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    parsed = tuple(_parsed(seed, section_count=1) for seed in ("a", "b", "c"))
    items = tuple(
        SummaryBatchItem(
            f"item-{index}",
            document,
            _publish(repository, document, run_id=f"parse-{index}"),
        )
        for index, document in enumerate(parsed)
    )
    fake = FakeTaskService(fail_document_digest=parsed[0].document_digest)
    runner = SummaryBatchRunner(repository)

    first = runner.execute(
        "fail-fast-run",
        items,
        service=PaperSummaryService(fake),
        max_workers=1,
        failure_mode=FailureMode.FAIL_FAST,
    )
    replay = runner.execute(
        "fail-fast-run",
        items,
        service=PaperSummaryService(fake),
        max_workers=1,
        failure_mode=FailureMode.FAIL_FAST,
    )

    assert first.status is RunStatus.SUCCEEDED
    assert replay == first
    document = _result_document(repository, "fail-fast-run")
    assert document["complete"] is False
    assert len(document["units"]) == 1
    assert document["units"][0]["status"] == "failed"
    assert len(document["pending_unit_ids"]) == 2
    invocations = len(fake.invocations)

    retried = runner.execute(
        "fail-fast-retry",
        items,
        service=PaperSummaryService(fake),
        max_workers=1,
        failure_mode=FailureMode.FAIL_FAST,
    )
    assert retried.status is RunStatus.SUCCEEDED
    assert len(fake.invocations) == invocations + 1


def test_multiple_child_pauses_are_resumed_by_their_namespaced_keys(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    documents = (_parsed("a", section_count=1), _parsed("b", section_count=1))
    items = tuple(
        SummaryBatchItem(
            f"item-{index}",
            document,
            _publish(repository, document, run_id=f"multi-parse-{index}"),
        )
        for index, document in enumerate(documents)
    )
    fake = FakeTaskService(
        pause_document_digests={item.document_digest for item in documents}
    )
    runner = SummaryBatchRunner(repository)

    first_pause = runner.execute(
        "multi-pause-run",
        items,
        service=PaperSummaryService(fake),
        max_workers=2,
    )
    assert first_pause.status is RunStatus.PAUSED
    assert first_pause.awaiting is not None

    second_pause = runner.resume(
        "multi-pause-run",
        items,
        input=resume_input_to_document(
            ResumeInput(first_pause.awaiting.resume_key, ResumeAction.CONTINUE)
        ),
        service=PaperSummaryService(fake),
        max_workers=2,
    )
    assert second_pause.status is RunStatus.PAUSED
    assert second_pause.awaiting is not None
    assert second_pause.awaiting.resume_key != first_pause.awaiting.resume_key

    completed = runner.resume(
        "multi-pause-run",
        items,
        input=resume_input_to_document(
            ResumeInput(second_pause.awaiting.resume_key, ResumeAction.CONTINUE)
        ),
        service=PaperSummaryService(fake),
        max_workers=2,
    )
    assert completed.status is RunStatus.SUCCEEDED
    supplied_keys = [resume_key for _, resume_key in fake.invocations if resume_key]
    assert supplied_keys == [
        first_pause.awaiting.resume_key,
        second_pause.awaiting.resume_key,
    ]


def test_collect_records_every_terminal_item_and_locator_does_not_change_semantics(
    tmp_path: Path,
) -> None:
    repository = RunRepository(tmp_path)
    first = _parsed("a", section_count=1)
    second = _parsed("b", section_count=1)
    first_source = _publish(repository, first, run_id="collect-parse-first")
    second_source = _publish(repository, second, run_id="collect-parse-second")
    items = (
        SummaryBatchItem("first", first, first_source),
        SummaryBatchItem("second", second, second_source),
    )
    fake = FakeTaskService(fail_document_digest=first.document_digest)
    runner = SummaryBatchRunner(repository)

    snapshot = runner.execute(
        "collect-run",
        items,
        service=PaperSummaryService(fake),
        max_workers=1,
        failure_mode=FailureMode.COLLECT,
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    result = _result_document(repository, "collect-run")
    assert result["complete"] is True
    assert [unit["status"] for unit in result["units"]] == [
        "failed",
        "succeeded",
    ]
    assert result["pending_unit_ids"] == []

    relocated = SummaryBatchItem(
        "first",
        first,
        ArtifactSourceRef(
            "other-run",
            "other/artifact",
            ArtifactDigest(
                "sha256",
                first_source.expected_digest.value,
                first_source.expected_digest.size_bytes,
            ),
        ),
    )
    assert relocated.semantic_document() == items[0].semantic_document()
