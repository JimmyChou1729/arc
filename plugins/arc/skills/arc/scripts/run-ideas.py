from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from arc_jobs import RunEngine, RunRepository, RunSnapshot, RunSpec
from arc_llm import LLMTaskService
from arc_proposer_reviewer import (
    BatchProjectionIntegrityError,
    ProposerReviewerHandler,
    ProposerReviewerService,
    ExecutionOptions,
    inspect_batch,
    read_batch_trace,
)
from arc_proposer_reviewer.protocol import encode_batch_request

from _arc_workflows.evidence import ArcPaperEvidenceResolver
from _arc_workflows.ideas_config import (
    IdeasConfig,
    load_ideas_config,
)
from _arc_workflows.ideas_policy import (
    concurrency_warning,
    max_concurrent_loops,
    model_tier_warnings,
)
from _arc_workflows.ideas_progress import (
    combined_progress_callback,
    emit_progress,
    foreground_progress_callback,
    progress_sidechannel_callback,
)
from _arc_workflows.ideas_result import (
    dry_run_result,
    not_started_result,
    observed_result,
)
from _arc_workflows.ideas_templates import (
    batch_request,
    caller_context_warnings,
    materialize_ideas,
    read_json,
)


class BatchExecutor(Protocol):
    """Typed seam for durable batch execution in workflow tests and hosts."""

    def __call__(
        self,
        repository: RunRepository,
        spec: RunSpec,
        handler: ProposerReviewerHandler,
    ) -> RunSnapshot: ...


def run_ideas(
    config: IdeasConfig | Mapping[str, Any],
    *,
    executor: BatchExecutor | None = None,
    llm_service: LLMTaskService | None = None,
    evidence_resolver: ArcPaperEvidenceResolver | None = None,
    base_env: Mapping[str, str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Materialize and run one typed proposer-reviewer ideas batch."""

    ideas_config = (
        config if isinstance(config, IdeasConfig) else load_ideas_config(config)
    )
    ideas = materialize_ideas(ideas_config)
    request = batch_request(ideas_config, ideas)
    max_concurrent = max_concurrent_loops(len(ideas))
    warnings = [
        concurrency_warning(
            ideas_config,
            len(ideas),
            max_concurrent=max_concurrent,
            request=request,
        ),
        *ideas_config.routing_warnings,
        *model_tier_warnings(request),
        *caller_context_warnings(ideas),
    ]
    repository = RunRepository(ideas_config.run_dir)

    if dry_run:
        return dry_run_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
        )
    if stop_check is not None and stop_check():
        return not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="stopped",
        )

    resolver = evidence_resolver or ArcPaperEvidenceResolver()
    handler = ProposerReviewerHandler(
        ProposerReviewerService(llm_service or LLMTaskService()),
        options=ExecutionOptions(
            max_concurrent_loops=max_concurrent,
            max_concurrent_workers=1,
            interaction_resolver=resolver,
        ),
    )
    spec = RunSpec(
        ideas_config.run_id,
        handler.name,
        encode_batch_request(request),
    )
    effective_progress = combined_progress_callback(
        progress_callback,
        progress_sidechannel_callback(base_env),
    )
    emit_progress(
        effective_progress,
        {"event": "ideas_batch_started", "run_id": ideas_config.run_id},
    )
    try:
        snapshot = (executor or _execute_batch)(repository, spec, handler)
    except Exception as exc:
        warnings.append(
            f"ideas_batch_execution_failed: {type(exc).__name__}"
        )
        return not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="failed",
        )

    try:
        inspection = inspect_batch(repository, snapshot.run_id)
    except Exception as exc:
        warnings.append(
            f"ideas_batch_inspection_failed: {type(exc).__name__}"
        )
        return not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status=snapshot.status.value,
        )

    try:
        trace = read_batch_trace(repository, snapshot.run_id)
    except BatchProjectionIntegrityError:
        trace = None
        warnings.append(
            "committed_trace_unavailable: committed artifacts could not be verified"
        )
    emit_progress(
        effective_progress,
        {
            "event": "ideas_batch_finished",
            "run_id": ideas_config.run_id,
            "status": inspection.run_lifecycle,
        },
    )
    return observed_result(
        ideas_config,
        repository=repository,
        request=request,
        ideas=ideas,
        warnings=warnings,
        max_concurrent=max_concurrent,
        inspection=inspection,
        trace=trace,
        evidence_resolver=resolver,
    )


def _execute_batch(
    repository: RunRepository,
    spec: RunSpec,
    handler: ProposerReviewerHandler,
) -> RunSnapshot:
    return RunEngine(repository).execute(spec, handler)


def _read_config_file(path: str) -> dict[str, Any]:
    return read_json(Path(path))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARC ideas workflow helper")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stop_event = threading.Event()
    installed_handlers: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            installed_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass
    try:
        result = run_ideas(
            _read_config_file(args.config),
            dry_run=args.dry_run,
            progress_callback=foreground_progress_callback(),
            stop_check=stop_event.is_set,
        )
    finally:
        for signum, handler in installed_handlers.items():
            signal.signal(signum, handler)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for warning in result.get("warnings", []):
            print(warning)
        print(result["status"])
        table = result.get("round_score_table", {}).get("markdown")
        if table:
            print(table)
    return 1 if result.get("status") in {"failed", "stopped", "paused"} else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
