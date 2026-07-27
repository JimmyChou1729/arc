"""Six-command protocol CLI for source-anchored Companion builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
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

from .project import CompanionProjectError, CompanionProjectPaths
from .reader_labels import ReaderLabelError, resolve_reader_labels
from .release import (
    CompanionReleaseError,
    CompanionReleasePublisher,
    validate_current_delivery,
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
from .translation_adapter import (
    CompanionTranslationRuntimeError,
    require_translation_runtime,
)
from .translation_reuse import (
    TranslationReuseError,
    TranslationReuseReceipt,
    TranslationReuseSource,
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
    build.add_argument(
        "--reuse-translation-from",
        help=(
            "reuse exact language, glossary, and translation results from the "
            "selected successful run in another Companion project, and supply "
            "its prior guide as optional model context"
        ),
    )
    _host_authority_argument(build)
    _paper_cache_argument(build)

    status = commands.add_parser(
        "status",
        help="inspect the selected build and active release",
        description="Inspect the selected Companion build and active release.",
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
    _paper_cache_argument(render)

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
        CompanionReleaseError,
        CompanionRenderError,
        CompanionServiceError,
        CompanionTranslationRuntimeError,
        TranslationReuseError,
        RichDocumentValidationError,
    ) as exc:
        code = str(exc.code)
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
    reuse_source = (
        TranslationReuseSource(args.reuse_translation_from)
        if args.reuse_translation_from is not None
        else None
    )
    reuse_plan = None
    if reuse_source is not None:
        reuse_plan = service.plan_translation_reuse(
            reuse_source, request, recipe
        )
        request = replace(
            request, translation_reuse_digest=reuse_plan.reuse_digest
        )
    run_id = companion_run_id(request, recipe)
    prepared = service.prepare(request, recipe=recipe, run_id=run_id)
    run_id = prepared.run_id
    if reuse_source is not None:
        assert reuse_plan is not None
        service.stage_translation_reuse(
            run_id, reuse_source, plan=reuse_plan
        )
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
        paper_cache_root=args.paper_cache_root,
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
        paper_cache_root=args.paper_cache_root,
    )


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
    (
        current,
        available_formats,
        release_warnings,
        delivery_warnings,
    ) = _status_release_state(paths)
    selected_run = snapshot_data(view.snapshot)
    release_matches_selected_run = (
        current is not None and current["run_id"] == run_id
    )
    active_release = (
        {
            **current,
            "available_formats": list(available_formats),
        }
        if current is not None
        else None
    )
    data: dict[str, Any] = {
        "selected_run": selected_run,
        "active_release": active_release,
        "release_matches_selected_run": release_matches_selected_run,
        "build_diagnostics": CompanionService(
            paths.jobs_root
        ).build_diagnostics(run_id),
    }
    receipt = CompanionService(paths.jobs_root).translation_reuse_receipt(
        run_id
    )
    if receipt is not None:
        data["translation_reuse"] = dict(receipt.document)
    artifacts = ()
    if (
        current is not None
        and release_matches_selected_run
        and not release_warnings
        and not delivery_warnings
    ):
        artifacts = (
            *(
                (
                    CommandArtifact(
                        "pdf",
                        str(current["release_id"]),
                        str(paths.delivery_pdf),
                    ),
                )
                if "pdf" in available_formats
                else ()
            ),
            CommandArtifact(
                "web", str(current["release_id"]), str(paths.delivery_html)
            ),
            CommandArtifact(
                "manifest",
                str(current["release_id"]),
                str(paths.root / str(current["manifest"])),
            ),
        )
    if receipt is not None:
        artifacts = (
            *artifacts,
            _translation_reuse_command_artifact(paths, run_id, receipt),
        )
    return CommandResult(
        base.status,
        run=base.run,
        data=data,
        artifacts=artifacts,
        warnings=(
            *_source_warnings(paths, run_id),
            *_build_warnings(paths, run_id),
            *release_warnings,
            *delivery_warnings,
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
    try:
        release = _publisher(
            paths, paper_cache_root=args.paper_cache_root
        ).publish(book, run_id=run_id)
    except CompanionRenderError as exc:
        return _unpublished_render_result(exc)
    roles = {"pdf", "web"} if args.format == "all" else {args.format}
    artifacts = [
        CommandArtifact("manifest", release.release_id, str(release.manifest))
    ]
    if "pdf" in roles and release.pdf is not None:
        artifacts.append(
            CommandArtifact("pdf", release.release_id, str(paths.delivery_pdf))
        )
    if "web" in roles:
        artifacts.append(
            CommandArtifact("web", release.release_id, str(paths.delivery_html))
        )
    return CommandResult(
        CommandStatus.COMPLETED,
        data={
            "release_id": release.release_id,
            "reused": release.reused,
            "available_formats": list(release.available_formats),
            "delivery": _delivery_paths(paths, release.available_formats),
        },
        artifacts=tuple(artifacts),
        warnings=_release_render_warnings(release),
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
        data={
            "release_id": release.release_id,
            "valid": True,
            "available_formats": list(release.available_formats),
            "delivery": _delivery_paths(paths, release.available_formats),
        },
        artifacts=(
            CommandArtifact(
                "manifest", release.release_id, str(release.manifest)
            ),
            *(
                (
                    CommandArtifact(
                        "pdf",
                        release.release_id,
                        str(paths.delivery_pdf),
                    ),
                )
                if release.pdf is not None
                else ()
            ),
            CommandArtifact(
                "web", release.release_id, str(paths.delivery_html)
            ),
        ),
    )


def _snapshot_result(
    paths: CompanionProjectPaths,
    snapshot: Any,
    *,
    warnings: tuple[str, ...] = (),
    paper_cache_root: str | Path | None = None,
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
    service = CompanionService(paths.jobs_root)
    book = service.accepted_book(snapshot.run_id)
    receipt = service.translation_reuse_receipt(snapshot.run_id)
    try:
        release = _publisher(
            paths, paper_cache_root=paper_cache_root
        ).publish(book, run_id=snapshot.run_id)
    except CompanionRenderError as exc:
        return CommandResult(
            CommandStatus.COMPLETED,
            run=base.run,
            data={
                "run": snapshot_data(snapshot),
                "published": False,
                "available_formats": [],
                "delivery": {},
                **(
                    {"translation_reuse": dict(receipt.document)}
                    if receipt is not None
                    else {}
                ),
            },
            artifacts=(
                (
                    _translation_reuse_command_artifact(
                        paths, snapshot.run_id, receipt
                    ),
                )
                if receipt is not None
                else ()
            ),
            warnings=(
                *command_warnings,
                _web_render_warning(exc),
            ),
        )
    return CommandResult(
        CommandStatus.COMPLETED,
        run=base.run,
        data={
            "run": snapshot_data(snapshot),
            "release_id": release.release_id,
            "reused": release.reused,
            "available_formats": list(release.available_formats),
            "delivery": _delivery_paths(paths, release.available_formats),
            **(
                {"translation_reuse": dict(receipt.document)}
                if receipt is not None
                else {}
            ),
        },
        artifacts=(
            *(
                (
                    CommandArtifact(
                        "pdf",
                        release.release_id,
                        str(paths.delivery_pdf),
                    ),
                )
                if release.pdf is not None
                else ()
            ),
            CommandArtifact("web", release.release_id, str(paths.delivery_html)),
            CommandArtifact(
                "manifest", release.release_id, str(release.manifest)
            ),
            *(
                (
                    _translation_reuse_command_artifact(
                        paths, snapshot.run_id, receipt
                    ),
                )
                if receipt is not None
                else ()
            ),
        ),
        warnings=(
            *command_warnings,
            *_release_render_warnings(release),
        ),
    )


def _translation_reuse_command_artifact(
    paths: CompanionProjectPaths,
    run_id: str,
    receipt: TranslationReuseReceipt,
) -> CommandArtifact:
    return CommandArtifact(
        "translation_reuse_receipt",
        receipt.artifact_ref.artifact_id,
        str(
            paths.jobs_root
            / "runs"
            / run_id
            / receipt.artifact_ref.relative_path
        ),
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


def _publisher(
    paths: CompanionProjectPaths,
    *,
    paper_cache_root: str | Path | None = None,
) -> CompanionReleasePublisher:
    paper: ArcPaperService | None = None

    def load_asset(digest: str) -> bytes | None:
        frozen = paths.frozen_asset_path(digest)
        try:
            if frozen.is_file():
                payload = frozen.read_bytes()
                if hashlib.sha256(payload).hexdigest() == digest:
                    return payload
        except OSError:
            pass
        nonlocal paper
        if paper is None:
            paper = ArcPaperService(cache_root=paper_cache_root)
        try:
            asset = paper.repository.get_asset(digest)
            payload = paper.repository.read_asset_bytes(asset)
            paths.freeze_asset(digest, payload)
            return payload
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


def _delivery_paths(
    paths: CompanionProjectPaths,
    available_formats: tuple[str, ...],
) -> dict[str, str]:
    delivery = {"html": str(paths.delivery_html)}
    if "pdf" in available_formats:
        delivery["pdf"] = str(paths.delivery_pdf)
    return delivery


def _release_render_warnings(
    release: Any,
) -> tuple[CommandWarning, ...]:
    if "pdf" in release.available_formats:
        return ()
    messages = tuple(release.warnings) or (
        "PDF rendering or validation failed; published the Web reader only.",
    )
    return tuple(
        CommandWarning(
            "pdf_render_failed",
            message,
            {
                "format": "pdf",
                "available_formats": list(release.available_formats),
            },
        )
        for message in messages
    )


def _web_render_warning(exc: CompanionRenderError) -> CommandWarning:
    return CommandWarning(
        "web_render_failed",
        str(exc),
        {"format": "web", "available_formats": []},
    )


def _unpublished_render_result(
    exc: CompanionRenderError,
) -> CommandResult:
    return CommandResult(
        CommandStatus.COMPLETED,
        data={
            "published": False,
            "available_formats": [],
            "delivery": {},
        },
        warnings=(_web_render_warning(exc),),
    )


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


def _status_release_state(
    paths: CompanionProjectPaths,
) -> tuple[
    dict[str, Any] | None,
    tuple[str, ...],
    tuple[CommandWarning, ...],
    tuple[CommandWarning, ...],
]:
    # Do not create a lease file for a project that has never published. A
    # concurrent first publication linearizes after this empty observation.
    if paths.current_release() is None:
        return None, (), (), ()
    with file_lease(paths.delivery_lease, blocking=True):
        current = paths.current_release()
        release_warnings = _release_pointer_warnings(paths, current)
        available_formats, delivery_warnings = _delivery_state(
            paths,
            current,
            release_warnings=release_warnings,
        )
        return (
            current,
            available_formats,
            release_warnings,
            delivery_warnings,
        )


def _delivery_state(
    paths: CompanionProjectPaths,
    current: Mapping[str, Any] | None,
    *,
    release_warnings: tuple[CommandWarning, ...],
) -> tuple[tuple[str, ...], tuple[CommandWarning, ...]]:
    if current is None or release_warnings:
        return (), ()
    try:
        available_formats = validate_current_delivery(paths, dict(current))
    except CompanionReleaseError as exc:
        return (), (CommandWarning("delivery_invalid", str(exc)),)
    return available_formats, ()


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
