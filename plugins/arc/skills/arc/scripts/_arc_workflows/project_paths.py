"""Project path policy shared by ARC Skill command adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any


HOST_INTERNAL_PARTS = {".claude", ".codex"}
WINDOWS_RESERVED_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')


class ProjectDirError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_project_dir(
    *,
    name: str,
    run_root: str | Path,
) -> dict[str, Any]:
    project_dir_name = _validate_name(name)
    try:
        root = Path(run_root).expanduser().resolve()
    except OSError as exc:
        raise ProjectDirError(
            "invalid_run_root",
            f"Run root cannot be resolved: {run_root!s}.",
        ) from exc
    _validate_run_root(root)
    return {
        "run_root": str(root),
        "project_dir_name": project_dir_name,
        "project_dir": str(root / project_dir_name),
    }


def _validate_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise ProjectDirError(
            "invalid_project_dir_name",
            "Project directory name is empty.",
        )
    if (
        name in {".", ".."}
        or name.startswith(".")
        or Path(name).is_absolute()
        or "/" in name
        or "\\" in name
        or any(character in WINDOWS_INVALID_CHARACTERS for character in name)
        or any(ord(character) < 32 for character in name)
        or name.endswith(".")
    ):
        detail = (
            " Nested `arc-output/<name>` directories are not allowed."
            if "arc-output" in name.lower()
            else ""
        )
        raise ProjectDirError(
            "invalid_project_dir_name",
            "Project directory name must be a single safe stem, "
            f"got: {raw!r}.{detail}",
        )
    if name.lower() == "arc-output":
        raise ProjectDirError(
            "invalid_project_dir_name",
            "`arc-output` is not a project directory name; "
            "use the ARC safe-dir stem directly.",
        )
    windows_stem = name.split(".", maxsplit=1)[0].casefold()
    if windows_stem in WINDOWS_RESERVED_STEMS:
        raise ProjectDirError(
            "invalid_project_dir_name",
            f"Project directory name is reserved on supported platforms: {raw!r}.",
        )
    return name


def _validate_run_root(root: Path) -> None:
    if not root.exists():
        raise ProjectDirError(
            "invalid_run_root",
            f"Run root does not exist: {root}",
        )
    if not root.is_dir():
        raise ProjectDirError(
            "invalid_run_root",
            f"Run root is not a directory: {root}",
        )
    if root.parent == root:
        raise ProjectDirError(
            "invalid_run_root",
            f"Filesystem root cannot be used as an ARC run root: {root}",
        )
    parts = {part.casefold() for part in root.parts}
    if parts.intersection(HOST_INTERNAL_PARTS):
        raise ProjectDirError(
            "invalid_run_root",
            "Run root must be the user's launch directory, not host-internal "
            f"storage such as .claude or .codex: {root}",
        )
    if root.name.casefold() == "arc-output":
        raise ProjectDirError(
            "invalid_run_root",
            f"Run root must not be an inserted arc-output directory: {root}",
        )


__all__ = ["ProjectDirError", "resolve_project_dir"]
