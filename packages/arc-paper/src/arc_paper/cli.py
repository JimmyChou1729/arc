"""Protocol-only command line interface for deterministic arc-paper operations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from typing import Any

from arc_jobs import (
    CommandError,
    CommandResult,
    CommandStatus,
    CommandWarning,
    command_result_json,
    run_control_main,
)

from .registry import dispatch_operation, registry_document, to_json_value


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="arc-paper", add_help=False)
    parser.add_argument("--help", action="store_true", dest="root_help")
    commands = parser.add_subparsers(dest="command")

    extract = commands.add_parser("extract-paper-ids", add_help=False)
    extract.add_argument("text", nargs="+")

    safe = commands.add_parser("safe-dir-name", add_help=False)
    safe.add_argument("ids", nargs="+")

    for name in (
        "get-title",
        "get-abstract",
        "get-authors",
        "get-metadata",
        "get-citer-count",
    ):
        command = commands.add_parser(name, add_help=False)
        _paper_arguments(command)

    references = commands.add_parser("get-references", add_help=False)
    _paper_arguments(references)
    references.add_argument("--enrich", action="store_true")

    citers = commands.add_parser("get-citers", add_help=False)
    _paper_arguments(citers)
    citers.add_argument("--limit", type=int, default=1000)
    citers.add_argument(
        "--sort", choices=("mostrecent", "mostcited"), default="mostrecent"
    )

    search = commands.add_parser("search-metadata", add_help=False)
    search.add_argument("query", nargs="+")
    search.add_argument("--limit", type=int, default=20)

    toc = commands.add_parser("get-arxiv-table-of-contents", add_help=False)
    _arxiv_arguments(toc)

    section = commands.add_parser("get-arxiv-section", add_help=False)
    _arxiv_arguments(section)
    section.add_argument("selector")

    full_text = commands.add_parser("search-arxiv-full-text", add_help=False)
    _arxiv_arguments(full_text)
    full_text.add_argument("query", nargs="+")
    full_text.add_argument("--limit", type=int, default=20)
    full_text.add_argument("--context-lines", type=int, default=1)
    full_text.add_argument("--case-sensitive", action="store_true")

    equations = commands.add_parser("search-arxiv-equations", add_help=False)
    _arxiv_arguments(equations)
    equations.add_argument("query", nargs="+")
    equations.add_argument("--limit", type=int, default=20)
    equations.add_argument("--case-sensitive", action="store_true")

    for name in ("fetch-arxiv-auto", "fetch-arxiv-pdf"):
        command = commands.add_parser(name, add_help=False)
        _paper_arguments(command)
        command.add_argument("--cache-root")

    imported = commands.add_parser("import-source", add_help=False)
    imported.add_argument("path")
    imported.add_argument(
        "--format",
        dest="source_format",
        choices=("html", "markdown", "tex", "pdf"),
    )
    imported.add_argument("--cache-root")

    parsed = commands.add_parser("parse-local", add_help=False)
    parsed.add_argument("primary_path")
    parsed.add_argument("--validator", action="append", default=[])
    parsed.add_argument(
        "--format",
        dest="primary_format",
        choices=("html", "markdown", "tex", "pdf"),
    )
    parsed.add_argument(
        "--validator-format",
        action="append",
        default=[],
        choices=("html", "markdown", "tex", "pdf"),
    )
    parsed.add_argument(
        "--policy",
        choices=("none", "deterministic_only", "visual_all_pages"),
    )
    parsed.add_argument("--cache-root")

    commands.add_parser("operations", add_help=False)
    return parser


def _paper_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paper_id")
    parser.add_argument("--refresh", action="store_true")


def _arxiv_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("arxiv_id")
    parser.add_argument("--refresh", action="store_true")


def _parameters(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "extract-paper-ids":
        return {"text": " ".join(args.text)}
    if command == "safe-dir-name":
        return {"ids": args.ids}
    if command in {
        "get-title",
        "get-abstract",
        "get-authors",
        "get-metadata",
        "get-citer-count",
    }:
        return {"paper_id": args.paper_id, "refresh": args.refresh}
    if command == "get-references":
        return {
            "paper_id": args.paper_id,
            "refresh": args.refresh,
            "enrich": args.enrich,
        }
    if command == "get-citers":
        return {
            "paper_id": args.paper_id,
            "refresh": args.refresh,
            "limit": args.limit,
            "sort": args.sort,
        }
    if command == "search-metadata":
        return {"query": " ".join(args.query), "limit": args.limit}
    if command == "get-arxiv-table-of-contents":
        return {"arxiv_id": args.arxiv_id, "refresh": args.refresh}
    if command == "get-arxiv-section":
        return {
            "arxiv_id": args.arxiv_id,
            "selector": args.selector,
            "refresh": args.refresh,
        }
    if command == "search-arxiv-full-text":
        return {
            "arxiv_id": args.arxiv_id,
            "query": " ".join(args.query),
            "limit": args.limit,
            "context_lines": args.context_lines,
            "case_sensitive": args.case_sensitive,
            "refresh": args.refresh,
        }
    if command == "search-arxiv-equations":
        return {
            "arxiv_id": args.arxiv_id,
            "query": " ".join(args.query),
            "limit": args.limit,
            "case_sensitive": args.case_sensitive,
            "refresh": args.refresh,
        }
    if command in {"fetch-arxiv-auto", "fetch-arxiv-pdf"}:
        return {
            "paper_id": args.paper_id,
            "refresh": args.refresh,
            "cache_root": args.cache_root,
        }
    if command == "import-source":
        return {
            "path": args.path,
            "source_format": args.source_format,
            "cache_root": args.cache_root,
        }
    if command == "parse-local":
        return {
            "primary_path": args.primary_path,
            "validator_paths": args.validator,
            "validator_formats": args.validator_format,
            "primary_format": args.primary_format,
            "policy": args.policy,
            "cache_root": args.cache_root,
        }
    raise _UsageError(f"unsupported command: {command}")


def _help_data() -> dict[str, Any]:
    return {
        "program": "arc-paper",
        "commands": [
            "extract-paper-ids",
            "safe-dir-name",
            "get-title",
            "get-abstract",
            "get-authors",
            "get-metadata",
            "get-references",
            "get-citers",
            "get-citer-count",
            "search-metadata",
            "get-arxiv-table-of-contents",
            "get-arxiv-section",
            "search-arxiv-full-text",
            "search-arxiv-equations",
            "fetch-arxiv-auto",
            "fetch-arxiv-pdf",
            "import-source",
            "parse-local",
            "operations",
            "status (arc-jobs)",
            "cancel (arc-jobs)",
            "validate (arc-jobs)",
        ],
    }


def _result_data(value: Any) -> Mapping[str, Any]:
    encoded = to_json_value(value)
    if isinstance(encoded, Mapping):
        return dict(encoded)
    return {"result": encoded}


def _emit(result: CommandResult, *, exit_code: int) -> int:
    sys.stdout.write(command_result_json(result) + "\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"status", "cancel", "validate"}:
        # arc-jobs is the sole implementation of generic durable-run controls.
        return run_control_main(arguments)
    try:
        args = _parser().parse_args(arguments)
        if args.root_help or args.command is None:
            return _emit(
                CommandResult(CommandStatus.COMPLETED, data=_help_data()),
                exit_code=0,
            )
        if args.command == "operations":
            return _emit(
                CommandResult(
                    CommandStatus.COMPLETED,
                    data=registry_document(),
                ),
                exit_code=0,
            )
        value = dispatch_operation(args.command, _parameters(args))
        warnings: tuple[CommandWarning, ...] = ()
        raw_warnings = (
            value.get("warnings", ())
            if isinstance(value, Mapping)
            else getattr(value, "warnings", ())
        )
        if raw_warnings:
            warnings = tuple(
                CommandWarning("paper_warning", str(item)) for item in raw_warnings
            )
        return _emit(
            CommandResult(
                CommandStatus.COMPLETED,
                data=_result_data(value),
                warnings=warnings,
            ),
            exit_code=0,
        )
    except (_UsageError, ValueError, TypeError) as exc:
        code = str(getattr(exc, "code", "invalid_request"))
        message = str(getattr(exc, "message", str(exc)))
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError(code, message),
            ),
            exit_code=2,
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "arc_paper_error"))
        message = str(getattr(exc, "message", str(exc)))
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError(code, message),
            ),
            exit_code=1,
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
