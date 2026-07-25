from __future__ import annotations

import argparse
import json
from pathlib import Path

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from _arc_workflows.calculate_config import ConfigError, _read_json
from _arc_workflows.calculate_runner import run_calculation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ARC calculate workflow runner"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        payload = _read_json(Path(args.config))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        result = run_calculation(payload, dry_run=args.dry_run)
    except ConfigError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
