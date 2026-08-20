"""Protocol CLI for independent translation workflow steps."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arc_jobs import (
    CommandArtifact,
    CommandError,
    CommandResult,
    CommandRun,
    CommandStatus,
    CommandWarning,
    InvalidRunIdError,
    InvalidStateError,
    RunNotFoundError,
    command_result_from_snapshot,
    command_result_json,
    snapshot_data,
)
from arc_llm import HostAuthority, LLMExecutionOptions, ModelSelection
from arc_document import ArcDocumentService

from .contracts import (
    BlocksRequest,
    GenerationRecipe,
    GlossaryRequest,
    LanguageRequest,
    ExecutionOptions,
)
from .delivery import (
    TranslationDeliveryError,
    publish_translation_layer,
    validate_translation_layer,
)
from .project import TranslationProject, TranslationProjectError
from .service import TranslationService, TranslationServiceError
from .source import TranslationSourceError, resolve_translation_source
from .workflow import GlossaryResult, LanguageResult, TranslationResult


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
        prog="arc-translate",
        description=(
            "Run source-bound language detection, glossary, and block "
            "translation steps. Results are always JSON."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    language = commands.add_parser(
        "detect-language",
        help="detect source language and bind a target language",
        description="Detect a verified source language and record the target language.",
    )
    language.add_argument("source", help="local source")
    language.add_argument("--target-language", required=True, help="requested target language")
    _project_argument(language)
    _generation_arguments(language)

    glossary = commands.add_parser(
        "build-glossary",
        help="build a bilingual glossary",
        description="Build a reviewed bilingual glossary for the verified source.",
    )
    glossary.add_argument("source", help="same local source used for detection")
    glossary.add_argument(
        "--approx-term-count", type=int, default=50, help="target term count (default: 50)"
    )
    _project_argument(glossary)
    _generation_arguments(glossary)

    blocks = commands.add_parser(
        "translate-blocks",
        help="translate source blocks with the selected glossary",
        description="Translate verified source blocks using the selected language and glossary.",
    )
    blocks.add_argument("source", help="same local source used for detection")
    _project_argument(blocks)
    _generation_arguments(blocks)

    control_summaries = {
        "status": "inspect selected translation steps",
        "stop": "request a cooperative stop",
        "validate": "validate the selected translation run",
    }
    for name, summary in control_summaries.items():
        command = commands.add_parser(name, help=summary, description=summary.capitalize() + ".")
        _project_argument(command)
        if name == "stop":
            command.add_argument("--reason", help="human-readable stop reason")

    resume = commands.add_parser(
        "resume",
        help="resume the selected translation step",
        description="Resume the currently selected paused, interrupted, or failed translation step.",
    )
    _project_argument(resume)
    resume.add_argument("--input", help="JSON object or a path to one")
    _document_cache_argument(resume)
    _host_authority_argument(resume)

    get_result = commands.add_parser(
        "get-result",
        help="read a verified selected translation result",
        description="Read one verified successful selected translation result.",
    )
    _project_argument(get_result)
    get_result.add_argument(
        "--step",
        required=True,
        choices=("language", "glossary", "blocks"),
        help="selected translation step to read",
    )
    return parser


def _project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", required=True, help="translation project directory")


def _generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default="auto", help="LLM provider (default: auto)")
    parser.add_argument("--model", help="provider-specific model name")
    parser.add_argument("--refresh", action="store_true", help="refresh cached source data")
    _host_authority_argument(parser)
    _document_cache_argument(parser)


def _document_cache_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--document-cache-root",
        help=(
            "arc-document cache root; defaults to ARC_DOCUMENT_CACHE or "
            "<current-directory>/.arc/cache/arc-document"
        ),
    )


def _host_authority_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host-authority",
        choices=tuple(item.value for item in HostAuthority),
        default=HostAuthority.UNKNOWN.value,
        help="host permission attestation; unrestricted must be explicit",
    )


def _execution(args: argparse.Namespace) -> ExecutionOptions:
    return ExecutionOptions(
        llm=LLMExecutionOptions(host_authority=HostAuthority(args.host_authority))
    )


def _help_command(arguments: list[str]) -> str:
    command = (
        arguments[0]
        if arguments
        and arguments[0]
        in {
            "detect-language",
            "build-glossary",
            "translate-blocks",
            "status",
            "resume",
            "stop",
            "validate",
            "get-result",
        }
        else None
    )
    return " ".join(
        part for part in ("arc-translate", command, "--help") if part is not None
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
        TranslationProjectError,
        TranslationServiceError,
        TranslationSourceError,
        TranslationDeliveryError,
    ) as exc:
        result = _failed(
            exc.code,
            str(exc),
            details=(
                {"help_command": _help_command(arguments)}
                if exc.code == "invalid_request"
                else None
            ),
        )
    except OSError as exc:
        result = _failed("local_io_error", str(exc))
    except Exception as exc:
        code = str(getattr(exc, "code", "internal_error"))
        result = _failed(
            code,
            str(exc),
            details=(
                {"help_command": _help_command(arguments)}
                if code == "invalid_request"
                else None
            ),
        )
    sys.stdout.write(command_result_json(result) + "\n")
    return 0 if result.status is CommandStatus.COMPLETED else 1


def _dispatch(args: argparse.Namespace) -> CommandResult:
    if args.command == "detect-language":
        return _detect_language(args)
    if args.command == "build-glossary":
        return _build_glossary(args)
    if args.command == "translate-blocks":
        return _translate_blocks(args)
    if args.command == "status":
        return _status(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "validate":
        return _validate(args)
    if args.command == "get-result":
        return _get_result(args)
    raise _UsageError(f"unsupported command: {args.command}")


def _detect_language(args: argparse.Namespace) -> CommandResult:
    recipe = _recipe(args)
    if not args.target_language.strip():
        raise _UsageError("--target-language must be non-empty")
    project = TranslationProject.open(args.project_dir)
    paper = ArcDocumentService(cache_root=args.document_cache_root)
    source = resolve_translation_source(
        paper, args.source, refresh=args.refresh
    )
    service = TranslationService(project.jobs_root)
    snapshot = service.prepare_language(
        LanguageRequest(source, args.target_language), recipe=recipe
    )
    project.select("language", snapshot.run_id)
    snapshot = service.execute(snapshot.run_id, execution=_execution(args))
    return _snapshot_result(project, service, snapshot)


def _build_glossary(args: argparse.Namespace) -> CommandResult:
    recipe = _recipe(args)
    if not 1 <= args.approx_term_count <= 200:
        raise _UsageError("--approx-term-count must be between 1 and 200")
    project = TranslationProject.load(args.project_dir)
    language_run = _required_step(project, "language")
    service = TranslationService(project.jobs_root)
    language = service.result(language_run)
    if not isinstance(language, LanguageResult):
        raise TranslationServiceError(
            "prerequisite_invalid", "language prerequisite has the wrong type"
        )
    paper = ArcDocumentService(cache_root=args.document_cache_root)
    source = resolve_translation_source(
        paper, args.source, refresh=args.refresh
    )
    _require_same_source(language, source)
    snapshot = service.prepare_glossary(
        GlossaryRequest(
            source,
            language.target_language,
            args.approx_term_count,
            service.result_source(language_run),
        ),
        recipe=recipe,
    )
    project.select("glossary", snapshot.run_id)
    snapshot = service.execute(
        snapshot.run_id,
        keyword_provider=_keyword_provider(paper),
        execution=_execution(args),
    )
    return _snapshot_result(project, service, snapshot)


def _translate_blocks(args: argparse.Namespace) -> CommandResult:
    recipe = _recipe(args)
    project = TranslationProject.load(args.project_dir)
    language_run = _required_step(project, "language")
    glossary_run = _required_step(project, "glossary")
    service = TranslationService(project.jobs_root)
    language = service.result(language_run)
    glossary = service.result(glossary_run)
    if not isinstance(language, LanguageResult) or not isinstance(
        glossary, GlossaryResult
    ):
        raise TranslationServiceError(
            "prerequisite_invalid", "translation prerequisites have wrong types"
        )
    paper = ArcDocumentService(cache_root=args.document_cache_root)
    source = resolve_translation_source(
        paper, args.source, refresh=args.refresh
    )
    _require_same_source(language, source)
    snapshot = service.prepare_blocks(
        BlocksRequest(
            source,
            language.target_language,
            service.result_source(language_run),
            service.result_source(glossary_run),
        ),
        recipe=recipe,
    )
    project.select("blocks", snapshot.run_id)
    snapshot = service.execute(snapshot.run_id, execution=_execution(args))
    return _snapshot_result(project, service, snapshot)


def _status(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = _current_run(project)
    service = TranslationService(project.jobs_root)
    selected_snapshot = service.inspect(run_id).snapshot
    base = command_result_from_snapshot(selected_snapshot, query=True)
    steps: dict[str, Any] = {}
    for step in ("language", "glossary", "blocks"):
        selected = project.run_id(step)
        if selected is not None:
            steps[step] = snapshot_data(service.inspect(selected).snapshot)
    delivery_ok = (
        project.current_step == "blocks"
        and selected_snapshot.status.value == "succeeded"
        and project.translation_layer.is_file()
    )
    warnings = base.warnings
    if (
        project.current_step == "blocks"
        and selected_snapshot.status.value == "succeeded"
        and not delivery_ok
    ):
        warnings = (*warnings, CommandWarning(
            "delivery_missing",
            "successful translation has no visible arc-render Layer; "
            "resume or rerun its publishing step",
        ))
    return CommandResult(
        base.status,
        run=base.run,
        data={
            "current_step": project.current_step,
            "run": snapshot_data(selected_snapshot),
            "steps": steps,
        },
        artifacts=(
            CommandArtifact("layer", run_id, str(project.translation_layer)),
        )
        if delivery_ok
        else (),
        warnings=warnings,
        error=base.error,
        resume=base.resume,
    )


def _resume(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = _current_run(project)
    service = TranslationService(project.jobs_root)
    keyword_provider = None
    if project.current_step == "glossary":
        paper = ArcDocumentService(cache_root=args.document_cache_root)
        keyword_provider = _keyword_provider(paper)
    snapshot = service.resume(
        run_id,
        input=_json_input(args.input) if args.input is not None else None,
        keyword_provider=keyword_provider,
        execution=_execution(args),
    )
    return _snapshot_result(project, service, snapshot)


def _stop(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = _current_run(project)
    view = TranslationService(project.jobs_root).stop(
        run_id, reason=args.reason
    )
    return CommandResult(
        CommandStatus.COMPLETED,
        run=CommandRun(view.snapshot.run_id, view.snapshot.revision),
        data={
            "run": snapshot_data(view.snapshot),
            "stop_requested": view.stop_request is not None,
        },
    )


def _validate(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = _current_run(project)
    service = TranslationService(project.jobs_root)
    report = service.validate(run_id)
    if report.ok:
        snapshot = service.inspect(run_id).snapshot
        if snapshot.status.value == "succeeded":
            result = service.result(run_id)
            if isinstance(result, TranslationResult):
                validate_translation_layer(project, result=result)
        return CommandResult(
            CommandStatus.COMPLETED,
            run=CommandRun(snapshot.run_id, snapshot.revision),
            data={
                "valid": True,
                "issues": [],
                "delivery": (
                    {"layer": str(project.translation_layer)}
                    if snapshot.status.value == "succeeded"
                    and isinstance(result, TranslationResult)
                    else None
                ),
            },
            artifacts=(
                CommandArtifact(
                    "layer", run_id, str(project.translation_layer)
                ),
            )
            if snapshot.status.value == "succeeded"
            and isinstance(result, TranslationResult)
            else (),
        )
    return _failed(
        "run_invalid",
        "run validation failed",
        details={
            "issues": [
                {
                    "code": item.code,
                    "message": item.message,
                    "path": list(item.path),
                }
                for item in report.issues
            ]
        },
    )


def _get_result(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = project.run_id(args.step)
    if run_id is None:
        raise TranslationServiceError(
            "run_not_selected",
            f"project has no selected {args.step} translation run",
        )
    service = TranslationService(project.jobs_root)
    try:
        snapshot = service.inspect(run_id).snapshot
        result = service.result(run_id)
    except RunNotFoundError as exc:
        raise TranslationServiceError(
            "run_not_found",
            f"selected {args.step} translation run does not exist",
        ) from exc
    except InvalidRunIdError as exc:
        raise TranslationProjectError(
            "project_state_invalid",
            f"selected {args.step} translation run ID is invalid",
        ) from exc
    except InvalidStateError as exc:
        raise TranslationServiceError(
            "result_invalid",
            f"selected {args.step} translation run state is invalid",
        ) from exc

    expected_type = {
        "language": LanguageResult,
        "glossary": GlossaryResult,
        "blocks": TranslationResult,
    }[args.step]
    if not isinstance(result, expected_type):
        raise TranslationServiceError(
            "result_invalid",
            f"selected {args.step} translation run has the wrong result type",
        )

    data: dict[str, Any] = {
        "step": args.step,
        "result": result.to_document(),
    }
    artifacts: tuple[CommandArtifact, ...] = ()
    if args.step == "blocks" and project.translation_layer.is_file():
        assert isinstance(result, TranslationResult)
        data["delivery"] = {
            "layer": str(project.translation_layer),
            "revision_count": len(result.revision_artifacts),
        }
        artifacts = (
            CommandArtifact("layer", run_id, str(project.translation_layer)),
        )
    return CommandResult(
        CommandStatus.COMPLETED,
        run=CommandRun(snapshot.run_id, snapshot.revision),
        data=data,
        artifacts=artifacts,
    )


def _snapshot_result(
    project: TranslationProject,
    service: TranslationService,
    snapshot: Any,
) -> CommandResult:
    base = command_result_from_snapshot(snapshot)
    if snapshot.status.value != "succeeded":
        return CommandResult(
            base.status,
            run=base.run,
            data={"run": snapshot_data(snapshot)},
            artifacts=base.artifacts,
            warnings=base.warnings,
            error=base.error,
            resume=base.resume,
        )
    result = service.result(snapshot.run_id)
    if not isinstance(result, TranslationResult):
        return CommandResult(
            base.status,
            run=base.run,
            data={"run": snapshot_data(snapshot)},
            artifacts=base.artifacts,
            warnings=base.warnings,
            error=base.error,
            resume=base.resume,
        )
    delivery = publish_translation_layer(
        project,
        result=result,
        revision_payloads=service.revision_payloads(
            snapshot.run_id, result
        ),
    )
    revision_artifacts = tuple(
        CommandArtifact(
            "fragment-revision",
            item.revision.fragment_id,
            str(project.root / item.revision.path),
        )
        for item in result.revision_artifacts
    )
    return CommandResult(
        base.status,
        run=base.run,
        data={
            "run": snapshot_data(snapshot),
            "delivery": {
                "layer": str(delivery),
                "revision_count": len(result.revision_artifacts),
            },
        },
        artifacts=(
            CommandArtifact("layer", snapshot.run_id, str(delivery)),
            *revision_artifacts,
        ),
        warnings=base.warnings,
        error=base.error,
        resume=base.resume,
    )


def _recipe(args: argparse.Namespace) -> GenerationRecipe:
    if not args.provider.strip():
        raise _UsageError("--provider must be non-empty")
    if args.model is not None and not args.model.strip():
        raise _UsageError("--model must be non-empty")
    if args.model is not None and args.provider == "auto":
        raise _UsageError("--model requires an explicit --provider")
    return GenerationRecipe(
        model=ModelSelection(
            provider=args.provider,
            model=args.model,
            tier="medium",
        )
    )


def _keyword_provider(paper: ArcDocumentService) -> Any:
    try:
        from arc_document import KeywordInventoryService

        return KeywordInventoryService(paper.term_inventory_store)
    except (ImportError, AttributeError) as exc:
        raise TranslationServiceError(
            "keyword_provider_unavailable",
            "installed arc-document does not expose KeywordInventoryService",
        ) from exc


def _required_step(project: TranslationProject, step: str) -> str:
    run_id = project.run_id(step)
    if run_id is None:
        raise TranslationServiceError(
            "prerequisite_missing",
            f"{step} must complete successfully before this command",
        )
    return run_id


def _current_run(project: TranslationProject) -> str:
    run_id = project.current_run_id
    if run_id is None:
        raise TranslationServiceError(
            "run_not_selected", "project has no selected translation run"
        )
    return run_id


def _require_same_source(language: LanguageResult, source: Any) -> None:
    if (
        language.document_digest != source.document_digest
        or language.source_digest != source.source_digest
    ):
        raise TranslationServiceError(
            "source_binding_mismatch",
            "SOURCE differs from the verified language-detection source",
        )


def _json_input(raw: str) -> Mapping[str, Any]:
    path = Path(raw)
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else raw
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _UsageError("--input must be a JSON object or a readable JSON file") from exc
    if not isinstance(value, Mapping):
        raise _UsageError("--input must contain a JSON object")
    return value


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


__all__ = ["main"]
