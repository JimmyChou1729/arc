"""Pure contracts for optional cross-chapter Companion editorial review."""

from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arc_jobs import canonical_json_bytes
from arc_render import extract_markdown_citation_ids, normalize_markdown

from .rich_text import RichTextError, parse_markdown


EDITORIAL_INVENTORY_SCHEMA = "arc.companion.editorial_inventory.v1"
EDITORIAL_REVIEW_SCHEMA = "arc.companion.editorial_review.v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NONEMPTY = {"type": "string", "minLength": 1}
_STRING_IDS = {
    "type": "array",
    "items": _NONEMPTY,
    "uniqueItems": True,
}


def _closed(
    properties: Mapping[str, Any], required: Sequence[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_REVISE_EDIT_SCHEMA = _closed(
    {
        "edit_id": _NONEMPTY,
        "unit_id": _NONEMPTY,
        "base_content_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "action": {"const": "revise"},
        "title": _NONEMPTY,
        "markdown_body": _NONEMPTY,
    },
    (
        "edit_id",
        "unit_id",
        "base_content_digest",
        "action",
        "title",
        "markdown_body",
    ),
)
_OMIT_EDIT_SCHEMA = _closed(
    {
        "edit_id": _NONEMPTY,
        "unit_id": _NONEMPTY,
        "base_content_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "action": {"const": "omit"},
    },
    ("edit_id", "unit_id", "base_content_digest", "action"),
)
EDITORIAL_PROPOSAL_SCHEMA = _closed(
    {
        "inventory_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "findings": {
            "type": "array",
            "items": _closed(
                {
                    "finding_id": _NONEMPTY,
                    "unit_ids": {
                        "type": "array",
                        "items": _NONEMPTY,
                        "minItems": 2,
                        "uniqueItems": True,
                    },
                    "redundancy_assessment": _NONEMPTY,
                    "retained_value_analysis": _NONEMPTY,
                    "edits": {
                        "type": "array",
                        "items": {
                            "oneOf": [
                                _REVISE_EDIT_SCHEMA,
                                _OMIT_EDIT_SCHEMA,
                            ]
                        },
                    },
                },
                (
                    "finding_id",
                    "unit_ids",
                    "redundancy_assessment",
                    "retained_value_analysis",
                    "edits",
                ),
            ),
        },
    },
    ("inventory_digest", "findings"),
)
EDITORIAL_REVIEW_AUDIT_SCHEMA = _closed(
    {
        "inventory_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "proposal_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "checked_source_anchors": {"const": True},
        "checked_user_intent": {"const": True},
        "checked_frozen_references": {"const": True},
        "approved_edit_ids": _STRING_IDS,
        "rejected_edits": {
            "type": "array",
            "items": _closed(
                {"edit_id": _NONEMPTY, "reason": _NONEMPTY},
                ("edit_id", "reason"),
            ),
        },
    },
    (
        "inventory_digest",
        "proposal_digest",
        "checked_source_anchors",
        "checked_user_intent",
        "checked_frozen_references",
        "approved_edit_ids",
        "rejected_edits",
    ),
)


class EditorialReviewError(ValueError):
    """The program-owned editorial inventory or report is inconsistent."""


@dataclass(frozen=True)
class EditorialInventory:
    """Frozen model index and exact full-text view for generated guide units."""

    document: Mapping[str, Any]
    full_text: str

    @property
    def inventory_digest(self) -> str:
        return str(self.document["inventory_digest"])

    @property
    def applicable(self) -> bool:
        return bool(self.document["applicable"])


@dataclass(frozen=True)
class EditorialResolution:
    """Resolved publication view plus its complete editorial audit report."""

    chapters: tuple[dict[str, Any], ...]
    report: Mapping[str, Any]


@dataclass
class _EditRecord:
    finding_id: str | None
    raw: Any
    edit_id: str | None = None
    unit_id: str | None = None
    action: str | None = None
    base_content_digest: str | None = None
    title: str | None = None
    markdown_body: str | None = None
    validation_error: str | None = None


def editorial_unit_content_digest(unit: Mapping[str, Any]) -> str:
    """Return the base digest to which a proposed edit must bind."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "title": _nonempty_string(unit.get("title"), "unit title"),
                "markdown_body": normalize_markdown(
                    _nonempty_string(
                        unit.get("content_markdown"), "unit Markdown"
                    )
                ),
                "citation_ids": _string_list(
                    unit.get("citations"), "unit citations"
                ),
            }
        )
    ).hexdigest()


def freeze_editorial_inventory(
    chapters: Sequence[Mapping[str, Any]],
    *,
    frozen_reference_ids: Sequence[str] | None = None,
) -> EditorialInventory:
    """Freeze only model-generated chapter, section, and companion units."""

    chapter_values = tuple(chapters)
    chapter_ids: set[str] = set()
    unit_ids: set[str] = set()
    visible_reference_ids: list[str] = []
    lines: list[str] = []
    frozen_chapters: list[dict[str, Any]] = []
    unit_count = 0
    populated_chapters: set[str] = set()

    for chapter_order, chapter in enumerate(chapter_values, 1):
        if not isinstance(chapter, Mapping):
            raise EditorialReviewError("editorial chapter must be an object")
        chapter_id = _nonempty_string(
            chapter.get("chapter_id"), "chapter ID"
        )
        if chapter_id in chapter_ids:
            raise EditorialReviewError("editorial chapters repeat a chapter ID")
        chapter_ids.add(chapter_id)
        chapter_title = _nonempty_string(
            chapter.get("title"), "chapter title"
        )
        raw_units = chapter.get("learning_units")
        if not isinstance(raw_units, list) or any(
            not isinstance(item, Mapping) for item in raw_units
        ):
            raise EditorialReviewError(
                "chapter learning_units must be an array of objects"
            )
        frozen_units: list[dict[str, Any]] = []
        for unit_order, unit in enumerate(raw_units, 1):
            unit_id = _nonempty_string(unit.get("unit_id"), "unit ID")
            if unit_id in unit_ids:
                raise EditorialReviewError(
                    "editorial chapters repeat a learning-unit ID"
                )
            unit_ids.add(unit_id)
            purpose = _nonempty_string(unit.get("purpose"), "unit purpose")
            if purpose not in {"chapter", "section", "companion"}:
                raise EditorialReviewError(
                    "editorial inventory contains a non-guide learning unit"
                )
            title = _nonempty_string(unit.get("title"), "unit title")
            markdown = normalize_markdown(
                _nonempty_string(
                    unit.get("content_markdown"), "unit Markdown"
                )
            )
            anchors = _string_list(
                unit.get("anchor_block_ids"), "unit anchors", nonempty=True
            )
            citations = _string_list(unit.get("citations"), "unit citations")
            if tuple(citations) != extract_markdown_citation_ids(markdown):
                raise EditorialReviewError(
                    "unit citations differ from visible Markdown citations"
                )
            visible_reference_ids.extend(citations)

            if lines:
                lines.append("")
            view_start = len(lines) + 1
            lines.append(
                f"<!-- ARC_EDITORIAL_UNIT unit_id={unit_id} "
                f"chapter_id={chapter_id} -->"
            )
            title_line = len(lines) + 1
            lines.append(f"# {title}")
            lines.append("")
            body_start = len(lines) + 1
            body_lines = markdown.splitlines()
            lines.extend(body_lines)
            body_end = len(lines)
            frozen_units.append(
                {
                    "unit_id": unit_id,
                    "chapter_id": chapter_id,
                    "chapter_order": chapter_order,
                    "unit_order": unit_order,
                    "purpose": purpose,
                    "title": title,
                    "anchor_block_ids": anchors,
                    "citation_ids": citations,
                    "content_digest": editorial_unit_content_digest(unit),
                    "view_range": {
                        "line_start": view_start,
                        "line_end": body_end,
                        "title_line": title_line,
                        "markdown_line_start": body_start,
                        "markdown_line_end": body_end,
                    },
                }
            )
            unit_count += 1
            populated_chapters.add(chapter_id)
        frozen_chapters.append(
            {
                "chapter_id": chapter_id,
                "chapter_order": chapter_order,
                "title": chapter_title,
                "units": frozen_units,
            }
        )

    full_text = ("\n".join(lines).rstrip() + "\n") if lines else ""
    if frozen_reference_ids is None:
        reference_ids = list(dict.fromkeys(visible_reference_ids))
    else:
        if isinstance(frozen_reference_ids, (str, bytes, bytearray)) or any(
            not isinstance(item, str) or not item
            for item in frozen_reference_ids
        ):
            raise EditorialReviewError(
                "frozen reference IDs must contain non-empty strings"
            )
        reference_ids = list(dict.fromkeys(frozen_reference_ids))
        missing = next(
            (
                item
                for item in visible_reference_ids
                if item not in set(reference_ids)
            ),
            None,
        )
        if missing is not None:
            raise EditorialReviewError(
                "visible unit citation is absent from frozen references: "
                f"{missing}"
            )
    material = {
        "schema_version": EDITORIAL_INVENTORY_SCHEMA,
        "chapters": frozen_chapters,
        "reference_ids": reference_ids,
        "full_text_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
        "full_text_size": len(full_text.encode("utf-8")),
        "chapter_count": len(chapter_values),
        "unit_count": unit_count,
        "applicable": len(chapter_values) >= 2 and len(populated_chapters) >= 2,
    }
    digest = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
    return EditorialInventory(
        document={**material, "inventory_digest": digest},
        full_text=full_text,
    )


def editorial_proposal_digest(proposal: Mapping[str, Any]) -> str:
    """Return the exact JSON artifact digest a final reviewer must acknowledge."""

    if not isinstance(proposal, Mapping):
        raise EditorialReviewError("editorial proposal must be an object")
    try:
        return hashlib.sha256(
            canonical_json_bytes(dict(proposal)) + b"\n"
        ).hexdigest()
    except (TypeError, ValueError) as exc:
        raise EditorialReviewError("editorial proposal is not canonical JSON") from exc


def resolve_editorial_review(
    chapters: Sequence[Mapping[str, Any]],
    inventory: EditorialInventory,
    proposal: Mapping[str, Any] | None,
    final_review: Mapping[str, Any] | None,
    *,
    proposer_artifact_digest: str | None = None,
    reviewer_artifact_digest: str | None = None,
) -> EditorialResolution:
    """Apply only well-bound edits explicitly approved by a closed final audit."""

    _validate_inventory_binding(chapters, inventory)
    proposer_digest = _optional_digest(
        proposer_artifact_digest, "proposer artifact digest"
    )
    reviewer_digest = _optional_digest(
        reviewer_artifact_digest, "reviewer artifact digest"
    )
    if not inventory.applicable:
        return _terminal_resolution(
            chapters,
            inventory,
            status="not_applicable",
            reason=(
                "Cross-chapter review requires generated units in at least two "
                "chapters."
            ),
            proposer_artifact_digest=proposer_digest,
            reviewer_artifact_digest=reviewer_digest,
        )

    proposal_digest = (
        editorial_proposal_digest(proposal)
        if isinstance(proposal, Mapping)
        else None
    )
    units, unit_chapters = _units_by_id(chapters)
    references = set(inventory.document["reference_ids"])
    findings, edits, proposal_errors = _decode_proposal(
        proposal,
        inventory=inventory,
        units=units,
        unit_chapters=unit_chapters,
    )
    _validate_edits(edits, units=units, references=references)
    audit, audit_error = _decode_final_audit(
        final_review,
        inventory_digest=inventory.inventory_digest,
        proposal_digest=proposal_digest,
        edit_ids=[item.edit_id for item in edits if item.edit_id is not None],
    )

    global_errors = [*proposal_errors]
    if audit_error is not None:
        global_errors.append(audit_error)
    approved = set(audit["approved_edit_ids"]) if audit is not None else set()
    rejected_reasons = (
        {
            str(item["edit_id"]): str(item["reason"])
            for item in audit["rejected_edits"]
        }
        if audit is not None
        else {}
    )

    resolved = [copy.deepcopy(dict(chapter)) for chapter in chapters]
    resolved_units = {
        str(unit["unit_id"]): (chapter, unit)
        for chapter in resolved
        for unit in chapter["learning_units"]
    }
    changes: list[dict[str, Any]] = []
    applied_ids: set[str] = set()
    revised = 0
    omitted = 0
    for edit in edits:
        original = _unit_text(units.get(edit.unit_id))
        reason = edit.validation_error
        if global_errors:
            reason = "; ".join(global_errors)
        elif edit.edit_id not in approved:
            reason = rejected_reasons.get(
                edit.edit_id or "", "reviewer did not approve this edit"
            )
        applied = reason is None
        final = original
        if applied:
            assert edit.edit_id is not None
            assert edit.unit_id is not None
            assert edit.action is not None
            chapter, unit = resolved_units[edit.unit_id]
            if edit.action == "omit":
                chapter["learning_units"] = [
                    item
                    for item in chapter["learning_units"]
                    if str(item.get("unit_id")) != edit.unit_id
                ]
                final = None
                omitted += 1
            else:
                assert edit.title is not None
                assert edit.markdown_body is not None
                unit["title"] = edit.title
                unit["content_markdown"] = edit.markdown_body
                unit["citations"] = list(
                    extract_markdown_citation_ids(edit.markdown_body)
                )
                final = _unit_text(unit)
                revised += 1
            applied_ids.add(edit.edit_id)
        changes.append(
            {
                "finding_id": edit.finding_id,
                "edit_id": edit.edit_id,
                "unit_id": edit.unit_id,
                "action": edit.action,
                "approved": edit.edit_id in approved,
                "applied": applied,
                "review_artifact_digest": reviewer_digest,
                "rejection_reason": reason,
                "original": original,
                "final": final,
            }
        )

    finding_reports = _finding_reports(findings, changes)
    status = "applied" if applied_ids else "no_changes"
    warnings = list(dict.fromkeys(global_errors))
    report = _report(
        status=status,
        inventory=inventory,
        reason=(
            str(final_review.get("reason"))
            if isinstance(final_review, Mapping)
            and isinstance(final_review.get("reason"), str)
            else "Editorial review produced no applicable changes."
        ),
        proposer_artifact_digest=proposer_digest,
        reviewer_artifact_digest=reviewer_digest,
        proposal_digest=proposal_digest,
        findings=finding_reports,
        changes=changes,
        warnings=warnings,
        reviewed_units=int(inventory.document["unit_count"]),
        revised_units=revised,
        omitted_units=omitted,
    )
    return EditorialResolution(tuple(resolved), report)


def unavailable_editorial_review(
    chapters: Sequence[Mapping[str, Any]],
    inventory: EditorialInventory,
    *,
    reason: str,
    proposer_artifact_digest: str | None = None,
    reviewer_artifact_digest: str | None = None,
) -> EditorialResolution:
    """Publish an advisory failure without losing valid chapter guides."""

    _validate_inventory_binding(chapters, inventory)
    return _terminal_resolution(
        chapters,
        inventory,
        status="unavailable" if inventory.applicable else "not_applicable",
        reason=_nonempty_string(reason, "unavailable reason"),
        proposer_artifact_digest=_optional_digest(
            proposer_artifact_digest, "proposer artifact digest"
        ),
        reviewer_artifact_digest=_optional_digest(
            reviewer_artifact_digest, "reviewer artifact digest"
        ),
    )


def _decode_proposal(
    proposal: Mapping[str, Any] | None,
    *,
    inventory: EditorialInventory,
    units: Mapping[str, Mapping[str, Any]],
    unit_chapters: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[_EditRecord], list[str]]:
    errors: list[str] = []
    findings: list[dict[str, Any]] = []
    edits: list[_EditRecord] = []
    if not isinstance(proposal, Mapping):
        return findings, edits, ["editorial proposal is missing or invalid"]
    if set(proposal) != {"inventory_digest", "findings"}:
        errors.append("editorial proposal has invalid fields")
    if proposal.get("inventory_digest") != inventory.inventory_digest:
        errors.append("editorial proposal inventory digest does not match")
    raw_findings = proposal.get("findings")
    if not isinstance(raw_findings, list):
        return findings, edits, [*errors, "editorial findings must be an array"]
    finding_ids = [
        item.get("finding_id")
        for item in raw_findings
        if isinstance(item, Mapping)
        and isinstance(item.get("finding_id"), str)
    ]
    duplicate_findings = {
        item for item, count in Counter(finding_ids).items() if count > 1
    }
    for raw in raw_findings:
        finding_error: str | None = None
        if not isinstance(raw, Mapping):
            finding_error = "editorial finding must be an object"
            finding_id = None
            unit_ids: list[str] = []
            redundancy = None
            retained = None
            raw_edits: list[Any] = []
        else:
            expected = {
                "finding_id",
                "unit_ids",
                "redundancy_assessment",
                "retained_value_analysis",
                "edits",
            }
            finding_id = raw.get("finding_id")
            redundancy = raw.get("redundancy_assessment")
            retained = raw.get("retained_value_analysis")
            raw_unit_ids = raw.get("unit_ids")
            raw_edits_value = raw.get("edits")
            unit_ids = (
                list(raw_unit_ids)
                if isinstance(raw_unit_ids, list)
                and all(isinstance(item, str) for item in raw_unit_ids)
                else []
            )
            raw_edits = (
                list(raw_edits_value)
                if isinstance(raw_edits_value, list)
                else []
            )
            if set(raw) != expected:
                finding_error = "editorial finding has invalid fields"
            elif not isinstance(finding_id, str) or not finding_id.strip():
                finding_error = "editorial finding ID is invalid"
            elif finding_id in duplicate_findings:
                finding_error = "editorial finding ID is duplicated"
            elif (
                len(unit_ids) < 2
                or len(unit_ids) != len(set(unit_ids))
                or any(item not in units for item in unit_ids)
                or len({unit_chapters.get(item) for item in unit_ids}) < 2
            ):
                finding_error = (
                    "editorial finding must bind unique known units from at "
                    "least two chapters"
                )
            elif not isinstance(redundancy, str) or not redundancy.strip():
                finding_error = "editorial redundancy assessment is invalid"
            elif not isinstance(retained, str) or not retained.strip():
                finding_error = "editorial retained-value analysis is invalid"
            elif not isinstance(raw_edits_value, list):
                finding_error = "editorial finding edits must be an array"
        normalized_finding = {
            "finding_id": finding_id,
            "unit_ids": unit_ids,
            "redundancy_assessment": redundancy,
            "retained_value_analysis": retained,
            "validation_error": finding_error,
        }
        findings.append(normalized_finding)
        for raw_edit in raw_edits:
            edit = _decode_edit(
                raw_edit,
                finding_id=finding_id if isinstance(finding_id, str) else None,
            )
            if finding_error is not None:
                edit.validation_error = finding_error
            elif (
                edit.unit_id is not None
                and edit.unit_id in units
                and edit.unit_id not in unit_ids
            ):
                edit.validation_error = (
                    "editorial edit target is absent from its finding units"
                )
            edits.append(edit)

    edit_ids = [item.edit_id for item in edits if item.edit_id is not None]
    duplicates = {item for item, count in Counter(edit_ids).items() if count > 1}
    unit_targets = [item.unit_id for item in edits if item.unit_id is not None]
    duplicate_units = {
        item for item, count in Counter(unit_targets).items() if count > 1
    }
    for edit in edits:
        if edit.edit_id in duplicates:
            edit.validation_error = "editorial edit ID is duplicated"
        if edit.unit_id in duplicate_units:
            edit.validation_error = "multiple editorial edits target one unit"
    return findings, edits, errors


def _decode_edit(raw: Any, *, finding_id: str | None) -> _EditRecord:
    edit = _EditRecord(finding_id, raw)
    if not isinstance(raw, Mapping):
        edit.validation_error = "editorial edit must be an object"
        return edit
    edit.edit_id = raw.get("edit_id") if isinstance(raw.get("edit_id"), str) else None
    edit.unit_id = raw.get("unit_id") if isinstance(raw.get("unit_id"), str) else None
    edit.action = raw.get("action") if isinstance(raw.get("action"), str) else None
    edit.base_content_digest = (
        raw.get("base_content_digest")
        if isinstance(raw.get("base_content_digest"), str)
        else None
    )
    if edit.action == "revise":
        expected = {
            "edit_id",
            "unit_id",
            "base_content_digest",
            "action",
            "title",
            "markdown_body",
        }
        edit.title = raw.get("title") if isinstance(raw.get("title"), str) else None
        edit.markdown_body = (
            raw.get("markdown_body")
            if isinstance(raw.get("markdown_body"), str)
            else None
        )
    elif edit.action == "omit":
        expected = {"edit_id", "unit_id", "base_content_digest", "action"}
    else:
        expected = set(raw)
        edit.validation_error = "editorial edit action is invalid"
    if set(raw) != expected:
        edit.validation_error = "editorial edit has invalid fields"
    if edit.edit_id is None or not edit.edit_id.strip():
        edit.validation_error = "editorial edit ID is invalid"
    if edit.unit_id is None or not edit.unit_id.strip():
        edit.validation_error = "editorial edit unit ID is invalid"
    if (
        edit.base_content_digest is None
        or _SHA256.fullmatch(edit.base_content_digest) is None
    ):
        edit.validation_error = "editorial edit base digest is invalid"
    return edit


def _validate_edits(
    edits: Sequence[_EditRecord],
    *,
    units: Mapping[str, Mapping[str, Any]],
    references: set[str],
) -> None:
    for edit in edits:
        if edit.validation_error is not None:
            continue
        unit = units.get(edit.unit_id)
        if unit is None:
            edit.validation_error = "editorial edit refers to an unknown unit"
            continue
        if edit.base_content_digest != editorial_unit_content_digest(unit):
            edit.validation_error = "editorial edit base digest is stale"
            continue
        if edit.action != "revise":
            continue
        if edit.title is None or not edit.title.strip():
            edit.validation_error = "editorial replacement title is invalid"
            continue
        if edit.markdown_body is None:
            edit.validation_error = "editorial replacement Markdown is invalid"
            continue
        edit.title = edit.title.strip()
        try:
            edit.markdown_body = normalize_markdown(edit.markdown_body)
            parse_markdown(edit.markdown_body)
        except (RichTextError, ValueError) as exc:
            edit.validation_error = f"editorial replacement Markdown is invalid: {exc}"
            continue
        citations = extract_markdown_citation_ids(edit.markdown_body)
        unknown = next((item for item in citations if item not in references), None)
        if unknown is not None:
            edit.validation_error = (
                f"editorial replacement cites an unknown reference: {unknown}"
            )
            continue
        if (
            edit.title == str(unit["title"])
            and edit.markdown_body
            == normalize_markdown(str(unit["content_markdown"]))
        ):
            edit.validation_error = "editorial replacement makes no actual change"


def _decode_final_audit(
    final_review: Mapping[str, Any] | None,
    *,
    inventory_digest: str,
    proposal_digest: str | None,
    edit_ids: Sequence[str],
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(final_review, Mapping):
        return None, "final editorial review is missing"
    expected_review = {"schema_version", "action", "reason", "feedback", "payload"}
    if set(final_review) != expected_review:
        return None, "final editorial review has invalid fields"
    if final_review.get("schema_version") != "arc.proposer_reviewer.review.v1":
        return None, "final editorial review schema is invalid"
    if (
        not isinstance(final_review.get("reason"), str)
        or not str(final_review["reason"]).strip()
        or not isinstance(final_review.get("feedback"), Mapping)
    ):
        return None, "final editorial review envelope is invalid"
    if final_review.get("action") != "stop":
        return None, "editorial reviewer did not return a final stop decision"
    payload = final_review.get("payload")
    if not isinstance(payload, Mapping) or set(payload) != {
        "inventory_digest",
        "proposal_digest",
        "checked_source_anchors",
        "checked_user_intent",
        "checked_frozen_references",
        "approved_edit_ids",
        "rejected_edits",
    }:
        return None, "final editorial review audit has invalid fields"
    if payload.get("inventory_digest") != inventory_digest:
        return None, "final editorial review binds the wrong inventory digest"
    if proposal_digest is None or payload.get("proposal_digest") != proposal_digest:
        return None, "final editorial review binds the wrong proposal digest"
    if any(
        payload.get(name) is not True
        for name in (
            "checked_source_anchors",
            "checked_user_intent",
            "checked_frozen_references",
        )
    ):
        return None, "final editorial review did not complete required checks"
    approved = payload.get("approved_edit_ids")
    rejected = payload.get("rejected_edits")
    if not isinstance(approved, list) or any(
        not isinstance(item, str) or not item for item in approved
    ):
        return None, "approved editorial edit IDs are invalid"
    if len(approved) != len(set(approved)):
        return None, "approved editorial edit IDs are duplicated"
    if not isinstance(rejected, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"edit_id", "reason"}
        or not isinstance(item.get("edit_id"), str)
        or not str(item.get("edit_id"))
        or not isinstance(item.get("reason"), str)
        or not str(item.get("reason")).strip()
        for item in rejected
    ):
        return None, "rejected editorial edits are invalid"
    rejected_ids = [str(item["edit_id"]) for item in rejected]
    if len(rejected_ids) != len(set(rejected_ids)):
        return None, "rejected editorial edit IDs are duplicated"
    if set(approved) & set(rejected_ids):
        return None, "editorial audit both approves and rejects one edit"
    if len(edit_ids) != len(set(edit_ids)) or set(approved) | set(rejected_ids) != set(
        edit_ids
    ):
        return None, "editorial audit does not exactly cover proposed edit IDs"
    return {
        "approved_edit_ids": list(approved),
        "rejected_edits": [dict(item) for item in rejected],
    }, None


def _finding_reports(
    findings: Sequence[Mapping[str, Any]], changes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    values = []
    for finding in findings:
        finding_changes = [
            item
            for item in changes
            if item.get("finding_id") == finding.get("finding_id")
        ]
        applied = sum(bool(item["applied"]) for item in finding_changes)
        status = (
            "approved"
            if finding_changes and applied == len(finding_changes)
            else "partial"
            if applied
            else "rejected"
        )
        values.append(
            {
                "finding_id": finding.get("finding_id"),
                "unit_ids": list(finding.get("unit_ids", [])),
                "redundancy_assessment": finding.get("redundancy_assessment"),
                "retained_value_analysis": finding.get("retained_value_analysis"),
                "approval_status": status,
                "validation_error": finding.get("validation_error"),
                "edit_ids": [item.get("edit_id") for item in finding_changes],
            }
        )
    return values


def _terminal_resolution(
    chapters: Sequence[Mapping[str, Any]],
    inventory: EditorialInventory,
    *,
    status: str,
    reason: str,
    proposer_artifact_digest: str | None,
    reviewer_artifact_digest: str | None,
) -> EditorialResolution:
    return EditorialResolution(
        tuple(copy.deepcopy(dict(chapter)) for chapter in chapters),
        _report(
            status=status,
            inventory=inventory,
            reason=reason,
            proposer_artifact_digest=proposer_artifact_digest,
            reviewer_artifact_digest=reviewer_artifact_digest,
            proposal_digest=None,
            findings=[],
            changes=[],
            warnings=[reason] if status == "unavailable" else [],
            reviewed_units=0,
            revised_units=0,
            omitted_units=0,
        ),
    )


def _report(
    *,
    status: str,
    inventory: EditorialInventory,
    reason: str,
    proposer_artifact_digest: str | None,
    reviewer_artifact_digest: str | None,
    proposal_digest: str | None,
    findings: Sequence[Mapping[str, Any]],
    changes: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    reviewed_units: int,
    revised_units: int,
    omitted_units: int,
) -> dict[str, Any]:
    report = {
        "schema_version": EDITORIAL_REVIEW_SCHEMA,
        "status": status,
        "inventory_digest": inventory.inventory_digest,
        "proposal_digest": proposal_digest,
        "proposer_artifact_digest": proposer_artifact_digest,
        "reviewer_artifact_digest": reviewer_artifact_digest,
        "reason": reason,
        "warnings": list(warnings),
        "counts": {
            "reviewed_units": reviewed_units,
            "findings": len(findings),
            "proposed_edits": len(changes),
            "revised_units": revised_units,
            "omitted_units": omitted_units,
            "rejected_edits": sum(not bool(item["applied"]) for item in changes),
        },
        "findings": [copy.deepcopy(dict(item)) for item in findings],
        "changes": [copy.deepcopy(dict(item)) for item in changes],
    }
    validate_editorial_report(report)
    return report


def validate_editorial_report(value: Mapping[str, Any]) -> None:
    """Validate the stable, publication-owned editorial report envelope."""

    expected = {
        "schema_version",
        "status",
        "inventory_digest",
        "proposal_digest",
        "proposer_artifact_digest",
        "reviewer_artifact_digest",
        "reason",
        "warnings",
        "counts",
        "findings",
        "changes",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise EditorialReviewError("editorial report has invalid fields")
    if value["schema_version"] != EDITORIAL_REVIEW_SCHEMA:
        raise EditorialReviewError("unsupported editorial report schema")
    if value["status"] not in {
        "not_applicable",
        "no_changes",
        "applied",
        "unavailable",
    }:
        raise EditorialReviewError("editorial report status is invalid")
    _required_digest(value["inventory_digest"], "inventory digest")
    for key in (
        "proposal_digest",
        "proposer_artifact_digest",
        "reviewer_artifact_digest",
    ):
        _optional_digest(value[key], key.replace("_", " "))
    _nonempty_string(value["reason"], "editorial report reason")
    if not isinstance(value["warnings"], list) or any(
        not isinstance(item, str) or not item for item in value["warnings"]
    ):
        raise EditorialReviewError("editorial report warnings are invalid")
    counts = value["counts"]
    if not isinstance(counts, Mapping) or set(counts) != {
        "reviewed_units",
        "findings",
        "proposed_edits",
        "revised_units",
        "omitted_units",
        "rejected_edits",
    } or any(type(item) is not int or item < 0 for item in counts.values()):
        raise EditorialReviewError("editorial report counts are invalid")
    if not isinstance(value["findings"], list) or not isinstance(
        value["changes"], list
    ):
        raise EditorialReviewError("editorial report details are invalid")


def _validate_inventory_binding(
    chapters: Sequence[Mapping[str, Any]], inventory: EditorialInventory
) -> None:
    if not isinstance(inventory, EditorialInventory):
        raise EditorialReviewError("editorial inventory type is invalid")
    current = freeze_editorial_inventory(
        chapters,
        frozen_reference_ids=tuple(inventory.document["reference_ids"]),
    )
    if (
        current.full_text != inventory.full_text
        or current.document != inventory.document
    ):
        raise EditorialReviewError(
            "editorial inventory does not match the current chapter guides"
        )


def _units_by_id(
    chapters: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    units: dict[str, Mapping[str, Any]] = {}
    owners: dict[str, str] = {}
    for chapter in chapters:
        chapter_id = str(chapter["chapter_id"])
        for unit in chapter["learning_units"]:
            unit_id = str(unit["unit_id"])
            units[unit_id] = unit
            owners[unit_id] = chapter_id
    return units, owners


def _unit_text(unit: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if unit is None:
        return None
    return {
        "title": str(unit["title"]),
        "markdown_body": normalize_markdown(str(unit["content_markdown"])),
        "citation_ids": list(unit["citations"]),
        "content_digest": editorial_unit_content_digest(unit),
    }


def _nonempty_string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditorialReviewError(f"{description} must be a non-empty string")
    return value.strip()


def _string_list(
    value: Any, description: str, *, nonempty: bool = False
) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ) or (nonempty and not value) or len(value) != len(set(value)):
        raise EditorialReviewError(
            f"{description} must contain unique non-empty strings"
        )
    return list(value)


def _required_digest(value: Any, description: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EditorialReviewError(f"{description} must be a SHA-256 digest")
    return value


def _optional_digest(value: Any, description: str) -> str | None:
    if value is None:
        return None
    return _required_digest(value, description)


__all__ = [
    "EDITORIAL_INVENTORY_SCHEMA",
    "EDITORIAL_PROPOSAL_SCHEMA",
    "EDITORIAL_REVIEW_AUDIT_SCHEMA",
    "EDITORIAL_REVIEW_SCHEMA",
    "EditorialInventory",
    "EditorialResolution",
    "EditorialReviewError",
    "editorial_proposal_digest",
    "editorial_unit_content_digest",
    "freeze_editorial_inventory",
    "resolve_editorial_review",
    "unavailable_editorial_review",
    "validate_editorial_report",
]
