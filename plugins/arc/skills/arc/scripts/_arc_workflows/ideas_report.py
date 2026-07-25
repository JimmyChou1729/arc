"""JSON diagnostics and Markdown rendering for ranked ARC ideas."""

from __future__ import annotations

import re
from typing import Any, Mapping

from _arc_workflows.ideas_marking import report_columns


CROSS_REPORT_COLUMNS = [
    ("IR", "user_intent_relevance"),
    ("TR", "cross_domain_transfer_quality"),
    ("TC", "substantive_target_contribution"),
    ("N", "novelty"),
    ("CN", "confidence_of_novelty"),
    ("SV", "scientific_value"),
    ("F", "calculation_feasibility"),
    ("WD", "problem_well_definedness"),
    ("T", "total_score"),
]


def cross_diagnostics(
    run_id: str,
    *,
    ranking: list[dict[str, Any]],
    top_three: list[dict[str, Any]],
    unqualified: list[dict[str, Any]],
    portfolio_excluded: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    top_keys = {(entry["loop_id"], entry["round"]) for entry in top_three}
    candidates = []
    for qualified, entries in ((True, ranking), (False, unqualified)):
        for entry in entries:
            candidates.append(
                {
                    "loop_id": entry["loop_id"],
                    "round": entry["round"],
                    "title": entry["title"],
                    "qualified": qualified,
                    "qualification_reasons": entry.get(
                        "qualification_reasons", []
                    ),
                    "compatibility_classification": entry.get(
                        "compatibility_classification", {}
                    ),
                    "transfer_signature": entry.get(
                        "normalized_transfer_signature", ""
                    ),
                    "central_mechanism": entry.get(
                        "normalized_central_mechanism", ""
                    ),
                    "top_three": (
                        entry["loop_id"],
                        entry["round"],
                    )
                    in top_keys,
                    "marks": entry["marks"],
                }
            )
    for entry in portfolio_excluded:
        candidates.append(
            {
                "loop_id": entry["loop_id"],
                "round": entry["round"],
                "title": entry["title"],
                "qualified": True,
                "portfolio_excluded": True,
                "portfolio_exclusion_reason": entry[
                    "portfolio_exclusion_reason"
                ],
                "qualification_reasons": entry.get(
                    "qualification_reasons", []
                ),
                "transfer_signature": entry.get(
                    "normalized_transfer_signature", ""
                ),
                "central_mechanism": entry.get(
                    "normalized_central_mechanism", ""
                ),
                "top_three": False,
                "marks": entry["marks"],
            }
        )
    return {
        "schema_version": "arc.ideas.cross_domain_diagnostics.v1",
        "run_id": run_id,
        "qualified_count": len(ranking),
        "unqualified_count": len(unqualified),
        "portfolio_excluded_count": len(portfolio_excluded),
        "top_three_count": len(top_three),
        "distinct_qualified_transfer_signatures": len(
            {
                entry.get("normalized_transfer_signature", "")
                for entry in ranking
            }
        ),
        "warnings": warnings,
        "candidates": candidates,
    }


def single_domain_diagnostics(
    run_id: str,
    *,
    ranking: list[dict[str, Any]],
    top_three: list[dict[str, Any]],
    unqualified: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    top_keys = {(entry["loop_id"], entry["round"]) for entry in top_three}
    candidates = []
    for qualified, entries in ((True, ranking), (False, unqualified)):
        for entry in entries:
            assessment = entry.get("idea_assessment", {})
            candidates.append(
                {
                    "loop_id": entry["loop_id"],
                    "round": entry["round"],
                    "title": entry["title"],
                    "qualified": qualified,
                    "qualification_policy": entry.get(
                        "qualification_policy", ""
                    ),
                    "qualification_reasons": entry.get(
                        "qualification_reasons", []
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
                    "top_three": (
                        entry["loop_id"],
                        entry["round"],
                    )
                    in top_keys,
                    "marks": entry["marks"],
                }
            )
    return {
        "schema_version": "arc.ideas.single_domain_diagnostics.v1",
        "run_id": run_id,
        "qualified_count": len(ranking),
        "unqualified_count": len(unqualified),
        "top_three_count": len(top_three),
        "warnings": warnings,
        "candidates": candidates,
    }


def markdown_table(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    if payload.get("warnings"):
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
    if payload.get("cross_domain"):
        lines.extend(
            ["", "# Appendix: Unqualified Cross-Domain Candidates"]
        )
        if not payload.get("unqualified"):
            lines.extend(["", "None."])
        for entry in payload.get("unqualified", []):
            lines.extend(
                [
                    "",
                    f"## `{entry['loop_id']}` — {_heading_text(entry['title'])}",
                    "",
                    f"- Best observed round: `{entry['round']}`",
                    "- Qualification failures:",
                    *[
                        f"  - {reason}"
                        for reason in entry.get("qualification_reasons", [])
                    ],
                ]
            )
        lines.extend(
            [
                "",
                "# Appendix: Portfolio-Excluded Cross-Domain Candidates",
            ]
        )
        if not payload.get("portfolio_excluded"):
            lines.extend(["", "None."])
        for entry in payload.get("portfolio_excluded", []):
            lines.extend(
                [
                    "",
                    f"## `{entry['loop_id']}` — {_heading_text(entry['title'])}",
                    "",
                    f"- Selected round: `{entry['round']}`",
                    f"- Exclusion: `{entry['portfolio_exclusion_reason']}`",
                ]
            )
    elif payload.get("single_domain_qualification"):
        lines.extend(
            ["", "# Appendix: Unqualified Single-Domain Candidates"]
        )
        if not payload.get("unqualified"):
            lines.extend(["", "None."])
        for entry in payload.get("unqualified", []):
            lines.extend(
                [
                    "",
                    f"## `{entry['loop_id']}` — {_heading_text(entry['title'])}",
                    "",
                    f"- Best observed round: `{entry['round']}`",
                    "- Qualification failures:",
                    *[
                        f"  - {reason}"
                        for reason in entry.get("qualification_reasons", [])
                    ],
                ]
            )
    return "\n".join(lines)


def _summary_table(payload: dict[str, Any]) -> str:
    if payload.get("cross_domain"):
        return _cross_summary_table(payload)
    lines = [
        "# Ideas",
        "",
        "Abbreviations:",
        "",
        "IR=intent relevance, N=novelty, CN=confidence of novelty, SV=scientific value, "
        "PL=planning, WD=well-definedness, T=total.",
    ]
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
        _compact_round_marks_table(entry),
    ]


def _compact_round_marks_table(entry: dict[str, Any]) -> str:
    columns = [
        ("IR", "user_intent_relevance"),
        ("N", "novelty"),
        ("CN", "confidence_of_novelty"),
        ("SV", "scientific_value"),
        ("PL", "planning"),
        ("WD", "problem_well_definedness"),
        ("T", "total_score"),
    ]
    lines = [
        "| Round | IR | N | CN | SV | PL | WD | T |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        mark_values = " | ".join(
            _format_mark(marks.get(field)) for _, field in columns
        )
        lines.append(f"| {round_entry['round']} | {mark_values} |")
    return "\n".join(lines)


def _cross_summary_table(payload: dict[str, Any]) -> str:
    lines = [
        "# Ideas",
        "",
        "Abbreviations:",
        "",
        "IR=intent relevance, TR=transfer quality, TC=target contribution, N=novelty, "
        "CN=confidence of novelty, SV=scientific value, F=feasibility, WD=well-definedness, T=total.",
    ]
    for warning in payload.get("warnings", []):
        lines.extend(["", str(warning)])
    for entry in payload.get("ranking", []):
        lines.extend(["", *_round_marks_summary_section_cross(entry)])
    return "\n".join(lines)


def _round_marks_summary_section_cross(
    entry: dict[str, Any],
) -> list[str]:
    return [
        f"## `{entry['loop_id']}`",
        "",
        _heading_text(entry["title"]),
        "",
        _compact_cross_marks_table(entry),
    ]


def _compact_cross_marks_table(entry: dict[str, Any]) -> str:
    headers = " | ".join(label for label, _field in CROSS_REPORT_COLUMNS)
    separators = "|".join("---:" for _ in CROSS_REPORT_COLUMNS)
    lines = [f"| Round | {headers} |", f"|---:|{separators}|"]
    for round_entry in entry.get("rounds", []):
        marks = round_entry["marks"]
        values = " | ".join(
            _format_mark(marks.get(field))
            for _label, field in CROSS_REPORT_COLUMNS
        )
        lines.append(f"| {round_entry['round']} | {values} |")
    return "\n".join(lines)


def _appendix_section(entry: dict[str, Any]) -> list[str]:
    proposer_artifact = entry["proposer_artifact"]
    review_artifact = entry["review_artifact"]
    return [
        f"### {entry['rank']}. {_heading_text(entry['title'])}",
        "",
        f"- Loop: `{entry['loop_id']}`",
        f"- Selected round: `{entry['round']}`",
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


def _round_marks_table(entry: dict[str, Any]) -> str:
    if "cross_domain_assessment" in entry:
        columns = [
            {"label": label, "field": field}
            for label, field in CROSS_REPORT_COLUMNS
        ]
    else:
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
            r"(?<![\w$])([A-Za-z]+\^[A-Za-z0-9]+_[A-Za-z0-9+-]+)(?![\w])",
            lambda match: f"${_format_math(match.group(1))}$",
            parts[index],
        )
        parts[index] = re.sub(
            r"(?<![\w$])([A-Za-zαβγδεηθκλρτΦΣΔπℓ]+_[A-Za-z0-9+-]+)(?![\w])",
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
