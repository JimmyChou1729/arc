"""Validate and materialize closed domain seed provenance."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_paper import normalize_paper_id
from jsonschema import Draft202012Validator


SEED_PROVENANCE_SCHEMA_VERSION = (
    "arc.workflow.domain_seed_provenance.v1"
)
_ORIGIN_SELECTION_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "workflows/json/domain-origin-selection.schema.json"
)
_ORIGIN_SELECTION_SCHEMA = json.loads(
    _ORIGIN_SELECTION_SCHEMA_PATH.read_text(encoding="utf-8")
)
_ORIGIN_SELECTION_VALIDATOR = Draft202012Validator(
    _ORIGIN_SELECTION_SCHEMA
)


class SeedProvenanceError(ValueError):
    pass


def build_seed_provenance(
    context: Mapping[str, Any],
    *,
    seed_by_domain: Mapping[str, str],
) -> dict[str, Any]:
    """Return one closed provenance document for all requested/build seeds."""

    requested = _normalized_ids(
        context.get("seed_paper_list"),
        "context.json seed_paper_list",
        nonempty=True,
    )
    origins = _source_origins(
        context.get("origin_selections"),
        requested=set(requested),
        seed_by_domain=seed_by_domain,
    )
    deduplications = _deduplications(
        context.get("domain_deduplications"),
        seed_by_domain=seed_by_domain,
        requested=set(requested),
        source_label="context.json domain_deduplications",
    )
    mappings = _expected_mappings(origins, deduplications)
    if set(mappings) != set(requested):
        missing = sorted(set(requested) - set(mappings))
        extra = sorted(set(mappings) - set(requested))
        details = [
            *(["missing " + ", ".join(missing)] if missing else []),
            *(["unexpected " + ", ".join(extra)] if extra else []),
        ]
        raise SeedProvenanceError(
            "requested seeds must be covered exactly once: "
            + "; ".join(details)
        )
    payload = {
        "schema_version": SEED_PROVENANCE_SCHEMA_VERSION,
        "requested_seed_mappings": [
            mappings[seed] for seed in requested
        ],
        "build_origins": origins,
        "deduplications": deduplications,
    }
    canonical_json_bytes(payload)
    return payload


def validate_seed_provenance(
    payload: Mapping[str, Any],
    *,
    expected_domain_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Validate one current, closed provenance document."""

    data = _closed(
        payload,
        {
            "schema_version",
            "requested_seed_mappings",
            "build_origins",
            "deduplications",
        },
        "seed provenance",
    )
    if data["schema_version"] != SEED_PROVENANCE_SCHEMA_VERSION:
        raise SeedProvenanceError(
            "seed provenance schema_version must be "
            f"{SEED_PROVENANCE_SCHEMA_VERSION}"
        )
    origins = _embedded_origins(data["build_origins"])
    seed_by_domain = {
        item["domain_id"]: item["build_seed"] for item in origins
    }
    if (
        expected_domain_ids is not None
        and set(seed_by_domain) != expected_domain_ids
    ):
        raise SeedProvenanceError(
            "seed provenance build_origins must cover manifest "
            "domain_package_ids exactly"
        )
    deduplications = _deduplications(
        data["deduplications"],
        seed_by_domain=seed_by_domain,
        requested=None,
        source_label="seed provenance deduplications",
    )
    mappings = _mapping_array(data["requested_seed_mappings"])
    expected = _expected_mappings(origins, deduplications)
    actual = {item["requested_seed"]: item for item in mappings}
    if actual != expected:
        raise SeedProvenanceError(
            "seed provenance requested_seed_mappings is inconsistent "
            "with build_origins and deduplications"
        )
    validated = {
        "schema_version": SEED_PROVENANCE_SCHEMA_VERSION,
        "requested_seed_mappings": mappings,
        "build_origins": origins,
        "deduplications": deduplications,
    }
    canonical_json_bytes(validated)
    return validated


def _source_origins(
    raw: Any,
    *,
    requested: set[str],
    seed_by_domain: Mapping[str, str],
) -> list[dict[str, Any]]:
    values = _array(raw, "context.json origin_selections", nonempty=True)
    result_by_domain: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(values):
        label = f"context.json origin_selections[{index}]"
        mode = _mode(value, label)
        keys = (
            {"mode", "domain_id", "build_seed", "requested_seed"}
            if mode == "explicit_seed"
            else {
                "mode",
                "domain_id",
                "build_seed",
                "requested_seed",
                "field_id",
                "selection_run_id",
                "selection",
            }
        )
        item = _closed(value, keys, label)
        domain_id = _text(item["domain_id"], f"{label}.domain_id")
        build_seed = _paper_id(
            item["build_seed"], f"{label}.build_seed"
        )
        if seed_by_domain.get(domain_id) != build_seed:
            raise SeedProvenanceError(
                f"{label} does not match a domain_record"
            )
        if domain_id in result_by_domain:
            raise SeedProvenanceError(
                f"origin_selections covers domain {domain_id} more "
                "than once"
            )
        origin = _origin_body(item, build_seed=build_seed, label=label)
        requested_seed = origin.get("requested_seed")
        if requested_seed is not None and requested_seed not in requested:
            raise SeedProvenanceError(
                f"{label}.requested_seed is not in seed_paper_list"
            )
        result_by_domain[domain_id] = {
            "domain_id": domain_id,
            "build_seed": build_seed,
            "origin_selection": origin,
        }
    if set(result_by_domain) != set(seed_by_domain):
        raise SeedProvenanceError(
            "origin_selections must cover domain_records exactly"
        )
    return [
        result_by_domain[domain_id] for domain_id in seed_by_domain
    ]


def _embedded_origins(raw: Any) -> list[dict[str, Any]]:
    values = _array(raw, "seed provenance build_origins", nonempty=True)
    result: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    for index, value in enumerate(values):
        label = f"seed provenance build_origins[{index}]"
        item = _closed(
            value,
            {"domain_id", "build_seed", "origin_selection"},
            label,
        )
        domain_id = _text(item["domain_id"], f"{label}.domain_id")
        if domain_id in seen_domains:
            raise SeedProvenanceError(
                f"{label}.domain_id duplicates {domain_id}"
            )
        seen_domains.add(domain_id)
        build_seed = _paper_id(
            item["build_seed"], f"{label}.build_seed"
        )
        origin_raw = item["origin_selection"]
        mode = _mode(origin_raw, f"{label}.origin_selection")
        keys = (
            {"mode", "requested_seed"}
            if mode == "explicit_seed"
            else {
                "mode",
                "requested_seed",
                "field_id",
                "selection_run_id",
                "selection",
            }
        )
        origin = _origin_body(
            _closed(
                origin_raw,
                keys,
                f"{label}.origin_selection",
            ),
            build_seed=build_seed,
            label=f"{label}.origin_selection",
        )
        result.append(
            {
                "domain_id": domain_id,
                "build_seed": build_seed,
                "origin_selection": origin,
            }
        )
    return result


def _origin_body(
    item: Mapping[str, Any],
    *,
    build_seed: str,
    label: str,
) -> dict[str, Any]:
    mode = _mode(item, label)
    requested_seed = _optional_paper_id(
        item.get("requested_seed"),
        f"{label}.requested_seed",
    )
    if mode == "explicit_seed":
        if requested_seed != build_seed:
            raise SeedProvenanceError(
                f"{label}.requested_seed must equal build_seed"
            )
        return {
            "mode": mode,
            "requested_seed": requested_seed,
        }
    if requested_seed is not None and requested_seed != build_seed:
        raise SeedProvenanceError(
            f"{label}.requested_seed must be null or equal build_seed"
        )
    selection = _origin_result(
        item.get("selection"), f"{label}.selection"
    )
    if selection["selected_paper_id"] != build_seed:
        raise SeedProvenanceError(
            f"{label}.selection.selected_paper_id must equal "
            "build_seed"
        )
    return {
        "mode": mode,
        "requested_seed": requested_seed,
        "field_id": _text(item.get("field_id"), f"{label}.field_id"),
        "selection_run_id": _text(
            item.get("selection_run_id"),
            f"{label}.selection_run_id",
        ),
        "selection": selection,
    }


def _origin_result(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SeedProvenanceError(f"{label} must be an object")
    errors = sorted(
        _ORIGIN_SELECTION_VALIDATOR.iter_errors(dict(raw)),
        key=lambda error: tuple(
            str(component) for component in error.path
        ),
    )
    if errors:
        raise SeedProvenanceError(
            f"{label} is invalid: {errors[0].message}"
        )
    result = copy.deepcopy(dict(raw))
    confidence = float(result["confidence"])
    if confidence < 0.8:
        raise SeedProvenanceError(
            f"{label}.confidence must be at least 0.8"
        )
    result["confidence"] = confidence
    result["selected_paper_id"] = _paper_id(
        result["selected_paper_id"],
        f"{label}.selected_paper_id",
    )
    candidate_ids: set[str] = set()
    for index, assessment in enumerate(
        result["candidate_assessments"]
    ):
        paper_id = _paper_id(
            assessment["paper_id"],
            f"{label}.candidate_assessments[{index}].paper_id",
        )
        if paper_id in candidate_ids:
            raise SeedProvenanceError(
                f"{label}.candidate_assessments[{index}].paper_id "
                "is duplicated"
            )
        candidate_ids.add(paper_id)
        assessment["paper_id"] = paper_id
    if result["selected_paper_id"] not in candidate_ids:
        raise SeedProvenanceError(
            f"{label}.selected_paper_id must appear in "
            "candidate_assessments"
        )
    return result


def _deduplications(
    raw: Any,
    *,
    seed_by_domain: Mapping[str, str],
    requested: set[str] | None,
    source_label: str,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(
        _array(raw, source_label, nonempty=False)
    ):
        label = f"{source_label}[{index}]"
        item = _closed(
            value,
            {"requested_seed", "kept_build_seed", "domain_id"},
            label,
        )
        requested_seed = _paper_id(
            item["requested_seed"], f"{label}.requested_seed"
        )
        if requested_seed in seen:
            raise SeedProvenanceError(
                f"{label}.requested_seed is duplicated"
            )
        seen.add(requested_seed)
        build_seed = _paper_id(
            item["kept_build_seed"], f"{label}.kept_build_seed"
        )
        domain_id = _text(item["domain_id"], f"{label}.domain_id")
        if seed_by_domain.get(domain_id) != build_seed:
            raise SeedProvenanceError(
                f"{label} does not identify a build origin"
            )
        if requested_seed == build_seed:
            raise SeedProvenanceError(
                f"{label} must map to a different kept_build_seed"
            )
        if requested is not None:
            if requested_seed not in requested:
                raise SeedProvenanceError(
                    f"{label}.requested_seed is not in seed_paper_list"
                )
        result.append(
            {
                "requested_seed": requested_seed,
                "kept_build_seed": build_seed,
                "domain_id": domain_id,
            }
        )
    return result


def _mapping_array(raw: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    valid_resolutions = {
        "explicit_seed",
        "origin_selected",
        "deduplicated",
    }
    for index, value in enumerate(
        _array(
            raw,
            "seed provenance requested_seed_mappings",
            nonempty=True,
        )
    ):
        label = f"seed provenance requested_seed_mappings[{index}]"
        item = _closed(
            value,
            {
                "requested_seed",
                "build_seed",
                "domain_id",
                "resolution",
            },
            label,
        )
        requested_seed = _paper_id(
            item["requested_seed"], f"{label}.requested_seed"
        )
        if requested_seed in seen:
            raise SeedProvenanceError(
                f"{label}.requested_seed is duplicated"
            )
        seen.add(requested_seed)
        resolution = _text(
            item["resolution"], f"{label}.resolution"
        )
        if resolution not in valid_resolutions:
            raise SeedProvenanceError(
                f"{label}.resolution is invalid"
            )
        result.append(
            {
                "requested_seed": requested_seed,
                "build_seed": _paper_id(
                    item["build_seed"], f"{label}.build_seed"
                ),
                "domain_id": _text(
                    item["domain_id"], f"{label}.domain_id"
                ),
                "resolution": resolution,
            }
        )
    return result


def _expected_mappings(
    origins: list[dict[str, Any]],
    deduplications: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for origin in origins:
        requested_seed = origin["origin_selection"].get(
            "requested_seed"
        )
        if requested_seed is None:
            continue
        _add_mapping(
            result,
            requested_seed,
            origin["build_seed"],
            origin["domain_id"],
            origin["origin_selection"]["mode"],
        )
    for item in deduplications:
        _add_mapping(
            result,
            item["requested_seed"],
            item["kept_build_seed"],
            item["domain_id"],
            "deduplicated",
        )
    return result


def _add_mapping(
    target: dict[str, dict[str, str]],
    requested_seed: str,
    build_seed: str,
    domain_id: str,
    resolution: str,
) -> None:
    if requested_seed in target:
        raise SeedProvenanceError(
            "requested seed is mapped more than once: "
            f"{requested_seed}"
        )
    target[requested_seed] = {
        "requested_seed": requested_seed,
        "build_seed": build_seed,
        "domain_id": domain_id,
        "resolution": resolution,
    }


def _mode(raw: Any, label: str) -> str:
    if not isinstance(raw, Mapping):
        raise SeedProvenanceError(f"{label} must be an object")
    mode = _text(raw.get("mode"), f"{label}.mode")
    if mode not in {"explicit_seed", "origin_selected"}:
        raise SeedProvenanceError(
            f"{label}.mode must be explicit_seed or origin_selected"
        )
    return mode


def _normalized_ids(
    raw: Any,
    label: str,
    *,
    nonempty: bool,
) -> list[str]:
    values = _array(raw, label, nonempty=nonempty)
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        paper_id = _paper_id(value, f"{label}[{index}]")
        if paper_id in seen:
            raise SeedProvenanceError(
                f"{label}[{index}] duplicates normalized seed "
                f"{paper_id}"
            )
        seen.add(paper_id)
        result.append(paper_id)
    return result


def _array(raw: Any, label: str, *, nonempty: bool) -> list[Any]:
    if not isinstance(raw, list) or (nonempty and not raw):
        expected = "a non-empty array" if nonempty else "an array"
        raise SeedProvenanceError(f"{label} must be {expected}")
    return raw


def _closed(
    raw: Any,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SeedProvenanceError(f"{label} must be an object")
    if set(raw) != keys:
        raise SeedProvenanceError(
            f"{label} must contain exactly "
            + ", ".join(sorted(keys))
        )
    return copy.deepcopy(dict(raw))


def _paper_id(raw: Any, label: str) -> str:
    return normalize_paper_id(_text(raw, label))


def _optional_paper_id(raw: Any, label: str) -> str | None:
    return None if raw is None else _paper_id(raw, label)


def _text(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SeedProvenanceError(
            f"{label} must be a non-empty string"
        )
    return raw.strip()


__all__ = [
    "SEED_PROVENANCE_SCHEMA_VERSION",
    "SeedProvenanceError",
    "build_seed_provenance",
    "validate_seed_provenance",
]
