#!/usr/bin/env python3
"""CLI adapter for ARC domain-manifest construction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _arc_workflows._arc_script_bootstrap import (
    bootstrap_arc_pythonpath,
)

bootstrap_arc_pythonpath()

from _arc_workflows.domain_manifest_inputs import (
    ManifestError,
)
from _arc_workflows.domain_manifest_publish import (
    write_domain_manifest,
)
from _arc_workflows.workflow_io import (
    read_json_object,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a project-local ARC domain manifest."
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        destination = write_domain_manifest(
            Path(args.project_dir),
            Path(args.output) if args.output else None,
        )
        payload = read_json_object(destination)
        result = {
            "status": "completed",
            "manifest_path": str(destination),
            "package_count": payload["package_count"],
            "field_count": payload["field_count"],
            "duplicate_count": len(payload["duplicates"]),
        }
        print(
            json.dumps(result, ensure_ascii=False)
            if args.json
            else str(destination)
        )
        return 0
    except ManifestError as exc:
        result = {"status": "failed", "error": str(exc)}
        print(
            json.dumps(result, ensure_ascii=False)
            if args.json
            else f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
