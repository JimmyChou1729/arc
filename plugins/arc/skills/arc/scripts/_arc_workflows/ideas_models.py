"""Internal data models for materialized ARC idea loops."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _arc_workflows.ideas_config import VariantConfig


@dataclass(frozen=True)
class IdeaPlan:
    idea_id: str
    variant_id: str
    idea_index: int
    loop_id: str
    variant: VariantConfig
    caller_context: dict[str, Any]
    workspace_input_paths: tuple[Path, ...]
