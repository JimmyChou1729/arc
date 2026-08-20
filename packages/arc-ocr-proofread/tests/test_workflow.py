from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess
import zlib

import pytest

from arc_jobs import ImmutableArtifactStore, ResumeReason, RunStatus
from arc_llm import LLMCompleted, LLMPaused
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
        assert options.limits.idle_timeout_seconds == 600
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


class BoundaryTasks(Tasks):
    def __init__(self, *, boundary_action="join", join_mode="space"):
        super().__init__(None)
        self.boundary_action = boundary_action
        self.join_mode = join_mode

    def execute_or_resume(self, _context, request, *, options):
        self.calls += 1
        if "boundary_prompt" in request.task_id:
            value = {
                "action": self.boundary_action,
                "join_mode": (
                    self.join_mode
                    if self.boundary_action == "join"
                    else None
                ),
                "reason": "The sentence continues across the page turn.",
            }
        else:
            value = {
                "edits": [],
                "source_typo_candidates": [],
                "uncertainties": [],
                "checks": {
                    "all_visible_text": True,
                    "all_visible_equations": True,
                    "page_boundary": True,
                },
            }
        return LLMCompleted(value, "codex", "gpt-5.6-luna", None, None)


class InterruptedBoundaryTasks(BoundaryTasks):
    def execute_or_resume(self, context, request, *, options):
        if "boundary_prompt" in request.task_id:
            self.calls += 1
            return LLMPaused(
                ResumeReason.EXECUTION_INTERRUPTED,
                "provider-retry-exhausted",
            )
        return super().execute_or_resume(context, request, options=options)


class PlateBoundaryTasks(BoundaryTasks):
    def execute_or_resume(self, context, request, *, options):
        if "boundary_prompt" in request.task_id:
            self.calls += 1
            action = "separate" if request.task_id.endswith("000001") else "uncertain"
            return LLMCompleted(
                {
                    "action": action,
                    "join_mode": None,
                    "reason": "The plate has no paragraph candidate.",
                },
                "codex",
                "gpt-5.6-luna",
                None,
                None,
            )
        return super().execute_or_resume(context, request, options=options)


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


def _two_page_bundle(tmp_path: Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book_origin.pdf"
    content = tmp_path / "book_content_list.json"
    markdown.write_text(
        "This paragraph continues on the next page.\n", encoding="utf-8"
    )
    pdf.write_bytes(b"%PDF fixture")
    content.write_text(
        json.dumps(
            [
                {
                    "type": "text",
                    "text": "This paragraph continues",
                    "page_idx": 0,
                    "bbox": [0, 0, 1, 1],
                },
                {
                    "type": "text",
                    "text": "on the next page.",
                    "page_idx": 1,
                    "bbox": [0, 0, 1, 1],
                },
            ]
        ),
        encoding="utf-8",
    )

    def fake_run(command, **_kwargs):
        assert command[0] == "pdfinfo"
        return subprocess.CompletedProcess(command, 0, "Pages: 2\n", "")

    monkeypatch.setattr("arc_ocr_proofread.source.subprocess.run", fake_run)
    return load_mineru_source(markdown, pdf, content)


def _two_page_footer_bundle(tmp_path: Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book_origin.pdf"
    content = tmp_path / "book_content_list.json"
    markdown.write_text("fixture\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF fixture")
    content.write_text(
        json.dumps(
            [
                {
                    "type": "text",
                    "text": "apparent motion along",
                    "page_idx": 0,
                    "bbox": [0, 0, 1, 1],
                },
                {
                    "type": "text",
                    "text": "32",
                    "page_idx": 0,
                    "bbox": [0, 0, 1, 1],
                },
                {
                    "type": "text",
                    "text": "the ecliptic; the sentence continues.",
                    "page_idx": 1,
                    "bbox": [0, 0, 1, 1],
                },
            ]
        ),
        encoding="utf-8",
    )

    def fake_run(command, **_kwargs):
        assert command[0] == "pdfinfo"
        return subprocess.CompletedProcess(command, 0, "Pages: 2\n", "")

    monkeypatch.setattr("arc_ocr_proofread.source.subprocess.run", fake_run)
    return load_mineru_source(markdown, pdf, content)


def _three_page_plate_bundle(tmp_path: Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    markdown = tmp_path / "book.md"
    pdf = tmp_path / "book_origin.pdf"
    content = tmp_path / "book_content_list.json"
    markdown.write_text("fixture\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF fixture")
    content.write_text(
        json.dumps(
            [
                {
                    "type": "text",
                    "text": "The answer cannot be particularly",
                    "page_idx": 0,
                    "bbox": [0, 0, 1, 1],
                },
                {
                    "type": "text",
                    "text": "cheerful one.",
                    "page_idx": 2,
                    "bbox": [0, 0, 1, 1],
                },
            ]
        ),
        encoding="utf-8",
    )

    def fake_run(command, **_kwargs):
        assert command[0] == "pdfinfo"
        return subprocess.CompletedProcess(command, 0, "Pages: 3\n", "")

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
        "boundaries": [
            {"id": item["id"], "verdict": "pass"}
            for item in request["boundaries"]
        ],
    }


def _accept_boundary_joins(request: dict) -> dict:
    return {
        "resume_key": request["resume_key"],
        "decisions": [
            {
                "id": item["id"],
                "action": "join",
                "join_mode": item["join_mode"],
                "left_block_id": item["left"]["block_id"],
                "right_block_id": item["right"]["block_id"],
            }
            for item in request["items"]
        ],
    }


def test_page_schema_accepts_descriptive_edit_kinds() -> None:
    kind = PAGE_OUTPUT_SCHEMA["$defs"]["edit"]["properties"]["kind"]

    assert kind == {"type": "string", "minLength": 1}


def test_boundary_repair_reuses_verified_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    source = _two_page_bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    baseline_tasks = BoundaryTasks(boundary_action="separate")

    snapshot = service.execute(
        snapshot.run_id, task_service=baseline_tasks, renderer=Renderer()
    )
    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    snapshot = service.resume(
        snapshot.run_id,
        input=_pass_audit(audit),
        task_service=baseline_tasks,
        renderer=Renderer(),
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    assert service.result()["page_boundary_repairs"] == 0
    baseline = service.result()
    baseline.pop("page_boundary_repairs")
    project.manifest.write_text(json.dumps(baseline), encoding="utf-8")

    snapshot = service.prepare_boundary_repair(source.pdf_path)
    repair_tasks = BoundaryTasks(boundary_action="join", join_mode="space")
    snapshot = service.execute(
        snapshot.run_id, task_service=repair_tasks, renderer=Renderer()
    )
    assert snapshot.status is RunStatus.PAUSED
    review = _artifact_json(service, snapshot)
    assert review["schema_version"] == "arc.ocr_proofread.boundary_review_request.v3"
    snapshot = service.resume(
        snapshot.run_id,
        input=_accept_boundary_joins(review),
        task_service=repair_tasks,
        renderer=Renderer(),
    )
    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    assert len(audit["boundaries"]) == 1
    assert {item["page_index"] for item in audit["pages"]} == {0, 1}
    audit_input = _pass_audit(audit)
    page_one = next(item for item in audit_input["pages"] if item["id"] == "page-000001")
    page_one["edits"] = [
        {
            "before": "next page.",
            "after": "following page.",
            "occurrence": 1,
            "kind": "wording",
            "reason": "Main-agent audit found the scanned wording was mistranscribed.",
        }
    ]
    snapshot = service.resume(
        snapshot.run_id,
        input=audit_input,
        task_service=repair_tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    text = project.markdown.read_text(encoding="utf-8")
    assert "This paragraph continues on the following page." in text
    assert text.index("on the following page.") < text.index("<!-- Source PDF page 2 -->")
    manifest = service.result()
    assert manifest["page_boundary_repairs"] == 1
    assert manifest["ocr_corrections"] == 1
    assert manifest["corrections_per_page"] == 1.0
    ledger = [
        json.loads(line)
        for line in project.changes.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["category"] for item in ledger] == [
        "ocr_correction",
        "page_boundary_repair",
    ]
    assert service.validate(snapshot.run_id).ok


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


def test_llm_reviewed_boundary_join_moves_marker_below_merged_paragraph(
    tmp_path: Path, monkeypatch
) -> None:
    source = _two_page_bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = BoundaryTasks()

    snapshot = service.execute(
        snapshot.run_id, task_service=tasks, renderer=Renderer()
    )

    assert snapshot.status is RunStatus.PAUSED
    review = _artifact_json(service, snapshot)
    assert review["schema_version"] == "arc.ocr_proofread.boundary_review_request.v3"
    snapshot = service.resume(
        snapshot.run_id,
        input=_accept_boundary_joins(review),
        task_service=tasks,
        renderer=Renderer(),
    )
    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    assert len(audit["boundaries"]) == 1
    assert tasks.calls == 3

    snapshot = service.resume(
        snapshot.run_id,
        input=_pass_audit(audit),
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    delivered = project.markdown.read_text(encoding="utf-8")
    assert (
        "This paragraph continues on the next page.\n\n"
        "<!-- Source PDF page 2 -->"
    ) in delivered
    assert delivered.count("<!-- Source PDF page 2 -->") == 1
    manifest = service.result()
    assert manifest["page_boundary_repairs"] == 1
    assert manifest["corrections_per_page"] == 0.5


def test_interrupted_boundary_review_routes_to_main_agent(
    tmp_path: Path, monkeypatch
) -> None:
    source = _two_page_bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = InterruptedBoundaryTasks()

    snapshot = service.execute(
        snapshot.run_id, task_service=tasks, renderer=Renderer()
    )

    assert snapshot.status is RunStatus.PAUSED
    review = _artifact_json(service, snapshot)
    assert review["schema_version"] == "arc.ocr_proofread.boundary_review_request.v3"
    assert review["items"][0]["action"] == "uncertain"
    assert review["items"][0]["provider"] is None

    snapshot = service.resume(
        snapshot.run_id,
        input={
            "resume_key": review["resume_key"],
            "decisions": [
                {
                    "id": review["items"][0]["id"],
                    "action": "separate",
                    "join_mode": None,
                    "left_block_id": None,
                    "right_block_id": None,
                }
            ],
        },
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    assert audit["schema_version"] == "arc.ocr_proofread.audit_request.v1"
    assert {item["page_index"] for item in audit["pages"]} == {0, 1}


def test_main_agent_can_replace_a_trailing_footer_boundary_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    source = _two_page_footer_bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = BoundaryTasks()

    snapshot = service.execute(
        snapshot.run_id, task_service=tasks, renderer=Renderer()
    )

    assert snapshot.status is RunStatus.PAUSED
    review = _artifact_json(service, snapshot)
    item = review["items"][0]
    assert item["left"]["markdown"] == "32"
    left = next(
        candidate
        for candidate in item["left_candidates"]
        if candidate["markdown"] == "apparent motion along"
    )
    right = next(
        candidate
        for candidate in item["right_candidates"]
        if candidate["markdown"] == "the ecliptic; the sentence continues."
    )
    snapshot = service.resume(
        snapshot.run_id,
        input={
            "resume_key": review["resume_key"],
            "decisions": [
                {
                    "id": item["id"],
                    "action": "join",
                    "join_mode": "space",
                    "left_block_id": left["block_id"],
                    "right_block_id": right["block_id"],
                }
            ],
        },
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    snapshot = service.resume(
        snapshot.run_id,
        input=_pass_audit(audit),
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    delivered = project.markdown.read_text(encoding="utf-8")
    assert "apparent motion along the ecliptic; the sentence continues." in delivered
    assert "32" in delivered
    assert "32 the ecliptic" not in delivered
    footer = delivered.index("\n\n32\n\n")
    assert delivered.index("the sentence continues.") < footer
    assert footer < delivered.index("<!-- Source PDF page 2 -->")


def test_boundary_join_can_span_an_empty_plate_page(
    tmp_path: Path, monkeypatch
) -> None:
    source = _three_page_plate_bundle(tmp_path / "source", monkeypatch)
    project = ProofreadProject.open(tmp_path / "project")
    service = ProofreadService(project)
    snapshot = service.prepare(source)
    tasks = PlateBoundaryTasks()

    snapshot = service.execute(
        snapshot.run_id, task_service=tasks, renderer=Renderer()
    )

    assert snapshot.status is RunStatus.PAUSED
    review = _artifact_json(service, snapshot)
    item = review["items"][0]
    assert item["id"] == "boundary-000002-000003"
    left = item["left_candidates"][0]
    right = item["right_candidates"][0]
    assert left["page_index"] == 0
    assert right["page_index"] == 2
    snapshot = service.resume(
        snapshot.run_id,
        input={
            "resume_key": review["resume_key"],
            "decisions": [
                {
                    "id": item["id"],
                    "action": "join",
                    "join_mode": "space",
                    "left_block_id": left["block_id"],
                    "right_block_id": right["block_id"],
                }
            ],
        },
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.PAUSED
    audit = _artifact_json(service, snapshot)
    snapshot = service.resume(
        snapshot.run_id,
        input=_pass_audit(audit),
        task_service=tasks,
        renderer=Renderer(),
    )

    assert snapshot.status is RunStatus.SUCCEEDED
    delivered = project.markdown.read_text(encoding="utf-8")
    merged = delivered.index("The answer cannot be particularly cheerful one.")
    page_two = delivered.index("<!-- Source PDF page 2 -->")
    page_three = delivered.index("<!-- Source PDF page 3 -->")
    assert merged < page_two < page_three
    assert service.result()["page_boundary_repairs"] == 1


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
