from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from arc_jobs import RunRepository, RunSpec

from arc_paper import (
    DEFAULT_EXCLUDED_EFFECTS,
    JsonOutputCodec,
    OPERATION_REGISTRY,
    OperationEffect,
    dispatch_operation,
    registry_document,
    resolve_operations,
)
from arc_paper.cli import main
from arc_paper.registry import OperationRequestError


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src" / "arc_paper"


def test_registry_has_one_typed_spec_per_operation_and_safe_default_projection() -> None:
    all_specs = {spec.operation_id: spec for spec in OPERATION_REGISTRY.values()}
    safe = resolve_operations()

    assert len(all_specs) >= 10
    assert all(spec.operation_id.endswith(f".v{spec.version}") for spec in all_specs.values())
    assert all(spec.input_codec.schema_id for spec in all_specs.values())
    assert all(spec.output_codec.schema_id for spec in all_specs.values())
    assert all(spec.output_codec.schema for spec in all_specs.values())
    assert all(
        not spec.effect_flags.intersection(DEFAULT_EXCLUDED_EFFECTS)
        for spec in safe
    )
    assert "import-source" not in {spec.name for spec in safe}
    assert (
        OperationEffect.ARBITRARY_LOCAL_PATH
        in OPERATION_REGISTRY["import-source"].effect_flags
    )
    assert {
        "arc-paper.get-table-of-contents.v1",
        "arc-paper.get-section.v1",
        "arc-paper.search-full-text.v1",
        "arc-paper.search-equations.v1",
        "arc-paper.fetch-arxiv-auto.v2",
        "arc-paper.search-citers.v1",
    } <= set(OPERATION_REGISTRY)
    assert OPERATION_REGISTRY["fetch-arxiv-auto"].version == 2
    assert OPERATION_REGISTRY["parse-local"].version == 2
    export = OPERATION_REGISTRY["export-rich-document"]
    assert export.operation_id == "arc-paper.export-rich-document.v1"
    assert export.effect_flags == {
        OperationEffect.CACHE_WRITE,
        OperationEffect.ARBITRARY_LOCAL_PATH,
    }
    assert registry_document()["schema_version"] == "arc.paper.operation_registry.v1"


def test_search_citers_registry_contract_has_bounded_network_cache_effects() -> None:
    spec = OPERATION_REGISTRY["search-citers"]
    properties = spec.input_codec.schema["properties"]

    assert spec.operation_id == "arc-paper.search-citers.v1"
    assert spec.input_codec.schema["required"] == ["paper_id", "terms"]
    assert properties["terms"]["minItems"] == 1
    assert properties["scan_limit"]["maximum"] == 1000
    assert properties["limit"]["maximum"] == 50
    assert spec.effect_flags == {
        OperationEffect.NETWORK,
        OperationEffect.CACHE_WRITE,
    }
    assert set(spec.output_codec.schema["required"]) == {
        "paper_id",
        "total_citer_count",
        "scanned_count",
        "scan_complete",
        "scan_strategy",
        "terms",
        "matched_count",
        "returned_count",
        "matches_truncated",
        "matches",
        "control_sample",
    }


def test_acquire_reference_cli_omits_unsupported_title_parameter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed = []

    def dispatch(name, values):
        observed.append((name, values))
        return {}

    monkeypatch.setattr("arc_paper.cli.dispatch_operation", dispatch)
    assert main(["acquire-reference", "--arxiv-id", "0911.3380"]) == 0
    capsys.readouterr()
    assert observed == [
        (
            "acquire-reference",
            {
                "doi": None,
                "arxiv_id": "0911.3380",
                "url": None,
                "cache_root": None,
                "refresh": False,
            },
        )
    ]


def test_registry_dispatch_is_strict_and_python_values_are_typed() -> None:
    assert dispatch_operation(
        "extract-paper-ids", {"text": "See arXiv:0911.3380."}
    ) == ["arXiv:0911.3380"]
    with pytest.raises(OperationRequestError, match="Additional properties"):
        dispatch_operation(
            "extract-paper-ids",
            {"text": "0911.3380", "future_optional": None},
        )
    with pytest.raises(OperationRequestError, match="required"):
        dispatch_operation("extract-paper-ids", {})
    with pytest.raises(OperationRequestError) as error:
        dispatch_operation("removed-worker-call", {})
    assert error.value.code == "operation_not_found"


def test_registry_output_codec_validates_encoded_values() -> None:
    codec = JsonOutputCodec(
        "arc.paper.test.result.v1",
        {"type": "string"},
        lambda value: value,
    )

    assert codec.encode("valid") == "valid"
    with pytest.raises(OperationRequestError) as error:
        codec.encode({"not": "a string"})
    assert error.value.code == "invalid_result"


@pytest.mark.parametrize(
    ("argv", "expected_status", "exit_code"),
    [
        (["extract-paper-ids", "See", "0911.3380"], "completed", 0),
        (["safe-dir-name", "0911.3380", "hep-th/0601001"], "completed", 0),
        (["unknown"], "failed", 2),
    ],
)
def test_cli_stdout_is_exactly_one_command_result(
    argv: list[str],
    expected_status: str,
    exit_code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv) == exit_code
    captured = capsys.readouterr()
    lines = captured.out.splitlines()

    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["schema_version"] == "arc.command_result.v2"
    assert value["status"] == expected_status


@pytest.mark.parametrize(
    ("argv", "operation", "parameters"),
    [
        (
            ["get-title", "0911.3380", "--refresh"],
            "get-title",
            {"paper_id": "0911.3380", "refresh": True},
        ),
        (
            ["get-metadata", "0911.3380"],
            "get-metadata",
            {"paper_id": "0911.3380", "refresh": False},
        ),
        (
            ["get-references", "0911.3380", "--enrich"],
            "get-references",
            {"paper_id": "0911.3380", "refresh": False, "enrich": True},
        ),
        (
            ["get-citers", "0911.3380", "--limit", "3", "--sort", "mostcited"],
            "get-citers",
            {
                "paper_id": "0911.3380",
                "refresh": False,
                "limit": 3,
                "sort": "mostcited",
            },
        ),
        (
            [
                "search-citers",
                "1503.08043",
                "--term",
                "ultra slow roll",
                "--term",
                "non-attractor",
                "--scan-limit",
                "800",
                "--limit",
                "40",
                "--refresh",
            ],
            "search-citers",
            {
                "paper_id": "1503.08043",
                "terms": ["ultra slow roll", "non-attractor"],
                "refresh": True,
                "scan_limit": 800,
                "limit": 40,
            },
        ),
        (
            ["search-metadata", "specific", "mechanism", "--limit", "7"],
            "search-metadata",
            {"query": "specific mechanism", "limit": 7},
        ),
        (
            [
                "search-full-text",
                "--term",
                "heavy field",
                "--term",
                "massive exchange",
                "--limit",
                "100",
                "--context-lines",
                "2",
                "--case-sensitive",
            ],
            "search-full-text",
            {
                "targets": [],
                "terms": ["heavy field", "massive exchange"],
                "source_format": None,
                "refresh": False,
                "limit": 100,
                "context_lines": 2,
                "case_sensitive": True,
                "cache_root": None,
            },
        ),
        (
            ["get-table-of-contents", "--reference", "0911.3380", "--refresh"],
            "get-table-of-contents",
            {
                "target": {"kind": "reference", "reference": "0911.3380"},
                "structure": None,
                "source_format": None,
                "refresh": True,
                "cache_root": None,
            },
        ),
        (
            ["get-section", "--reference", "0911.3380", "Introduction"],
            "get-section",
            {
                "target": {"kind": "reference", "reference": "0911.3380"},
                "structure": None,
                "selector": "Introduction",
                "source_format": None,
                "refresh": False,
                "cache_root": None,
            },
        ),
        (
            ["get-section", "--reference", "0911.3380", "0"],
            "get-section",
            {
                "target": {"kind": "reference", "reference": "0911.3380"},
                "structure": None,
                "selector": "0",
                "source_format": None,
                "refresh": False,
                "cache_root": None,
            },
        ),
        (
            ["get-section", "--reference", "0911.3380", "--ordinal", "0"],
            "get-section",
            {
                "target": {"kind": "reference", "reference": "0911.3380"},
                "structure": None,
                "selector": 0,
                "source_format": None,
                "refresh": False,
                "cache_root": None,
            },
        ),
        (
            [
                "search-full-text",
                "--reference",
                "0911.3380",
                "--term",
                "Hamiltonian constraint",
                "--limit",
                "7",
                "--context-lines",
                "2",
                "--case-sensitive",
            ],
            "search-full-text",
            {
                "targets": [{"kind": "reference", "reference": "0911.3380"}],
                "terms": ["Hamiltonian constraint"],
                "source_format": None,
                "refresh": False,
                "limit": 7,
                "context_lines": 2,
                "case_sensitive": True,
                "cache_root": None,
            },
        ),
        (
            ["search-equations", "--reference", "0911.3380", "--term", "H^2", "--limit", "3"],
            "search-equations",
            {
                "targets": [{"kind": "reference", "reference": "0911.3380"}],
                "terms": ["H^2"],
                "source_format": None,
                "refresh": False,
                "limit": 3,
                "context_lines": 8,
                "case_sensitive": False,
                "cache_root": None,
            },
        ),
        (
            ["fetch-arxiv-pdf", "hep-th/0601001", "--cache-root", "/cache"],
            "fetch-arxiv-pdf",
            {
                "paper_id": "hep-th/0601001",
                "refresh": False,
                "cache_root": "/cache",
            },
        ),
        (
            [
                "parse-local",
                "paper.tex",
                "--format",
                "tex",
                "--validator",
                "paper.pdf",
                "--validator-format",
                "pdf",
                "--policy",
                "deterministic_only",
            ],
            "parse-local",
            {
                "primary_path": "paper.tex",
                "validator_paths": ["paper.pdf"],
                "validator_formats": ["pdf"],
                "primary_format": "tex",
                "policy": "deterministic_only",
                "cache_root": None,
            },
        ),
        (
            ["parse-local", "paper.html", "--format", "html"],
            "parse-local",
            {
                "primary_path": "paper.html",
                "validator_paths": [],
                "validator_formats": [],
                "primary_format": "html",
                "policy": None,
                "cache_root": None,
            },
        ),
        (
            [
                "export-rich-document",
                "paper.md",
                "--output-dir",
                "handoff",
                "--validator",
                "paper.pdf",
                "--format",
                "markdown",
                "--cache-root",
                "/cache",
            ],
            "export-rich-document",
            {
                "source": "paper.md",
                "output_dir": "handoff",
                "validator": "paper.pdf",
                "source_format": "markdown",
                "cache_root": "/cache",
            },
        ),
        (
            [
                "parse-local",
                "paper.md",
                "--format",
                "markdown",
                "--validator",
                "paper.pdf",
                "--validator-format",
                "pdf",
            ],
            "parse-local",
            {
                "primary_path": "paper.md",
                "validator_paths": ["paper.pdf"],
                "validator_formats": ["pdf"],
                "primary_format": "markdown",
                "policy": None,
                "cache_root": None,
            },
        ),
    ],
)
def test_cli_routes_supported_provider_and_source_commands(
    argv: list[str],
    operation: str,
    parameters: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def dispatch(name: str, values: dict[str, object]) -> dict[str, object]:
        observed.append((name, values))
        return {"operation": name}

    monkeypatch.setattr("arc_paper.cli.dispatch_operation", dispatch)

    assert main(argv) == 0
    value = json.loads(capsys.readouterr().out)

    assert observed == [(operation, parameters)]
    assert value["status"] == "completed"
    assert value["data"] == {"operation": operation}


@pytest.mark.parametrize(
    "argv",
    [
        ["get-section", "--reference", "0911.3380"],
        ["get-section", "--reference", "0911.3380", "--ordinal", "-1"],
        [
            "get-section",
            "--reference",
            "0911.3380",
            "Introduction",
            "--ordinal",
            "0",
        ],
    ],
)
def test_cli_requires_one_unambiguous_section_selector(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "failed"
    assert value["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            OperationRequestError("invalid_parameters", "bad request"),
            "invalid_parameters",
        ),
        (OSError("disk unavailable"), "local_io_error"),
        (RuntimeError("unexpected failure"), "internal_error"),
    ],
)
def test_cli_classifies_typed_io_and_internal_failures(
    error: Exception,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_name, _values):
        raise error

    monkeypatch.setattr("arc_paper.cli.dispatch_operation", fail)

    assert main(["get-title", "0911.3380"]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "failed"
    assert value["error"] == {
        "code": code,
        "message": str(error),
        "details": {},
    }


def test_cli_preserves_nonfatal_domain_warnings_in_shared_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "arc_paper.cli.dispatch_operation",
        lambda operation, parameters: {
            "warnings": ["PDF validator was unavailable"],
        },
    )

    assert main(["get-title", "0911.3380"]) == 0
    value = json.loads(capsys.readouterr().out)

    assert value["status"] == "completed"
    assert value["warnings"] == [
        {
            "code": "paper_warning",
            "details": {},
            "message": "PDF validator was unavailable",
        }
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["extract-paper-ids", "--help"],
        ["safe-dir-name", "--help"],
        ["get-title", "--help"],
        ["get-abstract", "--help"],
        ["get-authors", "--help"],
        ["get-metadata", "--help"],
        ["get-citer-count", "--help"],
        ["get-references", "--help"],
        ["get-citers", "--help"],
        ["search-citers", "--help"],
        ["search-metadata", "--help"],
        ["search-full-text", "--help"],
        ["get-table-of-contents", "--help"],
        ["get-section", "--help"],
        ["search-equations", "--help"],
        ["fetch-arxiv-auto", "--help"],
        ["fetch-arxiv-pdf", "--help"],
        ["import-source", "--help"],
        ["parse-local", "--help"],
        ["export-rich-document", "--help"],
        ["extract-keywords", "--help"],
        ["cache", "--help"],
        ["cache", "list", "--help"],
        ["cache", "remove", "--help"],
        ["cache", "update", "--help"],
        ["status", "--help"],
        ["stop", "--help"],
        ["validate", "--help"],
    ],
)
def test_cli_root_subcommand_and_nested_cache_help_is_human_readable(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("usage: arc-")
    assert "arc.command_result.v2" not in captured.out


def test_cached_search_help_guides_specific_multi_term_queries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["search-full-text", "--help"]) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "cached corpus" in output
    assert "mixed reference and document targets" in output


def test_operations_cli_is_removed_but_registry_introspection_remains_public(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == 0
    root_help = capsys.readouterr().out
    assert "  operations" not in root_help

    assert main(["operations"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["error"]["code"] == "invalid_request"
    assert value["error"]["details"] == {
        "help_command": "arc-paper --help"
    }
    assert registry_document()["schema_version"] == "arc.paper.operation_registry.v1"


def test_nested_cache_usage_error_points_to_contextual_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["cache", "list", "--since", "not-a-duration"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["error"]["code"] == "invalid_request"
    assert value["error"]["details"] == {
        "help_command": "arc-paper cache list --help"
    }


def test_cli_import_and_parse_local_use_content_addressed_repository(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    note = tmp_path / "note.md"
    note.write_text("# Note\nInline $x+y$.\n", encoding="utf-8")
    cache = tmp_path / "cache"

    assert main(["import-source", str(note), "--cache-root", str(cache)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["data"]["source_format"] == "markdown"
    digest = imported["data"]["artifact_digest"]
    assert len(digest) == 64

    assert main(["parse-local", str(note), "--cache-root", str(cache)]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["document"]["source"]["artifact_digest"] == digest
    assert len(parsed["data"]["document"]["math_spans"]) == 1


def test_cli_delegates_generic_run_status_to_arc_jobs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = RunRepository(tmp_path)
    repository.create(RunSpec("paper-run", "test.paper", {"case": "status"}))

    assert main(
        ["status", "--run-root", str(tmp_path), "--run-id", "paper-run"]
    ) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["schema_version"] == "arc.command_result.v2"
    assert value["run"]["id"] == "paper-run"


@pytest.mark.parametrize("command", ["status", "stop", "validate"])
def test_delegated_run_controls_keep_arc_paper_help_and_usage_labels(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([command, "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(f"usage: arc-paper {command}")
    assert "usage: arc-jobs" not in captured.out

    assert main([command]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["error"]["details"] == {
        "help_command": f"arc-paper {command} --help"
    }


def test_keyword_cli_exposes_explicit_host_authority(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extract-keywords", "--help"]) == 0
    assert "--host-authority" in capsys.readouterr().out


def test_removed_control_planes_and_duplicate_parsers_stay_absent() -> None:
    removed = (
        "broker_jobs.py",
        "cache.py",
        "capabilities.py",
        "execution.py",
        "host.py",
        "reference_inference.py",
        "results.py",
        "runtime_context.py",
        "search.py",
        "worker_cli.py",
        "worker_controller.py",
        "worker_guard.py",
        "worker_session.py",
        "batch/db.py",
        "batch/runner.py",
        "providers/arxiv_source.py",
        "parse/source.py",
        "parse/document.py",
        "summary/checkpoint.py",
        "summary/providers/select.py",
    )
    assert not [path for path in removed if (SOURCE_ROOT / path).exists()]


def test_source_has_no_private_queue_thread_pool_or_detached_process_owner() -> None:
    forbidden_imports = {
        "queue",
        "threading",
        "concurrent.futures",
        "arc_llm.runner",
        "arc_paper.cache",
        "arc_paper.capabilities",
        "arc_paper.execution",
        "arc_paper.runtime_context",
    }
    subprocess_adapters = {
        SOURCE_ROOT / "_ripgrep.py",
        SOURCE_ROOT / "parse" / "parser.py",
        SOURCE_ROOT / "parse" / "visual.py",
    }
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not imported.intersection(forbidden_imports), path
        if "subprocess" in imported:
            assert path in subprocess_adapters
        source = path.read_text(encoding="utf-8")
        assert "subprocess.Popen" not in source
        assert "start_new_session=True" not in source
        assert "creationflags=" not in source
