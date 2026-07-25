"""Semantic field grouping for ARC domain packages."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Callable

from _arc_workflows.domain_manifest_inputs import (
    ManifestError,
)
from _arc_workflows.workflow_io import read_json_object


GROUPING_SCHEMA_VERSION = "arc.workflow.domain_field_grouping.v1"
HARD_SEPARATION_CONFIDENCE = 0.80
GROUPING_LLM_RUN_DIRNAME = "field-grouping-llm"
GROUPING_SCHEMA_FILENAME = "domain-field-grouping.schema.json"
GROUPING_PROMPT_FILENAME = "domain-field-grouping.prompt.md"
GroupingRunner = Callable[[Any, Path], Any]


class GroupingConstraintError(ManifestError):
    pass


class GroupingLLMRunError(RuntimeError):
    """A typed LLM outcome that must stop manifest generation."""


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self._parent[item] != item:
            self._parent[item] = self._parent[
                self._parent[item]
            ]
            item = self._parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self._parent[max(left_root, right_root)] = min(
                left_root, right_root
            )


def _default_grouping_runner(request: Any, run_root: Path) -> Any:
    from arc_llm import LLMClient

    return LLMClient().generate(request, run_root=run_root)


def _validate_grouping(
    payload: dict[str, Any] | None,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(packages) == 1 and payload is None:
        return []
    if not isinstance(payload, dict) or not isinstance(
        payload.get("pairs"), list
    ):
        raise ManifestError("semantic field grouping was unavailable")
    expected = set(
        itertools.combinations(
            sorted(
                item["domain_package_id"] for item in packages
            ),
            2,
        )
    )
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload["pairs"]:
        if not isinstance(item, dict):
            raise ManifestError("grouping pairs must be objects")
        pair = tuple(
            sorted(
                (
                    str(item.get("package_a", "")),
                    str(item.get("package_b", "")),
                )
            )
        )
        label = str(item.get("classification", ""))
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ManifestError(
                f"invalid confidence for pair {pair}"
            ) from exc
        if (
            pair not in expected
            or pair in found
            or label
            not in {
                "same_field",
                "distinct_field",
                "uncertain",
            }
            or not 0 <= confidence <= 1
        ):
            raise ManifestError(
                f"invalid or duplicate grouping pair {pair}"
            )
        if not isinstance(item.get("evidence"), dict):
            raise ManifestError(f"pair {pair} requires evidence")
        found[pair] = {
            "package_a": pair[0],
            "package_b": pair[1],
            "classification": label,
            "confidence": confidence,
            "reason": str(item.get("reason", "")),
            "evidence": item["evidence"],
        }
    if set(found) != expected:
        raise ManifestError(
            "grouping must classify every package pair"
        )
    ordered = [found[pair] for pair in sorted(found)]
    _validate_pair_constraints(
        ordered,
        sorted(item["domain_package_id"] for item in packages),
    )
    return ordered


def _validate_pair_constraints(
    pairs: list[dict[str, Any]],
    package_ids: list[str],
) -> None:
    """Require conservative mergeability to be an equivalence relation.

    Every pair below the hard-distinct threshold is a conservative merge edge.
    If such edges transitively connect a hard-distinct pair, any split would
    depend on iteration order rather than model-supported evidence, so reject
    the grouping.
    """
    components = _UnionFind(package_ids)
    hard: list[dict[str, Any]] = []
    for item in pairs:
        if (
            item["classification"] == "distinct_field"
            and item["confidence"]
            >= HARD_SEPARATION_CONFIDENCE
        ):
            hard.append(item)
        else:
            components.union(
                item["package_a"], item["package_b"]
            )
    conflicts = [
        item
        for item in hard
        if components.find(item["package_a"])
        == components.find(item["package_b"])
    ]
    if conflicts:
        formatted = ", ".join(
            f"{item['package_a']}–{item['package_b']}"
            for item in conflicts
        )
        raise GroupingConstraintError(
            "contradictory/non-transitive semantic grouping: "
            "hard-distinct pair(s) "
            f"{formatted} are transitively connected by conservative "
            "same-field relations"
        )


def _build_field_groups(
    packages: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    *,
    intent: str,
    force_single: bool,
) -> list[dict[str, Any]]:
    package_ids = sorted(
        item["domain_package_id"] for item in packages
    )
    components = _UnionFind(package_ids)

    if force_single:
        for package_id in package_ids[1:]:
            components.union(package_ids[0], package_id)
    else:
        for item in pairs:
            hard = (
                item["classification"] == "distinct_field"
                and item["confidence"]
                >= HARD_SEPARATION_CONFIDENCE
            )
            if not hard:
                components.union(
                    item["package_a"], item["package_b"]
                )
    by_root: dict[str, list[str]] = {}
    for package_id in package_ids:
        by_root.setdefault(
            components.find(package_id), []
        ).append(package_id)
    bins = sorted(
        (sorted(items) for items in by_root.values()),
        key=lambda items: tuple(items),
    )
    by_id = {
        item["domain_package_id"]: item for item in packages
    }
    intent_hash = hashlib.sha256(intent.encode()).hexdigest()
    result = []
    for ids in bins:
        members = [by_id[item] for item in ids]
        digest = hashlib.sha256(
            (
                "\n".join(ids) + "\n" + intent_hash
            ).encode()
        ).hexdigest()[:16]
        relevant_pairs = [
            item
            for item in pairs
            if item["package_a"] in ids
            or item["package_b"] in ids
        ]
        internal_pairs = [
            item
            for item in relevant_pairs
            if item["package_a"] in ids
            and item["package_b"] in ids
        ]
        confidence_values = [
            float(item["confidence"]) for item in internal_pairs
        ]
        if not confidence_values:
            confidence_values = [
                float(item["confidence"])
                for item in relevant_pairs
                if item["classification"] == "distinct_field"
                and item["confidence"]
                >= HARD_SEPARATION_CONFIDENCE
            ]
        confidence = (
            min(confidence_values)
            if confidence_values
            else (0.0 if force_single else 1.0)
        )
        reasons = [
            str(item["reason"]).strip()
            for item in relevant_pairs
            if str(item["reason"]).strip()
        ]
        result.append(
            {
                "field_id": f"field-{digest}",
                "domain_package_ids": ids,
                "confidence": confidence,
                "reason": (
                    "Conservative fallback merged all packages because "
                    "semantic grouping was unavailable."
                    if force_single
                    else "; ".join(reasons)
                    or "Single package field; no pairwise merge evidence "
                    "required."
                ),
                "evidence": [
                    {
                        "package_a": item["package_a"],
                        "package_b": item["package_b"],
                        "classification": item[
                            "classification"
                        ],
                        "confidence": item["confidence"],
                        "evidence": item["evidence"],
                    }
                    for item in relevant_pairs
                ],
                "field_card": {
                    "seed_papers": [
                        item["seed_paper"] for item in members
                    ],
                    "titles": [
                        item["title"] for item in members
                    ],
                    "overviews": [
                        item["overview"]
                        for item in members
                        if item["overview"]
                    ],
                    "task_focus": [
                        item["task_focus"]
                        for item in members
                        if item["task_focus"]
                    ],
                    "methodology": [
                        method
                        for item in members
                        if isinstance(item["methodology"], list)
                        for method in item["methodology"]
                    ],
                    "known_solved_cases": [
                        case
                        for item in members
                        if isinstance(
                            item["known_solved_cases"], list
                        )
                        for case in item["known_solved_cases"]
                    ],
                    "open_axes_for_new_work": [
                        axis
                        for item in members
                        if isinstance(
                            item["open_axes_for_new_work"], list
                        )
                        for axis in item[
                            "open_axes_for_new_work"
                        ]
                    ],
                    "mathematical_opportunities": {
                        "well_defined_problems": [
                            problem
                            for item in members
                            for problem in item[
                                "mathematical_opportunities"
                            ].get("well_defined_problems", [])
                            if isinstance(
                                item[
                                    "mathematical_opportunities"
                                ],
                                dict,
                            )
                        ]
                    },
                    "summary_schema_versions": [
                        item["summary_schema_version"]
                        for item in members
                    ],
                    "summary_json_paths": [
                        item["summary_json_path"]
                        for item in members
                    ],
                    "summary_markdown_paths": [
                        item["summary_markdown_path"]
                        for item in members
                    ],
                    "paper_json_pack_paths": [
                        item["paper_json_pack_path"]
                        for item in members
                    ],
                    "paper_ids": sorted(
                        {
                            paper
                            for item in members
                            for paper in item["paper_ids"]
                        }
                    ),
                    "citation_edges": sorted(
                        {
                            tuple(edge)
                            for item in members
                            for edge in item["citation_edges"]
                        }
                    ),
                },
            }
        )
    return result


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
    "GroupingConstraintError",
    "GroupingLLMRunError",
    "GroupingRunner",
    "HARD_SEPARATION_CONFIDENCE",
    "_build_field_groups",
    "_compact_packages",
    "_default_grouping_runner",
    "_grouping_task_id",
    "_llm_grouping",
    "_load_grouping_prompt_template",
    "_load_grouping_schema",
    "_materialize_grouping_prompt",
    "_validate_grouping",
    "_validate_pair_constraints",
]
