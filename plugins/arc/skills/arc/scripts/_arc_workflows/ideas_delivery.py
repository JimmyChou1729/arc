"""Visible PDF delivery for formal and provisional ARC ideas reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from _arc_workflows.ideas_report import markdown_table
from _arc_workflows.report_delivery import (
    publish_visible_copy,
    render_markdown_pdf,
)


def publish_ideas_pdf(
    *,
    project_dir: str | Path,
    run_id: str,
    payload: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    if mode not in {"formal", "partial"}:
        raise ValueError("mode must be formal or partial")
    project = Path(project_dir).expanduser().resolve()
    basename = "ranked-ideas" if mode == "formal" else "partial-ideas"
    source = (
        project
        / ".arc"
        / "ideas"
        / "reports"
        / run_id
        / f"{basename}.md"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(markdown_table(dict(payload)), encoding="utf-8")
    archived = render_markdown_pdf(
        project_dir=project,
        source=source,
        output=project / "ideas" / run_id / f"{basename}.pdf",
    )
    latest = publish_visible_copy(
        project_dir=project,
        source=archived,
        output=project / f"{basename}.pdf",
    )
    result = {
        "schema_version": "arc.ideas.delivery.v1",
        "format": "pdf",
        "artifacts": [str(archived), str(latest)],
    }
    if mode == "partial":
        result["mode"] = "partial"
    return result


__all__ = ["publish_ideas_pdf"]
