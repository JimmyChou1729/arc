#!/usr/bin/env python3
"""Rank the best scored round from each ARC ideas loop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from _arc_workflows.ideas_ranking import rank_run
from _arc_workflows.ideas_report import markdown_table
from _arc_workflows.report_delivery import (
    publish_visible_copy,
    render_markdown_pdf,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Select each loop's highest-marked round and rank "
            "task-to-be-planned candidates."
        )
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        help="explicit durable ARC run repository root",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="ARC project directory; uses <project>/.arc/ideas",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="durable proposer-reviewer run ID",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown", "pdf"],
        default="markdown",
    )
    args = parser.parse_args()

    if (args.run_root is None) == (args.project_dir is None):
        parser.error("exactly one of --run-root or --project-dir is required")
    project = args.project_dir.expanduser().resolve() if args.project_dir else None
    run_root = (
        project / ".arc" / "ideas"
        if project is not None
        else args.run_root.expanduser().resolve()
    )
    payload = rank_run(run_root, args.run_id)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(markdown_table(payload))
    else:
        if project is None:
            parser.error("--format pdf requires --project-dir")
        source = project / ".arc" / "ideas" / "reports" / args.run_id / "ranked-ideas.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(markdown_table(payload), encoding="utf-8")
        archived = render_markdown_pdf(
            project_dir=project,
            source=source,
            output=project / "ideas" / args.run_id / "ranked-ideas.pdf",
        )
        latest = publish_visible_copy(
            project_dir=project,
            source=archived,
            output=project / "ranked-ideas.pdf",
        )
        print(
            json.dumps(
                {
                    "schema_version": "arc.ideas.delivery.v1",
                    "format": "pdf",
                    "artifacts": [str(archived), str(latest)],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
