"""Typed, package-owned views of exported ARC domain artifacts.

This module validates one domain summary together with its paper JSON pack.
Project-level seed coverage, field grouping, and ideas routing deliberately
remain outside the package boundary.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from arc_paper import normalize_paper_id
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from jsonschema.exceptions import SchemaError as JsonSchemaError

from .packs import PAPER_JSON_PACK_SCHEMA_VERSION
from .paths import safe_domain_id
from .summary import DOMAIN_SUMMARY_SCHEMA


class DomainPackageValidationError(ValueError):
    """A summary or paper-pack artifact violates its package contract."""

    code = "domain_package_invalid"


@dataclass(frozen=True)
class DomainSummaryView:
    """Validated fields owned by one current domain-summary artifact."""

    schema_version: str
    title: str
    overview: str
    task_focus: Mapping[str, Any]
    foundation_paper_id: str
    best_reference_paper_id: str
    methodology: tuple[Mapping[str, Any], ...]
    mathematical_opportunities: Mapping[str, Any]
    known_solved_cases: tuple[Mapping[str, Any], ...]
    open_axes_for_new_work: tuple[Mapping[str, Any], ...]
    referenced_paper_ids: tuple[str, ...]
    _paper_references: tuple[tuple[str, str], ...] = field(
        repr=False,
    )


@dataclass(frozen=True)
class DomainPaperPackView:
    """Validated identity and coverage fields from one paper JSON pack."""

    domain_id: str
    foundation_paper_id: str
    paper_ids: tuple[str, ...]
    citation_edges: tuple[tuple[str, str], ...]
    _alias_groups: tuple[frozenset[str], ...] = field(
        repr=False,
    )

    def covers(self, paper_id: str) -> bool:
        """Return whether the pack contains this paper identity or an alias."""

        normalized = normalize_paper_id(str(paper_id or "").strip())
        return bool(normalized) and any(
            normalized in aliases for aliases in self._alias_groups
        )

    def equivalent(self, left: str, right: str) -> bool:
        """Return whether two identifiers resolve to one paper in this pack."""

        normalized_left = normalize_paper_id(str(left or "").strip())
        normalized_right = normalize_paper_id(str(right or "").strip())
        if not normalized_left or not normalized_right:
            return False
        return any(
            normalized_left in aliases and normalized_right in aliases
            for aliases in self._alias_groups
        )


@dataclass(frozen=True)
class DomainPackageView:
    """A validated summary and paper pack with one authoritative identity."""

    domain_id: str
    summary: DomainSummaryView
    paper_pack: DomainPaperPackView


def decode_domain_summary(value: Any) -> DomainSummaryView:
    """Decode the closed current v5 summary artifact."""

    document = _mapping(value, path="summary")
    schema_version = _nonempty_string(
        document.get("schema_version"),
        path="summary.schema_version",
    )
    current_version = DOMAIN_SUMMARY_SCHEMA["properties"]["schema_version"][
        "const"
    ]
    if schema_version != current_version:
        raise DomainPackageValidationError(
            f"summary.schema_version must be {current_version}"
        )
    error = _schema_error(document, DOMAIN_SUMMARY_SCHEMA)
    if error is not None:
        raise DomainPackageValidationError(
            f"summary does not match {schema_version}: {error}"
        )

    title = _nonempty_string(document["domain_title"], path="summary.domain_title")
    references = tuple(_summary_paper_references(document))
    normalized_references = tuple(
        sorted(
            {
                _normalized_paper_id(paper_id, path=path)
                for path, paper_id in references
            }
        )
    )
    return DomainSummaryView(
        schema_version=schema_version,
        title=title,
        overview=str(document["brief_introduction"]),
        task_focus=deepcopy(dict(document["task_focus"])),
        foundation_paper_id=_normalized_paper_id(
            document["foundation_paper"]["paper_id"],
            path="summary.foundation_paper.paper_id",
        ),
        best_reference_paper_id=_normalized_paper_id(
            document["best_reference_paper"]["paper_id"],
            path="summary.best_reference_paper.paper_id",
        ),
        methodology=tuple(deepcopy(document["methodology"])),
        mathematical_opportunities=deepcopy(
            dict(document["mathematical_opportunities"])
        ),
        known_solved_cases=tuple(deepcopy(document["known_solved_cases"])),
        open_axes_for_new_work=tuple(
            deepcopy(document["open_axes_for_new_work"])
        ),
        referenced_paper_ids=normalized_references,
        _paper_references=references,
    )


def decode_domain_paper_pack(
    value: Any,
    *,
    expected_domain_id: str | None = None,
) -> DomainPaperPackView:
    """Decode the closed paper-pack shape and validate its internal coverage."""

    document = _mapping(value, path="paper_pack")
    _closed_keys(
        document,
        {
            "schema_version",
            "domain_id",
            "foundation_paper",
            "paper_count",
            "papers",
            "warnings",
            "created_at",
        },
        path="paper_pack",
    )
    if document["schema_version"] != PAPER_JSON_PACK_SCHEMA_VERSION:
        raise DomainPackageValidationError(
            "paper_pack.schema_version must be "
            f"{PAPER_JSON_PACK_SCHEMA_VERSION}"
        )
    domain_id = _domain_id(document["domain_id"], path="paper_pack.domain_id")
    if expected_domain_id is not None:
        expected = _domain_id(expected_domain_id, path="expected_domain_id")
        if domain_id != expected:
            raise DomainPackageValidationError(
                f"paper_pack.domain_id {domain_id!r} does not match expected "
                f"domain ID {expected!r}"
            )
    foundation_paper_id = _normalized_paper_id(
        document["foundation_paper"],
        path="paper_pack.foundation_paper",
    )
    papers = _list(document["papers"], path="paper_pack.papers")
    paper_count = document["paper_count"]
    if type(paper_count) is not int or paper_count < 0:
        raise DomainPackageValidationError(
            "paper_pack.paper_count must be a non-negative integer"
        )
    if paper_count != len(papers):
        raise DomainPackageValidationError(
            "paper_pack.paper_count does not match paper_pack.papers"
        )
    _string_list(document["warnings"], path="paper_pack.warnings")
    _nonempty_string(document["created_at"], path="paper_pack.created_at")

    primary_ids: list[str] = []
    alias_groups: list[frozenset[str]] = []
    citation_edges: set[tuple[str, str]] = set()
    observed_aliases: set[str] = set()
    for index, raw_paper in enumerate(papers):
        path = f"paper_pack.papers[{index}]"
        paper = _mapping(raw_paper, path=path)
        _closed_keys(
            paper,
            {"paper_id", "role", "metadata", "references", "toc", "warnings"},
            path=path,
        )
        paper_id = _normalized_paper_id(
            paper["paper_id"],
            path=f"{path}.paper_id",
        )
        _nonempty_string(paper["role"], path=f"{path}.role")
        metadata = _mapping(paper["metadata"], path=f"{path}.metadata")
        references = _mapping_list(
            paper["references"],
            path=f"{path}.references",
        )
        _mapping_list(paper["toc"], path=f"{path}.toc")
        _string_list(paper["warnings"], path=f"{path}.warnings")

        aliases = frozenset(
            {
                paper_id,
                *_paper_aliases(paper),
                *_paper_aliases(metadata),
            }
        )
        overlap = observed_aliases.intersection(aliases)
        if overlap:
            raise DomainPackageValidationError(
                f"{path}.paper_id duplicates an existing paper identity: "
                f"{sorted(overlap)[0]}"
            )
        observed_aliases.update(aliases)
        primary_ids.append(paper_id)
        alias_groups.append(aliases)
        for reference_index, reference in enumerate(references):
            if "paper_id" not in reference:
                continue
            target_id = _normalized_paper_id(
                reference["paper_id"],
                path=f"{path}.references[{reference_index}].paper_id",
            )
            citation_edges.add((paper_id, target_id))

    foundation_matches = [
        aliases for aliases in alias_groups if foundation_paper_id in aliases
    ]
    if len(foundation_matches) != 1:
        raise DomainPackageValidationError(
            "paper_pack.foundation_paper must identify exactly one packed paper"
        )

    return DomainPaperPackView(
        domain_id=domain_id,
        foundation_paper_id=foundation_paper_id,
        paper_ids=tuple(sorted(primary_ids)),
        citation_edges=tuple(sorted(citation_edges)),
        _alias_groups=tuple(alias_groups),
    )


def decode_domain_package(
    summary: Any,
    paper_pack: Any,
    *,
    expected_domain_id: str | None = None,
) -> DomainPackageView:
    """Decode and cross-check the two artifacts that define one domain package."""

    decoded_pack = decode_domain_paper_pack(
        paper_pack,
        expected_domain_id=expected_domain_id,
    )
    decoded_summary = decode_domain_summary(summary)
    if not decoded_pack.equivalent(
        decoded_summary.foundation_paper_id,
        decoded_pack.foundation_paper_id,
    ):
        raise DomainPackageValidationError(
            "summary.foundation_paper does not match "
            "paper_pack.foundation_paper"
        )
    missing = [
        path
        for path, paper_id in decoded_summary._paper_references
        if not decoded_pack.covers(paper_id)
    ]
    if missing:
        raise DomainPackageValidationError(
            "summary references papers absent from paper_pack: "
            + ", ".join(missing)
        )
    return DomainPackageView(
        domain_id=decoded_pack.domain_id,
        summary=decoded_summary,
        paper_pack=decoded_pack,
    )


def _summary_paper_references(
    document: Mapping[str, Any],
) -> Iterator[tuple[str, str]]:
    for key in ("foundation_paper", "best_reference_paper"):
        yield f"summary.{key}.paper_id", document[key]["paper_id"]
    for index, item in enumerate(document["methodology"]):
        for paper_index, paper_id in enumerate(item["papers"]):
            yield (
                f"summary.methodology[{index}].papers[{paper_index}]",
                paper_id,
            )
    opportunities = document.get("mathematical_opportunities")
    if isinstance(opportunities, Mapping):
        for index, item in enumerate(opportunities["well_defined_problems"]):
            for paper_index, paper_id in enumerate(
                item["target_domain_papers"]
            ):
                yield (
                    "summary.mathematical_opportunities.well_defined_problems"
                    f"[{index}].target_domain_papers[{paper_index}]",
                    paper_id,
                )
    for key in ("known_solved_cases", "open_axes_for_new_work"):
        for index, item in enumerate(document[key]):
            for paper_index, paper_id in enumerate(item["papers"]):
                yield (
                    f"summary.{key}[{index}].papers[{paper_index}]",
                    paper_id,
                )


def _paper_aliases(record: Mapping[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for container in (record, record.get("identifiers")):
        if not isinstance(container, Mapping):
            continue
        for key in (
            "paper_id",
            "id",
            "upi",
            "arxiv",
            "arxiv_id",
            "inspire",
            "inspire_recid",
            "doi",
        ):
            value = str(container.get(key) or "").strip()
            if not value:
                continue
            if key in {"arxiv", "arxiv_id"} and ":" not in value:
                value = f"arXiv:{value}"
            elif key in {"inspire", "inspire_recid"} and value.isdigit():
                value = f"inspire:{value}"
            normalized = normalize_paper_id(value)
            if normalized:
                aliases.add(normalized)
    return aliases


def _schema_error(value: Any, schema: Mapping[str, Any]) -> str | None:
    try:
        validate_json_schema(instance=value, schema=schema)
    except (JsonSchemaValidationError, JsonSchemaError) as exc:
        return str(exc)
    return None


def _closed_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    path: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(repr(key) for key in actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise DomainPackageValidationError(
            f"{path} must use its closed shape ({'; '.join(details)})"
        )


def _mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainPackageValidationError(f"{path} must be an object")
    return value


def _list(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise DomainPackageValidationError(f"{path} must be an array")
    return value


def _mapping_list(value: Any, *, path: str) -> list[Mapping[str, Any]]:
    items = _list(value, path=path)
    output: list[Mapping[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise DomainPackageValidationError(
                f"{path}[{index}] must be an object"
            )
        output.append(item)
    return output


def _string_list(value: Any, *, path: str) -> list[str]:
    items = _list(value, path=path)
    output: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise DomainPackageValidationError(
                f"{path}[{index}] must be a string"
            )
        output.append(item)
    return output


def _nonempty_string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainPackageValidationError(
            f"{path} must be a non-empty string"
        )
    return value.strip()


def _normalized_paper_id(value: Any, *, path: str) -> str:
    text = _nonempty_string(value, path=path)
    normalized = normalize_paper_id(text)
    if not normalized:
        raise DomainPackageValidationError(
            f"{path} must contain a valid paper identifier"
        )
    return normalized


def _domain_id(value: Any, *, path: str) -> str:
    text = _nonempty_string(value, path=path)
    try:
        return safe_domain_id(text)
    except ValueError as exc:
        raise DomainPackageValidationError(f"{path} is invalid: {exc}") from exc


__all__ = [
    "DomainPackageValidationError",
    "DomainPackageView",
    "DomainPaperPackView",
    "DomainSummaryView",
    "decode_domain_package",
    "decode_domain_paper_pack",
    "decode_domain_summary",
]
