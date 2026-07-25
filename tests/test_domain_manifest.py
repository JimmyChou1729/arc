from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/arc/skills/arc/scripts/write-domain-manifest.py"
SCRIPTS = SCRIPT.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("write_domain_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
        sys.dont_write_bytecode = old_dont_write_bytecode
    return module


def _write_domain(
    project: Path,
    prefix: str,
    domain_id: str,
    seed: str,
    *,
    schema_version: str = "arc.domain_summary.v4",
) -> None:
    domain = project / "domain"
    domain.mkdir(parents=True, exist_ok=True)
    context_path = project / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    records = context.setdefault("domain_records", [])
    if not any(
        isinstance(item, dict) and item.get("domain_id") == domain_id
        for item in records
    ):
        records.append({"domain_id": domain_id, "seed_paper": seed})
        context_path.write_text(json.dumps(context), encoding="utf-8")
    summary = {
        "schema_version": schema_version,
        "domain_title": f"Domain {domain_id}",
        "foundation_paper": {"paper_id": seed},
    }
    if schema_version == "arc.domain_summary.v4":
        summary["domain_id"] = domain_id
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
                "paper_count": 0,
                "papers": [],
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
    (project / "domain" / f"{prefix}_paper_json_pack.json").write_text(
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

    assert "bootstrap_arc_pythonpath()" in source
    assert "LLMClient().generate" in source
    assert "run_json" not in source
    assert "LLMAbortScope" not in source


def test_json_reader_preserves_manifest_error_contract(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "context.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(
        module.ManifestError,
        match=f"JSON root must be an object: {path}",
    ):
        module._read_object(path)


def test_manifest_uses_distinct_domain_ids_and_relative_paths(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "cross fields", "seed_paper_list": ["seed:a", "seed:b"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    _write_domain(project, "duplicate", "domain-a", "seed:a2")

    payload = module.build_domain_manifest(project)

    assert payload["schema_version"] == "arc.workflow.domain_manifest.v2"
    assert payload["package_count"] == 2
    assert payload["field_count"] == 1
    assert payload["research_scope"] == "single_domain"
    assert [item["domain_package_id"] for item in payload["domain_packages"]] == ["domain-a", "domain-b"]
    assert payload["domain_packages"][0]["summary_json_path"] == "domain/a_domain_summary.json"
    assert payload["domain_packages"][0]["seed_paper"] == "seed:a"
    assert payload["duplicates"] == [
        {
            "domain_id": "domain-a",
            "kept_summary_json_path": "domain/a_domain_summary.json",
            "duplicate_summary_json_path": "domain/duplicate_domain_summary.json",
        }
    ]


def test_manifest_preserves_requested_seed_order(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "cross fields", "seed_paper_list": ["seed:z", "seed:a"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "z", "domain-z", "seed:z")

    payload = module.build_domain_manifest(project)

    assert [item["seed_paper"] for item in payload["domain_packages"]] == ["seed:z", "seed:a"]


def test_manifest_indexes_mixed_v4_v5_summaries_without_rewriting_them(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"seed_paper_list": ["seed:a", "seed:b"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a", schema_version="arc.domain_summary.v4")
    _write_domain(project, "b", "domain-b", "seed:b", schema_version="arc.domain_summary.v5")

    payload = module.build_domain_manifest(project)

    assert [item["domain_package_id"] for item in payload["domain_packages"]] == ["domain-a", "domain-b"]
    assert json.loads((project / "domain/a_domain_summary.json").read_text())["schema_version"] == (
        "arc.domain_summary.v4"
    )
    assert json.loads((project / "domain/b_domain_summary.json").read_text())["schema_version"] == (
        "arc.domain_summary.v5"
    )
    assert "domain_id" not in json.loads(
        (project / "domain/b_domain_summary.json").read_text()
    )


def test_manifest_rejects_legacy_summary_identity_mismatch(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    summary_path = project / "domain/a_domain_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["domain_id"] = "wrong-domain"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(module.ManifestError, match="does not match paper-pack"):
        module.build_domain_manifest(project)


def test_manifest_rejects_domain_id_in_closed_v5_summary(tmp_path: Path) -> None:
    module = _load_module()
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
    summary_path = project / "domain/a_domain_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["domain_id"] = "domain-a"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(
        module.ManifestError,
        match="arc.domain_summary.v5 must not contain domain_id",
    ):
        module.build_domain_manifest(project)


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        (None, "missing required string field schema_version"),
        (
            "arc.domain_summary.v99",
            "schema_version must be arc.domain_summary.v4 or arc.domain_summary.v5",
        ),
    ],
)
def test_manifest_rejects_missing_or_unknown_summary_schema(
    tmp_path: Path,
    schema_version: str | None,
    message: str,
) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    summary_path = project / "domain/a_domain_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if schema_version is None:
        summary.pop("schema_version")
    else:
        summary["schema_version"] = schema_version
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(module.ManifestError, match=message):
        module.build_domain_manifest(project)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": "wrong"}, "schema_version must be"),
        ({"domain_id": None}, "missing required string field domain_id"),
    ],
)
def test_manifest_rejects_invalid_paper_pack_identity_contract(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    pack_path = project / "domain/a_paper_json_pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack.update(mutation)
    pack_path.write_text(json.dumps(pack), encoding="utf-8")

    with pytest.raises(module.ManifestError, match=message):
        module.build_domain_manifest(project)


def test_manifest_without_domain_records_uses_legacy_seed_fallback(
    tmp_path: Path,
) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    context_path = project / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domain_records"] = []
    context_path.write_text(json.dumps(context), encoding="utf-8")

    payload = module.build_domain_manifest(project)

    assert payload["domain_packages"][0]["domain_package_id"] == "domain-a"
    assert payload["domain_packages"][0]["seed_paper"] == "seed:a"


def test_manifest_nonempty_domain_records_must_cover_every_paper_pack(
    tmp_path: Path,
) -> None:
    module = _load_module()
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
        module.ManifestError,
        match="is missing copied paper-pack domain IDs: domain-b",
    ):
        module.build_domain_manifest(project)


def test_manifest_rejects_orphan_pack_even_when_domain_records_cover_its_id(
    tmp_path: Path,
) -> None:
    module = _load_module()
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
        module.ManifestError,
        match=(
            "copied paper pack has no matching domain summary: "
            "domain/orphan_paper_json_pack.json"
        ),
    ):
        module.build_domain_manifest(project)


def test_manifest_without_domain_records_ignores_orphan_pack(
    tmp_path: Path,
) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    context_path = project / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_orphan_pack(project, "orphan", "domain-orphan", "seed:orphan")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domain_records"] = []
    context_path.write_text(json.dumps(context), encoding="utf-8")

    payload = module.build_domain_manifest(project)

    assert [
        package["domain_package_id"] for package in payload["domain_packages"]
    ] == ["domain-a"]


def test_manifest_rejects_domain_records_without_copied_packs(
    tmp_path: Path,
) -> None:
    module = _load_module()
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
        module.ManifestError,
        match="domain IDs with no copied paper pack: domain-extra",
    ):
        module.build_domain_manifest(project)


def test_manifest_prefers_requested_seed_domain_records_over_foundation(tmp_path: Path) -> None:
    module = _load_module()
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

    payload = module.build_domain_manifest(project)

    assert payload["domain_packages"][0]["seed_paper"] == "arXiv:1234.5678"


def test_manifest_hard_separates_only_high_confidence_distinct_fields(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "bridge", "seed_paper_list": ["seed:a", "seed:b"]}))
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    pair = {"package_a": "domain-a", "package_b": "domain-b", "classification": "distinct_field",
            "confidence": 0.8, "reason": "different methods", "evidence": {"semantic": "x"}}

    payload = module.build_domain_manifest(project, grouping_result={"pairs": [pair]})

    assert payload["field_count"] == 2
    assert payload["research_scope"] == "cross_domain"
    assert all(item["field_id"].startswith("field-") for item in payload["field_groups"])


def test_manifest_low_confidence_or_failed_grouping_merges_conservatively(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(json.dumps({"user_intent": "same area"}))
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")
    pair = {"package_a": "domain-a", "package_b": "domain-b", "classification": "distinct_field",
            "confidence": 0.79, "reason": "weak", "evidence": {}}

    low = module.build_domain_manifest(project, grouping_result={"pairs": [pair]})
    failed = module.build_domain_manifest(project, grouping_result={"pairs": []})

    assert low["field_count"] == 1
    assert failed["field_count"] == 1
    assert failed["grouping_method"] == "conservative_fallback"
    assert failed["grouping_warnings"]


def test_manifest_three_package_grouping_is_deterministic_and_evidence_backed(tmp_path: Path) -> None:
    module = _load_module()
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

    first = module.build_domain_manifest(project, grouping_result={"pairs": pairs})
    second = module.build_domain_manifest(project, grouping_result={"pairs": list(reversed(pairs))})

    assert first["field_count"] == 2
    assert [item["field_id"] for item in first["field_groups"]] == [item["field_id"] for item in second["field_groups"]]
    merged = next(item for item in first["field_groups"] if len(item["domain_package_ids"]) == 2)
    assert merged["confidence"] == 0.91
    assert merged["reason"]
    assert merged["evidence"]


def test_manifest_falls_back_on_nontransitive_grouping_across_hard_distinct_pair(tmp_path: Path) -> None:
    module = _load_module()
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

    payload = module.build_domain_manifest(project, grouping_result={"pairs": pairs})

    assert payload["field_count"] == 1
    assert payload["research_scope"] == "single_domain"
    assert payload["grouping_method"] == "conservative_fallback"
    assert "non-transitive" in payload["grouping_warnings"][0]
    grouping = json.loads((project / payload["grouping_artifact"]).read_text(encoding="utf-8"))
    assert grouping["warnings"] == payload["grouping_warnings"]


def test_manifest_requires_companion_artifacts(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    (project / "domain").mkdir(parents=True)
    (project / "context.json").write_text("{}\n", encoding="utf-8")
    (project / "domain/x_domain_summary.json").write_text(
        json.dumps(
            {
                "schema_version": "arc.domain_summary.v4",
                "domain_id": "x",
                "domain_title": "X",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.ManifestError, match="required domain artifact"):
        module.build_domain_manifest(project)


def test_write_manifest_uses_injected_typed_llm_runner(tmp_path: Path) -> None:
    from arc_llm import LLMCompleted

    module = _load_module()
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

    destination = module.write_domain_manifest(project, grouping_runner=runner)

    assert destination == project / "domain" / "domain-manifest.json"
    assert len(calls) == 1
    request, run_root = calls[0]
    assert request.task_id.startswith("domain-field-grouping-")
    assert request.model.provider == "auto"
    assert request.model.tier == "medium"
    assert run_root == project / "domain" / "field-grouping-llm"
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["field_count"] == 2
    assert payload["research_scope"] == "cross_domain"


def test_write_manifest_single_package_does_not_call_llm_runner(tmp_path: Path) -> None:
    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "one field", "seed_paper_list": ["seed:a"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")

    def unexpected_runner(*_args):
        raise AssertionError("single-package grouping must not invoke an LLM")

    destination = module.write_domain_manifest(project, grouping_runner=unexpected_runner)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["field_count"] == 1
    assert payload["grouping_method"] == "llm_semantic_pair_classification"


def test_write_manifest_stops_for_typed_llm_pause(tmp_path: Path) -> None:
    from arc_jobs import ResumeReason
    from arc_llm import LLMPaused

    module = _load_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / "context.json").write_text(
        json.dumps({"user_intent": "bridge", "seed_paper_list": ["seed:a", "seed:b"]}),
        encoding="utf-8",
    )
    _write_domain(project, "a", "domain-a", "seed:a")
    _write_domain(project, "b", "domain-b", "seed:b")

    def paused_runner(*_args):
        return SimpleNamespace(
            outcome=LLMPaused(
                ResumeReason.EXTERNAL_CONDITION,
                "provider-unavailable",
            )
        )

    with pytest.raises(module.GroupingLLMRunError, match="paused"):
        module.write_domain_manifest(project, grouping_runner=paused_runner)
