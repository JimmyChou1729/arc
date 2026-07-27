"""Advisory semantic relationships between ARC domain packages."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Callable

from _arc_workflows.domain_manifest_inputs import ManifestError
from _arc_workflows.workflow_io import read_json_object


RELATIONSHIP_LLM_RUN_DIRNAME = "domain-relationships-llm"
RELATIONSHIP_SCHEMA_FILENAME = "domain-relationships.schema.json"
RELATIONSHIP_PROMPT_FILENAME = "domain-relationships.prompt.md"
RelationshipRunner = Callable[[Any, Path], Any]


class DomainRelationshipError(ValueError):
    """An advisory relationship payload is unusable."""


class RelationshipLLMOutcomeError(RuntimeError):
    """The relationship request did not produce a completed value."""


def _default_relationship_runner(request: Any, run_root: Path) -> Any:
    from arc_llm import LLMClient

    return LLMClient().generate(request, run_root=run_root)


def _llm_relationships(
    packages: list[dict[str, Any]],
    intent: str,
    *,
    run_root: Path,
    runner: RelationshipRunner,
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
    result = runner(
        LLMRequest(
            task_id=_relationship_task_id(compact, intent=intent),
            prompt=_materialize_relationship_prompt(
                compact, intent=intent
            ),
            output=JsonOutput(_load_relationship_schema()),
            model=ModelSelection(provider="auto", tier="medium"),
        ),
        run_root,
    )
    outcome = getattr(result, "outcome", None)
    if isinstance(outcome, LLMCompleted):
        if not isinstance(outcome.value, dict):
            raise ManifestError(
                "domain relationship analysis returned a non-object result"
            )
        return dict(outcome.value)
    if isinstance(outcome, LLMPaused):
        raise RelationshipLLMOutcomeError(
            "domain relationship analysis is paused: "
            f"{outcome.reason.value} ({outcome.resume_key})"
        )
    if isinstance(outcome, LLMFailed):
        raise RelationshipLLMOutcomeError(
            "domain relationship analysis failed: "
            f"{outcome.error.code.value}: {outcome.error}"
        )
    if isinstance(outcome, LLMStopped):
        raise RelationshipLLMOutcomeError(
            "domain relationship analysis was stopped"
        )
    raise RelationshipLLMOutcomeError(
        "domain relationship analysis returned no typed outcome"
    )


def normalize_domain_relationship_pairs(
    payload: dict[str, Any] | None,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and deterministically order advisory pair classifications."""

    package_ids = _package_ids(packages)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("pairs"), list
    ):
        raise DomainRelationshipError(
            "domain relationship analysis was unavailable"
        )
    expected = set(itertools.combinations(package_ids, 2))
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload["pairs"]:
        if not isinstance(item, dict):
            raise DomainRelationshipError(
                "domain relationship pairs must be objects"
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
            raise DomainRelationshipError(
                f"invalid confidence for pair {pair}"
            ) from exc
        if (
            pair not in expected
            or pair in found
            or label
            not in {"same_field", "distinct_field", "uncertain"}
            or not 0 <= confidence <= 1
        ):
            raise DomainRelationshipError(
                f"invalid or duplicate domain relationship pair {pair}"
            )
        if not isinstance(item.get("evidence"), dict):
            raise DomainRelationshipError(
                f"domain relationship pair {pair} requires evidence"
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
        raise DomainRelationshipError(
            "domain relationship analysis must classify every package pair"
        )
    return [found[pair] for pair in sorted(found)]


def _package_ids(packages: list[dict[str, Any]]) -> list[str]:
    if not isinstance(packages, list) or not packages:
        raise DomainRelationshipError(
            "domain packages must be a non-empty list"
        )
    package_ids: list[str] = []
    for item in packages:
        if not isinstance(item, dict):
            raise DomainRelationshipError(
                "domain packages must be objects"
            )
        package_id = str(item.get("domain_package_id", "")).strip()
        if not package_id:
            raise DomainRelationshipError(
                "each domain package requires domain_package_id"
            )
        package_ids.append(package_id)
    if len(set(package_ids)) != len(package_ids):
        raise DomainRelationshipError(
            "domain_package_id values must be unique"
        )
    return sorted(package_ids)


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
    return [{key: item[key] for key in keys} for item in packages]


def _materialize_relationship_prompt(
    compact_packages: list[dict[str, Any]],
    *,
    intent: str,
) -> str:
    return _load_relationship_prompt_template().format(
        intent=intent,
        packages_json=json.dumps(
            compact_packages, ensure_ascii=False
        ),
    )


def _relationship_task_id(
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
    return "domain-relationships-" + hashlib.sha256(
        task_material.encode("utf-8")
    ).hexdigest()[:24]


def _load_relationship_schema(
    workflow_json_dir: Path | None = None,
) -> dict[str, Any]:
    return read_json_object(
        _workflow_json_dir(workflow_json_dir)
        / RELATIONSHIP_SCHEMA_FILENAME
    )


def _load_relationship_prompt_template(
    workflow_json_dir: Path | None = None,
) -> str:
    return (
        _workflow_json_dir(workflow_json_dir)
        / RELATIONSHIP_PROMPT_FILENAME
    ).read_text(encoding="utf-8").rstrip("\n")


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
    "DomainRelationshipError",
    "RELATIONSHIP_LLM_RUN_DIRNAME",
    "RELATIONSHIP_PROMPT_FILENAME",
    "RELATIONSHIP_SCHEMA_FILENAME",
    "RelationshipLLMOutcomeError",
    "RelationshipRunner",
    "_compact_packages",
    "_default_relationship_runner",
    "_llm_relationships",
    "_load_relationship_prompt_template",
    "_load_relationship_schema",
    "_materialize_relationship_prompt",
    "_relationship_task_id",
    "normalize_domain_relationship_pairs",
]
