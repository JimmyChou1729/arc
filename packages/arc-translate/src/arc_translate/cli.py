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
    command_result_from_snapshot,
    command_result_json,
    snapshot_data,
)
from arc_llm import ModelSelection
from arc_paper import ArcPaperService

from .contracts import (
    BlocksRequest,
    GenerationRecipe,
    GlossaryRequest,
    LanguageRequest,
)
from .project import TranslationProject, TranslationProjectError
from .service import TranslationService, TranslationServiceError
from .source import TranslationSourceError, resolve_translation_source
from .workflow import GlossaryResult, LanguageResult


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="arc-translate")
    commands = parser.add_subparsers(dest="command", required=True)

    language = commands.add_parser("detect-language")
    language.add_argument("source")
    language.add_argument("--target-language", required=True)
    _project_argument(language)
    _generation_arguments(language)

    glossary = commands.add_parser("build-glossary")
    glossary.add_argument("source")
    glossary.add_argument("--approx-term-count", type=int, default=50)
    _project_argument(glossary)
    _generation_arguments(glossary)

    blocks = commands.add_parser("translate-blocks")
    blocks.add_argument("source")
    _project_argument(blocks)
    _generation_arguments(blocks)

    for name in ("status", "stop", "validate"):
        command = commands.add_parser(name)
        _project_argument(command)
        if name == "stop":
            command.add_argument("--reason")

    resume = commands.add_parser("resume")
    _project_argument(resume)
    resume.add_argument("--input", help="JSON object or a path to one")
    return parser


def _project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--json", action="store_true")


def _generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--model")
    parser.add_argument("--refresh", action="store_true")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = _dispatch(args)
    except _UsageError as exc:
        result = _failed("invalid_request", str(exc))
    except (
        TranslationProjectError,
        TranslationServiceError,
        TranslationSourceError,
    ) as exc:
        result = _failed(exc.code, str(exc))
    except OSError as exc:
        result = _failed("local_io_error", str(exc))
    except Exception as exc:
        result = _failed(
            str(getattr(exc, "code", "internal_error")),
            str(exc),
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
    raise _UsageError(f"unsupported command: {args.command}")


def _detect_language(args: argparse.Namespace) -> CommandResult:
    recipe = _recipe(args)
    if not args.target_language.strip():
        raise _UsageError("--target-language must be non-empty")
    project = TranslationProject.open(args.project_dir)
    paper = ArcPaperService(cache_root=project.paper_cache_root)
    source = resolve_translation_source(
        paper, args.source, refresh=args.refresh
    )
    service = TranslationService(project.jobs_root)
    snapshot = service.prepare_language(
        LanguageRequest(source, args.target_language), recipe=recipe
    )
    project.select("language", snapshot.run_id)
    snapshot = service.execute(snapshot.run_id)
    return _snapshot_result(snapshot)


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
    paper = ArcPaperService(cache_root=project.paper_cache_root)
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
    )
    return _snapshot_result(snapshot)


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
    paper = ArcPaperService(cache_root=project.paper_cache_root)
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
    snapshot = service.execute(snapshot.run_id)
    return _snapshot_result(snapshot)


def _status(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = _current_run(project)
    service = TranslationService(project.jobs_root)
    base = command_result_from_snapshot(service.inspect(run_id).snapshot)
    steps: dict[str, Any] = {}
    for step in ("language", "glossary", "blocks"):
        selected = project.run_id(step)
        if selected is not None:
            steps[step] = snapshot_data(service.inspect(selected).snapshot)
    return CommandResult(
        base.status,
        run=base.run,
        data={
            "current_step": project.current_step,
            "run": snapshot_data(service.inspect(run_id).snapshot),
            "steps": steps,
        },
        artifacts=base.artifacts,
        error=base.error,
        resume=base.resume,
    )


def _resume(args: argparse.Namespace) -> CommandResult:
    project = TranslationProject.load(args.project_dir)
    run_id = _current_run(project)
    paper = ArcPaperService(cache_root=project.paper_cache_root)
    snapshot = TranslationService(project.jobs_root).resume(
        run_id,
        input=_json_input(args.input) if args.input is not None else None,
        keyword_provider=_keyword_provider(paper),
    )
    return _snapshot_result(snapshot)


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
            service.result(run_id)
        return CommandResult(
            CommandStatus.COMPLETED,
            run=CommandRun(snapshot.run_id, snapshot.revision),
            data={"valid": True, "issues": []},
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


def _snapshot_result(snapshot: Any) -> CommandResult:
    base = command_result_from_snapshot(snapshot)
    artifacts = base.artifacts
    if snapshot.result_ref is not None:
        artifacts = (
            CommandArtifact(
                "result",
                snapshot.result_ref.artifact_id,
                snapshot.result_ref.relative_path,
            ),
        )
    return CommandResult(
        base.status,
        run=base.run,
        data={"run": snapshot_data(snapshot)},
        artifacts=artifacts,
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


def _keyword_provider(paper: ArcPaperService) -> Any:
    try:
        from arc_paper import KeywordInventoryService

        return KeywordInventoryService(paper.term_inventory_store)
    except (ImportError, AttributeError) as exc:
        raise TranslationServiceError(
            "keyword_provider_unavailable",
            "installed arc-paper does not expose KeywordInventoryService",
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
