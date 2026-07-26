"""Protocol-only command line interface for durable ARC domain builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from arc_jobs import (
    ArcJobsError,
    CommandError,
    CommandResult,
    CommandRun,
    CommandStatus,
    ImmutableArtifactStore,
    ProgressEvent,
    RunRepository,
    RunStatus,
    command_result_from_snapshot,
    command_result_json,
    decode_artifact_ref,
    encode_progress_event,
    run_control_main,
    snapshot_data,
)
from arc_llm import (
    HostAuthority,
    InvalidRequestError,
    LLMExecutionOptions,
    ModelSelection,
    decode_resume_input,
)
from arc_paper import ArcPaperService

from . import (
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
    DomainBuildRequest,
    DomainBuildRunner,
    decode_domain_build_policy,
    decode_domain_build_result,
    encode_domain_build_policy,
)
from .build import validate_domain_build_workers
from .catalog import DomainPublicationError, publish_domain_result, read_domain_catalog
from .fetch import DomainPaperAccess
from .paths import DomainPaths
from .progress import project_domain_progress


class _UsageError(ValueError):
    """An invalid invocation that should use the command's usage exit code."""


class _HelpRequested(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise _HelpRequested
        super().exit(status, message)


def _parser() -> _Parser:
    parser = _Parser(
        prog="arc-domain",
        description=(
            "Build, inspect, and publish durable research-domain evidence "
            "from a seed paper."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_project_dir(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--project-dir",
            required=True,
            help="project directory; durable domain state is stored in .arc/domain",
        )

    build = commands.add_parser(
        "build",
        help="build a domain from a seed paper",
        description="Build and publish a durable research domain from one seed paper.",
    )
    build.add_argument("seed_paper", help="seed paper identifier")
    build.add_argument("--intent", default="", help="research intent used to focus the build")
    build.add_argument(
        "--policy",
        help="inline complete domain-build policy JSON object",
    )
    build.add_argument("--recent-window-days", type=int, help="override the recent-paper window")
    build.add_argument("--citer-pool-limit", type=int, help="override the citer candidate limit")
    build.add_argument("--ranked-paper-limit", type=int, help="override the ranked-paper limit")
    build.add_argument("--graph-node-limit", type=int, help="override the graph node limit")
    build.add_argument(
        "--foundation-mode",
        choices=("infer-from-seed", "fixed-seed"),
        help="foundation-paper selection strategy",
    )
    build.add_argument(
        "--citer-selection-mode",
        choices=("representative-plus-recent", "strict-window"),
        help="citer selection strategy",
    )
    build.add_argument("--llm-provider", default="auto", help="LLM provider (default: auto)")
    build.add_argument("--model", help="provider-specific model name")
    build.add_argument(
        "--model-tier",
        choices=("low", "medium", "high", "xhigh"),
        default="medium",
        help="model reasoning tier (default: medium)",
    )
    build.add_argument("--workers", type=int, default=8, help="parallel workers (default: 8)")
    _host_authority_argument(build)
    add_project_dir(build)
    build.add_argument(
        "--paper-cache-root",
        help="override the shared arc-paper cache directory",
    )
    build.add_argument("--run-id", help="explicit durable run identifier")

    resume = commands.add_parser(
        "resume",
        help="resume a paused, interrupted, or failed domain build",
        description="Resume a paused, interrupted, or failed domain build.",
    )
    resume.add_argument("run_id", help="durable run identifier")
    resume.add_argument("--input", help="ResumeInput JSON object")
    resume.add_argument("--workers", type=int, default=8, help="parallel workers (default: 8)")
    _host_authority_argument(resume)
    add_project_dir(resume)
    resume.add_argument(
        "--paper-cache-root",
        help="override the shared arc-paper cache directory",
    )

    status = commands.add_parser(
        "status",
        help="inspect a build by run or domain ID",
        description="Inspect the latest state of a domain build.",
    )
    selectors = status.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--run-id", help="durable run identifier")
    selectors.add_argument("--domain-id", help="published domain identifier")
    add_project_dir(status)

    query_commands = {
        "get-summary": "read the active published domain summary",
        "get-graph": "read the active published domain graph",
    }
    for name, summary in query_commands.items():
        command = commands.add_parser(name, help=summary, description=summary.capitalize() + ".")
        command.add_argument("--domain-id", required=True, help="published domain identifier")
        add_project_dir(command)

    stop = commands.add_parser(
        "stop",
        help="request a durable build stop",
        description="Request a cooperative stop for a durable domain build.",
    )
    stop.add_argument("run_id", help="durable run identifier")
    add_project_dir(stop)
    stop.add_argument("--reason", help="human-readable stop reason")

    validate = commands.add_parser(
        "validate",
        help="validate durable build state",
        description="Validate the stored artifacts and state for a domain build.",
    )
    validate.add_argument("run_id", help="durable run identifier")
    add_project_dir(validate)
    return parser


def _emit(result: CommandResult, *, exit_code: int) -> int:
    sys.stdout.write(command_result_json(result) + "\n")
    return exit_code


def _stderr_event_sink(document: Mapping[str, Any]) -> None:
    progress = ProgressEvent(
        str(document["run_id"]),
        int(document["sequence"]),
        str(document["event"]),
        dict(document["data"]),
        str(document["emitted_at"]),
    )
    sys.stderr.write(
        json.dumps(
            encode_progress_event(progress),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stderr.flush()


def _paths(project_dir: str) -> DomainPaths:
    return DomainPaths.for_project(project_dir)


def _paper_access(paper_cache_root: str | None) -> DomainPaperAccess:
    return DomainPaperAccess(ArcPaperService(cache_root=paper_cache_root))


def _host_authority_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host-authority",
        choices=tuple(item.value for item in HostAuthority),
        default=HostAuthority.UNKNOWN.value,
        help="host permission attestation; unrestricted must be explicit",
    )


def _llm_options(args: argparse.Namespace) -> LLMExecutionOptions:
    return LLMExecutionOptions(host_authority=HostAuthority(args.host_authority))


def _repository(paths: DomainPaths) -> RunRepository:
    return RunRepository(paths.root)


def _validated_workers(value: object) -> int:
    try:
        return validate_domain_build_workers(value)
    except ValueError as exc:
        raise _UsageError(str(exc)) from exc


def _request_from_args(args: argparse.Namespace) -> DomainBuildRequest:
    if args.policy is None:
        policy_document: dict[str, Any] = {
            "schema_version": DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
            "as_of_date": datetime.now(timezone.utc).date().isoformat(),
            "recent_window_days": 365,
            "citer_pool_limit": 1000,
            "ranked_paper_limit": 50,
            "graph_node_limit": 90,
            "foundation_mode": "infer_from_seed",
            "citer_selection_mode": "representative_plus_recent",
        }
    else:
        try:
            policy_document = json.loads(args.policy)
        except json.JSONDecodeError as exc:
            raise _UsageError(f"--policy must be a JSON object: {exc.msg}") from exc
        if not isinstance(policy_document, dict):
            raise _UsageError("--policy must be a JSON object")
    try:
        policy = decode_domain_build_policy(policy_document)
        overrides = {
            name: value
            for name, value in {
                "recent_window_days": args.recent_window_days,
                "citer_pool_limit": args.citer_pool_limit,
                "ranked_paper_limit": args.ranked_paper_limit,
                "graph_node_limit": args.graph_node_limit,
            }.items()
            if value is not None
        }
        mode_overrides = {
            "foundation_mode": (
                args.foundation_mode.replace("-", "_")
                if args.foundation_mode is not None
                else None
            ),
            "citer_selection_mode": (
                args.citer_selection_mode.replace("-", "_")
                if args.citer_selection_mode is not None
                else None
            ),
        }
        mode_overrides = {
            name: value
            for name, value in mode_overrides.items()
            if value is not None
        }
        if overrides or mode_overrides:
            policy_document = encode_domain_build_policy(policy)
            policy_document.update(overrides)
            policy_document.update(mode_overrides)
            policy = decode_domain_build_policy(policy_document)
        return DomainBuildRequest(
            seed_paper=args.seed_paper,
            intent=args.intent,
            policy=policy,
            model=ModelSelection(
                provider=args.llm_provider,
                model=args.model,
                tier=args.model_tier,
            ),
        )
    except _UsageError:
        raise
    except (InvalidRequestError, ValueError) as exc:
        raise _UsageError(str(exc)) from exc


def _decode_succeeded_result(repository: RunRepository, run_id: str):
    snapshot = repository.inspect(run_id).snapshot
    if snapshot.status is not RunStatus.SUCCEEDED or snapshot.result_ref is None:
        raise DomainPublicationError(
            f"run {run_id!r} has no succeeded domain-build result to publish"
        )
    artifacts = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    try:
        document = json.loads(artifacts.read_bytes(snapshot.result_ref).decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("result artifact must contain a JSON object")
        return decode_domain_build_result(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DomainPublicationError(
            "succeeded run result artifact is not a valid DomainBuildResult"
        ) from exc


def _published_result(repository: RunRepository, paths: DomainPaths, snapshot) -> CommandResult:
    base = command_result_from_snapshot(snapshot)
    if base.status is not CommandStatus.COMPLETED:
        return base
    try:
        result = _decode_succeeded_result(repository, snapshot.run_id)
        publication = publish_domain_result(
            repository,
            paths,
            run_id=snapshot.run_id,
            result=result,
        )
    except (DomainPublicationError, OSError, ValueError) as exc:
        return CommandResult(
            CommandStatus.FAILED,
            run=CommandRun(snapshot.run_id, snapshot.revision),
            data={"run": snapshot_data(snapshot)},
            error=CommandError("domain_publication_failed", str(exc)),
        )
    return CommandResult(
        CommandStatus.COMPLETED,
        run=base.run,
        data={
            "run": snapshot_data(snapshot),
            "domain": {
                "id": publication.domain_id,
                "active": publication.active,
            },
        },
        artifacts=base.artifacts,
        warnings=base.warnings,
    )


def _build(args: argparse.Namespace) -> tuple[CommandResult, int]:
    max_workers = _validated_workers(args.workers)
    request = _request_from_args(args)
    paths = _paths(args.project_dir)
    repository = _repository(paths)
    snapshot = DomainBuildRunner(repository).execute(
        request,
        run_id=args.run_id,
        paper_access=_paper_access(args.paper_cache_root),
        llm=_llm_options(args),
        max_workers=max_workers,
        event_sink=_stderr_event_sink,
    )
    result = _published_result(repository, paths, snapshot)
    return result, _exit_code(result)


def _resume(args: argparse.Namespace) -> tuple[CommandResult, int]:
    max_workers = _validated_workers(args.workers)
    paths = _paths(args.project_dir)
    repository = _repository(paths)
    snapshot = DomainBuildRunner(repository).resume(
        args.run_id,
        input=_resume_input(args.input),
        paper_access=_paper_access(args.paper_cache_root),
        llm=_llm_options(args),
        max_workers=max_workers,
        event_sink=_stderr_event_sink,
    )
    result = _published_result(repository, paths, snapshot)
    return result, _exit_code(result)


def _status(args: argparse.Namespace) -> CommandResult:
    paths = _paths(args.project_dir)
    repository = _repository(paths)
    domain_id = args.domain_id
    if args.run_id is not None:
        snapshot = repository.inspect(args.run_id).snapshot
    else:
        catalog = read_domain_catalog(paths, domain_id=domain_id)
        if catalog is None or catalog.latest is None:
            raise DomainPublicationError(
                f"domain {domain_id!r} has no registered build run"
            )
        snapshot = repository.inspect(catalog.latest).snapshot
    base = command_result_from_snapshot(snapshot, query=True)
    data = dict(base.data)
    data["progress"] = project_domain_progress(repository, snapshot)
    if domain_id is not None:
        data["domain"] = {
            "id": domain_id,
            "latest": catalog.latest if catalog is not None else None,
            "active": catalog.active if catalog is not None else None,
        }
    return CommandResult(
        base.status,
        run=base.run,
        data=data,
        artifacts=base.artifacts,
        warnings=base.warnings,
        error=base.error,
        resume=base.resume,
    )


def _active_export(paths: DomainPaths, domain_id: str, filename: str) -> tuple[str, Any]:
    catalog = read_domain_catalog(paths, domain_id=domain_id)
    if catalog is None or catalog.active is None:
        raise DomainPublicationError(
            f"domain {domain_id!r} has no active published export"
        )
    generation = paths.export_generation(domain_id, catalog.active)
    manifest_path = generation / "export-manifest.json"
    artifact_path = generation / filename
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != "arc.domain.export_manifest.v1"
            or manifest.get("run_id") != catalog.active
            or not isinstance(files, dict)
            or filename not in files
        ):
            raise ValueError("active export manifest does not describe the requested artifact")
        artifact = decode_artifact_ref(files[filename])
        content = artifact_path.read_bytes()
        if (
            len(content) != artifact.digest.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.digest.value
        ):
            raise ValueError("active export does not match its manifest digest")
        return catalog.active, json.loads(content.decode("utf-8"))
    except (ArcJobsError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DomainPublicationError(
            f"active {filename} export is unavailable for domain {domain_id!r}"
        ) from exc


def _get(args: argparse.Namespace, *, filename: str, data_name: str) -> CommandResult:
    paths = _paths(args.project_dir)
    run_id, document = _active_export(paths, args.domain_id, filename)
    return CommandResult(
        CommandStatus.COMPLETED,
        data={"domain": {"id": args.domain_id, "active": run_id}, data_name: document},
    )


def _resume_input(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _UsageError(f"--input must be a ResumeInput JSON object: {exc.msg}") from exc
    if not isinstance(document, dict):
        raise _UsageError("--input must be a ResumeInput JSON object")
    try:
        decode_resume_input(document)
    except Exception as exc:
        raise _UsageError(f"--input is not a valid ResumeInput: {exc}") from exc
    return document


def _run_control(args: argparse.Namespace, *, command: str) -> int:
    paths = _paths(args.project_dir)
    argv = [command, "--run-root", str(paths.root), "--run-id", args.run_id]
    if command == "stop" and args.reason is not None:
        argv.extend(["--reason", args.reason])
    # arc-jobs owns these controls and writes the sole command envelope itself.
    return run_control_main(argv)


def _exit_code(result: CommandResult) -> int:
    return 1 if result.status is CommandStatus.FAILED else 0


def _dispatch(args: argparse.Namespace) -> tuple[CommandResult, int] | int:
    if args.command == "build":
        return _build(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "status":
        return _status(args), 0
    if args.command == "get-summary":
        return _get(args, filename="summary.json", data_name="summary"), 0
    if args.command == "get-graph":
        return _get(args, filename="graph.json", data_name="graph"), 0
    if args.command in {"stop", "validate"}:
        return _run_control(args, command=args.command)
    raise AssertionError(args.command)


def _help_command(arguments: list[str]) -> str:
    commands = {
        "build",
        "resume",
        "status",
        "get-summary",
        "get-graph",
        "stop",
        "validate",
    }
    command = arguments[0] if arguments and arguments[0] in commands else None
    return " ".join(
        part for part in ("arc-domain", command, "--help") if part is not None
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    try:
        args = parser.parse_args(arguments)
        dispatched = _dispatch(args)
        if isinstance(dispatched, int):
            return dispatched
        result, exit_code = dispatched
        return _emit(result, exit_code=exit_code)
    except _HelpRequested:
        return 0
    except _UsageError as exc:
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError(
                    "invalid_request",
                    str(exc),
                    {"help_command": _help_command(arguments)},
                ),
            ),
            exit_code=2,
        )
    except (ArcJobsError, DomainPublicationError, OSError, ValueError) as exc:
        code = {
            "RunNotFoundError": "run_not_found",
            "RunBusyError": "run_busy",
            "IdempotencyConflictError": "idempotency_conflict",
        }.get(type(exc).__name__, "domain_command_failed")
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError(code, str(exc)),
            ),
            exit_code=1,
        )
    except Exception as exc:
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError(
                    "internal_error", f"{type(exc).__name__}: {str(exc)[:300]}"
                ),
            ),
            exit_code=1,
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
