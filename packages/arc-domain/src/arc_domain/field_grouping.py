"""Deterministic semantic grouping for exported domain packages."""

from __future__ import annotations

import hashlib
import itertools
from typing import Any


HARD_SEPARATION_CONFIDENCE = 0.80


class FieldGroupingError(ValueError):
    """Semantic field-grouping input violates the package contract."""

    code = "field_grouping_invalid"


class FieldGroupingConstraintError(FieldGroupingError):
    """Pair classifications cannot form deterministic field groups."""

    code = "field_grouping_constraint"


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self._parent[max(left_root, right_root)] = min(
                left_root, right_root
            )


def normalize_field_grouping_pairs(
    payload: dict[str, Any] | None,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and deterministically order every package-pair classification."""

    package_ids = _package_ids(packages)
    if len(packages) == 1 and payload is None:
        return []
    if not isinstance(payload, dict) or not isinstance(
        payload.get("pairs"), list
    ):
        raise FieldGroupingError(
            "semantic field grouping was unavailable"
        )
    expected = set(
        itertools.combinations(
            package_ids,
            2,
        )
    )
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload["pairs"]:
        if not isinstance(item, dict):
            raise FieldGroupingError(
                "grouping pairs must be objects"
            )
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
            raise FieldGroupingError(
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
            raise FieldGroupingError(
                f"invalid or duplicate grouping pair {pair}"
            )
        if not isinstance(item.get("evidence"), dict):
            raise FieldGroupingError(
                f"pair {pair} requires evidence"
            )
        found[pair] = {
            "package_a": pair[0],
            "package_b": pair[1],
            "classification": label,
            "confidence": confidence,
            "reason": str(item.get("reason", "")),
            "evidence": item["evidence"],
        }
    if set(found) != expected:
        raise FieldGroupingError(
            "grouping must classify every package pair"
        )
    ordered = [found[pair] for pair in sorted(found)]
    validate_field_grouping_constraints(
        ordered,
        package_ids,
    )
    return ordered


def validate_field_grouping_constraints(
    pairs: list[dict[str, Any]],
    package_ids: list[str],
) -> None:
    """Require conservative mergeability to be an equivalence relation."""

    _validate_package_ids(package_ids)
    components = _UnionFind(package_ids)
    hard: list[dict[str, Any]] = []
    for item in pairs:
        if (
            item["classification"] == "distinct_field"
            and item["confidence"] >= HARD_SEPARATION_CONFIDENCE
        ):
            hard.append(item)
        else:
            components.union(item["package_a"], item["package_b"])
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
        raise FieldGroupingConstraintError(
            "contradictory/non-transitive semantic grouping: "
            "hard-distinct pair(s) "
            f"{formatted} are transitively connected by conservative "
            "same-field relations"
        )


def build_field_groups(
    packages: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    *,
    intent: str,
    force_single: bool,
) -> list[dict[str, Any]]:
    """Build stable field groups and evidence cards from normalized pairs."""

    package_ids = _package_ids(packages)
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
            ("\n".join(ids) + "\n" + intent_hash).encode()
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
                    "titles": [item["title"] for item in members],
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
                            if isinstance(
                                item[
                                    "mathematical_opportunities"
                                ],
                                dict,
                            )
                            for problem in item[
                                "mathematical_opportunities"
                            ].get("well_defined_problems", [])
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


def _package_ids(packages: Any) -> list[str]:
    if not isinstance(packages, list) or not packages:
        raise FieldGroupingError(
            "packages must be a non-empty list"
        )
    package_ids: list[str] = []
    for index, item in enumerate(packages):
        if not isinstance(item, dict):
            raise FieldGroupingError(
                f"packages[{index}] must be an object"
            )
        package_id = item.get("domain_package_id")
        if not isinstance(package_id, str) or not package_id.strip():
            raise FieldGroupingError(
                f"packages[{index}].domain_package_id must be "
                "a non-empty string"
            )
        package_ids.append(package_id)
    return _validate_package_ids(package_ids)


def _validate_package_ids(package_ids: Any) -> list[str]:
    if not isinstance(package_ids, list) or not package_ids:
        raise FieldGroupingError(
            "package_ids must be a non-empty list"
        )
    if any(
        not isinstance(package_id, str)
        or not package_id.strip()
        for package_id in package_ids
    ):
        raise FieldGroupingError(
            "package_ids must contain non-empty strings"
        )
    if len(set(package_ids)) != len(package_ids):
        raise FieldGroupingError(
            "domain_package_id values must be unique"
        )
    return sorted(package_ids)


__all__ = [
    "HARD_SEPARATION_CONFIDENCE",
    "FieldGroupingConstraintError",
    "FieldGroupingError",
    "build_field_groups",
    "normalize_field_grouping_pairs",
    "validate_field_grouping_constraints",
]
