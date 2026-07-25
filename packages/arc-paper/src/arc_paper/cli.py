"""Protocol-only command line interface for arc-paper operations and workflows."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from typing import Any

from arc_jobs import (
    CommandError,
    CommandResult,
    CommandStatus,
    CommandWarning,
    command_result_json,
    command_result_from_snapshot,
    run_control_main,
)

from .registry import dispatch_operation, to_json_value
from .workflows.keywords import KeywordExtractionPaused


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
        prog="arc-paper",
        description=(
            "Read, search, import, and cache research papers through a "
            "stable JSON command protocol."
        ),
        epilog=(
            "Durable keyword runs can be inspected with arc-paper status, "
            "stopped with arc-paper stop, and checked with arc-paper validate."
        ),
    )
    commands = parser.add_subparsers(dest="command")

    extract = commands.add_parser(
        "extract-paper-ids",
        help="extract normalized paper identifiers from text",
        description="Extract normalized arXiv and INSPIRE paper identifiers from text.",
    )
    extract.add_argument("text", nargs="+", help="text containing one or more paper IDs")

    safe = commands.add_parser(
        "safe-dir-name",
        help="derive a filesystem-safe name from paper identifiers",
        description="Derive a deterministic filesystem-safe name from paper identifiers.",
    )
    safe.add_argument("ids", nargs="+", help="paper identifiers to include")

    metadata_commands = {
        "get-title": "read a paper title",
        "get-abstract": "read a paper abstract",
        "get-authors": "read a paper author list",
        "get-metadata": "read normalized paper metadata",
        "get-citer-count": "read a paper citation count",
    }
    for name, summary in metadata_commands.items():
        command = commands.add_parser(name, help=summary, description=summary.capitalize() + ".")
        _paper_arguments(command)

    references = commands.add_parser(
        "get-references",
        help="list references cited by a paper",
        description="List references cited by a paper, optionally with enriched metadata.",
    )
    _paper_arguments(references)
    references.add_argument(
        "--enrich", action="store_true", help="include available metadata for each reference"
    )

    citers = commands.add_parser(
        "get-citers",
        help="list papers that cite a paper",
        description="List papers that cite a paper.",
    )
    _paper_arguments(citers)
    citers.add_argument("--limit", type=int, default=1000, help="maximum results (default: 1000)")
    citers.add_argument(
        "--sort",
        choices=("mostrecent", "mostcited"),
        default="mostrecent",
        help="result ordering (default: mostrecent)",
    )

    search = commands.add_parser(
        "search-metadata",
        help="search the paper metadata index",
        description="Search paper titles, abstracts, authors, and identifiers.",
    )
    search.add_argument("query", nargs="+", help="metadata search query")
    search.add_argument("--limit", type=int, default=20, help="maximum results (default: 20)")

    cached_full_text = commands.add_parser(
        "search-cached-full-text",
        help="search all locally cached full text",
        description=(
            "Search every cached parsed document. Repeat --term with specific "
            "multi-word synonyms or alternate spellings for better coverage."
        ),
    )
    cached_full_text.add_argument(
        "--term", action="append", required=True, help="search phrase; repeat for alternatives"
    )
    cached_full_text.add_argument(
        "--limit", type=int, default=100, help="maximum matches (default: 100)"
    )
    cached_full_text.add_argument(
        "--context-lines", type=int, default=0, help="context lines around each match"
    )
    cached_full_text.add_argument(
        "--case-sensitive", action="store_true", help="match letter case exactly"
    )

    toc = commands.add_parser(
        "get-arxiv-table-of-contents",
        help="read an arXiv paper table of contents",
        description="Read the parsed section hierarchy of an arXiv paper.",
    )
    _arxiv_arguments(toc)

    section = commands.add_parser(
        "get-arxiv-section",
        help="read one parsed arXiv section",
        description="Read one arXiv section by title selector or zero-based ordinal.",
    )
    _arxiv_arguments(section)
    section_selector = section.add_mutually_exclusive_group(required=True)
    section_selector.add_argument("selector", nargs="?", help="section title or selector")
    section_selector.add_argument(
        "--ordinal", type=_section_ordinal, help="zero-based section ordinal"
    )

    full_text = commands.add_parser(
        "search-arxiv-full-text",
        help="search one arXiv paper's full text",
        description="Search the parsed full text of one arXiv paper.",
    )
    _arxiv_arguments(full_text)
    full_text.add_argument("query", nargs="+", help="full-text search query")
    full_text.add_argument("--limit", type=int, default=20, help="maximum matches (default: 20)")
    full_text.add_argument(
        "--context-lines", type=int, default=1, help="context lines around each match"
    )
    full_text.add_argument(
        "--case-sensitive", action="store_true", help="match letter case exactly"
    )

    equations = commands.add_parser(
        "search-arxiv-equations",
        help="search equations in one arXiv paper",
        description="Search extracted equations and nearby text in one arXiv paper.",
    )
    _arxiv_arguments(equations)
    equations.add_argument("query", nargs="+", help="equation or surrounding-text query")
    equations.add_argument("--limit", type=int, default=20, help="maximum matches (default: 20)")
    equations.add_argument(
        "--case-sensitive", action="store_true", help="match letter case exactly"
    )

    fetch_commands = {
        "fetch-arxiv-auto": "fetch and cache the best available arXiv source",
        "fetch-arxiv-pdf": "fetch and cache an arXiv PDF",
    }
    for name, summary in fetch_commands.items():
        command = commands.add_parser(name, help=summary, description=summary.capitalize() + ".")
        _paper_arguments(command)
        command.add_argument("--cache-root", help="override the paper cache directory")

    imported = commands.add_parser(
        "import-source",
        help="import a local source into the content-addressed cache",
        description="Import a local HTML, Markdown, TeX, or PDF source.",
    )
    imported.add_argument("path", help="local source path")
    imported.add_argument(
        "--format",
        dest="source_format",
        choices=("html", "markdown", "tex", "pdf"),
        help="source format override",
    )
    imported.add_argument("--cache-root", help="override the paper cache directory")

    parsed = commands.add_parser(
        "parse-local",
        help="parse a local source with optional validators",
        description="Parse a primary local source and compare optional validator documents.",
    )
    parsed.add_argument("primary_path", help="primary local source path")
    parsed.add_argument(
        "--validator", action="append", default=[], help="validator source path; repeat as needed"
    )
    parsed.add_argument(
        "--format",
        dest="primary_format",
        choices=("html", "markdown", "tex", "pdf"),
        help="primary source format override",
    )
    parsed.add_argument(
        "--validator-format",
        action="append",
        default=[],
        choices=("html", "markdown", "tex", "pdf"),
        help="format for the corresponding --validator",
    )
    parsed.add_argument(
        "--policy",
        choices=("none", "deterministic_only", "visual_all_pages"),
        help="validation policy",
    )
    parsed.add_argument("--cache-root", help="override the paper cache directory")

    keywords = commands.add_parser(
        "extract-keywords",
        help="build an approximate keyword inventory",
        description="Build a durable, approximate keyword inventory for a verified source.",
    )
    keywords.add_argument("source", help="paper identifier or local source")
    keywords.add_argument("--project-dir", required=True, help="durable workflow directory")
    keywords.add_argument(
        "--approx-count", type=_approx_count, default=50, help="target term count (default: 50)"
    )
    keywords.add_argument("--cache-root", help="override the paper cache directory")
    keywords.add_argument("--refresh", action="store_true", help="refresh remote paper data")
    keywords.add_argument("--llm-provider", default="auto", help="LLM provider (default: auto)")
    keywords.add_argument("--model", help="provider-specific model name")
    keywords.add_argument(
        "--model-tier",
        choices=("low", "medium", "high", "xhigh"),
        default="medium",
        help="model reasoning tier (default: medium)",
    )
    keywords.add_argument("--run-id", help="explicit durable run identifier")
    keywords.add_argument("--resume-input", type=_json_object, help="resume response JSON object")

    cache = commands.add_parser(
        "cache",
        help="inspect or maintain cached paper data",
        description="Inspect or maintain the local paper cache.",
    )
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_list = cache_commands.add_parser(
        "list",
        help="list cache entries",
        description="List cache entries selected by paper ID, entry ID, or age.",
    )
    _cache_selector_arguments(cache_list)
    cache_list.add_argument(
        "--since", help="only entries updated within a duration such as 12h or 7d"
    )
    cache_remove = cache_commands.add_parser(
        "remove",
        help="preview or remove cache entries",
        description="Preview selected cache removals; pass --yes to apply them.",
    )
    _cache_selector_arguments(cache_remove)
    cache_remove.add_argument("--yes", action="store_true", help="apply the removal")
    cache_update = cache_commands.add_parser(
        "update",
        help="refresh selected cache entries",
        description="Refresh selected cache entries from their upstream sources.",
    )
    _cache_selector_arguments(cache_update)

    for name, summary in {
        "status": "inspect a durable keyword run through arc-jobs",
        "stop": "request a durable keyword run stop through arc-jobs",
        "validate": "validate a durable keyword run through arc-jobs",
    }.items():
        commands.add_parser(name, help=summary, description=summary.capitalize() + ".")

    return parser


def _paper_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paper_id", help="arXiv, INSPIRE, or normalized paper identifier")
    parser.add_argument("--refresh", action="store_true", help="refresh cached remote data")


def _arxiv_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("arxiv_id", help="arXiv identifier")
    parser.add_argument("--refresh", action="store_true", help="refresh cached arXiv data")


def _cache_selector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--id", dest="paper_ids", action="append", default=[], help="paper ID; repeat as needed"
    )
    parser.add_argument(
        "--entry-id",
        dest="entry_ids",
        action="append",
        default=[],
        help="exact cache entry ID",
    )
    parser.add_argument("--cache-root", help="override the paper cache directory")


def _section_ordinal(value: str) -> int:
    try:
        ordinal = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--ordinal must be a non-negative integer"
        ) from exc
    if ordinal < 0:
        raise argparse.ArgumentTypeError(
            "--ordinal must be a non-negative integer"
        )
    return ordinal


def _approx_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--approx-count must be an integer between 1 and 200"
        ) from exc
    if not 1 <= count <= 200:
        raise argparse.ArgumentTypeError(
            "--approx-count must be an integer between 1 and 200"
        )
    return count


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
    if command == "search-cached-full-text":
        return {
            "terms": args.term,
            "limit": args.limit,
            "context_lines": args.context_lines,
            "case_sensitive": args.case_sensitive,
        }
    if command == "get-arxiv-table-of-contents":
        return {"arxiv_id": args.arxiv_id, "refresh": args.refresh}
    if command == "get-arxiv-section":
        return {
            "arxiv_id": args.arxiv_id,
            "selector": (
                args.ordinal if args.ordinal is not None else args.selector
            ),
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
    if command == "extract-keywords":
        return {
            "source": args.source,
            "project_dir": args.project_dir,
            "approx_count": args.approx_count,
            "cache_root": args.cache_root,
            "refresh": args.refresh,
            "llm_provider": args.llm_provider,
            "model": args.model,
            "model_tier": args.model_tier,
            "run_id": args.run_id,
            "resume_input": args.resume_input,
        }
    if command == "cache":
        values: dict[str, Any] = {
            "paper_ids": args.paper_ids,
            "entry_ids": args.entry_ids,
            "cache_root": args.cache_root,
        }
        if args.cache_command == "list":
            values["since_seconds"] = _duration_seconds(args.since)
        elif args.cache_command == "remove":
            values["dry_run"] = not args.yes
        return values
    raise _UsageError(f"unsupported command: {command}")


def _duration_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", value)
    if match is None:
        raise _UsageError(
            "--since must be a positive integer followed by s, m, h, d, or w"
        )
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = int(match.group(1)) * factors[match.group(2)]
    if seconds > 36500 * 86400:
        raise _UsageError("--since cannot exceed 36500d")
    return seconds


def _json_object(value: str) -> dict[str, Any]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"--resume-input must be a JSON object: {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise argparse.ArgumentTypeError(
            "--resume-input must be a JSON object"
        )
    return document


def _result_data(value: Any) -> Mapping[str, Any]:
    encoded = to_json_value(value)
    if isinstance(encoded, Mapping):
        return dict(encoded)
    return {"result": encoded}


def _emit(result: CommandResult, *, exit_code: int) -> int:
    sys.stdout.write(command_result_json(result) + "\n")
    return exit_code


def _help_command(arguments: list[str]) -> str:
    parts = ["arc-paper"]
    commands = _parser()._subparsers._group_actions[0].choices
    if arguments and arguments[0] in commands:
        parts.append(arguments[0])
        if arguments[0] == "cache" and len(arguments) > 1:
            cache_commands = commands["cache"]._subparsers._group_actions[0].choices
            if arguments[1] in cache_commands:
                parts.append(arguments[1])
    return " ".join((*parts, "--help"))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"status", "stop", "validate"}:
        # arc-jobs is the sole implementation of generic durable-run controls.
        return run_control_main(arguments, prog="arc-paper")
    parser = _parser()
    try:
        args = parser.parse_args(arguments)
        if args.command is None:
            parser.print_help()
            return 0
        operation = (
            f"cache-{args.cache_command}"
            if args.command == "cache"
            else args.command
        )
        value = dispatch_operation(operation, _parameters(args))
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
    except KeywordExtractionPaused as exc:
        return _emit(
            command_result_from_snapshot(exc.snapshot),
            exit_code=0,
        )
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
    except OSError as exc:
        return _emit(
            CommandResult(
                CommandStatus.FAILED,
                error=CommandError("local_io_error", str(exc)),
            ),
            exit_code=1,
        )
    except Exception as exc:
        raw_code = getattr(exc, "code", None)
        code = (
            raw_code
            if isinstance(raw_code, str) and raw_code
            else "internal_error"
        )
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
