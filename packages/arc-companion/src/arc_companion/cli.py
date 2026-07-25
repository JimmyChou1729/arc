"""Six-command protocol CLI for source-anchored Companion builds."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arc_jobs import (
    ArcJobsError,
    CommandArtifact,
    CommandError,
    CommandResult,
    CommandRun,
    CommandStatus,
    CommandWarning,
    RunStatus,
    command_result_from_snapshot,
    command_result_json,
    snapshot_data,
)
from arc_llm import ModelSelection
from arc_paper import (
    ArcPaperService,
    RichDocumentParserService,
    RichDocumentValidationError,
    SourceBundle,
)

from .project import CompanionProjectError, CompanionProjectPaths
from .release import (
    CompanionReleaseError,
    CompanionReleasePublisher,
)
from .renderer import CompanionRenderError, CompanionRenderer
from .request_contracts import (
    CompanionBuildRequest,
    CompanionExecutionOptions,
    CompanionGenerationRecipe,
)
from .service import (
    CompanionService,
    CompanionServiceError,
    companion_run_id,
)


class _UsageError(ValueError):
    pass


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
        prog="arc-companion",
        description=(
            "Build, resume, render, and validate source-anchored Companion "
            "releases. Results are always JSON."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build",
        help="build a source-anchored Companion",
        description="Build a durable Companion from a verified paper or local source.",
    )
    build.add_argument("source", help="paper identifier or local source")
    build.add_argument("--project-dir", required=True, help="Companion project directory")
    build.add_argument(
        "--pdf",
        help="local PDF validator path, or 'fetch' for a remote paper source",
    )
    build.add_argument(
        "--target-language",
        default="zh-CN",
        help="target language (default: zh-CN)",
    )
    build.add_argument("--user-intent", default="", help="reader intent used to focus the guide")
    build.add_argument("--provider", default="auto", help="LLM provider (default: auto)")
    build.add_argument("--model", help="provider-specific model name")
    build.add_argument("--workers", type=int, default=4, help="parallel workers (default: 4)")
    build.add_argument(
        "--approx-term-count", type=int, default=50, help="target glossary size (default: 50)"
    )
    build.add_argument("--refresh", action="store_true", help="refresh cached source data")

    status = commands.add_parser(
        "status",
        help="inspect the selected build and active release",
        description="Inspect the selected Companion build and active release.",
    )
    status.add_argument("--project-dir", required=True, help="Companion project directory")

    resume = commands.add_parser(
        "resume",
        help="resume the selected Companion build",
        description="Resume the selected paused or interrupted Companion build.",
    )
    resume.add_argument("--project-dir", required=True, help="Companion project directory")
    resume.add_argument(
        "--input",
        help="JSON object or path containing the current pause response",
    )
    resume.add_argument("--workers", type=int, default=4, help="parallel workers (default: 4)")

    stop = commands.add_parser(
        "stop",
        help="request a cooperative build stop",
        description="Request a cooperative stop for the selected Companion build.",
    )
    stop.add_argument("--project-dir", required=True, help="Companion project directory")
    stop.add_argument("--reason", help="human-readable stop reason")

    render = commands.add_parser(
        "render",
        help="publish PDF and web release artifacts",
        description="Publish release artifacts from the accepted Companion book.",
    )
    render.add_argument("--project-dir", required=True, help="Companion project directory")
    render.add_argument(
        "--format",
        choices=("all", "pdf", "web"),
        default="all",
        help="artifact formats to report (default: all)",
    )

    validate = commands.add_parser(
        "validate",
        help="validate the active Companion release",
        description="Validate the active release manifest and rendered artifacts.",
    )
    validate.add_argument("--project-dir", required=True, help="Companion project directory")
    return parser


def _help_command(arguments: list[str]) -> str:
    command = (
        arguments[0]
        if arguments
        and arguments[0] in {"build", "status", "resume", "stop", "render", "validate"}
        else None
    )
    return " ".join(
        part for part in ("arc-companion", command, "--help") if part is not None
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(arguments)
        result = _dispatch(args)
    except _HelpRequested:
        return 0
    except _UsageError as exc:
        result = _failed(
            "invalid_request",
            str(exc),
            details={"help_command": _help_command(arguments)},
        )
    except (
        ArcJobsError,
        CompanionProjectError,
        CompanionReleaseError,
        CompanionRenderError,
        CompanionServiceError,
        RichDocumentValidationError,
    ) as exc:
        code = str(exc.code)
        result = _failed(
            code,
            str(exc),
            details=(
                {"help_command": _help_command(arguments)}
                if code == "invalid_request"
                else None
            ),
        )
    except OSError as exc:
        result = _failed("local_io_error", str(exc))
    except Exception as exc:
        result = _failed("internal_error", str(exc))
    sys.stdout.write(command_result_json(result) + "\n")
    return _exit_code(result)


def _dispatch(args: argparse.Namespace) -> CommandResult:
    if args.command == "build":
        return _build(args)
    if args.command == "status":
        return _status(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "render":
        return _render(args)
    if args.command == "validate":
        return _validate(args)
    raise _UsageError(f"unsupported command: {args.command}")


def _build(args: argparse.Namespace) -> CommandResult:
    _validate_workers(args.workers)
    if not args.target_language.strip():
        raise _UsageError("--target-language must be non-empty")
    if not args.provider.strip():
        raise _UsageError("--provider must be non-empty")
    if args.model is not None and not args.model.strip():
        raise _UsageError("--model must be non-empty")
    if args.model is not None and args.provider == "auto":
        raise _UsageError("--model requires an explicit --provider")
    if not 1 <= args.approx_term_count <= 200:
        raise _UsageError(
            "--approx-term-count must be between 1 and 200"
        )
    if (
        args.pdf is not None
        and args.pdf != "fetch"
        and not Path(args.pdf).is_file()
    ):
        raise _UsageError("--pdf must be an existing path or 'fetch'")
    # Unknown project state is refused before source/cache writes.
    paths = CompanionProjectPaths.open(args.project_dir)
    paper = ArcPaperService(cache_root=paths.paper_cache_root)
    rich, validators, warnings = _resolve_source(
        paper,
        args.source,
        pdf=args.pdf,
        refresh=args.refresh,
    )
    request = CompanionBuildRequest(
        source=rich,
        validator_digests=validators,
        target_language=args.target_language,
        user_intent=args.user_intent,
    )
    recipe = CompanionGenerationRecipe(
        model=ModelSelection(
            provider=args.provider,
            model=args.model,
            tier="medium",
        ),
        approx_term_count=args.approx_term_count,
    )
    execution = CompanionExecutionOptions(
        workers=args.workers,
        cache_root=paths.paper_cache_root,
    )
    run_id = companion_run_id(request, recipe)
    service = CompanionService(paths.jobs_root)
    prepared = service.prepare(request, recipe=recipe, run_id=run_id)
    run_id = prepared.run_id
    paths.select_run(run_id)
    paths.write_source_diagnostics(run_id, warnings)
    snapshot = service.execute(
        run_id,
        execution=execution,
    )
    return _snapshot_result(paths, snapshot, warnings=warnings)


def _resume(args: argparse.Namespace) -> CommandResult:
    _validate_workers(args.workers)
    paths = CompanionProjectPaths.load(args.project_dir)
    run_id = _current_run(paths)
    value = _json_input(args.input) if args.input is not None else None
    snapshot = CompanionService(paths.jobs_root).resume(
        run_id,
        input=value,
        execution=CompanionExecutionOptions(
            workers=args.workers,
            cache_root=paths.paper_cache_root,
        ),
    )
    return _snapshot_result(paths, snapshot)


def _validate_workers(workers: int) -> None:
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise _UsageError("--workers must be an integer")
    if not 1 <= workers <= 24:
        raise _UsageError("--workers must be between 1 and 24")


def _status(args: argparse.Namespace) -> CommandResult:
    paths = CompanionProjectPaths.load(args.project_dir)
    run_id = _current_run(paths)
    view = CompanionService(paths.jobs_root).inspect(run_id)
    base = command_result_from_snapshot(view.snapshot)
    current = paths.current_release()
    selected_run = snapshot_data(view.snapshot)
    release_matches_selected_run = (
        current is not None and current["run_id"] == run_id
    )
    data: dict[str, Any] = {
        "selected_run": selected_run,
        "active_release": current,
        "release_matches_selected_run": release_matches_selected_run,
    }
    return CommandResult(
        base.status,
        run=base.run,
        data=data,
        artifacts=base.artifacts,
        warnings=(
            *_source_warnings(paths, run_id),
            *_release_pointer_warnings(paths, current),
        ),
        error=base.error,
        resume=base.resume,
    )


def _stop(args: argparse.Namespace) -> CommandResult:
    paths = CompanionProjectPaths.load(args.project_dir)
    run_id = _current_run(paths)
    view = CompanionService(paths.jobs_root).stop(
        run_id, reason=args.reason
    )
    return CommandResult(
        CommandStatus.COMPLETED,
        CommandRun(view.snapshot.run_id, view.snapshot.revision),
        {
            "run": {
                "status": view.snapshot.status.value,
                "attempt": view.snapshot.attempt,
                "stop_requested": view.stop_request is not None,
            }
        },
    )


def _render(args: argparse.Namespace) -> CommandResult:
    paths = CompanionProjectPaths.load(args.project_dir)
    run_id = _current_run(paths)
    service = CompanionService(paths.jobs_root)
    snapshot = service.inspect(run_id).snapshot
    if snapshot.status is not RunStatus.SUCCEEDED:
        return command_result_from_snapshot(snapshot)
    book = service.accepted_book(run_id)
    release = _publisher(paths).publish(book, run_id=run_id)
    roles = {"pdf", "web"} if args.format == "all" else {args.format}
    artifacts = [
        CommandArtifact("manifest", release.release_id, str(release.manifest))
    ]
    if "pdf" in roles:
        artifacts.append(
            CommandArtifact("pdf", release.release_id, str(release.pdf))
        )
    if "web" in roles:
        artifacts.append(
            CommandArtifact("web", release.release_id, str(release.web_index))
        )
    return CommandResult(
        CommandStatus.COMPLETED,
        data={
            "release_id": release.release_id,
            "reused": release.reused,
        },
        artifacts=tuple(artifacts),
    )


def _validate(args: argparse.Namespace) -> CommandResult:
    paths = CompanionProjectPaths.load(args.project_dir)
    current = paths.current_release()
    if current is None:
        return _failed("release_not_found", "project has no current release")
    run_id = current["run_id"]
    book = CompanionService(paths.jobs_root).accepted_book(run_id)
    release = _publisher(paths).validate_current(current, book)
    return CommandResult(
        CommandStatus.COMPLETED,
        data={"release_id": release.release_id, "valid": True},
        artifacts=(
            CommandArtifact("manifest", release.release_id, str(release.manifest)),
            CommandArtifact("pdf", release.release_id, str(release.pdf)),
            CommandArtifact("web", release.release_id, str(release.web_index)),
        ),
    )


def _snapshot_result(
    paths: CompanionProjectPaths,
    snapshot: Any,
    *,
    warnings: tuple[str, ...] = (),
) -> CommandResult:
    base = command_result_from_snapshot(snapshot)
    persisted = paths.source_diagnostics(snapshot.run_id)
    effective_warnings = warnings or persisted
    command_warnings = tuple(
        CommandWarning("source_diagnostic", item)
        for item in effective_warnings
    )
    if snapshot.status is not RunStatus.SUCCEEDED:
        return CommandResult(
            base.status,
            run=base.run,
            data={"run": snapshot_data(snapshot)},
            artifacts=base.artifacts,
            warnings=command_warnings,
            error=base.error,
            resume=base.resume,
        )
    service = CompanionService(paths.jobs_root)
    book = service.accepted_book(snapshot.run_id)
    release = _publisher(paths).publish(book, run_id=snapshot.run_id)
    return CommandResult(
        CommandStatus.COMPLETED,
        run=base.run,
        data={
            "run": snapshot_data(snapshot),
            "release_id": release.release_id,
            "reused": release.reused,
        },
        artifacts=(
            CommandArtifact("pdf", release.release_id, str(release.pdf)),
            CommandArtifact("web", release.release_id, str(release.web_index)),
            CommandArtifact(
                "manifest", release.release_id, str(release.manifest)
            ),
        ),
        warnings=command_warnings,
    )


def _resolve_source(
    paper: ArcPaperService,
    source: str,
    *,
    pdf: str | None,
    refresh: bool,
) -> tuple[Any, tuple[str, ...], tuple[str, ...]]:
    source_path = Path(source)
    if source_path.is_file():
        primary = paper.import_source(source_path)
        if pdf == "fetch":
            raise _UsageError("--pdf fetch is only valid for a paper ID")
        validators = (
            (paper.import_source(Path(pdf)),) if pdf is not None else ()
        )
    else:
        primary = paper.fetch_arxiv_auto(source, refresh=refresh)
        if pdf is None:
            validators = ()
        elif pdf == "fetch":
            validators = (paper.fetch_arxiv_pdf(source, refresh=refresh),)
        else:
            pdf_path = Path(pdf)
            if not pdf_path.is_file():
                raise _UsageError("--pdf must be an existing path or 'fetch'")
            validators = (paper.import_source(pdf_path),)
    outcome = RichDocumentParserService(paper.repository).parse(
        SourceBundle(primary=primary, validators=validators)
    )
    return (
        outcome.document,
        tuple(item.artifact_digest for item in validators),
        tuple(outcome.warnings),
    )


def _publisher(paths: CompanionProjectPaths) -> CompanionReleasePublisher:
    paper = ArcPaperService(cache_root=paths.paper_cache_root)

    def load_asset(digest: str) -> bytes | None:
        try:
            asset = paper.repository.get_asset(digest)
            return paper.repository.read_asset_bytes(asset)
        except Exception:
            return None

    return CompanionReleasePublisher(
        paths, CompanionRenderer(asset_loader=load_asset)
    )


def _current_run(paths: CompanionProjectPaths) -> str:
    value = paths.current_run_id
    if value is None:
        raise CompanionProjectError(
            "run_not_found", "project has no selected build run"
        )
    return value


def _source_warnings(
    paths: CompanionProjectPaths, run_id: str
) -> tuple[CommandWarning, ...]:
    return tuple(
        CommandWarning("source_diagnostic", item)
        for item in paths.source_diagnostics(run_id)
    )


def _release_pointer_warnings(
    paths: CompanionProjectPaths,
    current: Mapping[str, Any] | None,
) -> tuple[CommandWarning, ...]:
    if current is None:
        return ()
    expected_manifest = (
        paths.releases_root / str(current["release_id"]) / "manifest.json"
    )
    expected_relative = expected_manifest.relative_to(paths.root).as_posix()
    if current["manifest"] != expected_relative:
        return (
            CommandWarning(
                "release_pointer_invalid",
                "active release manifest does not match its release ID",
            ),
        )
    if not expected_manifest.is_file():
        return (
            CommandWarning(
                "release_pointer_stale",
                "active release is missing; rerun render for the selected run",
            ),
        )
    return ()


def _json_input(value: str) -> Mapping[str, Any]:
    path = Path(value)
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else value
        document = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _UsageError(f"--input must be a JSON object or JSON file: {exc}") from exc
    if not isinstance(document, Mapping):
        raise _UsageError("--input must contain a JSON object")
    return dict(document)


def _failed(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> CommandResult:
    return CommandResult(
        CommandStatus.FAILED,
        error=CommandError(code, message, dict(details or {})),
    )


def _exit_code(result: CommandResult) -> int:
    return {
        CommandStatus.COMPLETED: 0,
        CommandStatus.PAUSED: 2,
        CommandStatus.FAILED: 1,
    }[result.status]


__all__ = ["main"]
