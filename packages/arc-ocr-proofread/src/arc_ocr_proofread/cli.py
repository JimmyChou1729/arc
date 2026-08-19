"""JSON CLI for durable OCR proofreading."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import sys
from pathlib import Path
from typing import Any

from arc_jobs import (
    CommandArtifact,
    CommandError,
    CommandResult,
    CommandStatus,
    RunStatus,
    command_result_from_snapshot,
    command_result_json,
    snapshot_data,
)

from .project import ProofreadProject, ProofreadProjectError
from .service import ProofreadService, ProofreadServiceError
from .source import ProofreadSourceError, load_mineru_source


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc-ocr-proofread",
        description="Proofread page-mapped OCR against complete PDF page images.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    proofread = commands.add_parser("proofread")
    proofread.add_argument("markdown")
    proofread.add_argument("--pdf", required=True)
    proofread.add_argument("--content-list", required=True)
    proofread.add_argument("--project-dir", required=True)
    proofread.add_argument("--provider", default="auto")
    proofread.add_argument("--model")
    proofread.add_argument("--model-tier", choices=("low", "medium", "high", "xhigh"), default="medium")
    proofread.add_argument("--workers", type=int, default=30)
    proofread.add_argument("--max-workers", type=int, default=200)

    for name in ("status", "validate", "get-result"):
        command = commands.add_parser(name)
        command.add_argument("--project-dir", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("--project-dir", required=True)
    stop.add_argument("--reason")
    resume = commands.add_parser("resume")
    resume.add_argument("--project-dir", required=True)
    resume.add_argument("--input")
    workers = commands.add_parser("workers")
    worker_commands = workers.add_subparsers(dest="worker_command", required=True)
    for name in ("get", "set"):
        command = worker_commands.add_parser(name)
        command.add_argument("--project-dir", required=True)
        if name == "set":
            command.add_argument("--workers", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = _dispatch(args)
    except (ProofreadProjectError, ProofreadServiceError, ProofreadSourceError, ValueError) as exc:
        result = CommandResult(
            CommandStatus.FAILED,
            error=CommandError(getattr(exc, "code", "invalid_request"), str(exc)),
        )
    except Exception as exc:
        result = CommandResult(
            CommandStatus.FAILED,
            error=CommandError(getattr(exc, "code", "ocr_proofread_failed"), str(exc)),
        )
    print(command_result_json(result))
    return 0 if result.status is CommandStatus.COMPLETED else 1


def _dispatch(args: argparse.Namespace) -> CommandResult:
    if args.command == "proofread":
        source = load_mineru_source(args.markdown, args.pdf, args.content_list)
        project = ProofreadProject.open(args.project_dir)
        service = ProofreadService(project)
        snapshot = service.prepare(
            source,
            provider=args.provider,
            model=args.model,
            model_tier=args.model_tier,
            workers=args.workers,
            max_workers=args.max_workers,
        )
        snapshot = service.execute(snapshot.run_id)
        return _generation_result(service, snapshot)
    project = ProofreadProject.load(args.project_dir)
    service = ProofreadService(project)
    run_id = project.current_run_id
    if run_id is None:
        raise ProofreadServiceError("run_not_selected", "project has no selected run")
    if args.command == "resume":
        snapshot = service.resume(run_id, input=_input(args.input))
        return _generation_result(service, snapshot)
    if args.command == "stop":
        snapshot = service.stop(run_id, reason=args.reason).snapshot
        return replace(
            command_result_from_snapshot(snapshot, query=True),
            data={"run": snapshot_data(snapshot), "metrics": service.metrics(run_id)},
        )
    if args.command == "workers":
        control = service.workers(run_id) if args.worker_command == "get" else service.set_workers(run_id, args.workers)
        snapshot = service.inspect(run_id).snapshot
        return CommandResult(
            CommandStatus.COMPLETED,
            data={
                "run": snapshot_data(snapshot),
                "group_workers": {
                    "target_workers": control.target_workers,
                    "capacity": control.capacity,
                    "maximum": service._config(run_id).max_workers,
                },
            },
        )
    if args.command == "status":
        snapshot = service.inspect(run_id).snapshot
        data: dict[str, Any] = {"run": snapshot_data(snapshot), "metrics": service.metrics(run_id)}
        try:
            control = service.workers(run_id)
            data["group_workers"] = {
                "target_workers": control.target_workers,
                "capacity": control.capacity,
                "maximum": service._config(run_id).max_workers,
            }
        except Exception:
            pass
        return CommandResult(CommandStatus.COMPLETED, data=data)
    if args.command == "validate":
        report = service.validate(run_id)
        return CommandResult(
            CommandStatus.COMPLETED if report.ok else CommandStatus.FAILED,
            data={
                "valid": report.ok,
                "issues": [
                    {"code": item.code, "message": item.message, "path": list(item.path)}
                    for item in report.issues
                ],
            },
            error=None if report.ok else CommandError("validation_failed", "OCR proofreading delivery is invalid"),
        )
    if args.command == "get-result":
        snapshot = service.inspect(run_id).snapshot
        if snapshot.status is not RunStatus.SUCCEEDED:
            raise ProofreadServiceError("result_unavailable", "selected run has not succeeded")
        return CommandResult(
            CommandStatus.COMPLETED,
            data={"result": service.result()},
            artifacts=(
                CommandArtifact("markdown", "proofread-markdown", "proofread.md"),
                CommandArtifact("manifest", "proofread-manifest", "proofread.manifest.json"),
                CommandArtifact("changes", "proofread-changes", "proofread.changes.jsonl"),
            ),
        )
    raise ValueError("unknown command")


def _generation_result(service: ProofreadService, snapshot) -> CommandResult:
    base = command_result_from_snapshot(snapshot)
    artifacts = base.artifacts
    if snapshot.status is RunStatus.SUCCEEDED:
        artifacts = (
            CommandArtifact("markdown", "proofread-markdown", "proofread.md"),
            CommandArtifact("manifest", "proofread-manifest", "proofread.manifest.json"),
            CommandArtifact("changes", "proofread-changes", "proofread.changes.jsonl"),
        )
    return replace(
        base,
        data={"run": snapshot_data(snapshot), "metrics": service.metrics(snapshot.run_id)},
        artifacts=artifacts,
    )


def _input(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    path = Path(raw)
    text = path.read_text(encoding="utf-8") if path.is_file() else raw
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("resume input must be a JSON object")
    return value


if __name__ == "__main__":
    sys.exit(main())
