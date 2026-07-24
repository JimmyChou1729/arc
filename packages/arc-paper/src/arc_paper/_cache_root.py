from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class _RootedRepository(Protocol):
    root: Path


def resolve_cache_root(
    explicit: str | Path | None = None,
    *,
    repository: _RootedRepository | None = None,
) -> Path:
    """Resolve the package cache root with one documented precedence order."""

    if explicit is not None:
        root = Path(explicit).expanduser()
    elif repository is not None:
        root = Path(repository.root)
    elif value := os.environ.get("ARC_PAPER_CACHE"):
        root = Path(value).expanduser()
    elif value := os.environ.get("ARC_HOME"):
        root = Path(value).expanduser() / "cache" / "arc-paper"
    elif value := os.environ.get("XDG_CACHE_HOME"):
        root = Path(value).expanduser() / "arc" / "arc-paper"
    else:
        root = Path.home() / ".cache" / "arc" / "arc-paper"

    if (
        explicit is not None
        and repository is not None
        and root.resolve(strict=False)
        != Path(repository.root).resolve(strict=False)
    ):
        raise ValueError("cache root must match the injected repository root")
    return root


__all__ = ["resolve_cache_root"]
