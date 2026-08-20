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
from .workflow import (
    BOUNDARY_IMAGE_GROUP_ID,
    BOUNDARY_GROUP_ID,
    BOUNDARY_REPAIR_HANDLER,
    GROUP_ID,
    HANDLER,
    BoundaryRepairConfig,
    BoundaryRepairHandler,
    ProofreadConfig,
    ProofreadHandler,
)


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
        handler = self._handler(run_id, task_service=task_service, renderer=renderer)
        return self.engine.execute(
            self.repository.read_spec(run_id),
            handler,
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
            self._handler(run_id, task_service=task_service, renderer=renderer),
            input=input,
        )

    def prepare_boundary_repair(
        self,
        pdf: str | Path,
        *,
        provider: str = "auto",
        model: str | None = None,
        model_tier: str = "medium",
        workers: int = 30,
        max_workers: int = 200,
    ) -> RunSnapshot:
        pdf_path = Path(pdf).resolve()
        for path in (
            self.project.markdown,
            self.project.manifest,
            self.project.changes,
            pdf_path,
        ):
            if not path.is_file():
                raise ProofreadServiceError(
                    "boundary_input_missing", f"required input is missing: {path}"
                )
        config = BoundaryRepairConfig(
            str(self.project.root),
            str(pdf_path),
            sha256_file(self.project.markdown),
            sha256_file(self.project.manifest),
            sha256_file(self.project.changes),
            sha256_file(pdf_path),
            provider,
            model,
            model_tier,
            workers,
            max_workers,
        )
        document = config.document()
        run_id = (
            "boundary-repair-"
            + hashlib.sha256(canonical_json_bytes(document)).hexdigest()[:20]
        )
        snapshot = self.repository.create(
            RunSpec(run_id, BOUNDARY_REPAIR_HANDLER, document)
        )
        self.project.select(run_id)
        return snapshot

    def inspect(self, run_id: str):
        return self.repository.inspect(run_id)

    def stop(self, run_id: str, *, reason: str | None = None):
        return self.repository.request_stop(run_id, reason=reason)

    def workers(self, run_id: str):
        for group_id in (GROUP_ID, BOUNDARY_GROUP_ID, BOUNDARY_IMAGE_GROUP_ID):
            try:
                return self.repository.group_workers(run_id, group_id)
            except Exception:
                continue
        raise ProofreadServiceError("workers_unavailable", "run has no work group")

    def set_workers(self, run_id: str, workers: int):
        maximum = self._handler_config(run_id).max_workers
        if not 1 <= workers <= maximum:
            raise ProofreadServiceError(
                "workers_invalid", f"workers must be between 1 and {maximum}"
            )
        control = None
        for group_id in (GROUP_ID, BOUNDARY_GROUP_ID, BOUNDARY_IMAGE_GROUP_ID):
            try:
                candidate = self.repository.set_group_workers(
                    run_id, group_id, workers
                )
                control = control or candidate
            except Exception:
                continue
        if control is None:
            raise ProofreadServiceError("workers_unavailable", "run has no work group")
        return control

    def metrics(self, run_id: str) -> dict[str, Any]:
        try:
            group = self.repository.inspect_group(run_id, GROUP_ID)
        except Exception:
            metrics: dict[str, Any] = {
                "total_pages": 0,
                "completed_pages": 0,
                "failed_pages": 0,
                "correction_records": 0,
                "corrections_per_completed_page": 0.0,
            }
        else:
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
            metrics = {
                "total_pages": len(group.units),
                "completed_pages": len(complete),
                "failed_pages": len(failed),
                "status_counts": status_counts,
                "correction_records": corrections,
                "corrections_per_completed_page": corrections / len(complete) if complete else 0.0,
            }
        try:
            boundaries = self.repository.inspect_group(
                run_id, BOUNDARY_GROUP_ID
            )
        except Exception:
            return metrics
        boundary_complete = [
            unit for unit in boundaries.units if unit.status == "succeeded"
        ]
        boundary_failed = [
            unit for unit in boundaries.units if unit.status == "failed"
        ]
        metrics.update(
            {
                "total_boundaries": len(boundaries.units),
                "completed_boundaries": len(boundary_complete),
                "failed_boundaries": len(boundary_failed),
                "proposed_boundary_joins": sum(
                    isinstance(unit.value, Mapping)
                    and unit.value.get("action") == "join"
                    for unit in boundary_complete
                ),
            }
        )
        return metrics

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

    def _handler_config(
        self, run_id: str
    ) -> ProofreadConfig | BoundaryRepairConfig:
        spec = self.repository.read_spec(run_id)
        if spec.handler == HANDLER:
            return ProofreadConfig.from_document(spec.semantic_input)
        if spec.handler == BOUNDARY_REPAIR_HANDLER:
            return BoundaryRepairConfig.from_document(spec.semantic_input)
        raise ProofreadServiceError(
            "run_handler_invalid", "run is not OCR proofreading"
        )

    def _handler(
        self,
        run_id: str,
        *,
        task_service: Any | None,
        renderer: Any | None,
    ) -> ProofreadHandler | BoundaryRepairHandler:
        config = self._handler_config(run_id)
        if isinstance(config, BoundaryRepairConfig):
            return BoundaryRepairHandler(
                config, task_service=task_service, renderer=renderer
            )
        return ProofreadHandler(
            config, task_service=task_service, renderer=renderer
        )


__all__ = ["ProofreadService", "ProofreadServiceError"]
