"""Protocol CLI for source-anchored Companion builds and revisions."""

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
    file_lease,
    snapshot_data,
)
from arc_llm import HostAuthority, LLMExecutionOptions, ModelSelection
from arc_paper import (
    ArcPaperService,
    PDF_VALIDATOR_MISSING_WARNING,
    RichDocumentParserService,
    RichDocumentValidationError,
    SourceBundle,
    detect_suspicious_equation_labels,
)
from arc_render import (
    HTMLRenderError,
    RenderWorkspaceError,
    read_publication_workspace_state,
    render_publication_html,
    validate_reader_in_browser,
    validate_publication_workspace,
    validate_standalone_html,
)

from .project import CompanionProjectError, CompanionProjectPaths
from .publication_revisions import (
    CompanionPublicationRevisionError,
    commit_publication_revision,
    committed_publication_review_ids,
    decode_publication_revision_request,
    encode_publication_revision_result,
)
from .reader_labels import ReaderLabelError, resolve_reader_labels
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
from .translation_adapter import (
    CompanionTranslationRuntimeError,
    require_translation_runtime,
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
            "publications. Results are always JSON."
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
    build.add_argument(
        "--author",
        action="append",
        default=[],
        help="author name supplied by the user; repeat for multiple authors",
    )
    build.add_argument(
        "--reader-labels",
        help="path to a complete JSON object of reader UI labels",
    )
    build.add_argument("--user-intent", default="", help="reader intent used to focus the guide")
    build.add_argument("--provider", default="auto", help="LLM provider (default: auto)")
    build.add_argument("--model", help="provider-specific model name")
    build.add_argument(
        "--workers",
        type=int,
        default=16,
        help="parallel workers (default: 16)",
    )
    build.add_argument(
        "--approx-term-count", type=int, default=50, help="target glossary size (default: 50)"
    )
    build.add_argument("--refresh", action="store_true", help="refresh cached source data")
    _host_authority_argument(build)
    _paper_cache_argument(build)

    status = commands.add_parser(
        "status",
        help="inspect the selected build",
        description="Inspect the selected Companion build and publication.",
    )
    status.add_argument("--project-dir", required=True, help="Companion project directory")

    resume = commands.add_parser(
        "resume",
        help="resume the selected Companion build",
        description="Resume the selected paused, interrupted, or failed Companion build.",
    )
    resume.add_argument("--project-dir", required=True, help="Companion project directory")
    resume.add_argument(
        "--input",
        help="JSON object or path containing the current pause response",
    )
    resume.add_argument(
        "--workers",
        type=int,
        default=16,
        help="parallel workers (default: 16)",
    )
    _paper_cache_argument(resume)
    _host_authority_argument(resume)

    stop = commands.add_parser(
        "stop",
        help="request a cooperative build stop",
        description="Request a cooperative stop for the selected Companion build.",
    )
    stop.add_argument("--project-dir", required=True, help="Companion project directory")
    stop.add_argument("--reason", help="human-readable stop reason")

    render = commands.add_parser(
        "render",
        help="render a standalone HTML publication",
        description="Render standalone HTML from the selected publication.",
    )
    render.add_argument("--project-dir", required=True, help="Companion project directory")

    revise = commands.add_parser(
        "revise",
        help="apply an audited publication revision",
        description="Commit and deliver a recoverable post-publication revision.",
    )
    revise.add_argument("--project-dir", required=True, help="Companion project directory")
    revise.add_argument("--request", required=True, help="revision request JSON file")
    revise.add_argument(
        "--browser",
        action="store_true",
        help="run optional local Chromium reader checks",
    )
    revise.add_argument(
        "--browser-executable",
        help="local Chromium-family executable for --browser",
    )
    revise.add_argument(
        "--browser-timeout",
        type=int,
        default=60,
        help="browser validation timeout in seconds (default: 60)",
    )

    validate = commands.add_parser(
        "validate",
        help="validate the selected Companion publication",
        description="Validate the publication workspace and standalone HTML.",
    )
    validate.add_argument("--project-dir", required=True, help="Companion project directory")
    validate.add_argument(
        "--browser",
        action="store_true",
        help="run optional local Chromium reader checks",
    )
    validate.add_argument(
        "--browser-executable",
        help="local Chromium-family executable for --browser",
    )
    validate.add_argument(
        "--browser-timeout",
        type=int,
        default=60,
        help="browser validation timeout in seconds (default: 60)",
    )
    return parser


def _help_command(arguments: list[str]) -> str:
    command = (
        arguments[0]
        if arguments
        and arguments[0]
        in {"build", "status", "resume", "stop", "render", "revise", "validate"}
        else None
    )
    return " ".join(
        part for part in ("arc-companion", command, "--help") if part is not None
    )


def _paper_cache_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--paper-cache-root",
        help=(
            "shared arc-paper cache root; defaults to ARC_PAPER_CACHE or the "
            "global ARC paper cache"
        ),
    )


def _host_authority_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host-authority",
        choices=tuple(item.value for item in HostAuthority),
        default=HostAuthority.UNKNOWN.value,
        help="host permission attestation; unrestricted must be explicit",
    )


def _execution_options(args: argparse.Namespace) -> CompanionExecutionOptions:
    return CompanionExecutionOptions(
        workers=args.workers,
        paper_cache_root=args.paper_cache_root,
        llm=LLMExecutionOptions(host_authority=HostAuthority(args.host_authority)),
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
        CompanionPublicationRevisionError,
        CompanionServiceError,
        CompanionTranslationRuntimeError,
        RichDocumentValidationError,
        HTMLRenderError,
    ) as exc:
        code = str(
            getattr(
                exc,
                "code",
                (
                    "html_render_failed"
                    if isinstance(exc, HTMLRenderError)
                    else "internal_error"
                ),
            )
        )
        raw_details = getattr(exc, "details", {}) or {}
        details = (
            dict(raw_details)
            if isinstance(raw_details, Mapping)
            else {"conflicts": list(raw_details)}
        )
        result = _failed(
            code,
            str(exc),
            details=(
                {"help_command": _help_command(arguments)}
                if code == "invalid_request"
                else details
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
    if args.command == "revise":
        return _revise(args)
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
    authors = tuple(author.strip() for author in args.author)
    if any(not author for author in authors):
        raise _UsageError("--author must be non-empty")
    supplied_reader_labels = (
        _reader_labels_file(args.reader_labels)
        if args.reader_labels is not None
        else None
    )
    try:
        reader_labels = resolve_reader_labels(
            args.target_language, supplied_reader_labels
        )
    except ReaderLabelError as exc:
        raise _UsageError(str(exc)) from exc
    if (
        args.pdf is not None
        and args.pdf != "fetch"
        and not Path(args.pdf).is_file()
    ):
        raise _UsageError("--pdf must be an existing path or 'fetch'")
    require_translation_runtime()
    # Unknown project state is refused before source/cache writes.
    paths = CompanionProjectPaths.open(args.project_dir)
    paper = ArcPaperService(cache_root=args.paper_cache_root)
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
        authors=authors,
        reader_labels=reader_labels,
    )
    recipe = CompanionGenerationRecipe(
        model=ModelSelection(
            provider=args.provider,
            model=args.model,
            tier="medium",
        ),
        approx_term_count=args.approx_term_count,
    )
    execution = _execution_options(args)
    service = CompanionService(paths.jobs_root)
    run_id = companion_run_id(request, recipe)
    prepared = service.prepare(request, recipe=recipe, run_id=run_id)
    run_id = prepared.run_id
    paths.select_run(run_id)
    paths.write_source_diagnostics(run_id, warnings)
    snapshot = service.execute(
        run_id,
        execution=execution,
    )
    return _snapshot_result(
        paths,
        snapshot,
        warnings=warnings,
    )


def _resume(args: argparse.Namespace) -> CommandResult:
    _validate_workers(args.workers)
    require_translation_runtime()
    paths = CompanionProjectPaths.load(args.project_dir)
    run_id = _current_run(paths)
    value = _json_input(args.input) if args.input is not None else None
    snapshot = CompanionService(paths.jobs_root).resume(
        run_id,
        input=value,
        execution=_execution_options(args),
    )
    return _snapshot_result(
        paths,
        snapshot,
    )


def _validate_workers(workers: int) -> None:
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise _UsageError("--workers must be an integer")
    if not 1 <= workers <= 24:
        raise _UsageError("--workers must be between 1 and 24")


def _status(args: argparse.Namespace) -> CommandResult:
    paths = CompanionProjectPaths.load(args.project_dir)
    with file_lease(paths.delivery_lease, blocking=True):
        return _status_locked(paths)


def _status_locked(paths: CompanionProjectPaths) -> CommandResult:
    run_id = _current_run(paths)
    view = CompanionService(paths.jobs_root).inspect(run_id)
    base = command_result_from_snapshot(
        view.snapshot,
        query=view.snapshot.status
        in {RunStatus.PENDING, RunStatus.RUNNING},
    )
    selected_run = snapshot_data(view.snapshot)
    data: dict[str, Any] = {
        "selected_run": selected_run,
        "build_diagnostics": CompanionService(
            paths.jobs_root
        ).build_diagnostics(run_id),
    }
    artifacts: tuple[CommandArtifact, ...] = ()
    publication_warnings: list[CommandWarning] = []
    if view.snapshot.status is RunStatus.SUCCEEDED:
        service = CompanionService(paths.jobs_root)
        publication = service.publication(run_id)
        data["publication_digest"] = publication.publication_digest
        workspace = paths.publication_workspace(run_id)
        validated_artifacts: list[CommandArtifact] = []
        try:
            publication_path = service.materialize_publication(
                run_id, workspace, project_paths=paths
            )
            diagnostics = validate_publication_workspace(publication_path)
            state = read_publication_workspace_state(publication_path)
            if state.publication_digest != publication.publication_digest:
                raise HTMLRenderError(
                    "materialized publication belongs to another run"
                )
            review_ids = committed_publication_review_ids(paths, run_id)
            data.update(_edition_data(state, review_ids))
            data["publication"] = str(publication_path)
            validated_artifacts.append(
                CommandArtifact("publication", run_id, str(publication_path))
            )
            publication_warnings.extend(
                CommandWarning("fragment_revision_diagnostic", item)
                for item in diagnostics
            )
        except (
            CompanionPublicationRevisionError,
            HTMLRenderError,
            RenderWorkspaceError,
            OSError,
        ) as exc:
            publication_warnings.append(
                CommandWarning(
                    "publication_workspace_invalid", str(exc)
                )
            )
            state = None
        if state is not None and paths.delivery_html.is_file():
            try:
                validate_standalone_html(
                    publication,
                    paths.delivery_html,
                    expected_selected_revision_digests=(
                        None
                        if state is None
                        else state.selected_revision_digests
                    ),
                )
                data["workspace_html_consistent"] = True
                validated_artifacts.append(
                    CommandArtifact(
                        "web", run_id, str(paths.delivery_html)
                    )
                )
            except HTMLRenderError as exc:
                data["workspace_html_consistent"] = False
                publication_warnings.append(
                    CommandWarning(
                        "standalone_html_stale", str(exc)
                    )
                )
        else:
            data["workspace_html_consistent"] = False
        artifacts = tuple(validated_artifacts)
    return CommandResult(
        base.status,
        run=base.run,
        data=data,
        artifacts=artifacts,
        warnings=(
            *_source_warnings(paths, run_id),
            *_build_warnings(paths, run_id),
            *publication_warnings,
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
    with file_lease(paths.delivery_lease, blocking=True):
        run_id = _current_run(paths)
        service = CompanionService(paths.jobs_root)
        snapshot = service.inspect(run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED:
            return command_result_from_snapshot(snapshot)
        return _render_locked(paths, run_id, service)


def _render_locked(
    paths: CompanionProjectPaths,
    run_id: str,
    service: CompanionService,
) -> CommandResult:
    publication_path = service.materialize_publication(
        run_id,
        paths.publication_workspace(run_id),
        project_paths=paths,
    )
    state = read_publication_workspace_state(publication_path)
    publication = state.publication
    warnings: list[CommandWarning] = []
    artifacts = [
        CommandArtifact("publication", run_id, str(publication_path))
    ]
    rendered = render_publication_html(
        publication_path,
        paths.publication_html(run_id),
    )
    validate_standalone_html(
        publication,
        paths.publication_html(run_id),
        expected_selected_revision_digests=state.selected_revision_digests,
    )
    promoted = paths._promote_publication_html_locked(run_id)
    if promoted:
        artifacts.append(
            CommandArtifact("web", run_id, str(paths.delivery_html))
        )
    else:
        warnings.append(
            CommandWarning(
                "publication_not_selected",
                "standalone HTML was rendered for the run but not promoted "
                "because another run is selected",
            )
        )
    warnings.extend(
        CommandWarning("fragment_revision_diagnostic", item)
        for item in rendered.warnings
    )
    return CommandResult(
        CommandStatus.COMPLETED,
        data={
            "publication_digest": publication.publication_digest,
            **_edition_data(
                state, committed_publication_review_ids(paths, run_id)
            ),
            "workspace_html_consistent": promoted,
            "delivery": (
                {"html": str(paths.delivery_html)}
                if promoted
                else {}
            ),
        },
        artifacts=tuple(artifacts),
        warnings=tuple(warnings),
    )


def _revise(args: argparse.Namespace) -> CommandResult:
    request_path = Path(args.request)
    if not request_path.is_file():
        raise _UsageError("--request must be an existing JSON file")
    try:
        request_value = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _UsageError(f"--request must contain valid JSON: {exc}") from exc
    if not isinstance(request_value, Mapping):
        raise _UsageError("--request must contain a JSON object")
    request = decode_publication_revision_request(request_value)
    paths = CompanionProjectPaths.load(args.project_dir)
    with file_lease(paths.delivery_lease, blocking=True):
        if _current_run(paths) != request.run_id:
            raise CompanionPublicationRevisionError(
                "publication_revision_run_mismatch",
                "revision request does not target the selected run",
            )
        service = CompanionService(paths.jobs_root)
        snapshot = service.inspect(request.run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED:
            return command_result_from_snapshot(snapshot)
        publication_path = service.materialize_publication(
            request.run_id,
            paths.publication_workspace(request.run_id),
            project_paths=paths,
        )
        result = commit_publication_revision(
            paths, request, publication_path
        )
        diagnostics = validate_publication_workspace(publication_path)
        state = read_publication_workspace_state(publication_path)
        rendered = render_publication_html(
            publication_path,
            paths.publication_html(request.run_id),
        )
        validate_standalone_html(
            state.publication,
            paths.publication_html(request.run_id),
            expected_selected_revision_digests=state.selected_revision_digests,
        )
        browser_data: dict[str, Any] = {}
        if bool(getattr(args, "browser", False)):
            checked = validate_reader_in_browser(
                paths.publication_html(request.run_id),
                browser_executable=getattr(args, "browser_executable", None),
                timeout_seconds=getattr(args, "browser_timeout", 60),
            )
            browser_data = {
                "browser": {
                    "executable": checked.executable,
                    "timeout_seconds": checked.timeout_seconds,
                }
            }
        if not paths._promote_publication_html_locked(request.run_id):
            raise CompanionPublicationRevisionError(
                "publication_revision_run_mismatch",
                "selected run changed before revision promotion",
            )
        return CommandResult(
            CommandStatus.COMPLETED,
            data={
                **encode_publication_revision_result(result),
                **_edition_data(
                    state,
                    committed_publication_review_ids(paths, request.run_id),
                ),
                "workspace_html_consistent": True,
                "delivery": {"html": str(paths.delivery_html)},
                **browser_data,
            },
            artifacts=(
                CommandArtifact(
                    "publication", request.run_id, str(publication_path)
                ),
                CommandArtifact("web", request.run_id, str(paths.delivery_html)),
            ),
            warnings=tuple(
                CommandWarning("fragment_revision_diagnostic", item)
                for item in (*diagnostics, *rendered.warnings)
            ),
        )


def _validate(args: argparse.Namespace) -> CommandResult:
    paths = CompanionProjectPaths.load(args.project_dir)
    with file_lease(paths.delivery_lease, blocking=True):
        return _validate_locked(
            paths,
            browser=bool(getattr(args, "browser", False)),
            browser_executable=getattr(args, "browser_executable", None),
            browser_timeout=getattr(args, "browser_timeout", 60),
        )


def _validate_locked(
    paths: CompanionProjectPaths,
    *,
    browser: bool = False,
    browser_executable: str | None = None,
    browser_timeout: int = 60,
) -> CommandResult:
    run_id = _current_run(paths)
    service = CompanionService(paths.jobs_root)
    publication_path = service.materialize_publication(
        run_id,
        paths.publication_workspace(run_id),
        project_paths=paths,
    )
    warnings = validate_publication_workspace(publication_path)
    state = read_publication_workspace_state(publication_path)
    publication = state.publication
    if not paths.delivery_html.is_file():
        raise HTMLRenderError(
            "the selected publication has no standalone HTML release"
        )
    validate_standalone_html(
        publication,
        paths.delivery_html,
        expected_selected_revision_digests=state.selected_revision_digests,
    )
    browser_data: dict[str, Any] = {}
    if browser:
        checked = validate_reader_in_browser(
            paths.delivery_html,
            browser_executable=browser_executable,
            timeout_seconds=browser_timeout,
        )
        browser_data = {
            "browser": {
                "executable": checked.executable,
                "timeout_seconds": checked.timeout_seconds,
            }
        }
    return CommandResult(
        CommandStatus.COMPLETED,
        data={
            "publication_digest": publication.publication_digest,
            **_edition_data(
                state, committed_publication_review_ids(paths, run_id)
            ),
            "workspace_html_consistent": True,
            "valid": True,
            **browser_data,
        },
        artifacts=(
            CommandArtifact("publication", run_id, str(publication_path)),
            *((CommandArtifact("web", run_id, str(paths.delivery_html)),)
              if paths.delivery_html.is_file() else ()),
        ),
        warnings=tuple(
            CommandWarning("fragment_revision_diagnostic", item)
            for item in warnings
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
    source_values = tuple(dict.fromkeys((*persisted, *warnings)))
    command_warnings = (
        tuple(
            CommandWarning("source_diagnostic", item)
            for item in source_values
        )
        + _build_warnings(paths, snapshot.run_id)
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
    with file_lease(paths.delivery_lease, blocking=True):
        try:
            delivered = _render_locked(
                paths,
                snapshot.run_id,
                CompanionService(paths.jobs_root),
            )
        except (
            CompanionProjectError,
            CompanionPublicationRevisionError,
            HTMLRenderError,
            OSError,
        ) as exc:
            return CommandResult(
                CommandStatus.COMPLETED,
                run=base.run,
                data={
                    "run": snapshot_data(snapshot),
                    "published": False,
                    "delivery": {},
                },
                artifacts=(),
                warnings=(
                    *command_warnings,
                    CommandWarning("web_render_failed", str(exc)),
                ),
            )
    return CommandResult(
        delivered.status,
        run=base.run,
        data={"run": snapshot_data(snapshot), **dict(delivered.data)},
        artifacts=delivered.artifacts,
        warnings=(*command_warnings, *delivered.warnings),
        error=delivered.error,
        resume=delivered.resume,
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
        outcome = RichDocumentParserService(paper.repository).parse(
            SourceBundle(primary=primary, validators=validators)
        )
        return (
            outcome.document,
            tuple(item.artifact_digest for item in validators),
            tuple(outcome.warnings),
        )

    parser = RichDocumentParserService(paper.repository)
    primary = paper.fetch_arxiv_auto(source, refresh=refresh)
    probe = parser.parse(SourceBundle(primary=primary))
    reasons = detect_suspicious_equation_labels(probe.document)
    forced_html_refresh = False
    if reasons and not refresh:
        primary = paper.fetch_arxiv_auto(source, refresh=True)
        forced_html_refresh = True
        probe = parser.parse(SourceBundle(primary=primary))
        reasons = detect_suspicious_equation_labels(probe.document)

    explicit_validator = pdf is not None
    validators: tuple[Any, ...]
    acquisition_warnings: list[str] = []
    if pdf == "fetch":
        validators = (
            paper.fetch_arxiv_pdf(
                source,
                refresh=refresh or forced_html_refresh,
            ),
        )
    elif pdf is not None:
        pdf_path = Path(pdf)
        if not pdf_path.is_file():
            raise _UsageError("--pdf must be an existing path or 'fetch'")
        validators = (paper.import_source(pdf_path),)
    elif reasons:
        try:
            validators = (
                paper.fetch_arxiv_pdf(
                    source,
                    refresh=refresh or forced_html_refresh,
                ),
            )
        except Exception as exc:
            code = getattr(exc, "code", "pdf_fetch_failed")
            acquisition_warnings.append(
                "PDF visual equation-label review could not acquire the PDF "
                f"({code}): {exc}; retaining web equation labels."
            )
            validators = ()
    else:
        validators = ()

    if explicit_validator:
        outcome = parser.parse(
            SourceBundle(primary=primary, validators=validators)
        )
    else:
        outcome = probe
    warnings = list(outcome.warnings)
    if reasons and not explicit_validator:
        warnings = [
            item
            for item in warnings
            if item != PDF_VALIDATOR_MISSING_WARNING
        ]
    warnings.extend(acquisition_warnings)
    return (
        outcome.document,
        tuple(item.artifact_digest for item in validators),
        tuple(dict.fromkeys(warnings)),
    )


def _current_run(paths: CompanionProjectPaths) -> str:
    value = paths.current_run_id
    if value is None:
        raise CompanionProjectError(
            "run_not_found", "project has no selected build run"
        )
    return value


def _edition_data(state: Any, review_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "edition_digest": state.edition_digest,
        "selected_revision_digests": list(
            state.selected_revision_digests
        ),
        "committed_review_ids": list(review_ids),
        "review_count": len(review_ids),
        "revision_count": len(state.revisions),
    }


def _source_warnings(
    paths: CompanionProjectPaths, run_id: str
) -> tuple[CommandWarning, ...]:
    return tuple(
        CommandWarning("source_diagnostic", item)
        for item in paths.source_diagnostics(run_id)
    )


def _build_warnings(
    paths: CompanionProjectPaths, run_id: str
) -> tuple[CommandWarning, ...]:
    diagnostics = CompanionService(paths.jobs_root).build_diagnostics(
        run_id
    )
    if diagnostics is None:
        return ()
    return tuple(
        CommandWarning("equation_label_review", item)
        for item in diagnostics["warnings"]
    )


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


def _reader_labels_file(value: str) -> dict[str, str]:
    path = Path(value)
    if not path.is_file():
        raise _UsageError("--reader-labels must be an existing JSON file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _UsageError(
            f"--reader-labels must contain valid JSON: {exc}"
        ) from exc
    if not isinstance(document, Mapping) or not document:
        raise _UsageError(
            "--reader-labels must contain a non-empty JSON object"
        )
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(item, str)
        or not item.strip()
        for key, item in document.items()
    ):
        raise _UsageError(
            "--reader-labels must contain only non-empty string keys and values"
        )
    return {
        str(key).strip(): str(item).strip()
        for key, item in document.items()
    }


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
