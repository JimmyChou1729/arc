"""Protocol-only command line interface for durable ARC domain builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any

from arc_jobs import (
    ArcJobsError,
    CommandError,
    CommandResult,
    CommandRun,
    CommandStatus,
    ImmutableArtifactStore,
    RunRepository,
    RunStatus,
    command_result_from_snapshot,
    command_result_json,
    decode_artifact_ref,
    run_control_main,
    snapshot_data,
)
from arc_llm import InvalidRequestError, ModelSelection, decode_resume_input

from . import (
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
    DomainBuildRequest,
    DomainBuildRunner,
    decode_domain_build_policy,
    decode_domain_build_result,
    encode_domain_build_policy,
)
from .catalog import DomainPublicationError, publish_domain_result, read_domain_catalog
from .paths import DomainPaths


class _UsageError(ValueError):
    """An invalid invocation that should use the command's usage exit code."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="arc-domain")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("seed_paper")
    build.add_argument("--intent", default="")
    build.add_argument("--policy")
    build.add_argument("--recent-window-days", type=int)
    build.add_argument("--citer-pool-limit", type=int)
    build.add_argument("--ranked-paper-limit", type=int)
    build.add_argument("--graph-node-limit", type=int)
    build.add_argument(
        "--foundation-mode",
        choices=("infer-from-seed", "fixed-seed"),
    )
    build.add_argument(
        "--citer-selection-mode",
        choices=("representative-plus-recent", "strict-window"),
    )
    build.add_argument("--llm-provider", default="auto")
    build.add_argument("--model")
    build.add_argument(
        "--model-tier", choices=("low", "medium", "high", "xhigh"), default="medium"
    )
    build.add_argument("--workers", type=int, default=8)
    build.add_argument("--cache-root")
    build.add_argument("--run-id")

    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--input")
    resume.add_argument("--cache-root")

    status = commands.add_parser("status")
    selectors = status.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--run-id")
    selectors.add_argument("--domain-id")
    status.add_argument("--cache-root")

    for name in ("get-summary", "get-graph"):
        command = commands.add_parser(name)
        command.add_argument("--domain-id", required=True)
        command.add_argument("--cache-root")

    stop = commands.add_parser("stop")
    stop.add_argument("run_id")
    stop.add_argument("--cache-root")
    stop.add_argument("--reason")

    validate = commands.add_parser("validate")
    validate.add_argument("run_id")
    validate.add_argument("--cache-root")
    return parser


def _emit(result: CommandResult, *, exit_code: int) -> int:
    sys.stdout.write(command_result_json(result) + "\n")
    return exit_code


def _paths(cache_root: str | None) -> DomainPaths:
    return DomainPaths.resolve(cache_root)


def _repository(paths: DomainPaths) -> RunRepository:
    return RunRepository(paths.root)


def _request_from_args(args: argparse.Namespace) -> DomainBuildRequest:
    if args.policy is None:
        policy_document: dict[str, Any] = {
            "schema_version": "arc.domain_build_policy.v1",
            "as_of_date": datetime.now(timezone.utc).date().isoformat(),
            "recent_window_days": 365,
            "citer_pool_limit": 1000,
            "ranked_paper_limit": 50,
            "graph_node_limit": 90,
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
        if any(value is not None for value in mode_overrides.values()):
            # Mode flags opt the whole semantic input into the closed v2
            # contract.  Carry every resolved numeric setting forward rather
            # than mixing a partial v2 document with a v1 policy.
            policy_document = {
                "schema_version": DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
                "as_of_date": policy.as_of_date,
                "recent_window_days": policy.recent_window_days,
                "citer_pool_limit": policy.citer_pool_limit,
                "ranked_paper_limit": policy.ranked_paper_limit,
                "graph_node_limit": policy.graph_node_limit,
                "foundation_mode": (
                    mode_overrides["foundation_mode"]
                    or policy.foundation_mode
                    or "infer_from_seed"
                ),
                "citer_selection_mode": (
                    mode_overrides["citer_selection_mode"]
                    or policy.citer_selection_mode
                    or "representative_plus_recent"
                ),
            }
            policy = decode_domain_build_policy(policy_document)
        if overrides:
            policy_document = encode_domain_build_policy(policy)
            policy_document.update(overrides)
            policy = decode_domain_build_policy(policy_document)
        if args.workers < 1:
            raise _UsageError("--workers must be at least one")
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
    request = _request_from_args(args)
    paths = _paths(args.cache_root)
    repository = _repository(paths)
    snapshot = DomainBuildRunner(repository).execute(
        request,
        run_id=args.run_id,
        max_workers=args.workers,
    )
    result = _published_result(repository, paths, snapshot)
    return result, _exit_code(result)


def _resume(args: argparse.Namespace) -> tuple[CommandResult, int]:
    paths = _paths(args.cache_root)
    repository = _repository(paths)
    snapshot = DomainBuildRunner(repository).resume(
        args.run_id, input=_resume_input(args.input)
    )
    result = _published_result(repository, paths, snapshot)
    return result, _exit_code(result)


def _status(args: argparse.Namespace) -> CommandResult:
    paths = _paths(args.cache_root)
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
    paths = _paths(args.cache_root)
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
    paths = _paths(args.cache_root)
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


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
        dispatched = _dispatch(args)
        if isinstance(dispatched, int):
            return dispatched
        result, exit_code = dispatched
        return _emit(result, exit_code=exit_code)
    except _UsageError as exc:
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError("invalid_request", str(exc)),
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
