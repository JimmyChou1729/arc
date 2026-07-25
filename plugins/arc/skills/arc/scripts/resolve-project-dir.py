#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _arc_workflows.project_paths import (
    ProjectDirError,
    resolve_project_dir,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve an ARC workflow project directory")
    parser.add_argument("--name", required=True, help="safe project directory stem")
    parser.add_argument("--run-root", default=".", help="workflow launch directory, usually pwd -P")
    parser.add_argument("--json", action="store_true", help="emit a JSON result envelope")
    args = parser.parse_args(argv)

    try:
        payload = resolve_project_dir(name=args.name, run_root=args.run_root)
    except ProjectDirError as exc:
        return _emit(
            {"ok": False, "data": None, "errors": [{"code": exc.code, "message": str(exc)}], "meta": {}},
            json_output=args.json,
            status=2,
        )

    return _emit({"ok": True, "data": payload, "errors": [], "meta": {}}, json_output=args.json, status=0)


def _emit(payload: dict[str, Any], *, json_output: bool, status: int) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif payload["ok"]:
        print(payload["data"]["project_dir"])
    else:
        print(payload["errors"][0]["message"], file=sys.stderr)
    return status

if __name__ == "__main__":
    raise SystemExit(main())
