"""Small project layout and legacy-state refusal for Companion 1.0.1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_json


PROJECT_SCHEMA = "arc.companion.project.v1"
CURRENT_SCHEMA = "arc.companion.current_release.v1"
DIAGNOSTICS_SCHEMA = "arc.companion.source_diagnostics.v1"


class CompanionProjectError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompanionProjectPaths:
    root: Path

    @classmethod
    def open(cls, value: str | Path) -> "CompanionProjectPaths":
        root = Path(value)
        marker = root / "companion-project.json"
        if root.exists():
            if not root.is_dir():
                raise CompanionProjectError(
                    "project_path_invalid", "project path is not a directory"
                )
            entries = tuple(root.iterdir())
            if entries and not marker.is_file():
                raise CompanionProjectError(
                    "legacy_project_state",
                    "project directory contains legacy or unrelated state; "
                    "use a new project directory",
                )
        root.mkdir(parents=True, exist_ok=True)
        paths = cls(root.resolve())
        if marker.exists():
            paths._read_project()
        else:
            paths._write_project(None)
        return paths

    @classmethod
    def load(cls, value: str | Path) -> "CompanionProjectPaths":
        """Open an existing v1 project without creating or changing files."""

        root = Path(value)
        marker = root / "companion-project.json"
        if not root.is_dir() or not marker.is_file():
            raise CompanionProjectError(
                "project_not_found",
                "Companion 1.0.1 project does not exist",
            )
        paths = cls(root.resolve())
        paths._read_project()
        return paths

    @property
    def marker(self) -> Path:
        return self.root / "companion-project.json"

    @property
    def runtime_root(self) -> Path:
        return self.root / ".arc-companion-v1"

    @property
    def jobs_root(self) -> Path:
        return self.runtime_root / "jobs"

    @property
    def paper_cache_root(self) -> Path:
        return self.runtime_root / "paper-cache"

    @property
    def releases_root(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current.json"

    @property
    def current_run_id(self) -> str | None:
        return self._read_project()["current_run_id"]

    def select_run(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        self._write_project(run_id)

    def current_release(self) -> dict[str, Any] | None:
        if not self.current.exists():
            return None
        value = _read_json(self.current, "current release")
        if set(value) != {
            "schema_version",
            "release_id",
            "manifest",
            "run_id",
        } or value.get("schema_version") != CURRENT_SCHEMA:
            raise CompanionProjectError(
                "project_state_invalid", "current release pointer is invalid"
            )
        for key in ("release_id", "manifest", "run_id"):
            if not isinstance(value.get(key), str) or not value[key]:
                raise CompanionProjectError(
                    "project_state_invalid", "current release pointer is invalid"
                )
        return value

    def write_source_diagnostics(
        self, run_id: str, warnings: tuple[str, ...]
    ) -> None:
        if not run_id or any(
            not isinstance(item, str) or not item for item in warnings
        ):
            raise ValueError("source diagnostics are invalid")
        _atomic_json(
            self._diagnostics_path(run_id),
            {
                "schema_version": DIAGNOSTICS_SCHEMA,
                "run_id": run_id,
                "warnings": list(warnings),
            },
        )

    def source_diagnostics(self, run_id: str) -> tuple[str, ...]:
        path = self._diagnostics_path(run_id)
        if not path.exists():
            return ()
        value = _read_json(path, "source diagnostics")
        if set(value) != {"schema_version", "run_id", "warnings"} or (
            value.get("schema_version") != DIAGNOSTICS_SCHEMA
            or value.get("run_id") != run_id
            or not isinstance(value.get("warnings"), list)
            or any(
                not isinstance(item, str) or not item
                for item in value["warnings"]
            )
        ):
            raise CompanionProjectError(
                "project_state_invalid", "source diagnostics are invalid"
            )
        return tuple(value["warnings"])

    def publish_current(
        self, *, release_id: str, manifest: Path, run_id: str
    ) -> None:
        try:
            relative = manifest.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise CompanionProjectError(
                "release_path_invalid",
                "release manifest must be inside the project",
            ) from exc
        _atomic_json(
            self.current,
            {
                "schema_version": CURRENT_SCHEMA,
                "release_id": release_id,
                "manifest": relative,
                "run_id": run_id,
            },
        )

    def _read_project(self) -> dict[str, Any]:
        value = _read_json(self.marker, "project marker")
        if set(value) != {"schema_version", "current_run_id"}:
            raise CompanionProjectError(
                "project_state_invalid", "project marker has invalid fields"
            )
        if value.get("schema_version") != PROJECT_SCHEMA:
            raise CompanionProjectError(
                "legacy_project_state",
                "project marker is not a Companion 1.0.1 project",
            )
        run_id = value.get("current_run_id")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise CompanionProjectError(
                "project_state_invalid", "project run ID is invalid"
            )
        return value

    def _write_project(self, run_id: str | None) -> None:
        _atomic_json(
            self.marker,
            {"schema_version": PROJECT_SCHEMA, "current_run_id": run_id},
        )

    def _diagnostics_path(self, run_id: str) -> Path:
        if (
            not run_id
            or "/" in run_id
            or "\\" in run_id
            or run_id in {".", ".."}
        ):
            raise ValueError("run_id must be a local identifier")
        return self.runtime_root / "diagnostics" / f"{run_id}.json"


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompanionProjectError(
            "project_state_invalid", f"{description} is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise CompanionProjectError(
            "project_state_invalid", f"{description} must be an object"
        )
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


__all__ = [
    "CURRENT_SCHEMA",
    "DIAGNOSTICS_SCHEMA",
    "PROJECT_SCHEMA",
    "CompanionProjectError",
    "CompanionProjectPaths",
]
