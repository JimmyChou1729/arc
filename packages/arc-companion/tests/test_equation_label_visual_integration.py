from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from arc_jobs import Awaiting, Paused, ResumeReason, RunStatus
from arc_llm import LLMCompleted, LLMPaused
from arc_paper import (
    RichBlockKind,
    RichDocument,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

import arc_companion.build as build_module
import arc_companion.cli as cli_module
from arc_companion.generation_validation import CompanionContentError
from arc_companion.cli import _build_warnings, _resolve_source
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
)
from arc_companion.service import CompanionService


def _html_document(
    repository: SourceRepository, labels: tuple[str, ...]
) -> RichDocument:
    equations = "\n".join(
        (
            '<table class="ltx_equation">'
            f'<tr><td><math alttext="x_{index} = {index}"></math></td>'
            f'<td><span class="ltx_tag">({label})</span></td></tr>'
            "</table>"
        )
        for index, label in enumerate(labels, 1)
    )
    payload = (
        f"<article><h1>Source</h1>{equations}</article>"
    ).encode("utf-8")
    artifact = repository.store_bytes(
        payload,
        source_format=SourceFormat.HTML,
        origin=SourceOrigin(
            SourceOriginKind.REMOTE_PROVIDER,
            provider="fixture",
            locator="https://example.test/html/paper",
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def _pdf(repository: SourceRepository):
    return repository.store_bytes(
        b"%PDF-1.4\n% visual fixture\n",
        source_format=SourceFormat.PDF,
        origin=SourceOrigin(
            SourceOriginKind.REMOTE_PROVIDER,
            provider="fixture-pdf",
            locator="https://example.test/pdf/paper",
        ),
    )


def _request_payload(prompt: str) -> tuple[str, dict]:
    if prompt.startswith("## Package protocol\n"):
        sections: dict[str, str] = {}
        for raw_section in prompt.removeprefix("## ").split("\n\n## "):
            heading, separator, body = raw_section.partition("\n")
            assert separator
            sections[heading] = body
        contract = sections["Worker instructions"].splitlines()[0]
        payload = json.loads(sections["Caller context"])
        payload["_round_task"] = json.loads(sections["Round task"])
        return contract.removeprefix("Contract: "), payload
    first, _blank, rest = prompt.partition("\n\n")
    _instruction, marker, payload = rest.partition("\n\nInput JSON:\n")
    assert marker
    return first.removeprefix("Contract: "), json.loads(payload)


class _GuideTasks:
    def __init__(self) -> None:
        self.plan_labels: list[str] = []

    def execute_or_resume(self, _context, request, **_kwargs):
        contract, payload = _request_payload(request.prompt)
        if "author-identity-prompt" in contract:
            value = {
                "authors": [],
                "confidence": "low",
                "basis": "No author is confirmed by this fixture.",
                "anchor_block_ids": [],
            }
        elif "chapter-learning-review-prompt" in contract:
            value = {
                "schema_version": "arc.proposer_reviewer.review.v1",
                "action": "stop",
                "reason": "The proposal is sufficient.",
                "feedback": {"guide-proposer": "No revision is needed."},
                "payload": {
                    "checked_complete_chapter": True,
                    "checked_part_numbers": [
                        int(item["part_number"])
                        for item in payload["chapter"]["parts"]
                    ],
                    "checked_section_numbers": [
                        int(item["section_number"])
                        for item in payload["chapter"]["sections"]
                    ],
                },
            }
        elif "chapter-learning-prompt" in contract:
            self.plan_labels.extend(
                str(part["equation_label"])
                for part in payload["chapter"]["parts"]
                if part["kind"] == "equation"
            )
            value = {
                "chapter_guide": {
                    "title": "Reading guide",
                    "content_markdown": (
                        "The fixture demonstrates equation-label provenance."
                    ),
                },
                "section_guides": [],
                "companions": [],
                "references": [],
            }
        else:  # pragma: no cover - the visual service is replaced in these tests
            raise AssertionError(f"unexpected task contract: {contract}")
        return LLMCompleted(value, "fake", "fake", None, None)

    def execute(self, context, request, **kwargs):
        return self.execute_or_resume(context, request, **kwargs)


class _Translation:
    def detect_language(self, _context, source, **kwargs):
        return {
            "schema_version": "arc.translate.language_result.v1",
            "document_digest": source.document_digest,
            "source_digest": source.source.artifact_digest,
            "language_tag": "en",
            "classification": "known",
            "confidence": 1.0,
            "target_language": kwargs["target_language"],
            "mode": "skipped",
        }


class _PausingTranslation(_Translation):
    def __init__(self) -> None:
        self.calls = 0

    def detect_language(self, context, source, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return Paused(
                Awaiting(
                    ResumeReason.EXTERNAL_CONDITION,
                    "language-pause",
                    False,
                )
            )
        return super().detect_language(context, source, **kwargs)


class _ScriptedReviewer:
    outcomes: deque[object] = deque()

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def review(self, *_args, **_kwargs):
        return self.outcomes.popleft()


def _corrected_source(document: RichDocument, _outcome) -> RichDocument:
    equations = [
        block
        for block in document.blocks
        if block.kind is RichBlockKind.EQUATION
    ]
    metadata = dict(document.metadata)
    metadata["equation_label_reconciliation"] = {
        block.block_id: {
            "source_label": str(block.payload["label"]),
            "pdf_label": str(index),
            "effective_label": str(index),
            "page_number": 1,
            "matching_method": "visual_pdf_page",
        }
        for index, block in enumerate(equations, 1)
    }
    return RichDocument(
        source=document.source,
        blocks=document.blocks,
        sections=document.sections,
        assets=document.assets,
        page_map=document.page_map,
        metadata=metadata,
    )


def _visual_outcome(*, complete: bool, warnings: tuple[str, ...] = ()):
    return SimpleNamespace(
        complete=complete,
        warnings=warnings,
        diagnostics_document={
            "schema_version": "arc.paper.equation_label_diagnostics.v1",
            "complete": complete,
        },
    )


def test_complete_visual_mapping_becomes_the_effective_build_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SourceRepository(tmp_path / "paper-cache")
    source = _html_document(repository, ("1", "3"))
    pdf = _pdf(repository)
    _ScriptedReviewer.outcomes = deque(
        [_visual_outcome(complete=True)]
    )
    monkeypatch.setattr(
        build_module, "EquationLabelReviewService", _ScriptedReviewer
    )
    monkeypatch.setattr(
        build_module,
        "apply_visual_equation_labels",
        _corrected_source,
    )
    tasks = _GuideTasks()
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(
            source, validator_digests=(pdf.artifact_digest,)
        ),
        execution=CompanionExecutionOptions(
            paper_cache_root=repository.root
        ),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=_Translation(),  # type: ignore[arg-type]
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.plan_labels == ["1", "2"]
    book = service.accepted_book(completed.run_id)
    assert book.document_digest != source.document_digest
    labels = [
        anchor.payload["label"]
        for chapter in book.chapters
        for anchor in chapter.source_anchors
        if anchor.kind == "equation"
    ]
    assert labels == ["1", "2"]
    diagnostics = service.build_diagnostics(completed.run_id)
    assert diagnostics is not None
    assert diagnostics["status"] == "applied"
    assert diagnostics["warnings"] == []


def test_incomplete_visual_mapping_warns_and_keeps_all_web_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SourceRepository(tmp_path / "paper-cache")
    source = _html_document(repository, ("1", "3"))
    pdf = _pdf(repository)
    _ScriptedReviewer.outcomes = deque(
        [
            _visual_outcome(
                complete=False,
                warnings=("visual mapping is ambiguous",),
            )
        ]
    )
    monkeypatch.setattr(
        build_module, "EquationLabelReviewService", _ScriptedReviewer
    )
    tasks = _GuideTasks()
    service = CompanionService(tmp_path / "jobs")

    completed = service.build(
        CompanionBuildRequest(
            source, validator_digests=(pdf.artifact_digest,)
        ),
        execution=CompanionExecutionOptions(
            paper_cache_root=repository.root
        ),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=_Translation(),  # type: ignore[arg-type]
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.plan_labels == ["1", "3"]
    assert service.accepted_book(completed.run_id).document_digest == (
        source.document_digest
    )
    diagnostics = service.build_diagnostics(completed.run_id)
    assert diagnostics is not None
    assert diagnostics["status"] == "retained_web_labels"
    assert diagnostics["warnings"] == ["visual mapping is ambiguous"]
    command_warnings = _build_warnings(
        SimpleNamespace(jobs_root=service.repository.root),  # type: ignore[arg-type]
        completed.run_id,
    )
    assert [(item.code, item.message) for item in command_warnings] == [
        ("equation_label_review", "visual mapping is ambiguous")
    ]


def test_visual_pause_propagates_and_resume_continues_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SourceRepository(tmp_path / "paper-cache")
    source = _html_document(repository, ("1", "3"))
    pdf = _pdf(repository)
    _ScriptedReviewer.outcomes = deque(
        [
            LLMPaused(
                ResumeReason.EXTERNAL_CONDITION,
                "equation-page-pause",
            ),
            _visual_outcome(
                complete=False,
                warnings=("review completed without a full mapping",),
            ),
        ]
    )
    monkeypatch.setattr(
        build_module, "EquationLabelReviewService", _ScriptedReviewer
    )
    tasks = _GuideTasks()
    translation = _Translation()
    service = CompanionService(tmp_path / "jobs")
    request = CompanionBuildRequest(
        source, validator_digests=(pdf.artifact_digest,)
    )
    execution = CompanionExecutionOptions(paper_cache_root=repository.root)

    prepared = service.prepare(request)
    paused = service.execute(
        prepared.run_id,
        execution=execution,
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,  # type: ignore[arg-type]
    )
    assert paused.status is RunStatus.PAUSED
    assert service.build_diagnostics(paused.run_id) is None

    completed = service.resume(
        paused.run_id,
        execution=execution,
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,  # type: ignore[arg-type]
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert service.build_diagnostics(completed.run_id)["status"] == (
        "retained_web_labels"
    )


def test_effective_source_replays_after_a_later_build_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SourceRepository(tmp_path / "paper-cache")
    source = _html_document(repository, ("1", "3"))
    pdf = _pdf(repository)
    _ScriptedReviewer.outcomes = deque(
        [_visual_outcome(complete=True)]
    )
    monkeypatch.setattr(
        build_module, "EquationLabelReviewService", _ScriptedReviewer
    )
    monkeypatch.setattr(
        build_module,
        "apply_visual_equation_labels",
        _corrected_source,
    )
    tasks = _GuideTasks()
    translation = _PausingTranslation()
    service = CompanionService(tmp_path / "jobs")
    request = CompanionBuildRequest(
        source, validator_digests=(pdf.artifact_digest,)
    )
    execution = CompanionExecutionOptions(paper_cache_root=repository.root)

    prepared = service.prepare(request)
    paused = service.execute(
        prepared.run_id,
        execution=execution,
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,  # type: ignore[arg-type]
    )
    assert paused.status is RunStatus.PAUSED
    assert service.build_diagnostics(paused.run_id)["status"] == "applied"
    assert not _ScriptedReviewer.outcomes

    completed = service.resume(
        paused.run_id,
        execution=execution,
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=translation,  # type: ignore[arg-type]
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.plan_labels == ["1", "2"]
    assert not _ScriptedReviewer.outcomes


class _Paper:
    def __init__(
        self,
        repository: SourceRepository,
        initial,
        refreshed,
        pdf,
        *,
        pdf_error: Exception | None = None,
    ) -> None:
        self.repository = repository
        self.initial = initial
        self.refreshed = refreshed
        self.pdf = pdf
        self.pdf_error = pdf_error
        self.html_refreshes: list[bool] = []
        self.pdf_calls = 0
        self.pdf_refreshes: list[bool] = []

    def fetch_arxiv_auto(self, _source: str, *, refresh: bool = False):
        self.html_refreshes.append(refresh)
        return self.refreshed if refresh else self.initial

    def fetch_arxiv_pdf(self, _source: str, *, refresh: bool = False):
        self.pdf_calls += 1
        self.pdf_refreshes.append(refresh)
        if self.pdf_error is not None:
            raise self.pdf_error
        return self.pdf


def test_remote_suspicion_refreshes_html_before_deciding_about_pdf(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "paper")
    suspicious = _html_document(repository, ("1", "3")).source
    corrected = _html_document(repository, ("1", "2")).source
    paper = _Paper(repository, suspicious, corrected, _pdf(repository))

    document, validators, _warnings = _resolve_source(
        paper,  # type: ignore[arg-type]
        "2205.10527",
        pdf=None,
        refresh=False,
    )

    assert paper.html_refreshes == [False, True]
    assert paper.pdf_calls == 0
    assert validators == ()
    assert [
        block.payload["label"]
        for block in document.blocks
        if block.kind is RichBlockKind.EQUATION
    ] == ["1", "2"]


def test_remote_suspicion_auto_fetches_pdf_only_after_refresh(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "paper")
    suspicious = _html_document(repository, ("1", "3")).source
    pdf = _pdf(repository)
    paper = _Paper(repository, suspicious, suspicious, pdf)

    _document, validators, warnings = _resolve_source(
        paper,  # type: ignore[arg-type]
        "2205.10527",
        pdf=None,
        refresh=False,
    )

    assert paper.html_refreshes == [False, True]
    assert paper.pdf_calls == 1
    assert paper.pdf_refreshes == [True]
    assert validators == (pdf.artifact_digest,)
    assert not any("no PDF validator" in item for item in warnings)


def test_user_refresh_checks_html_once_and_refreshes_the_auto_pdf(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "paper")
    suspicious = _html_document(repository, ("1", "3")).source
    pdf = _pdf(repository)
    paper = _Paper(repository, suspicious, suspicious, pdf)

    _document, validators, _warnings = _resolve_source(
        paper,  # type: ignore[arg-type]
        "2205.10527",
        pdf=None,
        refresh=True,
    )

    assert paper.html_refreshes == [True]
    assert paper.pdf_refreshes == [True]
    assert validators == (pdf.artifact_digest,)


def test_explicit_pdf_fetch_uses_the_forced_html_refresh_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SourceRepository(tmp_path / "paper")
    suspicious = _html_document(repository, ("1", "3")).source
    pdf = _pdf(repository)
    paper = _Paper(repository, suspicious, suspicious, pdf)
    real_parser = RichDocumentParserService

    class ParserWithoutDeterministicPDF:
        def __init__(self, source_repository) -> None:
            self.delegate = real_parser(source_repository)

        def parse(self, bundle):
            return self.delegate.parse(
                type(bundle)(primary=bundle.primary)
            )

    monkeypatch.setattr(
        cli_module,
        "RichDocumentParserService",
        ParserWithoutDeterministicPDF,
    )

    _document, validators, _warnings = _resolve_source(
        paper,  # type: ignore[arg-type]
        "2205.10527",
        pdf="fetch",
        refresh=False,
    )

    assert paper.html_refreshes == [False, True]
    assert paper.pdf_refreshes == [True]
    assert validators == (pdf.artifact_digest,)


def test_auto_pdf_failure_is_a_warning_not_a_source_failure(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "paper")
    suspicious = _html_document(repository, ("1", "3")).source
    class TransportFailure(Exception):
        pass

    error = TransportFailure("offline")
    error.code = "pdf_offline"  # type: ignore[attr-defined]
    paper = _Paper(
        repository,
        suspicious,
        suspicious,
        _pdf(repository),
        pdf_error=error,
    )

    document, validators, warnings = _resolve_source(
        paper,  # type: ignore[arg-type]
        "2205.10527",
        pdf=None,
        refresh=False,
    )

    assert validators == ()
    assert document.source.artifact_digest == suspicious.artifact_digest
    assert any("pdf_offline" in item for item in warnings)


def test_build_diagnostics_validator_rejects_replay_identity_drift() -> None:
    document = {
        "schema_version": "arc.companion.build_diagnostics.v1",
        "status": "retained_web_labels",
        "source_document_digest": "a" * 64,
        "effective_document_digest": "b" * 64,
        "trigger_reasons": ["labels have gaps"],
        "warnings": ["mapping incomplete"],
        "visual_review": None,
    }

    with pytest.raises(
        CompanionContentError, match="Retained-label"
    ):
        build_module.validate_build_diagnostics(document)
