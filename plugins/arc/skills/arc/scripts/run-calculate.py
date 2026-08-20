from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from arc_llm import ExecutionLimits, HostAuthority, LLMExecutionOptions
from _arc_workflows.calculate_config import (
    CALCULATOR_IDS,
    ConfigError,
    _read_json,
)
from _arc_workflows.calculate_runner import run_calculation


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a positive number of seconds"
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            "must be a positive number of seconds"
        )
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ARC calculate workflow runner"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-concurrent-calculators",
        type=int,
        choices=range(1, len(CALCULATOR_IDS) + 1),
        default=len(CALCULATOR_IDS),
        help=(
            "maximum calculators to run concurrently; defaults to all "
            "calculators"
        ),
    )
    parser.add_argument(
        "--idle-timeout-seconds",
        type=_positive_seconds,
        default=None,
        help=(
            "provider idle timeout in seconds; disabled by default and "
            "enforced by arc-llm when supported"
        ),
    )
    parser.add_argument(
        "--host-authority",
        choices=[authority.value for authority in HostAuthority],
        default=HostAuthority.UNKNOWN.value,
        help="explicit host authority attestation; defaults to unknown",
    )
    args = parser.parse_args(argv)

    try:
        payload = _read_json(Path(args.config))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        result = run_calculation(
            payload,
            dry_run=args.dry_run,
            max_concurrent_calculators=args.max_concurrent_calculators,
            llm_options=LLMExecutionOptions(
                limits=ExecutionLimits(
                    idle_timeout_seconds=args.idle_timeout_seconds
                ),
                host_authority=HostAuthority(args.host_authority)
            ),
        )
    except ConfigError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
