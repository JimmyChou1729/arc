from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/arc/skills/arc/scripts/write-domain-manifest.py"
SCRIPTS = SCRIPT.parent
GROUPING_MODULE = (
    SCRIPTS / "_arc_workflows/domain_field_grouping.py"
)
GROUPING_SCHEMA = (
    ROOT
    / "plugins/arc/skills/arc/workflows/json"
    / "domain-field-grouping.schema.json"
)
DOMAIN_STATE = Path(".arc") / "domain"
DOMAIN_PACKAGES = DOMAIN_STATE / "packages"

sys.path.insert(0, str(SCRIPTS))
try:
    from _arc_workflows import (
        domain_field_grouping as grouping,
        domain_manifest_inputs as inputs,
        domain_manifest_publish as publish,
    )
finally:
    sys.path.remove(str(SCRIPTS))


def _plain_json(value):
    if isinstance(value, Mapping):
        return {
            key: _plain_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _write_domain(
    project: Path,
    prefix: str,
    domain_id: str,
    seed: str,
    *,
    schema_version: str = "arc.domain_summary.v5",
) -> None:
    domain = project / DOMAIN_PACKAGES
    domain.mkdir(parents=True, exist_ok=True)
    context_path = project / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    records = context.setdefault("domain_records", [])
    matching_record = next((
        item
        for item in records
        if
        isinstance(item, dict) and item.get("domain_id") == domain_id
    ), None)
    if matching_record is None:
        matching_record = {
            "domain_id": domain_id,
            "seed_paper": seed,
        }
        records.append(matching_record)
    build_seed = str(matching_record["seed_paper"])
    requested = context.setdefault("seed_paper_list", [])
    if build_seed not in requested:
        requested.append(build_seed)
    origins = context.setdefault("origin_selections", [])
    if not any(
        isinstance(item, dict)
        and item.get("domain_id") == domain_id
        for item in origins
    ):
        origins.append(
            {
                "mode": "explicit_seed",
                "domain_id": domain_id,
                "build_seed": build_seed,
                "requested_seed": build_seed,
            }
        )
    context.setdefault("domain_deduplications", [])
    context_path.write_text(json.dumps(context), encoding="utf-8")
    summary = {
        "schema_version": schema_version,
        "domain_title": f"Domain {domain_id}",
        "brief_introduction": f"Overview of {domain_id}",
        "task_focus": {
            "user_intent": "test intent",
            "research_scope": "test scope",
            "priority_rules": [],
        },
        "foundation_paper": {
            "paper_id": seed,
            "title": f"Foundation {seed}",
            "reason": "test fixture",
        },
        "best_reference_paper": {
            "paper_id": seed,
            "title": f"Reference {seed}",
            "reason": "test fixture",
        },
        "methodology": [],
        "mathematical_opportunities": {
            "well_defined_problems": []
        },
        "known_solved_cases": [],
        "open_axes_for_new_work": [],
        "warnings": [],
    }
    if schema_version == "arc.domain_summary.v4":
        summary.update(
            {
                "domain_id": domain_id,
                "summary_method": "legacy fixture",
                "created_at": "2026-07-25T00:00:00+00:00",
            }
        )
        summary.pop("mathematical_opportunities")
    (domain / f"{prefix}_domain_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )
    (domain / f"{prefix}_domain_summary.md").write_text("# Domain\n", encoding="utf-8")
    (domain / f"{prefix}_paper_json_pack.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.domain_paper_json_pack.v1",
                "domain_id": domain_id,
                "foundation_paper": seed,
                "paper_count": 1,
                "papers": [
                    {
                        "paper_id": seed,
                        "role": "foundation",
                        "metadata": {},
                        "references": [],
                        "toc": [],
                        "warnings": [],
                    }
                ],
                "warnings": [],
                "created_at": "2026-07-25T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _write_orphan_pack(
    project: Path,
    prefix: str,
    domain_id: str,
    seed: str,
) -> None:
    (project / DOMAIN_PACKAGES / f"{prefix}_paper_json_pack.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.domain_paper_json_pack.v1",
                "domain_id": domain_id,
                "foundation_paper": seed,
                "paper_count": 0,
                "papers": [],
                "warnings": [],
                "created_at": "2026-07-25T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def test_manifest_helper_uses_source_bootstrap_and_typed_llm_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    grouping_source = GROUPING_MODULE.read_text(encoding="utf-8")

    assert "bootstrap_arc_pythonpath()" in source
    assert "LLMClient().generate" in grouping_source
    assert "run_json" not in source + grouping_source
    assert "LLMAbortScope" not in source + grouping_source


def test_field_grouping_request_materialization_golden() -> None:
    packages = [
        {
            "domain_package_id": "domain-b",
            "seed_paper": "seed:b",
            "foundation_paper_ids": ["seed:b"],
            "title": "Beta",
            "overview": "Second",
            "task_focus": {"goal": "B"},
            "methodology": ["m2"],
            "paper_ids": ["p2"],
            "citation_edges": [["p2", "p1"]],
            "ignored": "x",
        },
        {
            "domain_package_id": "domain-a",
            "seed_paper": "seed:a",
            "foundation_paper_ids": ["seed:a"],
            "title": "Alpha",
            "overview": "First",
            "task_focus": {"goal": "A"},
            "methodology": ["m1"],
            "paper_ids": ["p1"],
            "citation_edges": [],
            "ignored": "y",
        },
    ]
    calls = []

    def runner(request, run_root):
        calls.append((request, run_root))
        return SimpleNamespace(outcome=None)

    with pytest.raises(
        grouping.GroupingLLMRunError,
        match="returned no typed outcome",
    ):
        grouping._llm_grouping(
            packages,
            "桥接 intent",
            run_root=Path("/tmp/run"),
            runner=runner,
        )

    assert len(calls) == 1
    request, run_root = calls[0]
    assert request.task_id == (
        "domain-field-grouping-4a42a2e27bb159f6e3b22489"
    )
    assert request.prompt == (
        "Classify every unordered package pair as same_field, "
        "distinct_field, or uncertain. Exact intent: 桥接 intent\n"
        'Packages: [{"domain_package_id": "domain-b", '
        '"seed_paper": "seed:b", "foundation_paper_ids": '
        '["seed:b"], "title": "Beta", "overview": "Second", '
        '"task_focus": {"goal": "B"}, "methodology": ["m2"], '
        '"paper_ids": ["p2"], "citation_edges": [["p2", "p1"]]}, '
        '{"domain_package_id": "domain-a", "seed_paper": '
        '"seed:a", "foundation_paper_ids": ["seed:a"], '
        '"title": "Alpha", "overview": "First", "task_focus": '
        '{"goal": "A"}, "methodology": ["m1"], "paper_ids": '
        '["p1"], "citation_edges": []}]'
    )
    expected_schema = json.loads(
        GROUPING_SCHEMA.read_text(encoding="utf-8")
    )
    canonical_schema = json.dumps(
        expected_schema,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical_schema).hexdigest() == (
        "0ec499293698bfec75e3bc8efbfa92d96e077e9e7cafc7d7b7b"
        "43d4912983ca0"
    )
    assert _plain_json(request.output.schema) == expected_schema
    assert request.model.provider == "auto"
    assert request.model.tier == "medium"
    assert run_root == Path("/tmp/run")


def test_json_reader_preserves_manifest_error_contract(tmp_path: Path) -> None:
    path = tmp_path / "context.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match=f"JSON root must be an object: {path}",
    ):
        inputs._read_object(path)


def test_manifest_uses_distinct_domain_ids_and_relative_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "cross fields", "seed_paper_list": ["seed:a", "seed:b"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    _write_domain(project, "duplicate", "domain-a", "seed:a2")

    payload = publish.build_domain_manifest(project)

    assert payload["schema_version"] == "arc.workflow.domain_manifest.v3"
    assert payload["package_count"] == 2
    assert payload["field_count"] == 1
    assert payload["research_scope"] == "single_domain"
    assert [item["domain_package_id"] for item in payload["domain_packages"]] == ["domain-a", "domain-b"]
    assert payload["domain_packages"][0]["summary_json_path"] == ".arc/domain/packages/a_domain_summary.json"
    assert payload["domain_packages"][0]["seed_paper"] == "seed:a"
    assert payload["duplicates"] == [
        {
            "domain_id": "domain-a",
            "kept_summary_json_path": ".arc/domain/packages/a_domain_summary.json",
            "duplicate_summary_json_path": ".arc/domain/packages/duplicate_domain_summary.json",
        }
    ]


def test_manifest_preserves_requested_seed_order(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "cross fields", "seed_paper_list": ["seed:z", "seed:a"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "z", "domain-z", "seed:z")

    payload = publish.build_domain_manifest(project)

    assert [item["seed_paper"] for item in payload["domain_packages"]] == ["seed:z", "seed:a"]


def test_manifest_publishes_normalized_closed_seed_provenance(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    direct_seed = "https://arxiv.org/abs/2401.00001v2"
    selected_seed = "arXiv:2402.00001v3"
    displaced_seed = "DOI:10.1000/ABC"
    (project / "context.json").write_text(
        json.dumps(
            {
                "seed_paper_list": [
                    direct_seed,
                    selected_seed,
                    displaced_seed,
                ],
                "user_intent": "trace every seed",
            }
        ),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", direct_seed)
    _write_domain(project, "b", "domain-b", selected_seed)
    context_path = project / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    selection = {
        "schema_version": "arc.domain_origin_selection.v1",
        "selected_paper_id": selected_seed,
        "selected_paper_title": "Selected origin",
        "confidence": 0.9,
        "reasoning": "The candidate evidence identifies the origin.",
        "candidate_assessments": [
            {
                "paper_id": paper_id,
                "title": f"Candidate {index}",
                "role": (
                    "canonical_origin"
                    if index == 1
                    else "precursor"
                ),
                "citation_prior": "unknown",
                "assessment": "Evidence-backed assessment.",
            }
            for index, paper_id in enumerate(
                [
                    selected_seed,
                    "arXiv:2402.00002",
                    "inspire:12345",
                ],
                start=1,
            )
        ],
        "warnings": [],
    }
    context["origin_selections"][1] = {
        "mode": "origin_selected",
        "domain_id": "domain-b",
        "build_seed": selected_seed,
        "requested_seed": selected_seed,
        "field_id": "field-b",
        "selection_run_id": "origin-b",
        "selection": selection,
    }
    context["domain_deduplications"] = [
        {
            "requested_seed": displaced_seed,
            "kept_build_seed": direct_seed,
            "domain_id": "domain-a",
        }
    ]
    context_path.write_text(json.dumps(context), encoding="utf-8")

    from arc_llm import LLMCompleted

    def grouping_runner(_request, _run_root):
        return SimpleNamespace(
            outcome=LLMCompleted(
                value={
                    "pairs": [
                        {
                            "package_a": "domain-a",
                            "package_b": "domain-b",
                            "classification": "same_field",
                            "confidence": 0.9,
                            "reason": "fixture grouping",
                            "evidence": {},
                        }
                    ]
                },
                provider="test",
                model="test-model",
                session=None,
                usage=None,
            )
        )

    destination = publish.write_domain_manifest(
        project, grouping_runner=grouping_runner
    )
    manifest = json.loads(destination.read_text(encoding="utf-8"))
    reference = manifest["seed_provenance_artifact"]
    provenance_path = project / reference["path"]
    provenance = json.loads(
        provenance_path.read_text(encoding="utf-8")
    )

    assert reference["schema_version"] == (
        "arc.workflow.domain_seed_provenance.v1"
    )
    assert reference["sha256"] == hashlib.sha256(
        json.dumps(
            provenance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert provenance["requested_seed_mappings"] == [
        {
            "requested_seed": "arXiv:2401.00001",
            "build_seed": "arXiv:2401.00001",
            "domain_id": "domain-a",
            "resolution": "explicit_seed",
        },
        {
            "requested_seed": "arXiv:2402.00001",
            "build_seed": "arXiv:2402.00001",
            "domain_id": "domain-b",
            "resolution": "origin_selected",
        },
        {
            "requested_seed": "doi:10.1000/abc",
            "build_seed": "arXiv:2401.00001",
            "domain_id": "domain-a",
            "resolution": "deduplicated",
        },
    ]
    assert {
        item["domain_id"] for item in provenance["build_origins"]
    } == {"domain-a", "domain-b"}


def test_manifest_requires_exact_origin_and_requested_seed_coverage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}", encoding="utf-8")
    _write_domain(
        project,
        "a",
        "domain-a",
        "arXiv:2401.00001",
    )
    context_path = project / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["origin_selections"] = []
    context_path.write_text(json.dumps(context), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match="origin_selections must be a non-empty array",
    ):
        publish.build_domain_manifest(project)

    context["origin_selections"] = [
        {
            "mode": "explicit_seed",
            "domain_id": "domain-a",
            "build_seed": "arXiv:2401.00001",
            "requested_seed": "arXiv:2401.00001",
        }
    ]
    context["seed_paper_list"].append("doi:10.1000/unmapped")
    context_path.write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(
        inputs.ManifestError,
        match="requested seeds must be covered exactly once",
    ):
        publish.build_domain_manifest(project)


def test_manifest_rejects_nonclosed_seed_provenance_inputs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}", encoding="utf-8")
    _write_domain(
        project,
        "a",
        "domain-a",
        "arXiv:2401.00001",
    )
    context_path = project / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["origin_selections"][0]["legacy"] = True
    context_path.write_text(json.dumps(context), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match="must contain exactly",
    ):
        publish.build_domain_manifest(project)


def test_manifest_rejects_legacy_v4_summary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"seed_paper_list": ["seed:a"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a", schema_version="arc.domain_summary.v4")

    with pytest.raises(
        inputs.ManifestError,
        match="schema_version must be arc.domain_summary.v5",
    ):
        publish.build_domain_manifest(project)


def test_manifest_rejects_domain_id_in_closed_v5_summary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    _write_domain(
        project,
        "a",
        "domain-a",
        "seed:a",
        schema_version="arc.domain_summary.v5",
    )
    summary_path = project / DOMAIN_PACKAGES / "a_domain_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["domain_id"] = "domain-a"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match="invalid domain package",
    ):
        publish.build_domain_manifest(project)


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        (None, "summary.schema_version must be a non-empty string"),
        (
            "arc.domain_summary.v99",
            "summary.schema_version must be arc.domain_summary.v5",
        ),
    ],
)
def test_manifest_rejects_missing_or_unknown_summary_schema(
    tmp_path: Path,
    schema_version: str | None,
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    summary_path = project / DOMAIN_PACKAGES / "a_domain_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if schema_version is None:
        summary.pop("schema_version")
    else:
        summary["schema_version"] = schema_version
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(inputs.ManifestError, match=message):
        publish.build_domain_manifest(project)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": "wrong"}, "paper_pack.schema_version must be"),
        ({"domain_id": None}, "paper_pack.domain_id must be a non-empty string"),
    ],
)
def test_manifest_rejects_invalid_paper_pack_identity_contract(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    pack_path = project / DOMAIN_PACKAGES / "a_paper_json_pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack.update(mutation)
    pack_path.write_text(json.dumps(pack), encoding="utf-8")

    with pytest.raises(inputs.ManifestError, match=message):
        publish.build_domain_manifest(project)


def test_manifest_requires_nonempty_domain_records(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    context_path = project / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domain_records"] = []
    context_path.write_text(json.dumps(context), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match="domain_records must be a non-empty array",
    ):
        publish.build_domain_manifest(project)


def test_manifest_nonempty_domain_records_must_cover_every_paper_pack(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    context_path = project / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domain_records"] = [
        item
        for item in context["domain_records"]
        if item["domain_id"] == "domain-a"
    ]
    context_path.write_text(json.dumps(context), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match="is missing copied paper-pack domain IDs: domain-b",
    ):
        publish.build_domain_manifest(project)


def test_manifest_rejects_orphan_pack_even_when_domain_records_cover_its_id(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    context_path = project / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_orphan_pack(project, "orphan", "domain-orphan", "seed:orphan")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domain_records"].append(
        {"domain_id": "domain-orphan", "seed_paper": "seed:orphan"}
    )
    context_path.write_text(json.dumps(context), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match=(
            "copied paper pack has no matching domain summary: "
            ".arc/domain/packages/orphan_paper_json_pack.json"
        ),
    ):
        publish.build_domain_manifest(project)


def test_manifest_rejects_domain_records_without_copied_packs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    context_path = project / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domain_records"].append(
        {"domain_id": "domain-extra", "seed_paper": "seed:extra"}
    )
    context_path.write_text(json.dumps(context), encoding="utf-8")

    with pytest.raises(
        inputs.ManifestError,
        match="domain IDs with no copied paper pack: domain-extra",
    ):
        publish.build_domain_manifest(project)


def test_manifest_prefers_requested_seed_domain_records_over_foundation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps(
            {
                "seed_paper_list": ["arXiv:1234.5678"],
                "domain_records": [
                    {"domain_id": "domain-a", "seed_paper": "arXiv:1234.5678"}
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "arXiv:9999.0001")

    payload = publish.build_domain_manifest(project)

    assert payload["domain_packages"][0]["seed_paper"] == "arXiv:1234.5678"


def test_manifest_hard_separates_only_high_confidence_distinct_fields(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "bridge", "seed_paper_list": ["seed:a", "seed:b"]}))
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    pair = {"package_a": "domain-a", "package_b": "domain-b", "classification": "distinct_field",
            "confidence": 0.8, "reason": "different methods", "evidence": {"semantic": "x"}}

    payload = publish.build_domain_manifest(project, grouping_result={"pairs": [pair]})

    assert payload["field_count"] == 2
    assert payload["research_scope"] == "cross_domain"
    assert all(item["field_id"].startswith("field-") for item in payload["field_groups"])


def test_manifest_low_confidence_or_failed_grouping_merges_conservatively(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "same area"}))
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    pair = {"package_a": "domain-a", "package_b": "domain-b", "classification": "distinct_field",
            "confidence": 0.79, "reason": "weak", "evidence": {}}

    low = publish.build_domain_manifest(project, grouping_result={"pairs": [pair]})
    failed = publish.build_domain_manifest(project, grouping_result={"pairs": []})

    assert low["field_count"] == 1
    assert failed["field_count"] == 1
    assert failed["grouping_method"] == "conservative_fallback"
    assert failed["grouping_warnings"]


def test_field_grouping_ignores_non_object_mathematical_opportunities() -> None:
    package = {
        "domain_package_id": "domain-a",
        "seed_paper": "seed:a",
        "title": "Alpha",
        "overview": "Overview",
        "task_focus": {},
        "methodology": [],
        "known_solved_cases": [],
        "open_axes_for_new_work": [],
        "mathematical_opportunities": None,
        "summary_schema_version": "arc.domain_summary.v5",
        "summary_json_path": ".arc/domain/packages/a_domain_summary.json",
        "summary_markdown_path": ".arc/domain/packages/a_domain_summary.md",
        "paper_json_pack_path": ".arc/domain/packages/a_paper_json_pack.json",
        "paper_ids": ["seed:a"],
        "citation_edges": [],
    }

    groups = grouping._build_field_groups(
        [package],
        [],
        intent="",
        force_single=False,
    )

    assert groups[0]["field_card"]["mathematical_opportunities"] == {
        "well_defined_problems": []
    }


def test_manifest_three_package_grouping_is_deterministic_and_evidence_backed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "bridge"}), encoding="utf-8")
    for suffix in ("a", "b", "c"):
        _write_domain(project, suffix, f"domain-{suffix}", f"seed:{suffix}")
    pairs = [
        {"package_a": "domain-a", "package_b": "domain-b", "classification": "same_field", "confidence": 0.91, "reason": "same methods", "evidence": {"semantic": "shared"}},
        {"package_a": "domain-a", "package_b": "domain-c", "classification": "distinct_field", "confidence": 0.94, "reason": "different objects", "evidence": {"semantic": "distinct"}},
        {"package_a": "domain-b", "package_b": "domain-c", "classification": "distinct_field", "confidence": 0.88, "reason": "different objects", "evidence": {"semantic": "distinct"}},
    ]

    first = publish.build_domain_manifest(project, grouping_result={"pairs": pairs})
    second = publish.build_domain_manifest(project, grouping_result={"pairs": list(reversed(pairs))})

    assert first["field_count"] == 2
    assert [item["field_id"] for item in first["field_groups"]] == [item["field_id"] for item in second["field_groups"]]
    merged = next(item for item in first["field_groups"] if len(item["domain_package_ids"]) == 2)
    assert merged["confidence"] == 0.91
    assert merged["reason"]
    assert merged["evidence"]


def test_manifest_falls_back_on_nontransitive_grouping_across_hard_distinct_pair(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "bridge"}), encoding="utf-8")
    for suffix in ("a", "b", "c"):
        _write_domain(project, suffix, f"domain-{suffix}", f"seed:{suffix}")
    pairs = [
        {"package_a": "domain-a", "package_b": "domain-b", "classification": "same_field", "confidence": 0.9, "reason": "same", "evidence": {}},
        {"package_a": "domain-b", "package_b": "domain-c", "classification": "uncertain", "confidence": 0.7, "reason": "uncertain", "evidence": {}},
        {"package_a": "domain-a", "package_b": "domain-c", "classification": "distinct_field", "confidence": 0.95, "reason": "hard split", "evidence": {}},
    ]

    payload = publish.build_domain_manifest(project, grouping_result={"pairs": pairs})

    assert payload["field_count"] == 1
    assert payload["research_scope"] == "single_domain"
    assert payload["grouping_method"] == "conservative_fallback"
    assert "non-transitive" in payload["grouping_warnings"][0]
    assert not (project / payload["grouping_artifact"]).exists()
    assert not (project / DOMAIN_STATE / "domain-manifest.json").exists()


def test_manifest_requires_companion_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / DOMAIN_PACKAGES).mkdir(parents=True)
    (project / "context.json").write_text(
        json.dumps(
            {
                "domain_records": [
                    {"domain_id": "x", "seed_paper": "seed:x"}
                ]
            }
        ),
        encoding="utf-8",
    )
    (project / DOMAIN_PACKAGES / "x_domain_summary.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.domain_summary.v4",
                "domain_id": "x",
                "domain_title": "X",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(inputs.ManifestError, match="required domain artifact"):
        publish.build_domain_manifest(project)


def test_write_manifest_uses_injected_typed_llm_runner(tmp_path: Path) -> None:
    from arc_llm import LLMCompleted

    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "bridge", "seed_paper_list": ["seed:a", "seed:b"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    calls = []

    def runner(request, run_root):
        calls.append((request, run_root))
        return SimpleNamespace(
            outcome=LLMCompleted(
                value={
                    "pairs": [
                        {
                            "package_a": "domain-a",
                            "package_b": "domain-b",
                            "classification": "distinct_field",
                            "confidence": 0.9,
                            "reason": "different objects",
                            "evidence": {
                                "semantic": "different",
                                "paper_overlap": "none",
                                "citation_overlap": "none",
                            },
                        }
                    ]
                },
                provider="test",
                model="test-model",
                session=None,
                usage=None,
            )
        )

    destination = publish.write_domain_manifest(project, grouping_runner=runner)

    assert destination == project / DOMAIN_STATE / "domain-manifest.json"
    assert len(calls) == 1
    request, run_root = calls[0]
    assert request.task_id.startswith("domain-field-grouping-")
    assert request.model.provider == "auto"
    assert request.model.tier == "medium"
    assert run_root == project / DOMAIN_STATE / "field-grouping-llm"
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["field_count"] == 2
    assert payload["research_scope"] == "cross_domain"


def test_write_manifest_single_package_does_not_call_llm_runner(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field", "seed_paper_list": ["seed:a"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")

    def unexpected_runner(*_args):
        raise AssertionError("single-package grouping must not invoke an LLM")

    destination = publish.write_domain_manifest(project, grouping_runner=unexpected_runner)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["field_count"] == 1
    assert payload["grouping_method"] == "llm_semantic_pair_classification"


def test_write_manifest_holds_lease_and_publishes_manifest_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    events: list[tuple[str, object]] = []

    class RecordingLease:
        def __init__(self, path: Path):
            events.append(("lease-created", path))

        def acquire(self, *, blocking: bool = False):
            events.append(("lease-acquired", blocking))
            return self

        def release(self) -> None:
            events.append(("lease-released", None))

    real_writer = publish.write_json_object

    def recording_writer(path, payload, **kwargs):
        events.append(("write", Path(path)))
        return real_writer(path, payload, **kwargs)

    monkeypatch.setattr(publish, "FileLease", RecordingLease)
    monkeypatch.setattr(publish, "write_json_object", recording_writer)

    destination = publish.write_domain_manifest(project)
    manifest = json.loads(destination.read_text(encoding="utf-8"))
    grouping_path = project / manifest["grouping_artifact"]
    provenance_path = (
        project
        / manifest["seed_provenance_artifact"]["path"]
    )

    assert events == [
        (
            "lease-created",
            project / DOMAIN_STATE / ".domain-manifest.lock",
        ),
        ("lease-acquired", True),
        ("write", provenance_path),
        ("write", grouping_path),
        ("write", destination),
        ("lease-released", None),
    ]

    grouping_bytes = grouping_path.read_bytes()
    events.clear()
    second_destination = publish.write_domain_manifest(project)

    assert second_destination == destination
    assert grouping_path.read_bytes() == grouping_bytes
    assert events == [
        (
            "lease-created",
            project / DOMAIN_STATE / ".domain-manifest.lock",
        ),
        ("lease-acquired", True),
        ("write", destination),
        ("lease-released", None),
    ]


@pytest.mark.parametrize(
    "relative_path",
    [
        "context.json",
        ".arc/domain/packages/a_domain_summary.json",
        ".arc/domain/packages/a_domain_summary.md",
        ".arc/domain/packages/a_paper_json_pack.json",
    ],
)
def test_write_manifest_refuses_to_overwrite_referenced_inputs(
    tmp_path: Path,
    relative_path: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    protected_path = project / relative_path
    original = protected_path.read_bytes()
    preview = publish.build_domain_manifest(project)
    grouping_path = project / preview["grouping_artifact"]

    with pytest.raises(
        inputs.ManifestError,
        match="must not overwrite a referenced input artifact",
    ):
        publish.write_domain_manifest(
            project,
            output=protected_path,
        )

    assert protected_path.read_bytes() == original
    assert not grouping_path.exists()


def test_write_manifest_refuses_grouping_path_as_output(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    preview = publish.build_domain_manifest(project)
    grouping_path = project / preview["grouping_artifact"]

    with pytest.raises(
        inputs.ManifestError,
        match="must not be the immutable grouping artifact",
    ):
        publish.write_domain_manifest(
            project,
            output=grouping_path,
        )

    assert not grouping_path.exists()


def test_write_manifest_refuses_output_outside_project(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    preview = publish.build_domain_manifest(project)
    grouping_path = project / preview["grouping_artifact"]
    outside = tmp_path / "outside-manifest.json"

    with pytest.raises(
        inputs.ManifestError,
        match="must be inside the project directory",
    ):
        publish.write_domain_manifest(
            project,
            output=outside,
        )

    assert not outside.exists()
    assert not grouping_path.exists()


def test_custom_manifest_output_keeps_project_relative_provenance(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "arXiv:2401.00001")
    output = project / "handoffs/nested/current-domain.json"

    destination = publish.write_domain_manifest(
        project,
        output=output,
    )
    manifest = json.loads(destination.read_text(encoding="utf-8"))
    reference = manifest["seed_provenance_artifact"]

    assert destination == output
    assert reference["path"].startswith(
        ".arc/domain/seed-provenance/"
    )
    assert (project / reference["path"]).is_file()


def test_write_manifest_stops_for_incomplete_typed_llm_outcomes(
    tmp_path: Path,
) -> None:
    from arc_jobs import ResumeReason
    from arc_llm import (
        InvalidRequestError,
        LLMFailed,
        LLMPaused,
        LLMStopped,
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "bridge", "seed_paper_list": ["seed:a", "seed:b"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    old_manifest = project / DOMAIN_STATE / "domain-manifest.json"
    old_grouping = project / DOMAIN_STATE / "field-grouping.json"
    old_manifest.parent.mkdir(parents=True, exist_ok=True)
    old_manifest.write_bytes(b'{"old":"manifest"}\n')
    old_grouping.write_bytes(b'{"old":"grouping"}\n')

    outcomes = [
        (
            LLMPaused(
                ResumeReason.EXTERNAL_CONDITION,
                "provider-unavailable",
            ),
            "paused",
        ),
        (
            LLMFailed(InvalidRequestError("invalid grouping request")),
            "failed",
        ),
        (LLMStopped(), "stopped"),
    ]
    for outcome, message in outcomes:
        def incomplete_runner(*_args, outcome=outcome):
            return SimpleNamespace(outcome=outcome)

        with pytest.raises(
            grouping.GroupingLLMRunError,
            match=message,
        ):
            publish.write_domain_manifest(
                project,
                grouping_runner=incomplete_runner,
            )

    assert old_manifest.read_bytes() == b'{"old":"manifest"}\n'
    assert old_grouping.read_bytes() == b'{"old":"grouping"}\n'
    assert not (project / DOMAIN_STATE / "field-groupings").exists()


def test_write_manifest_runner_exception_publishes_nothing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "bridge"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    old_manifest = project / DOMAIN_STATE / "domain-manifest.json"
    old_grouping = project / DOMAIN_STATE / "field-grouping.json"
    old_manifest.parent.mkdir(parents=True, exist_ok=True)
    old_manifest.write_bytes(b'{"old":"manifest"}\n')
    old_grouping.write_bytes(b'{"old":"grouping"}\n')

    def failed_runner(*_args):
        raise RuntimeError("runner failed before typed completion")

    with pytest.raises(RuntimeError, match="runner failed"):
        publish.write_domain_manifest(
            project,
            grouping_runner=failed_runner,
        )

    assert old_manifest.read_bytes() == b'{"old":"manifest"}\n'
    assert old_grouping.read_bytes() == b'{"old":"grouping"}\n'
    assert not (project / DOMAIN_STATE / "field-groupings").exists()


def test_immutable_grouping_conflict_preserves_existing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    preview = publish.build_domain_manifest(project)
    grouping_path = project / preview["grouping_artifact"]
    provenance_path = (
        project / preview["seed_provenance_artifact"]["path"]
    )
    grouping_path.parent.mkdir(parents=True)
    grouping_path.write_text('{"conflict":true}\n', encoding="utf-8")
    manifest_path = project / DOMAIN_STATE / "domain-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(b'{"old":"manifest"}\n')
    writes: list[Path] = []
    real_writer = publish.write_json_object

    def recording_writer(path, payload, **kwargs):
        writes.append(Path(path))
        return real_writer(path, payload, **kwargs)

    monkeypatch.setattr(
        publish, "write_json_object", recording_writer
    )

    with pytest.raises(
        inputs.ManifestError,
        match="immutable field grouping conflicts",
    ):
        publish.write_domain_manifest(project)

    assert manifest_path.read_bytes() == b'{"old":"manifest"}\n'
    assert grouping_path.read_text(encoding="utf-8") == (
        '{"conflict":true}\n'
    )
    assert not provenance_path.exists()
    assert writes == []


def test_manifest_publication_failure_preserves_existing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field"}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    preview = publish.build_domain_manifest(project)
    grouping_path = project / preview["grouping_artifact"]
    manifest_path = project / DOMAIN_STATE / "domain-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(b'{"old":"manifest"}\n')
    real_writer = publish.write_json_object

    def failing_manifest_writer(path, payload, **kwargs):
        if Path(path) == manifest_path:
            raise OSError("manifest write failed")
        return real_writer(path, payload, **kwargs)

    monkeypatch.setattr(
        publish,
        "write_json_object",
        failing_manifest_writer,
    )

    with pytest.raises(OSError, match="manifest write failed"):
        publish.write_domain_manifest(project)

    assert manifest_path.read_bytes() == b'{"old":"manifest"}\n'
    assert grouping_path.is_file()
