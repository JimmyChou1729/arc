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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Select each loop's highest-marked round and rank "
            "task-to-be-planned candidates."
        )
    )
    parser.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="durable ARC run repository root",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="durable proposer-reviewer run ID",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
    )
    args = parser.parse_args()

    payload = rank_run(args.run_root, args.run_id)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(markdown_table(payload))


if __name__ == "__main__":
    main()
