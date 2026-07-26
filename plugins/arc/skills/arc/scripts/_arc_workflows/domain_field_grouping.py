"""Semantic field grouping for ARC domain packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from _arc_workflows.domain_manifest_inputs import (
    ManifestError,
)
from _arc_workflows.workflow_io import read_json_object


GROUPING_SCHEMA_VERSION = "arc.workflow.domain_field_grouping.v1"
GROUPING_LLM_RUN_DIRNAME = "field-grouping-llm"
GROUPING_SCHEMA_FILENAME = "domain-field-grouping.schema.json"
GROUPING_PROMPT_FILENAME = "domain-field-grouping.prompt.md"
GroupingRunner = Callable[[Any, Path], Any]


class GroupingLLMRunError(RuntimeError):
    """A typed LLM outcome that must stop manifest generation."""


def _default_grouping_runner(request: Any, run_root: Path) -> Any:
    from arc_llm import LLMClient

    return LLMClient().generate(request, run_root=run_root)


def _llm_grouping(
    packages: list[dict[str, Any]],
    intent: str,
    *,
    run_root: Path,
    runner: GroupingRunner,
) -> dict[str, Any]:
    from arc_llm import (
        JsonOutput,
        LLMCompleted,
        LLMFailed,
        LLMPaused,
        LLMRequest,
        LLMStopped,
        ModelSelection,
    )

    compact = _compact_packages(packages)
    prompt = _materialize_grouping_prompt(
        compact, intent=intent
    )
    task_id = _grouping_task_id(compact, intent=intent)
    result = runner(
        LLMRequest(
            task_id=task_id,
            prompt=prompt,
            output=JsonOutput(_load_grouping_schema()),
            model=ModelSelection(
                provider="auto", tier="medium"
            ),
        ),
        run_root,
    )
    outcome = getattr(result, "outcome", None)
    if isinstance(outcome, LLMCompleted):
        if not isinstance(outcome.value, dict):
            raise ManifestError(
                "semantic field grouping returned a non-object result"
            )
        return dict(outcome.value)
    if isinstance(outcome, LLMPaused):
        raise GroupingLLMRunError(
            "semantic field grouping is paused: "
            f"{outcome.reason.value} ({outcome.resume_key})"
        )
    if isinstance(outcome, LLMFailed):
        raise GroupingLLMRunError(
            "semantic field grouping failed: "
            f"{outcome.error.code.value}: {outcome.error}"
        )
    if isinstance(outcome, LLMStopped):
        raise GroupingLLMRunError(
            "semantic field grouping was stopped"
        )
    raise GroupingLLMRunError(
        "semantic field grouping returned no typed outcome"
    )


def _compact_packages(
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    keys = (
        "domain_package_id",
        "seed_paper",
        "foundation_paper_ids",
        "title",
        "overview",
        "task_focus",
        "methodology",
        "paper_ids",
        "citation_edges",
    )
    return [
        {key: item[key] for key in keys}
        for item in packages
    ]


def _materialize_grouping_prompt(
    compact_packages: list[dict[str, Any]],
    *,
    intent: str,
) -> str:
    template = _load_grouping_prompt_template()
    return template.format(
        intent=intent,
        packages_json=json.dumps(
            compact_packages, ensure_ascii=False
        ),
    )


def _grouping_task_id(
    compact_packages: list[dict[str, Any]],
    *,
    intent: str,
) -> str:
    task_material = json.dumps(
        {"packages": compact_packages, "intent": intent},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "domain-field-grouping-" + hashlib.sha256(
        task_material.encode("utf-8")
    ).hexdigest()[:24]


def _load_grouping_schema(
    workflow_json_dir: Path | None = None,
) -> dict[str, Any]:
    return read_json_object(
        _workflow_json_dir(workflow_json_dir)
        / GROUPING_SCHEMA_FILENAME
    )


def _load_grouping_prompt_template(
    workflow_json_dir: Path | None = None,
) -> str:
    template = (
        _workflow_json_dir(workflow_json_dir)
        / GROUPING_PROMPT_FILENAME
    ).read_text(encoding="utf-8")
    return template.rstrip("\n")


def _workflow_json_dir(
    workflow_json_dir: Path | None = None,
) -> Path:
    if workflow_json_dir is not None:
        return Path(workflow_json_dir).expanduser()
    return (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "json"
    )


__all__ = [
    "GROUPING_LLM_RUN_DIRNAME",
    "GROUPING_PROMPT_FILENAME",
    "GROUPING_SCHEMA_FILENAME",
    "GROUPING_SCHEMA_VERSION",
    "GroupingLLMRunError",
    "GroupingRunner",
    "_compact_packages",
    "_default_grouping_runner",
    "_grouping_task_id",
    "_llm_grouping",
    "_load_grouping_prompt_template",
    "_load_grouping_schema",
    "_materialize_grouping_prompt",
]
