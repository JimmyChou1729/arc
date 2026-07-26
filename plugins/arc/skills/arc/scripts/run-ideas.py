from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath

bootstrap_arc_pythonpath()

from arc_jobs import RunEngine, RunRepository, RunSnapshot, RunSpec
from arc_llm import HostAuthority, LLMExecutionOptions, LLMTaskService
from arc_proposer_reviewer import (
    BatchInputPayload,
    BatchRunner,
    ProposerReviewerHandler,
    ProposerReviewerService,
    ExecutionOptions,
    inspect_batch,
    read_batch_trace,
)

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
    foreground_progress_callback,
    IdeasProgressEmitter,
    IdeasStopController,
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
    workspace_input_paths,
)


class BatchExecutor(Protocol):
    """Typed seam for durable batch execution in workflow tests and hosts."""

    def __call__(
        self,
        repository: RunRepository,
        spec: RunSpec,
        handler: ProposerReviewerHandler,
    ) -> RunSnapshot: ...


_FOREGROUND_PROGRESS_EVENTS = frozenset(
    {
        "proposer_reviewer_loop_started",
        "proposer_reviewer_round_started",
        "proposer_reviewer_worker_started",
        "proposer_reviewer_worker_finished",
        "proposer_reviewer_round_committed",
        "proposer_reviewer_loop_finished",
    }
)


def run_ideas(
    config: IdeasConfig | Mapping[str, Any],
    *,
    executor: BatchExecutor | None = None,
    llm_service: LLMTaskService | None = None,
    llm_options: LLMExecutionOptions = LLMExecutionOptions(),
    base_env: Mapping[str, str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_controller: IdeasStopController | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Materialize and run one typed proposer-reviewer ideas batch."""

    ideas_config = (
        config if isinstance(config, IdeasConfig) else load_ideas_config(config)
    )
    ideas = materialize_ideas(ideas_config)
    request = batch_request(ideas_config, ideas)
    input_paths = workspace_input_paths(ideas)
    input_metadata = _workspace_input_metadata(input_paths)
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
    controller = stop_controller or IdeasStopController()

    if dry_run:
        return dry_run_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            workspace_inputs=input_metadata,
        )
    if controller.is_requested():
        return not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="paused",
            workspace_inputs=input_metadata,
        )

    repository = RunRepository(ideas_config.run_dir)

    effective_progress = combined_progress_callback(
        progress_callback,
        progress_sidechannel_callback(base_env),
    )
    progress = IdeasProgressEmitter(
        effective_progress,
        run_id=ideas_config.run_id,
    )

    def package_progress(event: dict[str, Any]) -> None:
        if event.get("event") in _FOREGROUND_PROGRESS_EVENTS:
            progress.emit(event)

    handler = ProposerReviewerHandler(
        ProposerReviewerService(llm_service or LLMTaskService()),
        options=ExecutionOptions(
            max_concurrent_loops=max_concurrent,
            max_concurrent_workers=1,
            llm=llm_options,
            progress_callback=package_progress,
        ),
    )
    progress.emit({"event": "ideas_batch_started"})
    snapshot = None
    execution_error: Exception | None = None
    try:
        runner = BatchRunner()
        runner.prepare(
            request,
            repository,
            ideas_config.run_id,
            input_payloads=_workspace_input_payloads(input_paths),
        )
        request = runner.read_request(repository, ideas_config.run_id)
        spec = repository.read_spec(ideas_config.run_id)
        with controller.bridge(
            lambda: runner.stop(
                repository,
                ideas_config.run_id,
                reason="run-ideas received a process signal",
            )
        ):
            try:
                snapshot = (executor or _execute_batch)(
                    repository,
                    spec,
                    handler,
                )
            except Exception as exc:
                execution_error = exc
    except Exception as exc:
        execution_error = exc
    for error_type in controller.errors:
        warnings.append(f"ideas_stop_request_failed: {error_type}")
    _append_progress_errors(warnings, progress)
    if execution_error is not None:
        warnings.append(
            f"ideas_batch_execution_failed: {type(execution_error).__name__}"
        )
        result = _recover_execution_failure(
            ideas_config,
            repository=repository,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            error=execution_error,
        )
        progress.emit(
            {"event": "ideas_batch_finished", "status": result["status"]}
        )
        _append_progress_errors(warnings, progress)
        return result
    assert snapshot is not None

    try:
        inspection = inspect_batch(repository, snapshot.run_id)
    except Exception as exc:
        warnings.append(
            f"ideas_batch_inspection_failed: {type(exc).__name__}"
        )
        result = not_started_result(
            ideas_config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="failed",
        )
        for loop in result["loops"]:
            loop["lifecycle"] = "unknown"
            loop["phase"] = "inspection_failed"
        result["inspection_error"] = {
            "code": "ideas_batch_inspection_failed",
            "exception_type": type(exc).__name__,
            "message": "proposer-reviewer batch inspection failed",
        }
        progress.emit(
            {"event": "ideas_batch_finished", "status": result["status"]}
        )
        _append_progress_errors(warnings, progress)
        return result

    try:
        trace = read_batch_trace(repository, snapshot.run_id)
    except Exception as exc:
        trace = None
        warnings.append(
            f"committed_trace_unavailable: {type(exc).__name__}"
        )
    result = observed_result(
        ideas_config,
        repository=repository,
        request=request,
        ideas=ideas,
        warnings=warnings,
        max_concurrent=max_concurrent,
        inspection=inspection,
        trace=trace,
    )
    progress.emit(
        {"event": "ideas_batch_finished", "status": result["status"]}
    )
    _append_progress_errors(warnings, progress)
    return result


def _execute_batch(
    repository: RunRepository,
    spec: RunSpec,
    handler: ProposerReviewerHandler,
) -> RunSnapshot:
    return RunEngine(repository).execute(spec, handler)


def _recover_execution_failure(
    config: IdeasConfig,
    *,
    repository: RunRepository,
    request: Any,
    ideas: list[Any],
    warnings: list[str],
    max_concurrent: int,
    error: Exception,
) -> dict[str, Any]:
    try:
        inspection = inspect_batch(repository, config.run_id)
    except Exception as inspection_error:
        warnings.append(
            "ideas_batch_inspection_failed: "
            f"{type(inspection_error).__name__}"
        )
        result = not_started_result(
            config,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            status="failed",
        )
        for loop in result["loops"]:
            loop["lifecycle"] = "unknown"
            loop["phase"] = "inspection_failed"
        result["inspection_error"] = {
            "code": "ideas_batch_inspection_failed",
            "exception_type": type(inspection_error).__name__,
            "message": "proposer-reviewer batch inspection failed",
        }
    else:
        try:
            trace = read_batch_trace(repository, config.run_id)
        except Exception as trace_error:
            trace = None
            warnings.append(
                "committed_trace_unavailable: "
                f"{type(trace_error).__name__}"
            )
        result = observed_result(
            config,
            repository=repository,
            request=request,
            ideas=ideas,
            warnings=warnings,
            max_concurrent=max_concurrent,
            inspection=inspection,
            trace=trace,
        )
        result["status"] = "failed"
        result["batch"]["durable_lifecycle"] = inspection.durable_lifecycle
    result["execution_error"] = {
        "code": "ideas_batch_execution_failed",
        "exception_type": type(error).__name__,
        "message": "proposer-reviewer batch execution failed",
    }
    return result


def _read_config_file(path: str) -> dict[str, Any]:
    return read_json(Path(path))


def _workspace_input_metadata(paths: tuple[Path, ...]) -> list[dict[str, str]]:
    return [
        {"input_id": f"domain-markdown-{index:03d}", "media_type": "text/markdown"}
        for index, _path in enumerate(paths, start=1)
    ]


def _workspace_input_payloads(paths: tuple[Path, ...]) -> tuple[BatchInputPayload, ...]:
    return tuple(
        BatchInputPayload(
            input_id=f"domain-markdown-{index:03d}",
            media_type="text/markdown",
            content=path.read_bytes(),
        )
        for index, path in enumerate(paths, start=1)
    )


def _append_progress_errors(
    warnings: list[str],
    progress: IdeasProgressEmitter,
) -> None:
    for error_type in progress.errors:
        warning = f"ideas_progress_callback_failed: {error_type}"
        if warning not in warnings:
            warnings.append(warning)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARC ideas workflow helper")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--host-authority",
        choices=[authority.value for authority in HostAuthority],
        default=HostAuthority.UNKNOWN.value,
        help="explicit host authority attestation; defaults to unknown",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stop_controller = IdeasStopController()
    installed_handlers: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_controller.request()

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
            stop_controller=stop_controller,
            llm_options=LLMExecutionOptions(
                host_authority=HostAuthority(args.host_authority)
            ),
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
    return 1 if result.get("status") in {"failed", "paused"} else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
