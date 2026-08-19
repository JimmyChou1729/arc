"""Public durable service for OCR proofreading."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from arc_jobs import (
    RunEngine,
    RunRepository,
    RunSnapshot,
    RunSpec,
    RunStatus,
    ValidationIssue,
    ValidationReport,
    canonical_json_bytes,
)

from .project import ProofreadProject
from .source import MineruSource, sha256_file
from .workflow import GROUP_ID, HANDLER, ProofreadConfig, ProofreadHandler


class ProofreadServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProofreadService:
    def __init__(self, project: ProofreadProject) -> None:
        self.project = project
        self.repository = RunRepository(project.jobs_root)
        self.engine = RunEngine(self.repository)

    def prepare(
        self,
        source: MineruSource,
        *,
        provider: str = "auto",
        model: str | None = None,
        model_tier: str = "medium",
        workers: int = 30,
        max_workers: int = 200,
    ) -> RunSnapshot:
        config = ProofreadConfig(
            str(self.project.root),
            str(source.markdown_path),
            str(source.pdf_path),
            str(source.content_list_path),
            source.markdown_sha256,
            source.pdf_sha256,
            source.content_list_sha256,
            provider,
            model,
            model_tier,
            workers,
            max_workers,
        )
        document = config.document()
        run_id = f"proofread-{hashlib.sha256(canonical_json_bytes(document)).hexdigest()[:20]}"
        snapshot = self.repository.create(RunSpec(run_id, HANDLER, document))
        self.project.select(run_id)
        return snapshot

    def execute(
        self,
        run_id: str,
        *,
        task_service: Any | None = None,
        renderer: Any | None = None,
    ) -> RunSnapshot:
        config = self._config(run_id)
        return self.engine.execute(
            self.repository.read_spec(run_id),
            ProofreadHandler(config, task_service=task_service, renderer=renderer),
        )

    def resume(
        self,
        run_id: str,
        *,
        input: Mapping[str, Any] | None = None,
        task_service: Any | None = None,
        renderer: Any | None = None,
    ) -> RunSnapshot:
        return self.engine.resume(
            run_id,
            ProofreadHandler(
                self._config(run_id), task_service=task_service, renderer=renderer
            ),
            input=input,
        )

    def inspect(self, run_id: str):
        return self.repository.inspect(run_id)

    def stop(self, run_id: str, *, reason: str | None = None):
        return self.repository.request_stop(run_id, reason=reason)

    def workers(self, run_id: str):
        return self.repository.group_workers(run_id, GROUP_ID)

    def set_workers(self, run_id: str, workers: int):
        maximum = self._config(run_id).max_workers
        if not 1 <= workers <= maximum:
            raise ProofreadServiceError(
                "workers_invalid", f"workers must be between 1 and {maximum}"
            )
        return self.repository.set_group_workers(run_id, GROUP_ID, workers)

    def metrics(self, run_id: str) -> dict[str, Any]:
        try:
            group = self.repository.inspect_group(run_id, GROUP_ID)
        except Exception:
            return {
                "total_pages": 0,
                "completed_pages": 0,
                "failed_pages": 0,
                "correction_records": 0,
                "corrections_per_completed_page": 0.0,
            }
        complete = [unit for unit in group.units if unit.status == "succeeded"]
        failed = [unit for unit in group.units if unit.status == "failed"]
        corrections = sum(
            len(unit.value.get("changes", []))
            for unit in complete
            if isinstance(unit.value, Mapping)
        )
        status_counts: dict[str, int] = {}
        for unit in complete:
            if isinstance(unit.value, Mapping):
                status = str(unit.value.get("status", "unknown"))
                status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "total_pages": len(group.units),
            "completed_pages": len(complete),
            "failed_pages": len(failed),
            "status_counts": status_counts,
            "correction_records": corrections,
            "corrections_per_completed_page": corrections / len(complete) if complete else 0.0,
        }

    def validate(self, run_id: str) -> ValidationReport:
        issues = list(self.repository.validate(run_id).issues)
        snapshot = self.repository.inspect(run_id).snapshot
        if snapshot.status is RunStatus.SUCCEEDED:
            try:
                manifest = json.loads(self.project.manifest.read_text(encoding="utf-8"))
                expected = manifest["delivery_sha256"]
                if sha256_file(self.project.markdown) != expected["markdown"]:
                    raise ValueError("proofread.md digest mismatch")
                if sha256_file(self.project.changes) != expected["changes"]:
                    raise ValueError("change ledger digest mismatch")
                for name, digest in expected["assets"].items():
                    if sha256_file(self.project.assets / name) != digest:
                        raise ValueError(f"asset digest mismatch: {name}")
            except Exception as exc:
                issues.append(ValidationIssue("delivery_invalid", str(exc)))
        return ValidationReport(tuple(issues))

    def result(self) -> dict[str, Any]:
        try:
            value = json.loads(self.project.manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProofreadServiceError("result_unavailable", "verified result is unavailable") from exc
        if not isinstance(value, dict):
            raise ProofreadServiceError("result_invalid", "result manifest is invalid")
        return value

    def _config(self, run_id: str) -> ProofreadConfig:
        spec = self.repository.read_spec(run_id)
        if spec.handler != HANDLER:
            raise ProofreadServiceError("run_handler_invalid", "run is not OCR proofreading")
        return ProofreadConfig.from_document(spec.semantic_input)


__all__ = ["ProofreadService", "ProofreadServiceError"]
