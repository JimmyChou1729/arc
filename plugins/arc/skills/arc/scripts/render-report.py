#!/usr/bin/env python3
"""Render one project Markdown report to a visible PDF delivery."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

sys.dont_write_bytecode = True

from _arc_workflows.report_delivery import render_markdown_pdf


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render project-local Markdown to a visible PDF with Pandoc/XeLaTeX."
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--input", required=True, dest="source")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = render_markdown_pdf(
            project_dir=args.project_dir,
            source=args.source,
            output=args.output,
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema_version": "arc.report_delivery.v1",
                "format": "pdf",
                "path": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
