from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess
import zlib

import pytest

from arc_jobs import ImmutableArtifactStore, RunStatus
from arc_llm import LLMCompleted
from arc_paper import RenderedPDFPage

from arc_ocr_proofread import ProofreadProject, ProofreadService, load_mineru_source
from arc_ocr_proofread.workflow import PAGE_OUTPUT_SCHEMA, ProofreadWorkflowError, _apply_edits


def _png(width: int = 40, height: int = 60) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Renderer:
    def render_page(self, _pdf: bytes, page_number: int) -> RenderedPDFPage:
        payload = _png()
        return RenderedPDFPage(page_number, payload, 40, 60)


class Tasks:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def execute_or_resume(self, _context, _request, *, options):
        self.calls += 1
        assert options.profile.value == "bounded"
        assert options.gate.global_limit == 200
        return LLMCompleted(self.value, "codex", "gpt-5.6-luna", None, None)


class SequenceTasks(Tasks):
    def __init__(self, values):
        super().__init__(None)
        self.values = list(values)
        self.task_ids = []
        self.prompts = []

    def execute_or_resume(self, _context, request, *, options):
        self.calls += 1
        self.task_ids.append(request.task_id)
        self.prompts.append(request.prompt)
        return LLMCompleted(self.values.pop(0), "codex", "gpt-5.6-luna", None, None)


def _bundle(tmp_path: Path, monkeypatch, *, pages: int = 1):
    tmp_path.mkdir(parents=True, exist_ok=True)
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book_origin.pdf"
    content = tmp_path / "book_content_list.json"
    markdown.write_text("Helo\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF fixture")
    content.write_text(
        json.dumps(
            [
                {"type": "text", "text": "Helo", "page_idx": 0, "bbox": [0, 0, 1, 1]}
            ]
        ),
        encoding="utf-8",
    )

    def fake_run(command, **_kwargs):
        assert command[0] == "pdfinfo"
        return subprocess.CompletedProcess(command, 0, f"Pages: {pages}\n", "")

    monkeypatch.setattr("arc_ocr_proofread.source.subprocess.run", fake_run)
    return load_mineru_source(markdown, pdf, content)


def _artifact_json(service: ProofreadService, snapshot) -> dict:
    assert snapshot.awaiting is not None and snapshot.awaiting.request_ref is not None
    store = ImmutableArtifactStore(
        service.repository.run_directory(snapshot.run_id),
        repository_root=service.repository.root,
    )
    return json.loads(store.read_bytes(snapshot.awaiting.request_ref).decode("utf-8"))


def _pass_audit(request: dict) -> dict:
    return {
        "resume_key": request["resume_key"],
        "changes": [{"id": item["id"], "verdict": "pass"} for item in request["changes"]],
        "pages": [{"id": item["id"], "verdict": "pass"} for item in request["pages"]],
    }


def test_page_schema_accepts_descriptive_edit_kinds() -> None:
    kind = PAGE_OUTPUT_SCHEMA["$defs"]["edit"]["properties"]["kind"]

    assert kind == {"type": "string", "minLength": 1}


def test_empty_anchor_inserts_only_into_empty_page() -> None:
    edit = {
        "before": "",
        "after": "Recovered page text",
        "occurrence": 1,
        "kind": "omission",
        "reason": "page OCR was empty",
    }

    assert _apply_edits("", [edit]) == "Recovered page text"
    with pytest.raises(ProofreadWorkflowError, match="entirely empty page"):
        _apply_edits("Existing", [edit])


def test_source_uses_pdf_count_and_keeps_blank_pages(tmp_path: Path, monkeypatch) -> None:
    source = _bundle(tmp_path, monkeypatch, pages=2)

    assert source.page_count == 2
    assert source.pages[0].markdown == "Helo"
    assert source.pages[1].markdown == ""


def test_durable_review_audit_and_delivery(tmp_path: Path, monkeypatch) -> None:
    source = _bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = Tasks(
        {
            "edits": [
                {
                    "before": "Helo",
                    "after": "Hello",
                    "occurrence": 1,
                    "kind": "spelling",
                    "reason": "visible missing l",
                }
            ],
            "source_typo_candidates": [
                {
                    "before": "Hello",
                    "after": "Hallo",
                    "occurrence": 1,
                    "kind": "grammar",
                    "reason": "printed source typo",
                }
            ],
            "uncertainties": [],
            "checks": {
                "all_visible_text": True,
                "all_visible_equations": True,
                "page_boundary": True,
            },
        }
    )

    snapshot = service.execute(snapshot.run_id, task_service=tasks, renderer=Renderer())
    assert snapshot.status is RunStatus.PAUSED
    review = _artifact_json(service, snapshot)
    assert review["schema_version"] == "arc.ocr_proofread.review_request.v1"
    review_input = {
        "resume_key": review["resume_key"],
        "decisions": [{"id": review["items"][0]["id"], "action": "accept"}],
    }
    snapshot = service.resume(
        snapshot.run_id,
        input=review_input,
        task_service=tasks,
        renderer=Renderer(),
    )
    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    assert audit["schema_version"] == "arc.ocr_proofread.audit_request.v1"

    snapshot = service.resume(
        snapshot.run_id,
        input=_pass_audit(audit),
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert tasks.calls == 1
    assert "Hallo" in project.markdown.read_text(encoding="utf-8")
    ledger = [json.loads(line) for line in project.changes.read_text(encoding="utf-8").splitlines()]
    assert [item["category"] for item in ledger] == [
        "ocr_correction",
        "approved_source_correction",
    ]
    assert [item["kind"] for item in ledger] == ["spelling", "grammar"]
    manifest = service.result()
    assert manifest["corrections_per_page"] == 2.0
    assert service.validate(snapshot.run_id).ok
    control = service.workers(snapshot.run_id)
    assert control.target_workers == 30
    assert service.set_workers(snapshot.run_id, 10).target_workers == 10


def test_no_review_items_pauses_directly_for_audit(tmp_path: Path, monkeypatch) -> None:
    source = _bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = Tasks(
        {
            "edits": [],
            "source_typo_candidates": [],
            "uncertainties": [],
            "checks": {
                "all_visible_text": True,
                "all_visible_equations": True,
                "page_boundary": True,
            },
        }
    )

    snapshot = service.execute(snapshot.run_id, task_service=tasks, renderer=Renderer())

    assert snapshot.status is RunStatus.PAUSED
    assert _artifact_json(service, snapshot)["schema_version"] == "arc.ocr_proofread.audit_request.v1"


def test_audit_can_apply_exact_page_corrections(tmp_path: Path, monkeypatch) -> None:
    source = _bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = Tasks(
        {
            "edits": [],
            "source_typo_candidates": [],
            "uncertainties": [],
            "checks": {
                "all_visible_text": True,
                "all_visible_equations": True,
                "page_boundary": True,
            },
        }
    )

    snapshot = service.execute(snapshot.run_id, task_service=tasks, renderer=Renderer())
    audit = _artifact_json(service, snapshot)
    audit_input = _pass_audit(audit)
    audit_input["pages"][0]["edits"] = [
        {
            "before": "Helo",
            "after": "Hello",
            "occurrence": 1,
            "kind": "spelling",
            "reason": "Main-agent page audit found the missing letter.",
        }
    ]

    snapshot = service.resume(
        snapshot.run_id,
        input=audit_input,
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    assert "Hello" in project.markdown.read_text(encoding="utf-8")
    ledger = [json.loads(line) for line in project.changes.read_text(encoding="utf-8").splitlines()]
    assert [(item["category"], item["kind"]) for item in ledger] == [
        ("ocr_correction", "spelling")
    ]
    assert service.result()["ocr_corrections"] == 1


def test_semantic_invalid_output_gets_one_fresh_generation(tmp_path: Path, monkeypatch) -> None:
    source = _bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    checks = {
        "all_visible_text": True,
        "all_visible_equations": True,
        "page_boundary": True,
    }
    tasks = SequenceTasks(
        [
            {
                "edits": [{"before": "absent", "after": "x", "occurrence": 1, "kind": "text", "reason": "bad"}],
                "source_typo_candidates": [],
                "uncertainties": [],
                "checks": checks,
            },
            {
                "edits": [],
                "source_typo_candidates": [],
                "uncertainties": [],
                "checks": checks,
            },
        ]
    )

    snapshot = service.execute(snapshot.run_id, task_service=tasks, renderer=Renderer())

    assert snapshot.status is RunStatus.PAUSED
    assert tasks.calls == 2
    assert tasks.task_ids[0] != tasks.task_ids[1]
    assert "semantic-retry" in tasks.task_ids[1]
    assert "Do not call tools or access files." in tasks.prompts[0]
