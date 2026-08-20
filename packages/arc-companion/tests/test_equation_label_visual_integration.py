from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from arc_jobs import Awaiting, Paused, ResumeReason, RunStatus
from arc_llm import LLMCompleted, LLMPaused
from arc_document import (
    ArcDocumentService,
    PDFTextLayer,
    RichBlockKind,
    RichDocument,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

import arc_companion.build as build_module
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
            "schema_version": "arc.document.equation_label_diagnostics.v1",
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
            document_cache_root=repository.root
        ),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=_Translation(),  # type: ignore[arg-type]
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.plan_labels == ["1", "2"]
    effective = service.publication(completed.run_id).source_document
    assert effective.document_digest != source.document_digest
    reconciliation = effective.metadata[
        "equation_label_reconciliation"
    ]
    assert [
        reconciliation[block.block_id]["effective_label"]
        for block in effective.blocks
        if block.kind is RichBlockKind.EQUATION
    ] == ["1", "2"]
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
            document_cache_root=repository.root
        ),
        task_service=tasks,  # type: ignore[arg-type]
        translation_adapter=_Translation(),  # type: ignore[arg-type]
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert tasks.plan_labels == ["1", "3"]
    assert service.publication(
        completed.run_id
    ).source_document.document_digest == (
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
    execution = CompanionExecutionOptions(document_cache_root=repository.root)

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
    execution = CompanionExecutionOptions(document_cache_root=repository.root)

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


def test_local_source_accepts_an_explicit_local_pdf_validator(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "documents")
    source_path = tmp_path / "source.html"
    source_path.write_bytes(repository.read_bytes(_html_document(repository, ("1", "3")).source))
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(repository.read_bytes(_pdf(repository)))
    class Extractor:
        contract_id = "arc.companion.tests.pdf_text.v1"

        def extract(self, _payload: bytes) -> PDFTextLayer:
            return PDFTextLayer(("Source x_1 = 1 (1) x_2 = 2 (3)",))

    document_service = ArcDocumentService(
        cache_root=repository.root,
        pdf_text_extractor=Extractor(),
    )

    document, validators, _warnings = _resolve_source(
        document_service,
        str(source_path),
        pdf=str(pdf_path),
        refresh=False,
    )

    assert validators
    assert document.source.source_format is SourceFormat.HTML


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
