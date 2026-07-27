#!/usr/bin/env python3
"""Render one project Markdown report to a visible PDF delivery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from _arc_workflows.report_delivery import (
    ReportDeliveryContractError,
    ReportDeliveryUnavailable,
    render_markdown_pdf,
)


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
    except ReportDeliveryContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ReportDeliveryUnavailable as exc:
        print(
            json.dumps(
                {
                    "schema_version": "arc.report_delivery.v2",
                    "delivery_status": "unavailable",
                    "format": "pdf",
                    "requested_artifacts": [
                        str(Path(args.output).expanduser().resolve())
                    ],
                    "artifacts": [],
                    "warnings": [
                        {
                            "code": "pdf_render_unavailable",
                            "message": str(exc),
                        }
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "schema_version": "arc.report_delivery.v2",
                "delivery_status": "published",
                "format": "pdf",
                "requested_artifacts": [str(output)],
                "artifacts": [str(output)],
                "warnings": [],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
