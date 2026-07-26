#!/usr/bin/env python3
"""Rank the best scored round from each ARC ideas loop."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from _arc_workflows.ideas_ranking import rank_run
from _arc_workflows.ideas_report import markdown_table
from _arc_workflows.ideas_delivery import publish_ideas_pdf


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
    parser.add_argument(
        "--mode",
        choices=["formal", "partial"],
        default="formal",
        help=(
            "formal ranks succeeded loops only; partial creates a non-formal "
            "provisional report from complete committed rounds"
        ),
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
    payload = rank_run(run_root, args.run_id, mode=args.mode)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(markdown_table(payload))
    else:
        if project is None:
            parser.error("--format pdf requires --project-dir")
        print(
            json.dumps(
                publish_ideas_pdf(
                    project_dir=project,
                    run_id=args.run_id,
                    payload=payload,
                    mode=args.mode,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
