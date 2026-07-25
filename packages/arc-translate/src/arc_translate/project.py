"""Small project selector for independent translation run lineages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_json


PROJECT_SCHEMA = "arc.translate.project.v1"
_STEPS = {"language", "glossary", "blocks"}


class TranslationProjectError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TranslationProject:
    root: Path

    @classmethod
    def open(cls, value: str | Path) -> "TranslationProject":
        root = Path(value)
        marker = root / "arc-translate-project.json"
        if root.exists():
            if not root.is_dir():
                raise TranslationProjectError(
                    "project_path_invalid", "project path is not a directory"
                )
            if tuple(root.iterdir()) and not marker.is_file():
                raise TranslationProjectError(
                    "project_state_conflict",
                    "project directory contains unrelated state",
                )
        root.mkdir(parents=True, exist_ok=True)
        project = cls(root.resolve())
        if marker.exists():
            project._read()
        else:
            project._write(None, None, {})
        return project

    @classmethod
    def load(cls, value: str | Path) -> "TranslationProject":
        root = Path(value)
        marker = root / "arc-translate-project.json"
        if not root.is_dir() or not marker.is_file():
            raise TranslationProjectError(
                "project_not_found", "arc-translate project does not exist"
            )
        project = cls(root.resolve())
        project._read()
        return project

    @property
    def marker(self) -> Path:
        return self.root / "arc-translate-project.json"

    @property
    def runtime_root(self) -> Path:
        return self.root / ".arc-translate-v1"

    @property
    def jobs_root(self) -> Path:
        return self.runtime_root / "jobs"

    @property
    def paper_cache_root(self) -> Path:
        return self.runtime_root / "paper-cache"

    @property
    def current_run_id(self) -> str | None:
        return self._read()["current_run_id"]

    @property
    def current_step(self) -> str | None:
        return self._read()["current_step"]

    def run_id(self, step: str) -> str | None:
        if step not in _STEPS:
            raise ValueError("unknown translation step")
        return self._read()["runs"].get(step)

    def select(self, step: str, run_id: str) -> None:
        if step not in _STEPS or not run_id:
            raise ValueError("translation run selection is invalid")
        document = self._read()
        runs = dict(document["runs"])
        runs[step] = run_id
        self._write(run_id, step, runs)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranslationProjectError(
                "project_state_invalid", "project marker is unreadable"
            ) from exc
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "current_run_id",
            "current_step",
            "runs",
        }:
            raise TranslationProjectError(
                "project_state_invalid", "project marker has invalid fields"
            )
        if value["schema_version"] != PROJECT_SCHEMA:
            raise TranslationProjectError(
                "project_state_invalid", "unsupported project schema"
            )
        current_run = value["current_run_id"]
        current_step = value["current_step"]
        runs = value["runs"]
        if (
            (current_run is not None and not isinstance(current_run, str))
            or (current_step is not None and current_step not in _STEPS)
            or not isinstance(runs, dict)
            or any(
                step not in _STEPS
                or not isinstance(run_id, str)
                or not run_id
                for step, run_id in runs.items()
            )
            or ((current_run is None) != (current_step is None))
            or (
                current_step is not None
                and runs.get(current_step) != current_run
            )
        ):
            raise TranslationProjectError(
                "project_state_invalid", "project run selection is invalid"
            )
        return value

    def _write(
        self,
        current_run_id: str | None,
        current_step: str | None,
        runs: dict[str, str],
    ) -> None:
        atomic_write_json(
            self.marker,
            {
                "schema_version": PROJECT_SCHEMA,
                "current_run_id": current_run_id,
                "current_step": current_step,
                "runs": dict(sorted(runs.items())),
            },
        )


__all__ = [
    "PROJECT_SCHEMA",
    "TranslationProject",
    "TranslationProjectError",
]
