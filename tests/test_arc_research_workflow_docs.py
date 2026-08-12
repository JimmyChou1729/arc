from __future__ import annotations

import json
import importlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/arc"
SKILL = PLUGIN / "skills/arc"
RULES = SKILL / "rules"
WF = SKILL / "workflows"
WJ = WF / "json"
SCRIPTS = SKILL / "scripts"


def _schema_keyword_nodes(value, *, path="$"):
    if isinstance(value, dict):
        if "const" in value or "enum" in value:
            yield path, value
        for key, child in value.items():
            yield from _schema_keyword_nodes(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _schema_keyword_nodes(child, path=f"{path}[{index}]")


def test_active_workflow_const_and_enum_nodes_declare_provider_types() -> None:
    missing = []
    for path in sorted(WJ.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for node_path, node in _schema_keyword_nodes(document):
            if "type" not in node:
                missing.append(f"{path.name}:{node_path}")
    assert missing == []


def test_calculation_workflow_files_exist() -> None:
    for name in ["plan.md", "calculate.md", "check.md"]:
        assert (WF / name).is_file()
    assert not (WF / "foundation.md").exists()
    for name in ["plan.schema.json", "foundation.schema.json", "calculate.schema.json"]:
        assert not (WJ / name).exists()
    assert not (WF / "scripts").exists()
    assert not (SCRIPTS / "_arc_workflows/filter-foundation-context.py").exists()


def test_arc_skill_routes_check_and_calculation_workflows() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert "references/" not in text
    assert "classify" in text.lower()
    assert "five cases" in text.lower()
    assert "check.md" in text
    assert "plan.md" in text
    assert "calculate.md" in text
    assert "companion.md" in text
    assert "foundation.md" not in text
    assert "work-note.md" in text


def test_arc_skill_defaults_managed_workflows_to_auto_without_startup_menu() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    text_flat = " ".join(text.split())

    assert "## Preflight Gate" in text
    assert "managed ARC workflow run" in text
    assert "workflow artifacts" in text
    assert "domain references" in text
    assert "ranked ideas" in text_flat
    assert "recommendations, research directions" in text_flat
    assert "All requests default to `automation_level: auto`" in text_flat
    assert "never ask the user to choose an execution mode at startup" in text_flat
    assert "Use `interactive` only when the user explicitly asks" in text_flat
    assert "collecting citers or references" in text_flat
    assert "generating paper summaries or summary batches" in text_flat
    assert "non-evaluative paper-data output" in text_flat
    assert "must not produce recommendations, research directions, scientific rankings" in text_flat
    assert "ARC reports, or project-local workflow artifacts" in text_flat
    assert "download papers that cited 0911.3380 since 2024" in text_flat
    assert "direct ARC tool orchestration" in text_flat
    assert "mode-eligible" not in text
    assert "provenance exposed by the host" not in text
    assert "Run automatically (Recommended)" not in text
    assert "Confirm major steps" not in text
    assert text.index("## Preflight Gate") < text.index("## Required References")


def test_arc_skill_frontloads_workflow_references_before_route_selection() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    required = text[text.index("## Required References") : text.index("## Workflow")]
    required_flat = " ".join(required.split())

    assert "Note checking, verification, or audit requests" in required
    assert "`workflows/check.md` before any parse, section read, or equation extraction call" in required_flat
    assert "When the user intent triggers a workflow-specific file" in required
    for name in ["check.md", "domain.md", "ideas.md", "plan.md", "calculate.md"]:
        assert f"`workflows/{name}`" in required
    assert "blocking requirement before any workflow CLI call" in required_flat


def test_arc_skill_case3_requires_full_check_workflow_phases() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    case3 = text[text.index("Case 3:") : text.index("Case 4:")]
    case3_flat = " ".join(case3.split())

    assert "`workflows/check.md` was already loaded in Required References" in case3
    assert (
        "Parse -> Preflight -> Write Planning Handoff -> Execute `plan.md` and "
        "`calculate.md` -> Record Note-Check Status"
    ) in case3_flat
    assert "Do not skip directly to parsing results" in case3
    assert "mandatory" in case3


def test_check_plan_calculate_workflows_treat_heavy_workload_as_nonoptional() -> None:
    for name in ["check.md", "plan.md", "calculate.md"]:
        text = " ".join((WF / name).read_text(encoding="utf-8").lower().split())
        assert "heavy workload" in text
        assert "workload size is not a stop condition" in text
        assert "must not skip mandatory phases" in text
        assert "user explicitly stops" in text


def test_arc_skill_references_pdf_export_manuals() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8").lower()
    manual = (SKILL / "manuals/arc-jobs.md").read_text(encoding="utf-8").lower()
    manual_flat = " ".join(manual.split())

    assert "markdown report export" in text
    assert "`rules/math_typeset.md`" in text
    assert "`manuals/arc-jobs.md`" in text
    assert "project-aware pdf renderer" in manual
    assert "ordinary blocking command" in manual_flat
    assert "instead of routing it through `arc-jobs`" in manual_flat
    assert "md2pdf" not in manual
    assert "markdown report" in manual
    assert "print `warning:`" in manual_flat
    assert "do not debug pandoc or tex" in manual_flat


def test_shared_docs_describe_public_proposer_reviewer_projection() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    manual = (SKILL / "manuals/arc-proposer-reviewer.md").read_text(encoding="utf-8")
    llm = (SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for text in (skill, manual):
        assert "arc-proposer-reviewer" in text
    for text in (skill, manual):
        assert "inspect" in text
        assert "trace" in text
        assert "show-round" in text
    assert "`manuals/arc-proposer-reviewer.md`" in skill
    assert "core-only" in skill
    assert "best effort" in skill
    assert "ranking, recovery, retries, or resume" in skill
    assert "best-effort activity" in manual
    assert "verified committed-round" in manual
    assert "public expansion" in manual
    assert "physical durable-state paths" in manual
    assert "arc-proposer-reviewer inspect" not in llm
    assert "arc-proposer-reviewer" not in readme
    assert "arc-llm run-text" not in readme
    assert "arc-llm run-json" not in readme


def test_math_typeset_rules_define_markdown_math_hygiene() -> None:
    text = (RULES / "math_typeset.md").read_text(encoding="utf-8")

    assert "ARC Math Typesetting Reference" in text
    assert "Use `$...$` for inline math" in text
    assert "Do not use Markdown code spans for TeX or math snippets" in text
    assert r"`\partial_{x_0}^2`" in text
    assert r"$\partial_{x_0}^2$" in text
    assert r"`\hat{\mathcal K}_+ - \hat{\mathcal K}_-`" in text
    assert r"$\hat{\mathcal K}_+ - \hat{\mathcal K}_-$" in text
    assert "stable IDs such as `eq_00009`" in text


def test_report_workflows_reference_math_typeset_rules() -> None:
    for name in ["check.md", "domain.md", "ideas.md", "plan.md", "calculate.md"]:
        text = (WF / name).read_text(encoding="utf-8")

        assert "`rules/math_typeset.md`" in text


def test_arc_skill_lists_math_typeset_reference() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert "`rules/math_typeset.md`" in text
    assert "Markdown math" in text


def test_workflows_start_pdf_export_for_user_facing_markdown() -> None:
    expected_pdf_guard_counts = {
        "check.md": 1,
        "domain.md": 1,
        "ideas.md": 1,
        "plan.md": 2,
        "calculate.md": 1,
    }
    for name, guard_count in expected_pdf_guard_counts.items():
        text = (WF / name).read_text(encoding="utf-8").lower()
        text_flat = " ".join(text.split())
        assert "`manuals/arc-jobs.md` markdown report export" in text
        assert "warning:" in text
        assert "md2pdf" not in text
        assert "report-export gate" not in text
        assert text_flat.count("do not claim") >= guard_count


def test_ideas_phase_4_uses_clean_selection_prompt_without_dry_run() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    manual = (SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8")
    lower = text.lower()

    assert "--dry-run" not in text
    assert "Check Planned Calls" not in text
    assert "idea workflow dry run" not in manual.lower()
    assert "### Phase 4: Select Next Action" in text
    assert "use the host's selection/menu" in " ".join(lower.split())
    assert "`Proceed with ranked idea #1 (Recommended)`" in text
    assert "`Proceed with ranked idea #2`" in text
    assert "`Proceed with ranked idea #3`" in text
    assert "`other`" not in text
    assert "or quit" not in lower
    assert "`Let's discuss`" not in text
    assert "The option labels must be the raw labels" not in text
    assert "with the same three options" in text


def test_arc_context_json_defines_run_identity_and_skill_paths() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert "`run_id`" in text
    assert "`created_at`" in text
    assert "`arc_run_root`" in text
    assert "`project_dir_name`" in text
    assert "`skill_version`" in text
    assert "`skill_dir`" in text
    assert "`skill_workflow_json_dir`" in text


def test_arc_skill_resolves_generated_project_dir_under_launch_cwd() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    setup = text[text.index("Step 4: Resolve `<project-dir>`.") : text.index("Step 5: Write `<project-dir>/context.json`.")]
    setup_flat = " ".join(setup.split())

    assert "Capture `<arc-run-root>` by running `pwd -P`" in setup
    assert "resolve-project-dir.py" in setup
    assert "<arc-run-root>/<project_dir_name>" in setup_flat
    assert "direct child" in setup
    assert "Do not create `arc-output/<project_dir_name>`" in setup_flat
    assert ".claude" in setup
    assert ".codex" in setup


def test_readme_keeps_generated_runs_in_the_ignored_local_tree() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "below the git-ignored `local/` tree" in text
    assert "0_ref/" not in text


def test_workflow_script_commands_use_skill_dir_placeholder() -> None:
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")
    calculate = (WF / "calculate.md").read_text(encoding="utf-8")

    assert "python3 <skill-dir>/scripts/run-ideas.py" in ideas
    assert "python3 <skill-dir>/scripts/rank-ideas.py" in ideas
    assert "python3 <skill-dir>/scripts/run-calculate.py" in calculate
    assert "python3 <skill-dir>/workflows/scripts/" not in ideas
    assert "python3 <skill-dir>/workflows/scripts/" not in calculate


def test_calculate_uses_runtime_launcher_for_core_only_batch_queries() -> None:
    calculate = (WF / "calculate.md").read_text(encoding="utf-8")

    for command in ("inspect", "trace", "show-round"):
        assert (
            f"<skill-dir>/scripts/arc-runtime arc-proposer-reviewer {command}"
            in calculate
        )


def test_domain_summary_warnings_are_visible_and_recorded() -> None:
    domain = (WF / "domain.md").read_text(encoding="utf-8")
    manual = (SKILL / "manuals/arc-domain.md").read_text(encoding="utf-8")

    assert "print `WARNING:` immediately" in domain
    assert "`<project-dir>/.arc/domain/warnings.md`" in domain
    assert "status, warnings, and published artifact references" in manual


def test_manuals_do_not_hardcode_checkout_cache_paths() -> None:
    for manual in sorted((SKILL / "manuals").glob("arc-*.md")):
        text = manual.read_text(encoding="utf-8")
        assert "/arc-dev/" not in text
        assert "--help" in text
    paper = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    paper_flat = " ".join(paper.split())
    assert "doctor-cache" not in paper
    assert "cache administration" in paper_flat
    assert "physical cache paths" in paper
    assert "ARC_JOBS_CACHE" not in (
        SKILL / "manuals/arc-jobs.md"
    ).read_text(encoding="utf-8")


def test_package_manuals_are_self_contained_quick_starts() -> None:
    expected = {
        "arc-paper.md": "`arc-paper`",
        "arc-domain.md": "`arc-domain`",
        "arc-jobs.md": "`arc-jobs`",
        "arc-llm.md": "`arc-llm`",
        "arc-proposer-reviewer.md": "`arc-proposer-reviewer`",
        "arc-translate.md": "`arc-translate`",
        "arc-render.md": "`arc-render`",
        "arc-companion.md": "`arc-companion`",
    }
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert {path.name for path in (SKILL / "manuals").glob("arc-*.md")} == set(expected)
    for name, package in expected.items():
        text = (SKILL / "manuals" / name).read_text(encoding="utf-8")
        assert "Quick Start" in text
        assert package in text
        assert "## Help" in text
        assert f"`manuals/{name}`" in skill


def test_manual_quick_start_argv_match_current_cli_parsers() -> None:
    for package in (
        "arc-jobs",
        "arc-llm",
        "arc-proposer-reviewer",
        "arc-paper",
        "arc-render",
        "arc-domain",
        "arc-translate",
        "arc-companion",
    ):
        sys.path.insert(0, str(ROOT / "packages" / package / "src"))

    modules = {
        name: importlib.import_module(module)
        for name, module in {
            "paper": "arc_paper.cli",
            "domain": "arc_domain.cli",
            "jobs": "arc_jobs.cli",
            "llm": "arc_llm.cli",
            "proposer": "arc_proposer_reviewer.cli",
            "translate": "arc_translate.cli",
            "companion": "arc_companion.cli",
        }.items()
    }
    parsers = {
        "paper": modules["paper"]._parser(),
        "domain": modules["domain"]._parser(),
        "jobs": modules["jobs"]._parser(),
        "llm": modules["llm"]._build_parser(),
        "proposer": modules["proposer"]._parser(),
        "translate": modules["translate"]._parser(),
        "companion": modules["companion"]._parser(),
    }
    cases = (
        ("arc-paper.md", "arc-paper get-metadata", "paper", ["get-metadata", "arXiv:1234.5678"]),
        (
            "arc-paper.md",
            "arc-paper get-table-of-contents",
            "paper",
            ["get-table-of-contents", "--reference", "1234.5678"],
        ),
        (
            "arc-paper.md",
            "arc-paper get-section",
            "paper",
            ["get-section", "--reference", "1234.5678", "Introduction"],
        ),
        (
            "arc-paper.md",
            "arc-paper parse-local",
            "paper",
            ["parse-local", "chapter.tex", "--validator", "book.pdf"],
        ),
        (
            "arc-paper.md",
            "arc-paper search-metadata",
            "paper",
            ["search-metadata", "quasi-single", "field", "inflation"],
        ),
        (
            "arc-paper.md",
            "arc-paper get-citer-count",
            "paper",
            ["get-citer-count", "arXiv:1234.5678"],
        ),
        (
            "arc-paper.md",
            "arc-paper search-citers",
            "paper",
            [
                "search-citers",
                "arXiv:1234.5678",
                "--term",
                "specific phrase",
                "--term",
                "alternate phrase",
            ],
        ),
        (
            "arc-paper.md",
            "arc-paper search-full-text",
            "paper",
            [
                "search-full-text",
                "--term",
                "specific phrase",
                "--term",
                "alternate phrase",
            ],
        ),
        (
            "arc-paper.md",
            "arc-paper search-equations",
            "paper",
            [
                "search-equations",
                "--reference",
                "1234.5678",
                "--term",
                "2.30",
            ],
        ),
        (
            "arc-paper.md",
            "arc-paper extract-keywords",
            "paper",
            ["extract-keywords", "source.md", "--project-dir", "run/keywords"],
        ),
        (
            "arc-domain.md",
            "arc-domain build",
            "domain",
            [
                "build",
                "arXiv:1234.5678",
                "--intent",
                "scientific intent",
                "--project-dir",
                "project",
            ],
        ),
        (
            "arc-domain.md",
            "arc-domain status",
            "domain",
            ["status", "--project-dir", "project", "--run-id", "domain_run"],
        ),
        (
            "arc-jobs.md",
            "arc-jobs validate",
            "jobs",
            ["validate", "--run-root", "runs", "--run-id", "run_1"],
        ),
        (
            "arc-llm.md",
            "arc-llm generate",
            "llm",
            ["generate", "--request", "request.json", "--run-root", "runs"],
        ),
        (
            "arc-proposer-reviewer.md",
            "arc-proposer-reviewer validate",
            "proposer",
            ["validate", "--request", "batch.json"],
        ),
        (
            "arc-proposer-reviewer.md",
            "arc-proposer-reviewer show-round",
            "proposer",
            [
                "show-round",
                "--run-root",
                "runs",
                "--run-id",
                "batch_1",
                "--loop-id",
                "loop_1",
                "--round",
                "1",
            ],
        ),
        (
            "arc-translate.md",
            "arc-translate detect-language",
            "translate",
            [
                "detect-language",
                "source.md",
                "--project-dir",
                "translation",
                "--target-language",
                "zh-CN",
            ],
        ),
        (
            "arc-translate.md",
            "arc-translate translate-blocks",
            "translate",
            ["translate-blocks", "source.md", "--project-dir", "translation"],
        ),
        (
            "arc-companion.md",
            "arc-companion build",
            "companion",
            [
                "build",
                "source.md",
                "--pdf",
                "source.pdf",
                "--project-dir",
                "companion",
            ],
        ),
        (
            "arc-companion.md",
            "arc-companion render",
            "companion",
            ["render", "--project-dir", "companion"],
        ),
    )

    for manual_name, command, parser_name, argv in cases:
        manual = (SKILL / "manuals" / manual_name).read_text(encoding="utf-8")
        assert command in manual
        parsers[parser_name].parse_args(argv)


def test_arc_paper_manual_documents_general_reference_reads() -> None:
    text = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")

    for command in (
        "get-metadata",
        "get-table-of-contents",
        "get-section",
        "get-references",
        "get-citers",
    ):
        assert f"arc-paper {command}" in text
    assert "cache-first" in text
    assert "first parseable representation" in text
    assert "`--refresh` applies only to reference targets" in text
    for source_format in ("html", "markdown", "tex", "pdf"):
        assert f"`--source-format {source_format}`" in text
    assert "data.source.document" in text
    assert "data.documents[].source.document" in text
    assert "The count is returned at `data.result`" in text
    assert "arc-paper <command> --help" in text


def test_arc_paper_docs_start_with_read_loop_and_result_paths() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    section = manual.split("## Start Here: Read One Paper", 1)[1].split(
        "\n## ", 1
    )[0]
    commands = (
        "arc-paper get-metadata",
        "arc-paper get-table-of-contents",
        "arc-paper search-full-text",
        "arc-paper get-section",
        "arc-paper search-equations",
    )

    positions = [section.index(command) for command in commands]
    assert positions == sorted(positions)
    for result_path in (
        "data.title",
        "data.entries[]",
        "data.occurrences[]",
        "data.text",
        "data.matches[]",
        "data.source.document",
        "data.documents[].source.document",
    ):
        assert result_path in section


def test_arc_paper_docs_explain_portable_and_checkout_launchers() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    section = manual.split("## Run ARC Paper", 1)[1].split("\n## ", 1)[0]

    assert "arc-paper --help" in section
    assert "<skill-dir>/scripts/arc-runtime arc-paper --help" in section
    assert "packages/arc-paper/.venv/bin/arc-paper --help" in section
    assert "/arc-dev/" not in section


def test_arc_paper_docs_define_unified_full_text_search() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    section = manual.split("### Search Full Text", 1)[1].split(
        "\n## ", 1
    )[0]
    compact = " ".join(section.split())

    assert "search-full-text" in compact
    assert compact.count("--term") >= 4
    assert "specific multiword alternatives" in compact
    assert "Repeated `--term` values form literal OR" in compact
    assert "Omit targets" in compact
    assert "explicit targets" in compact
    assert "data.failures" in compact
    assert "up to 50 paper titles" in compact
    assert "rg_unavailable" in compact
    assert "refinement_required" in compact


def test_arc_paper_docs_explain_general_equation_source_diagnosis() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    compact = " ".join(manual.split())

    assert "search-equations" in manual
    assert "compare representations before diagnosing ARC" in compact
    assert "PDF-only match" in compact
    assert "selected raw representation visibly contains" in compact
    assert "smallest reproducing command" in compact
    assert '--term "=" --limit 100 --context-lines 0' in compact
    assert "nonempty numeric `source_label`" in compact
    assert "cached ar5iv HTML has no literal" not in compact


def test_arc_paper_docs_map_search_commands_to_distinct_surfaces() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    section = manual.split("## Search the Right Surface", 1)[1].split(
        "\n## ", 1
    )[0]

    for command in (
        "search-metadata",
        "search-full-text",
        "search-equations",
        "search-citers",
    ):
        assert f"`{command}`" in section


def test_arc_paper_docs_contain_no_retired_read_or_search_commands() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")

    for retired in (
        "get-arxiv-table-of-contents",
        "get-arxiv-section",
        "get-cached-table-of-contents",
        "get-cached-section",
        "search-arxiv-full-text",
        "search-arxiv-equations",
        "search-cached-full-text",
        "search-cached-document",
    ):
        assert retired not in manual


def test_arc_paper_docs_define_bounded_citation_neighborhood_search() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    section = manual.split("### Search a Citation Neighborhood", 1)[1].split(
        "\n## ", 1
    )[0]
    compact = " ".join(section.split())

    assert "search-citers" in compact
    assert compact.count("--term") == 2
    assert "--scan-limit 1000" in compact
    assert "--limit 50" in compact
    assert "case, punctuation, and hyphens" in compact
    assert "most recent and most cited" in compact
    assert "scan_complete: false" in compact
    assert "not proof of novelty" in compact


def test_arc_paper_docs_cover_external_reference_reuse() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    section = manual.split("## Reuse an External Reference", 1)[1].split(
        "\n## ", 1
    )[0]

    for command in (
        "lookup-reference",
        "acquire-reference",
        "admit-reference",
        "materialize-reference",
    ):
        assert f"arc-paper {command}" in section
    assert "cache-only lookup" in section
    assert "complete `CachedResourceRef` object" in section


def test_translate_docs_define_standalone_approximate_workflows() -> None:
    translate = (SKILL / "manuals/arc-translate.md").read_text(encoding="utf-8")
    paper = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    companion = (SKILL / "manuals/arc-companion.md").read_text(encoding="utf-8")

    for command in (
        "detect-language",
        "build-glossary",
        "translate-blocks",
        "status",
        "resume",
        "stop",
        "validate",
    ):
        assert f"arc-translate {command}" in translate
    assert "runs only its named step" in translate
    assert "--approx-term-count 50" in translate
    assert "term\ncount is approximate" in translate
    assert "matched_sentences" in translate
    assert "never\ndefinitions or explanations" in translate

    assert "arc-paper extract-keywords <source>" in paper
    assert "--approx-count 50" in paper
    assert "machine-counted occurrence frequency" in paper
    assert "never definitions" in paper

    assert "`arc-translate`" in companion
    assert (
        "translation completes before reviewed guide generation"
        in " ".join(companion.split())
    )
    assert "glossary size is approximate" in companion


def test_companion_docs_define_on_demand_unbounded_reference_research() -> None:
    manual = (SKILL / "manuals/arc-companion.md").read_text(encoding="utf-8")
    workflow = (WF / "companion.md").read_text(encoding="utf-8")
    compact_manual = " ".join(manual.split())
    compact_workflow = " ".join(workflow.split())

    assert "does not run a document-wide literature survey" in compact_manual
    assert "no minimum or maximum reference count" in compact_manual
    assert "each chapter proposer and reviewer may research" in compact_manual
    assert "currently available, authorized host research or download tools" in (
        compact_manual
    )
    assert "every chapter enters it even when the initial proposal is empty" in (
        compact_workflow
    )
    assert "Both proposer and reviewer may research during their own turns" in (
        compact_workflow
    )
    assert "requiring installation, connection, or additional authority" in (
        compact_workflow
    )
    assert "not copies of whole works" in compact_workflow


def test_companion_docs_describe_html_only_run_owned_publication() -> None:
    manual = (SKILL / "manuals/arc-companion.md").read_text(encoding="utf-8")
    workflow = (WF / "companion.md").read_text(encoding="utf-8")
    render = (SKILL / "manuals/arc-render.md").read_text(encoding="utf-8")
    combined = f"{manual}\n{workflow}"
    compact = " ".join(combined.split())

    for retired in (
        "AcceptedBook",
        "releases/",
        "<project-dir>/companion.pdf",
        "--format",
        "--reuse-translation-from",
    ):
        assert retired not in combined
    assert "run-owned `arc-render` publication" in compact
    assert "<project-dir>/.arc/companion/publications/<run-id>/" in combined
    assert "build and resume commands automatically attempt" in compact
    assert "`render` makes no model calls" in compact
    assert "validate both the run-owned publication workspace and the root standalone HTML" in compact
    assert "user-side derivative" in compact
    assert "does not validate it, reproduce it, automatically publish it, or make durability guarantees" in compact
    assert "--pdf <validator.pdf>" in manual
    assert "PDF input validator" in workflow
    assert "--html reader.html" in render
    assert "validate" in render


def test_arc_paper_quick_start_defers_cache_administration_to_help() -> None:
    manual = (SKILL / "manuals/arc-paper.md").read_text(encoding="utf-8")
    compact = " ".join(manual.split())

    assert "cache administration" in compact
    assert "arc-paper <command> --help" in manual
    assert "arc-paper cache list" not in manual
    assert "arc-paper cache remove" not in manual


def test_ideas_workflow_describes_direct_research_tools() -> None:
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(ideas.split())

    assert "ARC and web search are complementary research surfaces" in ideas
    assert "shared ARC paper cache" in compact
    assert "tool ledger" in compact
    assert "paper-operation allowlist" in compact


def test_self_reflection_allows_missing_git_metadata() -> None:
    text = (SKILL / "rules/self-reflection.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "Git: unavailable" in text
    assert "Archive:" in text
    assert "Run: <run_id>" in text
    assert "concrete gap, an actionable ARC improvement, or an incomplete requested outcome" in compact
    assert "print a visible `WARNING:`" in compact
    assert "reflection logging is not a completion gate" in compact
    assert "do not append a no-op entry" in compact


def test_interaction_rules_define_portable_selection_menu() -> None:
    text = (SKILL / "rules/interaction.md").read_text(encoding="utf-8")
    lower = text.lower()

    assert "`request_user_input`" not in text
    assert "codex" not in lower
    assert "collaboration mode" not in lower
    assert "selection/menu tool" in lower
    assert "two or three real, bounded options" in lower
    assert "end that label with" in lower
    assert "`(Recommended)`" in text


def test_interaction_rules_define_default_auto_and_explicit_interactive() -> None:
    text = (SKILL / "rules/interaction.md").read_text(encoding="utf-8")
    lower = text.lower()
    lower_flat = " ".join(lower.split())

    assert "## Automation Policy" in text
    text_flat = " ".join(text.split())
    assert "Default to `automation_level: auto`" in text_flat
    assert "do not ask an execution-mode question at startup" in text_flat
    assert "manual, step-by-step, staged review, or confirmation at key steps" in text_flat
    assert "Direct ARC tool tasks default to automatic execution" in text_flat
    assert "recommendations, research directions, scientific rankings, ARC reports" in text_flat
    assert "non-evaluative paper-data outputs" in text_flat
    assert "Direct tasks must not produce" in text_flat
    assert "recommend research directions" in text
    assert "suggest ideas step by step" in text
    assert "what is the title and abstract" in text
    assert "direct paper lookup allowed" in text
    assert "download papers that cited 0911.3380 since 2024" in text_flat
    assert "direct tool orchestration allowed" in text_flat
    assert "do not include list numbering inside option labels" in lower_flat
    assert "Run automatically (Recommended)" not in text
    assert "Confirm major steps" not in text
    assert "Discuss before running" not in text


def test_runtime_automation_steering_semantics_are_explicit() -> None:
    skill = " ".join((SKILL / "SKILL.md").read_text(encoding="utf-8").split())
    interaction = " ".join(
        (SKILL / "rules/interaction.md").read_text(encoding="utf-8").split()
    )

    assert "latest explicit steer" in interaction
    assert "persists until the managed run ends" in interaction
    assert "adds one checkpoint only" in interaction
    assert "`continue`, `resume`, or approval at one checkpoint" in interaction
    assert "does not change the automation level" in interaction
    assert "next safe, controllable boundary" in interaction
    assert "Do not stop a provider call, CLI command, or background job already submitted" in interaction
    assert "also approves that checkpoint and continues" in interaction
    assert "update that field in place after a runtime switch" in interaction
    assert "Direct ARC tool tasks do not create an extra state file" in interaction
    assert "provenance exposed by the host" not in skill
    assert "explicitly names ARC" not in interaction


def test_runtime_steering_preserves_hard_gates_and_major_milestones() -> None:
    interaction = " ".join(
        (SKILL / "rules/interaction.md").read_text(encoding="utf-8").split()
    )

    for phrase in [
        "genuine authorization or destructive action boundary",
        "available evidence and deterministic checks are exhausted",
        "remaining choice is owned by the user",
        "unresolved convention or acceptance standard",
        "`Human expert question:`",
        "Ordinary validation, recoverable errors, and verified degraded results",
        "after domain artifacts and the manifest are complete",
        "after the top three",
        "after main-agent preflight",
        "after the work note passes internal review",
        "after each accepted step or coherent chunk",
        '"stop after the first chapter"',
        "Direct tool orchestration pauses only between major",
    ]:
        assert phrase in interaction

    assert "Use this protocol for real business choices" in interaction
    assert "Do not use it to choose an automation level" in interaction
    assert "unresolved scientific ambiguity" not in interaction
    assert "error recovery, or another mandatory safety gate" not in interaction


def test_operating_rules_use_evidence_not_time_or_silence_for_stop() -> None:
    operating = " ".join(
        (SKILL / "rules/operating.md").read_text(encoding="utf-8").split()
    )
    integrity = " ".join(
        (SKILL / "rules/integrity.md").read_text(encoding="utf-8").split()
    )
    jobs = " ".join(
        (SKILL / "manuals/arc-jobs.md").read_text(encoding="utf-8").split()
    )
    proposer = " ".join(
        (SKILL / "manuals/arc-proposer-reviewer.md")
        .read_text(encoding="utf-8")
        .split()
    )

    for text in (operating, integrity, jobs, proposer):
        assert "successive public" in text or "successive snapshots" in text
        assert "recurring error" in text or "recurring failure" in text
        assert "goal-directed progress" in text or "scientific objective" in text
    assert "Always honor an explicit user stop" in operating
    assert "silence alone is not proof of a loop" in integrity
    assert "pipe activity alone are not stop conditions" in proposer
    assert "Scientific proposer-reviewer rounds remain finite and configurable" in proposer


def test_restricted_host_requests_use_manual_pause_without_universal_broker() -> None:
    llm = " ".join(
        (SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8").split()
    )
    ideas = " ".join((WF / "ideas.md").read_text(encoding="utf-8").split())
    calculate = " ".join((WF / "calculate.md").read_text(encoding="utf-8").split())

    for text in (llm, ideas, calculate):
        assert "`restricted` or `unknown`" in text
        assert "durable manual pause" in text
        assert "production universal broker" in text
    assert "generic host broker" not in ideas


def test_domain_auto_handoff_preserves_visible_nonblocking_warnings() -> None:
    domain = " ".join((WF / "domain.md").read_text(encoding="utf-8").split())

    assert "manifest and all required artifacts validate" in domain
    assert "Print nonblocking degraded warnings" in domain
    assert "do not let those warnings alone block the requested handoff" in domain
    assert "typed pause or failure, missing required artifact, or integrity failure" in domain


def test_automatic_workflows_preserve_requested_scope() -> None:
    skill = " ".join((SKILL / "SKILL.md").read_text(encoding="utf-8").split())
    interaction = " ".join(
        (SKILL / "rules/interaction.md").read_text(encoding="utf-8").split()
    )
    domain = " ".join((WF / "domain.md").read_text(encoding="utf-8").split())
    ideas = " ".join((WF / "ideas.md").read_text(encoding="utf-8").split())

    assert "Perform exactly the workflow scope requested" in skill
    assert "does not authorize a downstream workflow" in interaction
    assert "stop after domain construction" in interaction
    assert "stop after ranked ideas" in interaction
    assert "`auto` does not authorize idea generation" in domain
    assert "`auto` does not authorize a move to calculation outside that scope" in ideas
    assert "proceed with ranked idea #1 in `auto` mode without asking" in ideas
    assert "In `auto` mode, use ranked idea #1 without asking" in skill
    assert "only in `interactive` mode" in skill


def test_workflow_docs_stay_human_readable() -> None:
    for name in [
        "check.md",
        "plan.md",
        "calculate.md",
    ]:
        text = (WF / name).read_text(encoding="utf-8")
        assert len(text.splitlines()) <= 240
        assert "0_ref/" not in text
        if name != "calculate.md":
            assert "/scripts/" not in text
        else:
            assert "scripts/run-calculate.py" in text


def test_check_workflow_keeps_notes_out_of_proposer_context() -> None:
    text = (WF / "check.md").read_text(encoding="utf-8").lower()

    assert "markdown or pdf research notes" in text
    assert "full note body" in text
    assert "calculator agents" in text
    assert "claims to check" in text
    assert "blind reference check" in text
    assert "reviewer_reference_claim" in text
    assert "user-specified" in text
    assert "inferred" in text


def test_plan_requires_review_after_drafting() -> None:
    text = (WF / "plan.md").read_text(encoding="utf-8")

    assert "review the plan" in text.lower()
    assert "independent reviewer" in text.lower()
    assert "main agent" in text.lower()
    assert "<project-dir>/.arc/calculate/<run-id>/work-notes/work-note-v001.md" in text
    assert "<project-dir>/.arc/calculate/<run-id>/work-note.md" in text
    assert "`manuals/arc-jobs.md` Markdown Report Export" in text
    assert "`<project-dir>/work-note.pdf`" in text
    assert "<project-dir>/plan.md" not in text


def test_plan_requires_explicit_step_quantity_contracts() -> None:
    text = " ".join((WF / "plan.md").read_text(encoding="utf-8").lower().split())

    assert "quantity to calculate" in text
    assert "required final representation" in text
    assert "quantities it must be expressed in terms of" in text
    assert "conventions, validity regime, and approximation order" in text
    assert "completion and scientific-agreement standard" in text
    assert "not a structured target contract" in text
    assert "largest coherent chunks" in text
    assert "do not split by raw equation count" in text
    assert "at least 20 steps" not in text
    assert "do not disclose an exact expected expression" in text
    assert "derive the target quantity in terms of the named dependencies" in text
    assert "target formula" in text


def test_plan_routes_reference_equations_to_blind_checks() -> None:
    text = (WF / "plan.md").read_text(encoding="utf-8").lower()

    assert "do not include the target equation or later text" in text
    assert "blind reference check" in text
    assert "reviewer-only reference claim" in text


def test_plan_requires_equation_coverage_ledger() -> None:
    text = " ".join((WF / "plan.md").read_text(encoding="utf-8").lower().split())

    assert "equation coverage ledger" in text
    assert "every parsed equation id" in text
    assert "ready step, rough step, or skipped-with-reason" in text
    assert "steps may cover multiple equations" in text
    assert "source_anchor alone is not enough" in text


def test_check_workflow_requires_equation_coverage_handoff() -> None:
    text = " ".join((WF / "check.md").read_text(encoding="utf-8").lower().split())

    assert "equation coverage ledger" in text
    assert "parsed equation inventory" in text
    assert "equation id or equation-id range" in text
    assert "source_excerpt" in text
    assert "source tools are disabled" in text


def test_plan_workflow_writes_work_note_versions() -> None:
    plan = (WF / "plan.md").read_text(encoding="utf-8")
    plan_lower = plan.lower()

    assert "<project-dir>/.arc/calculate/<run-id>/work-note.md" in plan
    assert "<project-dir>/.arc/calculate/<run-id>/work-notes/work-note-v001.md" in plan
    assert "<project-dir>/work-note.pdf" in plan
    assert "write immutable version first" in plan_lower
    assert "mirror" in plan_lower
    assert "version" in plan_lower


def test_calculate_workflow_uses_work_note_runtime_artifacts() -> None:
    calculate = (WF / "calculate.md").read_text(encoding="utf-8")

    assert "<project-dir>/.arc/calculate/<run-id>/work-note.md" in calculate
    assert "<project-dir>/.arc/calculate/<run-id>/execute/calculate.config.json" in calculate
    assert "<project-dir>/.arc/calculate/<run-id>/execute/<calculate-run-id>/state.json" in calculate
    assert "<project-dir>/work-note.pdf" in calculate
    assert "calculate.config.template.json" in calculate
    assert "calculation-report.md" not in calculate
    assert "foundation/latest.json" not in calculate
    assert "latest-plan.md" not in calculate
    assert "note-check-triage.json" not in calculate
    assert "validate-note-check" not in calculate


def test_check_workflow_hands_off_to_work_note() -> None:
    check = (WF / "check.md").read_text(encoding="utf-8")
    check_lower = check.lower()

    assert "planning-request" in check_lower
    assert "calculation-report.md" not in check
    assert "foundation/latest.json" not in check
    assert "latest-plan.md" not in check
    assert "note-check-triage.json" not in check
    assert "validate-note-check" not in check


def test_work_note_declares_required_sections() -> None:
    text = (WF / "plan.md").read_text(encoding="utf-8")
    archive_index = text.find("<project-dir>/.arc/calculate/<run-id>/work-notes/work-note-v001.md")
    assert archive_index != -1

    expected_headings = [
        "# Work Note",
        "## Task",
        "## Physics Background And Logic Flow",
        "## Notation And Conventions",
        "## Axioms And Starting Points",
        "## Accepted Derived Results",
        "## Calculation Remarks — Not Trusted Results",
        "## Validation-Only References",
        "## Detailed Steps Ready To Calculate",
        "## Rough Steps For Later Planning",
        "## Equation Coverage Ledger",
        "## Reviewer-Only Targets",
        "## Calculation Status",
        "## Open Questions",
        "## Revision History",
        "## Journal",
        "## Source Audit Trail",
    ]
    template = text[archive_index:]
    work_note_match = re.search(r"(?m)^# Work Note$", template)
    assert work_note_match is not None

    template_body = template[work_note_match.start():]
    template_end = template_body.find("Each equation-heavy section")
    if template_end == -1:
        template_end = template_body.find("```", len("# Work Note"))
    assert template_end != -1

    headings = [
        line
        for line in template_body[:template_end].splitlines()
        if line == "# Work Note" or line.startswith("## ")
    ]
    assert headings == expected_headings


def test_work_note_requires_physics_prose_and_logic_flow() -> None:
    text = (WF / "plan.md").read_text(encoding="utf-8").lower()

    assert "physics background" in text
    assert "logic flow" in text
    assert "use f1 and f2 to derive s3" in text
    assert "not only equations" in text
    assert "at least as clear" in text
    assert "journal" in text
    assert "main text explains physics" in text
    assert "verbatim" in text


def test_plan_workflow_owns_work_note_planning_only() -> None:
    plan = (WF / "plan.md").read_text(encoding="utf-8").lower()

    assert "plan.md owns work-note structure" in plan
    assert "initial foundations" in plan
    assert "accepted-premise promotion" in plan
    assert "ready-step boundaries" in plan
    assert "rough-step planning" in plan
    assert "plan.md owns consensus execution" not in plan
    assert "refer to the owning workflow" in plan


def test_plan_workflow_orders_blocks_by_dependency_then_source_anchor() -> None:
    plan = (WF / "plan.md").read_text(encoding="utf-8").lower()

    assert "dependency/topological order" in plan
    assert "same dependency priority" in plan
    assert "earliest source anchor" in plan
    assert "source line number" in plan
    assert "accepted results" in plan
    assert "journal and revision history" in plan
    assert "chronological" in plan


def test_plan_workflow_removes_promoted_steps_from_rough_list() -> None:
    plan = (WF / "plan.md").read_text(encoding="utf-8").lower()

    assert "remove that step from" in plan
    assert "accepted," in plan
    assert "ready, or blocked detailed steps must not remain" in plan
    assert "no accepted/ready/blocked step is duplicated" in plan


def test_accepted_steps_leave_detailed_ready_section() -> None:
    calculate = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())
    plan = " ".join((WF / "plan.md").read_text(encoding="utf-8").lower().split())

    assert "only `trusted_results` go to `## accepted derived results`" in calculate
    assert "remove the accepted step block from `## detailed steps ready to calculate`" in calculate
    assert "no `status: accepted` step block may remain" in calculate
    assert "`## detailed steps ready to calculate` is the executable backlog" in plan
    assert "accepted steps must live in `## accepted derived results`" in plan
    assert "not in the ready-step section" in plan


def test_accepted_steps_keep_trace_outside_ready_section() -> None:
    calculate = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())

    assert "calculation status" in calculate
    assert "revision history" in calculate
    assert "journal" in calculate
    assert "step id" in calculate
    assert "referee action" in calculate
    assert "both calculator ids" in calculate
    assert "batch run id" in calculate
    assert "verified public ref digests" in calculate


def test_calculate_workflow_owns_execution_results_only() -> None:
    calculate = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())

    assert "calculate.md owns calculation execution" in calculate
    assert "current-step result-status" in calculate
    assert "candidate reusable result" in calculate
    assert "write a planning request" in calculate
    assert "does not change ready-step boundaries" in calculate
    assert "does not change rough steps" in calculate
    assert "does not change future plan structure" in calculate
    assert "calculate.md owns note parsing" not in calculate
    assert "refer to the owning workflow" in calculate


def test_check_workflow_owns_note_parsing_only() -> None:
    check = (WF / "check.md").read_text(encoding="utf-8").lower()

    assert "check.md owns note parsing" in check
    assert "planning handoff" in check
    assert "check.md owns work-note structure" not in check
    assert "check.md owns consensus execution" not in check
    assert "consensus behavior" not in check
    assert "refer to the owning workflow" in check


def test_calculate_documents_public_batch_and_blind_reference_contract() -> None:
    text = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())

    assert "blind reference check" in text
    assert "reviewer_reference_claim" in text
    assert "deterministic, independent public" in text
    assert "batchrequest" in text
    assert "committed round" in text
    assert "arc paper tools or web research" in text
    assert "redacts referee feedback" in text
    assert "arc_paper_access" not in text
    assert "controller arc-paper access" not in text
    assert "source or reference mismatch" in text
    assert "explicitly untrusted remark" in text
    assert "may coexist with a trusted jointly derived result" in text
    assert "new derivation after a check" in text


def test_calculate_uses_exactly_two_fresh_calculators_and_finite_retries() -> None:
    text = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())
    compact = text

    assert "exactly two calculators" in text
    assert "two fresh, independent calculators" in text
    assert "neither receives the other calculator's answer" in text
    assert "locked output" not in text
    assert "one selected proposer again" not in text
    assert "concrete new hypothesis, algorithm, or recovery path" in compact
    assert "unbounded retry loop" in text


def test_calculate_uses_reviewer_judgment_not_mandatory_sympy_gate() -> None:
    text = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())

    assert "referee owns scientific validity and semantic equivalence" in text
    assert "sympy" in text
    assert "wolfram" in text
    assert "optional" in text
    assert "mandatory a-b" not in text
    assert "all_agree" not in text
    assert "two_agree" not in text
    assert "reference_disagrees" not in text
    assert "limits and numerics can discriminate" in text
    assert "not automatic proof" in text


def test_calculate_pause_requires_explicit_human_expert_question() -> None:
    text = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())

    assert "human expert question:" in text
    assert "do not merely say that the workflow paused" in text
    assert "atomic unresolved target" in text
    assert "competing formulas or claims" in text
    assert "available evidence" in text
    assert "user-facing response" in text


def test_calculate_human_resolution_continues_until_stop_condition() -> None:
    text = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())

    assert "human expert may resolve a precise atomic question" in text
    assert "thereby unblock the workflow" in text
    assert "continue with the next ready detailed step" in text
    assert "return to `plan.md`" in text
    assert "the user explicitly asks to pause or stop" in text


def test_check_workflow_repeats_until_requested_coverage_complete() -> None:
    text = " ".join((WF / "check.md").read_text(encoding="utf-8").lower().split())

    assert "repeat steps 1 and 2" in text
    assert "requested note-check coverage is complete" in text
    assert "do not stop only because one ready step was accepted" in text
    assert "rough or pending coverage remains" in text
    assert "return to `plan.md`" in text


def test_arc_skill_case4_repeats_until_requested_calculation_complete() -> None:
    text = " ".join((SKILL / "SKILL.md").read_text(encoding="utf-8").lower().split())

    assert "before leaving case 4" in text
    assert "ready detailed step exists" in text
    assert "rough or pending coverage remains from the original calculation request" in text
    assert "return to `workflows/plan.md`" in text
    assert "requested calculation coverage is complete" in text


def test_referee_trust_policy_keeps_remarks_out_of_premises() -> None:
    calculate = " ".join((WF / "calculate.md").read_text(encoding="utf-8").lower().split())
    plan = " ".join((WF / "plan.md").read_text(encoding="utf-8").lower().split())

    assert "only a result supported by both actual calculators and trusted by the referee" in calculate
    assert "one-calculator result" in calculate
    assert "remarks never become premises" in calculate
    assert "calculation remarks — not trusted results" in calculate
    assert "trusted common part" in calculate
    assert "strictly smaller unresolved targets" in calculate
    assert "dedicated adjudication round" in calculate
    assert "calculation remarks — not trusted results" in plan
    assert "never become allowed premises or accepted prior outputs" in plan
    assert "structured target contract" in plan
    assert "exact expected expression or target formula" in plan


def test_calculate_workflow_keeps_provenance_marker_templates() -> None:
    text = (WF / "calculate.md").read_text(encoding="utf-8")

    assert r"\definecolor{arcsourceissue}{HTML}{8B0000}" in text
    assert r"\definecolor{archumanresolved}{HTML}{003F8C}" in text
    assert r"\colorbox{arcsourceissue}{\textcolor{white}{[confirmed source issue]}}" in text
    assert r"\colorbox{arcsourceissue}{\textcolor{white}{[foundation added by agent]}}" in text
    assert r"\colorbox{archumanresolved}{\textcolor{white}{[human-resolved]}}" in text
    assert "Do not use custom no-argument marker macros" in text
    assert "not runner statuses" in text
    assert "automatically pauses the workflow" in text


def test_ideas_ranking_script_uses_durable_run_identifiers() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "rank-ideas.py"), "--help"],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
    )

    assert "--run-root RUN_ROOT" in result.stdout
    assert "--run-id RUN_ID" in result.stdout


def test_ideas_marking_scheme_is_centralized() -> None:
    scheme = json.loads((WJ / "ideas-marking-scheme.json").read_text(encoding="utf-8"))
    reviewer_schema_text = (WJ / "ideas-reviewer-output.schema.json").read_text(encoding="utf-8")
    reviewer = json.loads((WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8"))

    fields = [item["field"] for item in scheme["marks"]]
    maxima = {item["field"]: item["maximum"] for item in scheme["marks"]}

    assert fields == [
        "user_intent_relevance",
        "novelty",
        "confidence_of_novelty",
        "scientific_value",
        "planning",
        "problem_well_definedness",
        "simplicity",
        "generality",
    ]
    assert maxima == {
        "user_intent_relevance": 10,
        "novelty": 15,
        "confidence_of_novelty": 15,
        "scientific_value": 15,
        "planning": 10,
        "problem_well_definedness": 15,
        "simplicity": 10,
        "generality": 10,
    }
    assert sum(maxima.values()) == scheme["total_score"]["maximum"] == 100
    assert "evidence_of_novelty" not in reviewer_schema_text
    assert "0-30 scale" not in reviewer["prompt"]["template"]
    assert "marking scheme" in reviewer["prompt"]["template"]


def test_ideas_marking_scheme_has_discriminating_score_anchors() -> None:
    scheme = json.loads((WJ / "ideas-marking-scheme.json").read_text(encoding="utf-8"))
    guidance = {item["field"]: item["guidance"] for item in scheme["marks"]}

    assert "Use the full numeric range" in scheme["calibration_guidance"]
    assert "A total score above 90 should be rare" in scheme["calibration_guidance"]
    assert "merely reasonable idea with unclear novelty or weak execution plan should usually fall around 55-75" in scheme["calibration_guidance"]
    assert "15: confidently publishable in a top journal" in guidance["novelty"]
    assert "10: marginally publishable in a top journal" in guidance["novelty"]
    assert "5: marginally publishable in a second-tier or specialized journal" in guidance["novelty"]
    assert "0: not publishable" in guidance["novelty"]
    assert "shortest minimally sufficient route" in guidance["planning"]
    assert "10: the shortest minimally sufficient set" in guidance["planning"]
    assert "4: some steps are too broad, avoidably elaborate" in guidance["planning"]
    assert "0: most steps cannot be done by an AI agent" in guidance["planning"]
    assert "shortest minimally sufficient setup" in guidance["problem_well_definedness"]
    assert "one nontrivial idea nucleus" in guidance["simplicity"]
    assert "Necessary technical controls" in guidance["simplicity"]
    assert "breadth of applicability" in guidance["generality"]
    assert "direct observability is not a separate requirement" in guidance["generality"]


def test_ideas_reviewer_comments_turn_marks_into_scientific_guidance() -> None:
    reviewer = json.loads((WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8"))
    template = reviewer["prompt"]["template"]

    assert "marking scheme" in template
    assert "technical, proposal-specific feedback" in template
    assert "score-optimization advice" in template
    assert "one coherent idea nucleus" in template
    assert "removal counterfactual" in template
    assert "shortest minimally sufficient direct setup" in template
    assert "actionable feedback" in template
    assert "top-level targeted feedback" in template
    assert "do not leave it only in reviewer_benchmark" in template


def test_all_ideas_workers_share_soft_scientific_taste_guidance() -> None:
    proposer = json.loads(
        (WJ / "ideas-proposer.template.json").read_text(encoding="utf-8")
    )["prompt"]["template"]
    reviewer = json.loads(
        (WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8")
    )["prompt"]["template"]

    assert "minimal direct" in proposer
    assert "genuinely novel and consequential result" in proposer
    assert "more physically meaningful" in proposer
    assert "simpler formulation is valuable only if it remains new" in proposer
    assert "optional, non-exhaustive lens" in proposer
    assert "removal counterfactual" in proposer
    assert "shortest minimally sufficient setup" in proposer
    assert "optional follow-on" in proposer
    assert "repairable formulation error" in proposer

    assert "scientific taste" in reviewer
    assert "how broadly its core result applies" in reviewer
    assert "optional, non-exhaustive lens" in reviewer
    assert "removal counterfactual" in reviewer
    assert "shortest minimally sufficient direct" in reviewer
    assert "top-level targeted feedback" in reviewer
    assert "actionable feedback" in reviewer


def test_all_ideas_reviewers_require_bounded_citation_neighborhood_audits() -> None:
    template = json.loads(
        (WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8")
    )["prompt"]["template"]

    assert "every new idea nucleus" in template
    assert "first reviewer round" in template
    assert "one canonical paper" in template
    assert "no more than two prior-art papers" in template
    assert "outside domain seeds" in template
    assert "do not scan every proposer citation" in template
    assert template.index("arc-paper get-citer-count") < template.index(
        "arc-paper search-citers"
    )
    assert "--scan-limit 1000 --limit 50" in template
    assert "background, mechanism, and observable" in template
    assert "Read shortlist abstracts" in template
    assert "only for suspected direct overlap" in template
    assert "Reuse completed scans in later rounds" in template
    assert "idea nucleus, baseline paper, and novelty delta" in template
    assert "total citer count, scanned count, completeness" in template
    assert "matched papers" in template
    assert "reasons for excluding matches" in template
    assert "exact ARC commands and terms" in template
    assert "no direct precedent found in this citation neighborhood" in template
    assert "never treat it as proof of novelty" in template
    assert "INSPIRE is unavailable" in template
    assert "exceeds 1000 citers" in template
    assert "abstracts are missing" in template
    assert "lower novelty confidence" in template
    assert "never let this audit remove or hide the idea" in template
    assert "supplementary evidence signal" in template
    assert "does not change scores, ranks, or visibility" in template


def test_all_ideas_reviewers_keep_broader_novelty_checks_primary() -> None:
    prompt = json.loads(
        (WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8")
    )["prompt"]
    system = prompt["system"]
    instructions = f"{system}\n\n{prompt['template']}"

    assert "required supplementary signal" in system
    assert "never a replacement for the broader novelty review" in system
    assert "web, INSPIRE metadata, shared-cache" in system
    assert "regardless of the citation outcome" in system
    assert "base novelty and confidence on the combined evidence" in system
    assert "never raise either from a no-hit citation result alone" in system
    assert "both evidence classes and their actual queries" in system
    assert "existing evidence_checked and tool_queries_used arrays" in system
    assert "If the idea actually relies on a cross-domain transfer" in system
    assert "source/target/intersection literature" in system
    assert instructions.index("broader novelty review") < instructions.index(
        "perform a citation-neighborhood audit"
    )


def test_all_ideas_routes_use_one_common_marking_scheme_without_a_route_gate() -> None:
    variant = json.loads(
        (WJ / "ideas-general.variant.json").read_text(encoding="utf-8")
    )
    reviewer = json.loads(
        (WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8")
    )
    scheme = json.loads(
        (WJ / "ideas-marking-scheme.json").read_text(encoding="utf-8")
    )

    assert variant["variant_id"] == "general"
    assert variant["marking_scheme"] == "ideas-marking-scheme.json"
    assert "Model-selected scientific routes" in variant["description"]
    assert "one common scientific scale" in reviewer["prompt"]["system"]
    assert "Domain cards and pairwise relationship classifications are advisory evidence" in reviewer[
        "prompt"
    ]["system"]
    assert "never a qualification gate" in reviewer["prompt"]["system"]
    assert "Rank ideas by total_score on one common scale" in scheme["total_score"][
        "guidance"
    ]

    proposer = json.loads(
        (WJ / "ideas-proposer.template.json").read_text(encoding="utf-8")
    )
    proposer_prompt = proposer["prompt"]["template"]
    assert "all supplied domain cards" in proposer_prompt
    assert "never an instruction to use a single-domain or cross-domain formulation" in proposer_prompt
    assert "Choose the scientifically strongest and shortest sufficient route" in proposer_prompt
    assert "These examples are non-exhaustive and carry no quota or preference" in proposer_prompt


def test_ideas_marks_score_minimal_core_not_bundled_outputs() -> None:
    scheme = json.loads((WJ / "ideas-marking-scheme.json").read_text(encoding="utf-8"))
    guidance = {item["field"]: item["guidance"] for item in scheme["marks"]}
    assert "minimal" in guidance["novelty"]
    assert "bundling" in guidance["novelty"]
    assert "minimal core result" in guidance["scientific_value"]
    assert "sum of optional outputs or extensions" in guidance["scientific_value"]


def test_ideas_workflow_uses_post_batch_advisory_not_global_reviewer_worker() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")

    assert "run-ideas.py" in text
    assert "global reviewer" not in text
    assert "global_review" not in text
    assert "template's three rounds by default" in text
    assert "RunRepository" in text
    assert "proposer-reviewer/request" in text
    assert "idea_loops" not in text
    assert "ideas_batch_config" not in text
    assert "scripts/rank-ideas.py" in text
    assert "<project-dir>/ideas/<run-id>/ideas.md" not in text
    assert "<project-dir>/ideas.md" not in text


def test_ideas_workflow_documents_direct_tool_contract() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")

    compact = " ".join(text.split())
    assert "web, ARC paper tools, and the shared ARC paper cache" in compact
    assert "paper-operation allowlist" in compact
    assert "tool ledger" in compact
    assert "does not assume a production universal broker" in compact
    assert "generic host broker" not in compact
    assert "`inspect_batch`" in text
    assert "`read_batch_trace`" in text
    assert "`read_batch_round`" in text
    assert "--run-root <project-dir>/.arc/ideas" in text
    assert "--run-id <run-id>" in text
    assert "status is `completed` or `degraded`" in text
    assert "lifecycle is `succeeded`" in text


def test_ideas_workflow_documents_citation_neighborhood_default_policy() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "one canonical paper" in compact
    assert "at most two prior-art papers" in compact
    assert "need not be domain seeds" in compact
    assert "do not scan every paper cited by the proposer" in compact
    assert compact.index("`arc-paper get-citer-count`") < compact.index(
        "`arc-paper search-citers`"
    )
    assert "`--scan-limit 1000`" in compact
    assert "`--limit 50`" in compact
    assert "background, mechanism, and observable" in compact
    assert "Reuse a completed scan in later rounds" in compact
    assert "`evidence_checked`" in compact
    assert "`tool_queries_used`" in compact
    assert "no direct precedent found in this citation neighborhood" in compact
    assert "lower novelty confidence" in compact
    assert "citation-neighborhood audit itself" in compact
    assert "changes its score or rank" in compact
    assert "hides it from the report" in compact
    assert "existing focused novelty audit" in compact
    assert "do not create a separate evidence ledger" in compact
    assert "use the citation audit to alter scores, ranks, or visibility" in compact


def test_ideas_workflow_forbids_citation_only_novelty_reviews() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "broader novelty review remains the primary assessment" in compact
    assert "required supplementary signal, never a replacement" in compact
    assert "broader web search, INSPIRE metadata search, shared-cache search" in compact
    assert "regardless of whether the citation scan is complete or finds a direct hit" in compact
    assert "base novelty and confidence on the combined evidence" in compact
    assert "no-hit citation result alone must not raise either score" in compact
    assert "source domain, target domain, and intersection" in compact
    assert "Direct ideas do not acquire that bureaucracy" in compact
    assert "same existing arrays" in compact
    assert "do not report a citation-only audit as a completed novelty review" in compact
    assert "continues with the other available novelty checks" in compact


def test_ideas_workflow_requires_context_and_runner_artifacts() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    text_flat = " ".join(text.split())

    assert "If `<project-dir>/context.json` is missing" in text
    assert "lacks `automation_level`" in text
    assert "initialize that field to `auto` in place" in text_flat
    assert "without asking an execution-mode question" in text_flat
    assert "Do not synthesize ideas manually" in text
    assert "Final ranked ideas must come from `run-ideas.py`'s public committed batch data" in text


def test_ideas_workflow_has_deterministic_ranked_report_deliverable() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "<project-dir>/.arc/ideas/reports/<run-id>/" in text
    assert "<project-dir>/ideas/<run-id>/ranked-ideas.pdf" in text
    assert "<project-dir>/ranked-ideas.pdf" in text
    assert "--mode partial" in text
    assert "<project-dir>/ideas/<run-id>/partial-ideas.pdf" in text
    assert "<project-dir>/partial-ideas.pdf" in text
    assert "non-formal and provisional" in compact
    assert "manuals/arc-jobs.md" in text
    assert "ranked_ideas.md" not in text
    assert "<project-dir>/suggested-ideas.md" not in text


def test_ideas_workflow_documents_default_portfolio_advisory() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "one post-batch portfolio-level scientific assessment by default" in compact
    assert "single advisory over the portfolio, not another scoring pass" in compact
    assert "holistic and free-topic" in compact
    assert "common core shared by several ideas is only one possible finding" in compact
    assert "unranked and novelty-unassessed" in compact
    assert "never changes proposer or reviewer marks, selected rounds, scores, rank order, or candidate visibility" in compact
    assert "unavailability, failure, or malformed output adds a `WARNING:`" in compact
    assert "does not block the deterministic ranked report" in compact
    assert "ranking helper remains read-only" in compact
    assert "does not invoke this assessment, mutate the batch, or reinterpret its results" in compact


def test_ideas_selection_keeps_fixable_candidates_visible() -> None:
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(ideas.split())
    owned_json = (
        "ideas-marking-scheme.json",
        "ideas-general.variant.json",
        "ideas-proposer.template.json",
        "ideas-reviewer.template.json",
    )

    assert "preserve route, transfer, mathematical-definition, and feasibility concerns as scientific assessment and concrete repair advice" in compact
    assert "Keep every trace-verified candidate visible" in compact
    assert "List all trace-verified candidates in formal ranking order" in compact
    assert "hard gate" not in ideas.lower()
    assert "qualification gate" not in ideas.lower()
    for name in owned_json:
        value = (WJ / name).read_text(encoding="utf-8").lower()
        assert "hard gate" not in value
    reviewer = (WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8")
    assert "never a qualification gate" in reviewer


def test_agents_treats_profiles_as_lenses_and_fixable_errors_as_feedback() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "exploration profiles as optional lenses, not assignments or coverage quotas" in compact
    assert "separate an idea's nucleus from fixable formulation errors" in compact
    assert "preserve the direction and return actionable feedback" in compact
    assert "shortest minimally sufficient setup" in compact
    assert (
        "Do not turn model-correctable scientific weaknesses into hard "
        "disqualification"
    ) in compact
    assert "Reserve hard stops for conditions under which reasoning cannot" in compact


def test_general_ideas_profiles_are_optional_lenses_for_model_selected_routes() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "`open_axes_for_new_work`" in text
    assert "`mathematical_opportunities.well_defined_problems`" in text
    assert "general theoretical-physics exploration lenses" in text
    assert "exactly one profile object per loop" in compact
    assert "never create duplicate loops that differ only by ID" in compact
    assert "optional, non-exhaustive lens" in compact
    assert "rather than an assignment, required taxonomy, or coverage quota" in compact
    assert "may leave its lens when a stronger minimal direct route lies elsewhere" in compact
    assert "apply a removal counterfactual" in compact
    assert "shortest minimally sufficient setup and core calculation" in compact
    assert "lets that proposer choose the scientific formulation for its own idea" in compact
    assert "One batch may therefore contain direct, single-domain, overlapping-domain, cross-domain" in compact


def test_ideas_reviewer_template_uses_direct_research_policy() -> None:
    reviewer = json.loads((WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8"))
    template = reviewer["prompt"]["template"]

    assert reviewer["id"] == "reviewer_001"
    assert "available web and ARC tools" in template
    assert "shared paper cache" in template
    assert "resolver" not in template.lower()


def test_ideas_reviewer_uses_hundred_point_marking_scheme() -> None:
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        config_module = importlib.import_module("_arc_workflows.ideas_config")
        templates_module = importlib.import_module(
            "_arc_workflows.ideas_worker_templates"
        )
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path.remove(str(SCRIPTS))

    variant_path = WJ / "ideas-general.variant.json"
    variant = config_module._parse_variant(
        json.loads(variant_path.read_text(encoding="utf-8")),
        path=variant_path,
    )
    assert variant is not None
    reviewer_payload = templates_module.reviewer_worker_payload(
        variant
    )
    marks = reviewer_payload["output_schema"]["properties"]["marks"]
    mark_properties = marks["properties"]
    reviewer = json.loads((WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8"))

    assert marks["required"] == [
        "user_intent_relevance",
        "novelty",
        "confidence_of_novelty",
        "scientific_value",
        "planning",
        "problem_well_definedness",
        "simplicity",
        "generality",
        "total_score",
    ]
    assert mark_properties["user_intent_relevance"]["minimum"] == 0
    assert mark_properties["user_intent_relevance"]["maximum"] == 10
    assert mark_properties["novelty"]["maximum"] == 15
    assert mark_properties["confidence_of_novelty"]["maximum"] == 15
    assert mark_properties["scientific_value"]["minimum"] == 0
    assert mark_properties["scientific_value"]["maximum"] == 15
    assert mark_properties["planning"]["minimum"] == 0
    assert mark_properties["planning"]["maximum"] == 10
    assert mark_properties["problem_well_definedness"]["minimum"] == 0
    assert mark_properties["problem_well_definedness"]["maximum"] == 15
    assert mark_properties["simplicity"]["minimum"] == 0
    assert mark_properties["simplicity"]["maximum"] == 10
    assert mark_properties["generality"]["minimum"] == 0
    assert mark_properties["generality"]["maximum"] == 10
    assert mark_properties["total_score"]["minimum"] == 0
    assert mark_properties["total_score"]["maximum"] == 100
    assert "marking scheme" in reviewer["prompt"]["template"]
    assert "confidence_of_novelty" not in reviewer["prompt"]["template"]
    assert "evidence_of_novelty" not in reviewer["prompt"]["template"]
    assert "user_intent_fit" not in reviewer["prompt"]["template"]


def test_ideas_config_template_has_no_global_reviewer() -> None:
    config = json.loads((WJ / "ideas.config.template.json").read_text(encoding="utf-8"))
    loop = json.loads((WJ / "ideas-loop.template.json").read_text(encoding="utf-8"))

    assert "reviewer" not in config
    assert "artifact_options" not in config
    assert config["loops_per_variant"] == 3
    assert loop["max_rounds"] == 3
    assert config["domain_manifest_path"] == "<project-dir>/.arc/domain/domain-manifest.json"


def test_ideas_workflow_has_no_typed_evidence_accounting() -> None:
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(ideas.split())

    assert "tool ledger" in compact
    assert "paper-operation allowlist" in compact
    assert "does not assume a production universal broker" in compact
    assert "generic host broker" not in compact
    assert "`arc.workflow.ideas.result.v5`" in ideas
    assert "`arc.ideas.selected_rounds.v8`" in ideas
    assert "`arc.ideas.partial_selected_rounds.v4`" in ideas
    assert "scientific `status` separately from `durable_lifecycle`" in compact
    assert "no `run_lifecycle` alias" in compact
    assert "focused novelty audit" in compact
    assert "explicitly non-exhaustive" in compact
    for retired in (
        "budget of 24",
        "shared budget",
        "`consumed`",
        "`exhausted`",
        "`repeated_request`",
    ):
        assert retired not in ideas


def test_domain_and_ideas_workflows_use_explicit_domain_manifest() -> None:
    domain = (WF / "domain.md").read_text(encoding="utf-8")
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")
    manual = (SKILL / "manuals/arc-domain.md").read_text(encoding="utf-8")
    compact_domain = " ".join(domain.split())
    compact_ideas = " ".join(ideas.split())

    assert "write-domain-manifest.py" in domain
    assert "arc.workflow.domain_manifest.v4" in domain
    assert "arc.workflow.domain_seed_provenance.v1" in domain
    assert "preserving every\nseed-specific domain package as its own evidence card" in domain
    assert "domain_relationships.status" in domain
    assert "LLMClient.generate" in domain
    assert "domain-relationships-llm" in domain
    assert "typed pause, failure, or stop" in compact_domain
    assert "run_json" not in domain
    assert "LLMAbortScope" not in domain
    assert "arc.workflow.domain_manifest.v4" in manual
    assert "arc.workflow.domain_seed_provenance.v1" in manual
    assert "domain_manifest_path" in ideas
    assert "Pair classifications, confidence, and warnings are scientific context, not routing instructions" in compact_ideas
    assert "one enabled `general` variant" in ideas
    assert "pre-selects a scientific route" in ideas
    assert "paper JSON pack's `domain_id` is the authoritative" in compact_domain
    assert "v5 does not carry that identity field" in compact_domain
    assert "match in both directions" in compact_domain
    assert "scans every hidden `*_paper_json_pack.json`" in compact_domain
    assert "rejects any pack with no matching domain summary" in compact_domain
    assert "`domain_records` to be a non-empty array" in compact_domain
    assert "package-owned typed domain view" in compact_domain
    assert "only the current closed v5 summary contract" in compact_domain
    assert "Unsupported summary schemas" in compact_domain
    assert "do not route, rank, merge, delete, or disqualify ideas" in compact_domain
    assert "still publishes the package-complete manifest" in compact_domain
    assert "publishes `.arc/domain/domain-manifest.json` last" in compact_domain
    assert "manifest output must remain inside the project" in compact_domain
    assert "Input/package validation and publication-integrity errors" in compact_domain
    assert "no usable manifest can be established" in compact_domain


def test_general_ideas_document_optional_interdisciplinary_discovery() -> None:
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")
    compact = " ".join(ideas.split()).lower()

    assert "cross-disciplinary transfer is entirely optional" in compact
    assert "no idea, loop, or batch is required to include one" in compact
    assert "there is no interdisciplinary quota" in compact
    assert "receives no ranking reward" in compact
    assert "judge all candidates by the same scientific criteria" in compact
    assert "otherwise record the external method as not used" in compact
    assert "arc and web search are complementary research surfaces" in compact
    assert "shared arc paper cache" in compact
    for forced_wording in (
        "at least one interdisciplinary",
        "must consider an interdisciplinary",
        "must include an interdisciplinary",
        "must propose an interdisciplinary",
    ):
        assert forced_wording not in compact


def test_retired_cross_domain_partner_selection_artifacts_are_absent() -> None:
    assert not (
        SKILL / "scripts" / "write-cross-domain-pair-manifest.py"
    ).exists()
    assert not (
        WJ / "cross-domain-partner-critic.schema.json"
    ).exists()


def test_ideas_worker_templates_default_to_high_model_tier() -> None:
    general_variant = json.loads(
        (WJ / "ideas-general.variant.json").read_text(encoding="utf-8")
    )
    reviewer = json.loads((WJ / "ideas-reviewer.template.json").read_text(encoding="utf-8"))

    assert general_variant["proposer"]["model_tier"] == "high"
    assert reviewer["model_tier"] == "high"


def test_max_model_tier_requires_an_explicit_user_request() -> None:
    skill = " ".join((SKILL / "SKILL.md").read_text(encoding="utf-8").split())
    manual = " ".join((SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8").split())

    assert "Never select the `max` model tier automatically" in skill
    assert "only when the user explicitly requests the `max` model tier" in skill
    assert "`max`" not in manual


def test_readme_defers_provider_details_to_llm_manual_and_help() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    package_readme = (ROOT / "packages/arc-llm/README.md").read_text(
        encoding="utf-8"
    )
    manual = (SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8")

    assert "## LLM providers" not in readme
    assert "arc-llm doctor --provider" not in readme
    assert "Kimi Code\nsupport is experimental" not in readme
    assert "arc-llm --help" in package_readme
    assert "arc-llm doctor --provider auto" in manual
    assert "without printing\ncredentials" in manual
    for obsolete in (
        "arc-llm doctor host",
        "arc-llm doctor provider",
        "arc-llm doctor config",
    ):
        assert obsolete not in readme
    assert "supported host-native provider" in manual
    assert "provider diagnosis" in manual
    for retired in (
        "Kimi Code CLI `>=0.28.0`",
        "`kimi login`",
        "ARC_AGENT_HOST=kimi-code",
        "ARC_KIMI_BIN",
        "ARC_KIMI_WORK_DIR",
        "ARC_KIMI_IDLE_TIMEOUT_SECONDS",
        "ARC_LLM_KIMI_LOW_MODEL",
        "kimi-code-cli is experimental",
        "all fields in its usage object are null",
    ):
        assert retired not in manual


def test_readme_preserves_arc_token_warning_and_citation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    citation_start = readme.index("## Citation\n")
    install_start = readme.index("## Install\n")
    citation = readme[citation_start:install_start]
    install = readme[install_start:readme.index("## Start with ARC\n")]

    assert "As measured using Claude + DeepSeek" in install
    assert "1M uncached input tokens" in install
    assert "0.5M output tokens" in install
    assert "in about an hour's running time" in install
    assert "Be aware of token usage and costs." in install
    assert "ARC will need permissions to run Python scripts" in install
    assert "be aware of risk to your data and system" in install
    assert "please consider citing the ARC manual" in citation
    assert "ChinaXiv:202606.00234" in citation
    assert "https://chinaxiv.org/abs/202606.00234" in citation
    assert "```bibtex" in citation
    assert "@misc{ma2026arc," in citation
    assert "archivePrefix = {ChinaXiv}" in citation
    assert "note          = {Version 1}" in citation
    assert citation_start < install_start
    assert "### Citation" not in readme


def test_ideas_full_info_template_includes_domain_and_direct_tool_context() -> None:
    variant = json.loads((WJ / "ideas-general.variant.json").read_text(encoding="utf-8"))
    loop = json.loads((WJ / "ideas-loop.template.json").read_text(encoding="utf-8"))
    proposer = json.loads((WJ / "ideas-proposer.template.json").read_text(encoding="utf-8"))

    assert variant["loop_template"] == "ideas-loop.template.json"
    assert variant["proposer_template"] == "ideas-proposer.template.json"
    assert "workspace_input" not in loop["caller_context"]
    assert loop["caller_context"]["generation_mode"] == "model_selected_route"
    assert loop["caller_context"]["domain_cards"] == []
    assert loop["caller_context"]["domain_relationships"] == {}
    prompt = proposer["prompt"]["template"]
    assert "available web and ARC tools" in prompt
    assert "shared paper cache" in prompt
    assert "runtime" not in proposer
    assert "resolver" not in prompt.lower()


def test_retired_ideas_route_variants_and_specialized_assets_are_absent() -> None:
    retired = (
        "ideas-domain.variant.json",
        "ideas-cross-domain.variant.json",
        "ideas-no-info.variant.json",
        "ideas-domain-marking-scheme.json",
        "ideas-cross-domain-marking-scheme.json",
        "ideas-domain-reviewer.template.json",
        "ideas-cross-domain-reviewer.template.json",
        "ideas-domain-reviewer-output.schema.json",
        "ideas-cross-domain-reviewer-output.schema.json",
        "ideas-cross-domain-proposer.template.json",
        "ideas-no-info-proposer.template.json",
        "ideas-cross-domain-loop.template.json",
        "ideas-no-info-loop.template.json",
    )

    assert (WJ / "ideas-general.variant.json").is_file()
    for name in retired:
        assert not (WJ / name).exists()


def test_readme_keeps_idea_capability_without_package_details() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "proposer-reviewer idea or calculation loops" in text
    assert "typed proposer-reviewer" not in text
    assert "The Skill selects package commands" not in text
    assert "release idea workflow" not in text
    assert "no-info variant" not in text


def test_ideas_workflow_documents_enabled_variants_not_file_renaming() -> None:
    text = (WF / "ideas.md").read_text(encoding="utf-8")

    assert "one enabled `general` variant" in text
    assert "pre-selects a scientific route" in text
    assert "variant_inactivated" not in text
    assert "rename" not in text.lower()


def test_ideas_proposer_templates_use_concise_scientific_policy() -> None:
    proposer = json.loads(
        (WJ / "ideas-proposer.template.json").read_text(encoding="utf-8")
    )
    template = proposer["prompt"]["template"]

    assert "marking scheme" in template
    assert "available web and ARC tools" in template
    assert "shared paper cache" in template


def test_ideas_proposer_templates_request_report_ready_math() -> None:
    proposer = json.loads(
        (WJ / "ideas-proposer.template.json").read_text(encoding="utf-8")
    )
    template = proposer["prompt"]["template"]

    assert "report-ready Markdown math" in template


def test_ideas_proposer_schemas_are_codex_strict() -> None:
    proposer = json.loads((WJ / "ideas-proposer.template.json").read_text(encoding="utf-8"))
    schema = proposer["output_schema"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "title" in schema["required"]
    assert "scientific_route" in schema["required"]
    assert "calculation_plan" in schema["required"]
    scientific_route = schema["properties"]["scientific_route"]
    assert scientific_route["type"] == "object"
    assert scientific_route["additionalProperties"] is False
    assert scientific_route["required"] == [
        "description",
        "domain_package_ids_used",
        "rationale",
    ]
    assert "enum" not in scientific_route["properties"]["description"]


def test_build_domain_report_instructions_include_mathematical_opportunities() -> None:
    text = (WF / "domain.md").read_text(encoding="utf-8")

    assert "Task Focus for Idea Generation" in text
    assert "Key Papers" in text
    assert "foundation_paper" in text
    assert "best_reference_paper" in text
    assert "Mathematical Opportunities" in text
    assert "mathematical_opportunities.well_defined_problems" in text
    assert "important" in text
    assert "feasible" in text
    assert "external_search_lead" in text
    assert "Known Solved Cases" in text
    assert "Open Axes for New Work" in text
    assert "these axes are examples" in text
    assert "not a complete" in text
    assert "discover additional axes" in text
    assert "Frequently Asked" in text
    assert "Do not render separate" in text
    assert "llm_get_summary" not in text
    assert "foundation_<foundation-safe>.md" not in text
    assert "Summarize Best-Reference Papers" not in text
    assert "paper_json_pack" in text
    assert "arc-paper" in text


def test_arc_skill_preserves_seed_domain_anchor_in_user_intent() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert "scientific domain anchors" in text
    assert "field started by arXiv" in text
    assert "Remove operational instructions" in text
    assert "user_intent" in text
    assert "seed_paper_list" in text


def test_ideas_uses_optional_domain_markdown_workspace_inputs_not_single_paper_summaries() -> None:
    variant = json.loads((WJ / "ideas-general.variant.json").read_text(encoding="utf-8"))
    loop = json.loads((WJ / "ideas-loop.template.json").read_text(encoding="utf-8"))

    assert variant["context_policy"]["domain_markdown_workspace_input_required"] is False
    assert variant["context_policy"]["include_domain_markdown_workspace_input"] is True
    assert "content" not in loop["caller_context"]
    assert "best-reference paper summaries" not in json.dumps(loop)
    assert "single-paper LLM summaries" not in json.dumps(loop)


def test_root_plugin_manifests_use_canonical_arc_skill_tree() -> None:
    codex_manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    claude_manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))

    assert codex_manifest["name"] == "arc"
    assert codex_manifest["skills"] == "./skills/"
    assert "mcpServers" not in codex_manifest
    assert "mcpServers" not in claude_manifest
    assert not (PLUGIN / ".mcp.json").exists()
    assert not (PLUGIN / "bin/arc-mcp").exists()
    assert claude_manifest["name"] == "arc"
    assert (SKILL / "SKILL.md").is_file()
    legacy_skill = ROOT / "skills/arc"
    assert not legacy_skill.exists()
    assert not legacy_skill.is_symlink()


def test_arc_runtime_and_job_docs_cover_unified_context_and_lifecycle() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    jobs = (SKILL / "manuals/arc-jobs.md").read_text(encoding="utf-8")
    llm = (SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8")

    for value in (
        "ARC_HOME",
        "runtimes/",
        "cache/arc-paper/",
    ):
        assert value in skill
    for removed in ("cache/arc-domain/", "cache/arc-llm/", "tmp/arc-llm/"):
        assert removed not in skill
    assert "migration-conflicts/" not in skill
    assert "ARC_AGENT_HOST" not in skill
    assert "migration status" not in skill
    assert "Provider selection remains owned by" in skill
    assert "arc-jobs status" in jobs
    assert "arc-jobs stop" in jobs
    assert "arc-jobs validate" in jobs
    assert "does not create or resume package work" in jobs
    assert "owning package" in jobs
    assert "background-command facility" in skill
    assert "no default runtime or inactivity timeout" in skill
    assert "explicit positive idle timeout" in skill
    assert "30 minutes" not in skill
    assert "credentials" in llm
    for retired in ("arc-jobs submit", "arc-jobs list", "arc-jobs watch", "arc-jobs result"):
        assert retired not in jobs


def test_docs_do_not_advertise_retired_job_or_companion_commands() -> None:
    active_docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            SKILL / "SKILL.md",
            SKILL / "rules/interaction.md",
            SKILL / "rules/operating.md",
            WF / "calculate.md",
            WF / "domain.md",
        )
    )
    for retired in (
        "arc-jobs submit",
        "arc-jobs list",
        "arc-jobs watch",
        "arc-jobs result",
        "--stop-after-first-chapter",
        "arc-companion package",
        "arc-companion render-web",
        "arc-companion reference-translation",
        "arc-companion regenerate-segment",
    ):
        assert retired not in active_docs

    companion = (SKILL / "manuals/arc-companion.md").read_text(encoding="utf-8")
    for command in ("build", "status", "resume", "stop", "render", "validate"):
        assert f"arc-companion {command}" in companion
    for retired in ("arc-companion package", "arc-companion render-web"):
        assert retired not in companion


def test_domain_and_ideas_docs_use_advisory_relationships_and_frozen_recency() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    domain = (WF / "domain.md").read_text(encoding="utf-8")
    manual = (SKILL / "manuals/arc-domain.md").read_text(encoding="utf-8")
    ideas = (WF / "ideas.md").read_text(encoding="utf-8")

    for text in (skill, domain):
        assert "recent_window_days" in text
        assert "as_of_date" in text
        assert "corresponding" in text and "two" in text
    assert "recent_window_days" in manual
    assert "as_of_date" in manual
    assert "arc.workflow.domain_manifest.v4" in domain
    assert "arc.workflow.domain_manifest.v4" in manual
    assert "arc.workflow.domain_seed_provenance.v1" in domain
    assert "arc.workflow.domain_seed_provenance.v1" in manual
    assert "every\npackage-level domain card" in ideas
    assert "`domain_relationships`" in ideas
    assert "scientific context, not routing instructions" in ideas
    assert "does\nnot block Ideas" in ideas
    assert "lifecycle is `succeeded`" in ideas
    assert "failed, pending, running, paused" in ideas
    for text in (skill,):
        assert "no default runtime or inactivity timeout" in text
        assert "explicit positive idle timeout" in text
    assert "no default runtime or inactivity timeout" in ideas
    assert "30 minutes" not in ideas
    assert "owning package's status command" in skill
    assert "background-command facility" in skill
    assert "streams batch, loop, round, worker, and available provider-message progress" in ideas
    assert "arc-proposer-reviewer inspect" in ideas
    assert "arc-proposer-reviewer stop" in ideas
    assert "Use stop cautiously" in ideas
    assert "stderr" in ideas
    assert "SIGINT" in ideas and "SIGTERM" in ideas
    assert "same-run resume" in skill
    assert "stop" in skill
    assert "stop" in domain
    assert "stop" in ideas


def test_domain_origin_resolution_keeps_the_seed_date_unbounded() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    workflow = (WF / "domain.md").read_text(encoding="utf-8")
    manual = (SKILL / "manuals/arc-domain.md").read_text(encoding="utf-8")
    schema = json.loads(
        (WJ / "domain-origin-selection.schema.json").read_text(encoding="utf-8")
    )
    template = json.loads(
        (WJ / "domain-origin-selection.template.json").read_text(encoding="utf-8")
    )

    assert "date-unbounded canonical origin" in " ".join(skill.split())
    assert "modifies the **citer corpus**, not\nthe origin paper" in workflow
    assert "direct citers of that origin in the requested time window" in workflow
    assert "**3–10**" in workflow
    assert "100–1000 are a soft" in workflow
    assert "confidence is below `0.80`" in workflow
    assert "to be a recorded candidate ID" in workflow
    assert "--foundation-mode fixed-seed" in workflow
    assert "--citer-selection-mode strict-window" in workflow
    assert "complete\nclosed current policy" in workflow
    assert "persists the complete resolved policy" in workflow
    assert "strict date windows" in manual

    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "arc.domain_origin_selection.v1"
    assert schema["properties"]["schema_version"]["type"] == "string"
    assert set(schema["required"]) == {
        "schema_version",
        "selected_paper_id",
        "selected_paper_title",
        "confidence",
        "reasoning",
        "candidate_assessments",
        "warnings",
    }
    assert schema["properties"]["candidate_assessments"]["minItems"] == 3
    assert "ARC-resolvable identifier" in schema["properties"]["selected_paper_id"]["description"]
    assert template["schema_version"] == "arc.domain_origin_selection.v1"
    assert set(template) == set(schema["required"])
    Draft202012Validator(schema).validate(template)


def test_arc_llm_manual_uses_only_the_current_durable_cli() -> None:
    manual = (SKILL / "manuals/arc-llm.md").read_text(encoding="utf-8")
    domain = (SKILL / "workflows/domain.md").read_text(encoding="utf-8")

    for command in (
        "arc-llm generate",
        "arc-llm resume",
        "arc-llm status",
        "arc-llm stop",
        "arc-llm doctor --provider auto",
    ):
        assert command in manual
    for obsolete in (
        "arc-llm run-text",
        "arc-llm run-json",
        "arc-paper doctor",
        "arc-llm doctor host",
        "arc-llm doctor provider",
        "arc-llm doctor config",
        "arc-llm proposers-reviewer-loop",
        "arc-llm proposers-reviewer-bench",
        "arc-llm cache-audit",
        "arc-llm circuit",
    ):
        assert obsolete not in manual
    assert "arc-proposer-reviewer inspect" not in manual
    assert "arc-llm <command> --help" in manual
    assert "arc.llm.request.v4" in manual
    assert "arc.llm.request.v4" in domain
    assert "arc.llm.request.v3" not in manual
    assert "arc.llm.request.v3" not in domain
    assert '"capabilities"' not in manual
    assert "inherit_host_config" not in manual
    assert "allowed_tools" not in manual


def test_core_skill_docs_keep_arc_cli_only() -> None:
    operating = (SKILL / "rules/operating.md").read_text(encoding="utf-8")
    codex_manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    claude_manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))

    assert "CLI-only" in operating
    assert "does not register or ship an MCP server" in operating
    assert not (SKILL / "manuals/arc-mcp.md").exists()
    assert "mcpServers" not in codex_manifest
    assert "mcpServers" not in claude_manifest
    assert not (PLUGIN / ".mcp.json").exists()


def test_arc_plugin_has_no_packaged_skill_copies() -> None:
    legacy_skill = ROOT / "skills/arc"
    assert not legacy_skill.exists()
    assert not legacy_skill.is_symlink()
    assert not (ROOT / "packaging/codex/arc/skills/arc").exists()
    assert not (ROOT / "packaging/claude/arc/skills/arc").exists()


def test_arc_skill_tree_contains_no_python_bytecode() -> None:
    bad_paths = [
        str(path.relative_to(ROOT))
        for root in [PLUGIN, ROOT / ".claude-plugin"]
        if root.exists()
        for path in root.rglob("*")
        if Path(path).name == "__pycache__" or Path(path).suffix == ".pyc"
    ]
    assert bad_paths == []


def test_arc_workflow_helpers_disable_bytecode_writes() -> None:
    scripts = PLUGIN / "skills" / "arc" / "scripts"
    package_init = (scripts / "_arc_workflows" / "__init__.py").read_text(
        encoding="utf-8"
    )

    assert "sys.dont_write_bytecode = True" in package_init
    for script in scripts.glob("*.py"):
        source = script.read_text(encoding="utf-8")
        workflow_imports = [
            index
            for statement in ("from _arc_workflows", "import _arc_workflows")
            if (index := source.find(statement)) >= 0
        ]
        if workflow_imports:
            assert 0 <= source.find("sys.dont_write_bytecode = True") < min(
                workflow_imports
            ), script


def test_generated_python_caches_are_ignored_for_release_artifacts() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in ("__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/"):
        assert pattern in text


def test_readme_limits_install_recipes_to_supported_marketplaces() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    user_text, development = text.split("## Development and release\n", 1)

    assert "codex plugin marketplace add tririver/arc --ref stable" in text
    assert "codex plugin add arc@arc" in text
    assert "/plugin marketplace add tririver/arc@stable" in text
    assert "/plugin install arc" in text
    assert "### Other coding agents" in text
    assert "Give your coding agent this repository" in text
    for contributor_detail in (
        "plugins/arc/",
        "packages/",
        "arc-runtime",
        "typed JSON",
        "`uv`",
        "`pip`",
        "Standalone Skill",
        "| Package |",
    ):
        assert contributor_detail not in user_text
    assert "Python 3.11 or newer" in development
    assert "--import-mode=importlib" in development
    assert "scripts/check-packages.sh" in development


def test_readme_preserves_agent_examples_and_human_release_flow() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    development = text.split("## Development and release\n", 1)[1]

    for example in (
        "Use ARC to summarize a paper.",
        "Use ARC to build a domain from arXiv:0911.3380 with new papers since 2024.",
        "Use ARC to develop and review ideas from the resulting domain.",
        "Use ARC to check this calculation.",
    ):
        assert example in text
    assert "### Source checkout" not in text
    assert "pip install -e" not in text
    assert "explicit human operations" in development
    assert "scripts/release-arc.sh <version>" in development
    assert "pauses before its mutating Git steps" in development
    assert "See `AGENTS.md`" in development


def test_readme_examples_use_the_ignored_run_tree_not_reference_material() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "0_ref/" not in text
    assert "below the git-ignored `local/` tree" in text


def test_interaction_reference_allows_portable_typed_fallback() -> None:
    text = (SKILL / "rules/interaction.md").read_text(encoding="utf-8").lower()

    assert "typed fallback" in text
    assert "when no selection/menu tool" in text or "if no selection/menu tool" in text
    assert "enter the exact option label" in text
    assert "cannot present the required selection ui" not in text
