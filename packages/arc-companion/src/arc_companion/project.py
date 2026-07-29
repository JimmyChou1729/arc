"""Small project layout and managed-path ownership for Companion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_bytes, atomic_write_json, file_lease


PROJECT_SCHEMA = "arc.companion.project.v3"
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
        if root.exists():
            if not root.is_dir():
                raise CompanionProjectError(
                    "project_path_invalid", "project path is not a directory"
                )
        root.mkdir(parents=True, exist_ok=True)
        paths = cls(root.resolve())
        if paths.marker.is_file():
            paths._read_project()
        else:
            conflict = paths._initialization_conflict()
            if conflict is not None:
                raise CompanionProjectError(
                    "project_path_conflict",
                    "managed Companion path already exists: "
                    f"{conflict}",
                )
            paths._write_project(None)
        return paths

    @classmethod
    def load(cls, value: str | Path) -> "CompanionProjectPaths":
        """Open an existing project without creating or changing files."""

        root = Path(value)
        marker = root / ".arc" / "companion" / "project.json"
        if not root.is_dir() or not marker.is_file():
            raise CompanionProjectError(
                "project_not_found",
                "Companion project does not exist",
            )
        paths = cls(root.resolve())
        paths._read_project()
        return paths

    @property
    def marker(self) -> Path:
        return self.runtime_root / "project.json"

    @property
    def runtime_root(self) -> Path:
        return self.root / ".arc" / "companion"

    @property
    def jobs_root(self) -> Path:
        return self.runtime_root / "jobs"

    @property
    def diagnostics_visual_root(self) -> Path:
        """Project-owned visual diagnostics, grouped by logical run."""

        return self.runtime_root / "diagnostics" / "visual"

    def diagnostics_visual_run_path(self, run_id: str) -> Path:
        return self.diagnostics_visual_root / _validate_run_id(run_id)

    @property
    def operator_inputs_root(self) -> Path:
        """Project-owned explicit operator inputs, grouped by logical run."""

        return self.runtime_root / "operator-inputs"

    def operator_inputs_run_path(self, run_id: str) -> Path:
        return self.operator_inputs_root / _validate_run_id(run_id)

    @property
    def delivery_html(self) -> Path:
        return self.root / "companion.html"

    @property
    def delivery_lease(self) -> Path:
        return self.runtime_root / "delivery.lock"

    def publication_workspace(self, run_id: str) -> Path:
        return self.runtime_root / "publications" / _validate_run_id(run_id)

    def publication_html(self, run_id: str) -> Path:
        return self.publication_workspace(run_id) / "companion.html"

    @property
    def current_run_id(self) -> str | None:
        return self._read_project()["current_run_id"]

    def select_run(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        with file_lease(self.delivery_lease, blocking=True):
            self._write_project(run_id)

    def promote_publication_html(self, run_id: str) -> bool:
        """Promote a verified run-specific reader only while it is selected."""

        source = self.publication_html(run_id)
        with file_lease(self.delivery_lease, blocking=True):
            if self.current_run_id != run_id:
                return False
            try:
                payload = source.read_bytes()
            except OSError as exc:
                raise CompanionProjectError(
                    "publication_html_unavailable",
                    "run-specific standalone HTML is unreadable",
                ) from exc
            atomic_write_bytes(self.delivery_html, payload)
            return True

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

    def _read_project(self) -> dict[str, Any]:
        value = _read_json(self.marker, "project marker")
        if set(value) != {"schema_version", "current_run_id"}:
            raise CompanionProjectError(
                "project_state_invalid", "project marker has invalid fields"
            )
        if value.get("schema_version") != PROJECT_SCHEMA:
            raise CompanionProjectError(
                "project_state_invalid",
                "project marker uses an unsupported schema",
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

    def _initialization_conflict(self) -> Path | None:
        """Return the first unclaimed managed path that Companion cannot use."""

        arc_root = self.root / ".arc"
        if _path_exists(arc_root) and not arc_root.is_dir():
            return arc_root
        for path in (
            self.runtime_root,
            self.delivery_html,
        ):
            if _path_exists(path):
                return path
        return None

    def _diagnostics_path(self, run_id: str) -> Path:
        return (
            self.runtime_root
            / "diagnostics"
            / f"{_validate_run_id(run_id)}.json"
        )


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


def _validate_run_id(run_id: str) -> str:
    if (
        not isinstance(run_id, str)
        or not run_id
        or "/" in run_id
        or "\\" in run_id
        or run_id in {".", ".."}
    ):
        raise ValueError("run_id must be a local identifier")
    return run_id


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _path_exists(path: Path) -> bool:
    """Treat dangling symlinks as occupied paths."""

    return path.exists() or path.is_symlink()


__all__ = [
    "DIAGNOSTICS_SCHEMA",
    "PROJECT_SCHEMA",
    "CompanionProjectError",
    "CompanionProjectPaths",
]
