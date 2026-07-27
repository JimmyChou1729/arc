"""JSON diagnostics and Markdown rendering for ranked ARC ideas."""

from __future__ import annotations

import re
from typing import Any, Mapping

from _arc_workflows.ideas_marking import report_columns


COMMON_REPORT_COLUMNS = [
    ("IR", "user_intent_relevance", "intent relevance"),
    ("N", "novelty", "novelty"),
    ("CN", "confidence_of_novelty", "confidence of novelty"),
    ("SV", "scientific_value", "scientific value"),
    ("PL", "planning", "planning"),
    ("WD", "problem_well_definedness", "well-definedness"),
    ("SI", "simplicity", "simplicity"),
    ("GE", "generality", "generality"),
    ("T", "total_score", "total"),
]


def _readiness_counts(
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    states = ("ready", "ready_with_risk", "not_ready", "unassessed")
    return {
        f"{state}_count": sum(
            candidate.get("scientific_readiness") == state
            for candidate in candidates
        )
        for state in states
    }


def ideas_diagnostics(
    run_id: str,
    *,
    ranking: list[dict[str, Any]],
    top_three: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    """Build route-neutral, non-gating diagnostics."""
    top_keys = {(entry["loop_id"], entry["round"]) for entry in top_three}
    candidates = []
    for entry in ranking:
        assessment = entry.get("idea_assessment", {})
        candidates.append(
            {
                "loop_id": entry["loop_id"],
                "round": entry["round"],
                "title": entry["title"],
                "scientific_route": entry.get("scientific_route", {}),
                "scientific_readiness": entry.get(
                    "scientific_readiness", "unassessed"
                ),
                "scientific_warnings": entry.get(
                    "scientific_warnings", []
                ),
                "scientific_readiness_policy": entry.get(
                    "scientific_readiness_policy", ""
                ),
                "problem_importance": (
                    assessment.get("problem_importance", "")
                    if isinstance(assessment, Mapping)
                    else ""
                ),
                "importance_rationale": (
                    assessment.get("importance_rationale", "")
                    if isinstance(assessment, Mapping)
                    else ""
                ),
                "feasibility_classification": entry.get(
                    "feasibility_classification", {}
                ),
                "legacy_scientific_context": entry.get(
                    "legacy_scientific_context", {}
                ),
                "top_three": (
                    entry["loop_id"],
                    entry["round"],
                )
                in top_keys,
                "marks": entry["marks"],
            }
        )
    return {
        "schema_version": "arc.ideas.diagnostics.v3",
        "run_id": run_id,
        "candidate_count": len(ranking),
        **_readiness_counts(candidates),
        "top_three_count": len(top_three),
        "warnings": warnings,
        "candidates": candidates,
    }


def markdown_table(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    if payload.get("mode") == "partial":
        lines.extend(
            [
                "# Partial Ideas — Non-Formal Provisional Report",
                "",
                f"> {payload.get('notice', 'NON-FORMAL PROVISIONAL REPORT')}",
                "",
                (
                    "This report does not replace a formal ranking. It uses "
                    "only complete, trace-verified committed proposer-reviewer rounds."
                ),
                "",
            ]
        )
    if payload.get("warnings") and payload.get("mode") == "partial":
        lines.extend(
            [
                "# Ranking Warnings",
                "",
                *[f"> {warning}" for warning in payload["warnings"]],
                "",
            ]
        )
    lines.extend(
        [
            _summary_table(payload),
            "",
            "# Appendix: Idea Details",
        ]
    )
    for entry in payload["ranking"]:
        lines.extend(["", *_appendix_section(entry)])
    return "\n".join(lines)


def _summary_table(payload: dict[str, Any]) -> str:
    representative = _representative_entry(payload)
    columns = _report_columns(representative)
    abbreviations = ", ".join(
        f"{label}={description}"
        for label, _field, description in columns
    )
    lines = [
        (
            "## Provisional Ideas"
            if payload.get("mode") == "partial"
            else "# Ideas"
        ),
        "",
    ]
    portfolio = _portfolio_assessment_lines(payload)
    if portfolio:
        lines.extend([*portfolio, ""])
    lines.extend(["Abbreviations:", "", f"{abbreviations}."])
    for warning in payload.get("warnings", []):
        lines.extend(["", str(warning)])
    for entry in payload.get("ranking", []):
        lines.extend(["", *_round_marks_summary_section(entry)])
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _round_marks_summary_section(entry: dict[str, Any]) -> list[str]:
    return [
        f"## `{entry['loop_id']}`",
        "",
        _heading_text(entry["title"]),
        "",
        *_partial_metadata_lines(entry),
        *_scientific_route_lines(entry),
        *_scientific_caveat_lines(entry),
        "",
        _compact_round_marks_table(entry),
    ]


def _compact_round_marks_table(entry: dict[str, Any]) -> str:
    columns = [
        (label, field)
        for label, field, _description in _report_columns(entry)
    ]
    headers = " | ".join(label for label, _field in columns)
    separators = "|".join("---:" for _ in columns)
    lines = [
        f"| Round | {headers} |",
        f"|---:|{separators}|",
    ]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        mark_values = " | ".join(
            _format_mark(marks.get(field)) for _, field in columns
        )
        lines.append(f"| {round_entry['round']} | {mark_values} |")
    return "\n".join(lines)


def _portfolio_assessment_lines(
    payload: Mapping[str, Any],
) -> list[str]:
    if payload.get("mode") == "partial":
        return []
    raw = payload.get("portfolio_assessment")
    if not isinstance(raw, Mapping):
        return []
    status = str(raw.get("status", "missing")).strip() or "missing"
    lines = [
        "## Global Scientific Assessment (Advisory)",
        "",
        (
            "This portfolio-level assessment is non-binding. It does not "
            "change any referee mark, selected round, formal rank, top-three "
            "membership, or candidate visibility."
        ),
        "",
    ]
    content = raw.get("content")
    if status != "available" or not isinstance(content, Mapping):
        reason = str(raw.get("reason", "") or "").strip()
        detail = f" — {reason}" if reason else ""
        lines.append(f"> Assessment status: `{status}`{detail}")
        return lines

    overall = str(
        content.get("overall_assessment", "") or ""
    ).strip()
    if overall:
        lines.extend([_math_markdown_text(overall), ""])

    findings = content.get("cross_candidate_findings")
    if isinstance(findings, list) and findings:
        lines.extend(["### Cross-Candidate Findings", ""])
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            topic = str(finding.get("topic", "") or "").strip()
            text = str(finding.get("finding", "") or "").strip()
            candidate_ids = finding.get("candidate_ids")
            ids = (
                [
                    str(candidate_id).strip()
                    for candidate_id in candidate_ids
                    if str(candidate_id).strip()
                ]
                if isinstance(candidate_ids, list)
                else []
            )
            scope = (
                ", ".join(f"`{candidate_id}`" for candidate_id in ids)
                if ids
                else "portfolio-wide"
            )
            if topic and text:
                lines.append(
                    f"- **{topic}** ({scope}): {_math_markdown_text(text)}"
                )
        lines.append("")

    notes = content.get("candidate_notes")
    if isinstance(notes, list) and notes:
        lines.extend(["### Candidate Notes", ""])
        for note in notes:
            if not isinstance(note, Mapping):
                continue
            candidate_id = str(
                note.get("candidate_id", "") or ""
            ).strip()
            text = str(note.get("note", "") or "").strip()
            if candidate_id and text:
                lines.append(
                    f"- `{candidate_id}`: {_math_markdown_text(text)}"
                )
        lines.append("")

    directions = content.get("missing_or_underrepresented_directions")
    if isinstance(directions, list) and directions:
        lines.extend(
            ["### Missing or Underrepresented Directions", ""]
        )
        for direction in directions:
            if not isinstance(direction, Mapping):
                continue
            name = str(direction.get("direction", "") or "").strip()
            rationale = str(direction.get("rationale", "") or "").strip()
            first = str(
                direction.get("minimal_first_calculation", "") or ""
            ).strip()
            assessment_status = str(
                direction.get("assessment_status", "") or ""
            ).strip()
            if name:
                lines.append(f"- **{name}**")
                if rationale:
                    lines.append(
                        f"  - Rationale: {_math_markdown_text(rationale)}"
                    )
                if first:
                    lines.append(
                        "  - Minimal first calculation: "
                        + _math_markdown_text(first)
                    )
                if assessment_status:
                    lines.append(
                        f"  - Status: `{assessment_status}`"
                    )
        lines.append("")

    strategy = content.get("research_strategy")
    if isinstance(strategy, list) and strategy:
        lines.extend(
            [
                "### Research Strategy",
                "",
                *[
                    f"- {_math_markdown_text(str(item).strip())}"
                    for item in strategy
                    if str(item).strip()
                ],
                "",
            ]
        )
    limitations = content.get("limitations")
    if isinstance(limitations, list) and limitations:
        lines.extend(
            [
                "### Portfolio-Assessment Limitations",
                "",
                *[
                    f"- {_math_markdown_text(str(item).strip())}"
                    for item in limitations
                    if str(item).strip()
                ],
            ]
        )
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _appendix_section(entry: dict[str, Any]) -> list[str]:
    proposer_artifact = entry["proposer_artifact"]
    review_artifact = entry["review_artifact"]
    return [
        f"### {entry['rank']}. {_heading_text(entry['title'])}",
        "",
        f"- Loop: `{entry['loop_id']}`",
        f"- Selected round: `{entry['round']}`",
        *_partial_metadata_lines(entry),
        *_scientific_route_lines(entry),
        (
            "- Proposer artifact: "
            f"`{proposer_artifact['artifact_id']}` "
            f"(sha256 `{proposer_artifact['sha256']}`)"
        ),
        (
            "- Review artifact: "
            f"`{review_artifact['artifact_id']}` "
            f"(sha256 `{review_artifact['sha256']}`)"
        ),
        "",
        "#### Referee Marks by Round",
        "",
        _round_marks_table(entry),
        "",
        "#### Scientific Readiness and Caveats",
        "",
        *_scientific_caveat_lines(entry),
        *_scientific_taste_section(entry),
        "",
        "#### Focused Novelty Audit",
        "",
        "This is a focused evidence audit, not an exhaustive proof of novelty.",
        "",
        "Evidence checked:",
        "",
        *_bullet_items(entry.get("evidence_checked", [])),
        "",
        "Tool queries used:",
        "",
        *_bullet_items(entry.get("tool_queries_used", [])),
        "",
        "Unresolved reviewer limitations:",
        "",
        *_bullet_items(entry.get("reviewer_limitations", [])),
        "",
        "#### Full Idea Verbatim",
        "",
        _handoff_text(entry.get("proposer_output", {})),
    ]


def _scientific_taste_section(entry: Mapping[str, Any]) -> list[str]:
    benchmark = entry.get("reviewer_benchmark")
    if not isinstance(benchmark, Mapping):
        return []
    comparison = str(benchmark.get("comparison", "") or "").strip()
    alternative = str(
        benchmark.get("same_direction_alternative", "") or ""
    ).strip()
    preserves_direction = benchmark.get("preserves_proposer_direction")
    if not comparison and not alternative and not isinstance(
        preserves_direction, bool
    ):
        return []
    lines = ["", "#### Scientific Taste Review", ""]
    if comparison:
        lines.extend(["Overall comparison:", "", comparison, ""])
    if alternative:
        lines.extend(
            [
                "Simpler same-direction alternative:",
                "",
                alternative,
                "",
            ]
        )
    if isinstance(preserves_direction, bool):
        lines.append(
            "Preserves proposer direction: "
            f"`{'yes' if preserves_direction else 'no'}`"
        )
    if lines[-1] == "":
        lines.pop()
    return lines


def _representative_entry(
    payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    entries = payload.get("ranking")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping):
                return entry
    scheme = payload.get("marking_scheme")
    if isinstance(scheme, Mapping):
        return {"marking_scheme": scheme}
    return None


def _report_columns(
    entry: Mapping[str, Any] | None,
) -> list[tuple[str, str, str]]:
    scheme = entry.get("marking_scheme") if isinstance(entry, Mapping) else None
    configured_fields: set[str] = set()
    if isinstance(scheme, Mapping):
        marks = scheme.get("marks")
        if isinstance(marks, list):
            configured_fields.update(
                str(item.get("field", ""))
                for item in marks
                if isinstance(item, Mapping)
            )
        total = scheme.get("total_score")
        if isinstance(total, Mapping):
            configured_fields.add(str(total.get("field", "")))
    if not configured_fields:
        configured_fields = {
            field for _label, field, _description in COMMON_REPORT_COLUMNS
        }
    known = [
        column
        for column in COMMON_REPORT_COLUMNS
        if column[1] in configured_fields
    ]
    known_fields = {field for _label, field, _description in known}
    if isinstance(scheme, Mapping):
        for column in report_columns(scheme):
            field = column["field"]
            if field not in known_fields:
                label = column["label"]
                known.append((label, field, label.casefold()))
                known_fields.add(field)
    return known


def _partial_metadata_lines(entry: Mapping[str, Any]) -> list[str]:
    if "committed_round_count" not in entry:
        return []
    lines = [
        f"- Loop lifecycle: `{entry.get('loop_lifecycle', 'unknown')}`",
        f"- Complete committed rounds: `{entry.get('committed_round_count', 0)}`",
    ]
    pause_reason = entry.get("pause_reason")
    lines.append(
        f"- Pause reason: `{pause_reason}`"
        if pause_reason
        else "- Pause reason: none recorded"
    )
    return [*lines, ""]


def _scientific_route_lines(entry: Mapping[str, Any]) -> list[str]:
    route = entry.get("scientific_route")
    if not isinstance(route, Mapping):
        return []
    description = str(route.get("description", "") or "").strip()
    rationale = str(route.get("rationale", "") or "").strip()
    package_ids = route.get("domain_package_ids_used")
    ids = (
        [
            str(package_id).strip()
            for package_id in package_ids
            if str(package_id).strip()
        ]
        if isinstance(package_ids, list)
        else []
    )
    if not description and not rationale and not ids:
        return []
    lines = [
        "- Scientific route: "
        + (_math_markdown_text(description) if description else "not described"),
        (
            "- Domain packages used: "
            + (
                ", ".join(f"`{package_id}`" for package_id in ids)
                if ids
                else "none recorded"
            )
        ),
    ]
    if rationale:
        lines.append(
            "- Route rationale: " + _math_markdown_text(rationale)
        )
    return [*lines, ""]


def _scientific_caveat_lines(entry: Mapping[str, Any]) -> list[str]:
    readiness = str(
        entry.get("scientific_readiness", "unassessed")
    ).strip() or "unassessed"
    warning_values = entry.get("scientific_warnings")
    warnings = (
        [
            str(warning).strip()
            for warning in warning_values
            if str(warning).strip()
        ]
        if isinstance(warning_values, list)
        else []
    )
    lines = [
        f"- Scientific readiness: `{readiness}`",
        "- Scientific caveats:",
    ]
    if warnings:
        lines.extend(f"  - {warning}" for warning in warnings)
    else:
        lines.append("  - None recorded.")
    legacy = entry.get("legacy_scientific_context")
    if isinstance(legacy, Mapping) and legacy:
        lines.extend(_legacy_scientific_context_lines(legacy))
    return lines


def _legacy_scientific_context_lines(
    context: Mapping[str, Any],
) -> list[str]:
    """Render historical reviewer evidence as advisory scientific context."""
    lines = ["- Historical cross-domain review context (advisory):"]
    scalar_fields = (
        ("Source domain", "source_domain"),
        ("Target domain", "target_domain"),
        ("Transfer status", "transfer_status"),
        ("Target contribution", "target_contribution_status"),
        ("Source ingredient validity", "source_ingredient_validity"),
        ("Target adaptation validity", "target_adaptation_validity"),
        ("Feasibility", "feasibility_status"),
        ("Resulting capability", "resulting_new_capability"),
        ("Recommended action", "recommended_action"),
    )
    for label, field in scalar_fields:
        value = str(context.get(field, "") or "").strip()
        if value:
            lines.append(f"  - {label}: {_math_markdown_text(value)}")
    novelty = context.get("novelty_coverage")
    if isinstance(novelty, Mapping) and novelty:
        rendered = ", ".join(
            f"{scope}={'checked' if checked else 'not checked'}"
            for scope, checked in novelty.items()
            if isinstance(checked, bool)
        )
        if rendered:
            lines.append(f"  - Novelty coverage: {rendered}")
    for label, field in (
        ("Blocking compatibility failure", "blocking_compatibility_failures"),
        ("Manageable compatibility risk", "manageable_compatibility_risks"),
        ("Critical concern", "critical_concerns"),
    ):
        values = context.get(field)
        if isinstance(values, list):
            lines.extend(
                f"  - {label}: {_math_markdown_text(str(value))}"
                for value in values
                if str(value).strip()
            )
    return lines


def _round_marks_table(entry: dict[str, Any]) -> str:
    columns = report_columns(entry["marking_scheme"])
    mark_headers = " | ".join(column["label"] for column in columns)
    mark_separator = "|".join("---:" for _ in columns)
    lines = [
        f"| Loop | Round | {mark_headers} |",
        f"|---|---:|{mark_separator}|",
    ]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        mark_values = " | ".join(
            _format_mark(marks.get(column["field"])) for column in columns
        )
        lines.append(
            "| {loop_id} | {round} | {mark_values} |".format(
                loop_id=round_entry["loop_id"],
                round=round_entry["round"],
                mark_values=mark_values,
            )
        )
    return "\n".join(lines)


def _format_mark(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return ""


def _bullet_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["- None recorded."]
    items = [
        str(item).strip()
        for item in value
        if str(item).strip()
    ]
    return [f"- {item}" for item in items] or ["- None recorded."]


def _heading_text(value: Any) -> str:
    text = str(value).replace("\n", " ").strip()
    return text or "Untitled Idea"


def _handoff_text(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    fields = [
        ("Title", data.get("title", "")),
        ("Idea Summary", data.get("idea_summary", "")),
        ("Calculation Plan", data.get("calculation_plan", "")),
    ]
    lines: list[str] = []
    for label, item in fields:
        text = _math_markdown_text(str(item or "").strip())
        lines.append(f"{label}: {text}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _math_markdown_text(text: str) -> str:
    text = re.sub(r"\\\((.*?)\\\)", r"$\1$", text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.*?)\\\]", r"$$\1$$", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", _math_markdown_span, text)
    text = _display_math_lines(text)
    return _inline_raw_math_tokens(text)


def _math_markdown_span(match: re.Match[str]) -> str:
    content = match.group(1)
    if _looks_like_math(content):
        return f"${_format_math(content)}$"
    return match.group(0)


def _looks_like_math(text: str) -> bool:
    return bool(re.search(r"[=<>^_∫⟨⟩δΔκγρτλπℓεαβηθΦΣ{}|≈≤≥]", text))


def _inline_raw_math_tokens(text: str) -> str:
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$)", text, flags=re.DOTALL)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(
            r"(?<![\\\w$])([A-Za-z]+\^[A-Za-z0-9]+_[A-Za-z0-9+-]+)(?![\w])",
            lambda match: f"${_format_math(match.group(1))}$",
            parts[index],
        )
        parts[index] = re.sub(
            r"(?<![\\\w$])([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+_[A-Za-z0-9+-]+)(?![\w])",
            lambda match: f"${_format_math(match.group(1))}$",
            parts[index],
        )
    return "".join(parts)


def _display_math_lines(text: str) -> str:
    lines: list[str] = []
    in_display_math = False
    for line in text.splitlines():
        stripped = line.strip().rstrip(",")
        if stripped == "$$":
            lines.append(line)
            in_display_math = not in_display_math
            continue
        if in_display_math:
            lines.append(line)
            continue
        math_span = re.fullmatch(r"\$(.+)\$", stripped)
        if math_span and _looks_like_display_equation(math_span.group(1)):
            lines.extend(["$$", math_span.group(1), "$$"])
        elif _looks_like_display_equation(stripped):
            lines.extend(["$$", _format_math(stripped), "$$"])
        else:
            lines.append(line)
    return "\n".join(lines)


def _looks_like_display_equation(text: str) -> bool:
    if not text or ":" in text[:24]:
        return False
    if "=" in text and re.match(
        r"^(?:\\[A-Za-z]+|[A-Za-zαβγδεηθκλρτΦΣΔπℓ][A-Za-z0-9]*[_^(])",
        text,
    ):
        return True
    return bool(
        re.match(
            r"^([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+[A-Za-z0-9_]*\(|∫|\\int)",
            text,
        )
    )


def _format_math(text: str) -> str:
    text = str(text).strip()
    text = re.sub(
        r"\b([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+(?:\^[A-Za-z0-9]+)?)_([A-Za-z0-9+-]+)(?![\w])",
        lambda match: f"{match.group(1)}_{{{match.group(2)}}}",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()
