"""Project-local OCR proofreading ownership."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_json


PROJECT_SCHEMA = "arc.ocr_proofread.project.v1"


class ProofreadProjectError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProofreadProject:
    root: Path

    @classmethod
    def open(cls, value: str | Path) -> "ProofreadProject":
        root = Path(value)
        if root.exists() and not root.is_dir():
            raise ProofreadProjectError("project_path_invalid", "project path is not a directory")
        root.mkdir(parents=True, exist_ok=True)
        project = cls(root.resolve())
        if project.marker.exists():
            project._read()
        else:
            for managed in project.deliveries:
                if managed.exists():
                    raise ProofreadProjectError(
                        "project_state_conflict", f"unowned delivery already exists: {managed}"
                    )
            project._write(None)
        return project

    @classmethod
    def load(cls, value: str | Path) -> "ProofreadProject":
        project = cls(Path(value).resolve())
        if not project.marker.is_file():
            raise ProofreadProjectError("project_not_found", "OCR proofreading project not found")
        project._read()
        return project

    @property
    def runtime_root(self) -> Path:
        return self.root / ".arc" / "ocr-proofread"

    @property
    def marker(self) -> Path:
        return self.runtime_root / "project.json"

    @property
    def jobs_root(self) -> Path:
        return self.runtime_root / "jobs"

    @property
    def markdown(self) -> Path:
        return self.root / "proofread.md"

    @property
    def manifest(self) -> Path:
        return self.root / "proofread.manifest.json"

    @property
    def changes(self) -> Path:
        return self.root / "proofread.changes.jsonl"

    @property
    def assets(self) -> Path:
        return self.root / "proofread-assets"

    @property
    def deliveries(self) -> tuple[Path, ...]:
        return (self.markdown, self.manifest, self.changes, self.assets)

    @property
    def current_run_id(self) -> str | None:
        return self._read()["current_run_id"]

    def select(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        self._write(run_id)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProofreadProjectError("project_state_invalid", "project marker is unreadable") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "current_run_id"}
            or value.get("schema_version") != PROJECT_SCHEMA
            or (
                value.get("current_run_id") is not None
                and not isinstance(value.get("current_run_id"), str)
            )
        ):
            raise ProofreadProjectError("project_state_invalid", "project marker is invalid")
        return value

    def _write(self, run_id: str | None) -> None:
        atomic_write_json(
            self.marker,
            {"schema_version": PROJECT_SCHEMA, "current_run_id": run_id},
        )


__all__ = ["PROJECT_SCHEMA", "ProofreadProject", "ProofreadProjectError"]
