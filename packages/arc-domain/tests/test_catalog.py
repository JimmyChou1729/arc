from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from arc_jobs import (
    ImmutableArtifactStore,
    RunEngine,
    RunRepository,
    RunSpec,
    RunStatus,
    RunView,
    Succeeded,
)
from arc_domain.catalog import (
    DOMAIN_CATALOG_SCHEMA_VERSION,
    DOMAIN_EXPORT_MANIFEST_SCHEMA_VERSION,
    DomainPublicationError,
    publish_domain_result,
    read_domain_catalog,
    register_domain_run,
)
from arc_domain.contracts import (
    DomainBuildResult,
    DomainBuildWarning,
    decode_domain_build_result,
    encode_domain_build_result,
)
from arc_domain.paths import DomainPaths


def _repository(tmp_path: Path) -> RunRepository:
    return RunRepository(tmp_path / "cache")


class _DomainResultHandler:
    name = "arc.domain.build.v1"

    def __init__(self, domain_id: str) -> None:
        self.domain_id = domain_id

    def execute(self, context):
        result = _result(context.artifacts, domain_id=self.domain_id)
        result_ref = context.artifacts.publish_json(
            "domain-build-result", encode_domain_build_result(result)
        )
        return Succeeded(result_ref)


class _NoResultHandler:
    name = "arc.domain.build.v1"

    def execute(self, context):
        return Succeeded()


def _succeeded_run(
    repository: RunRepository, run_id: str, *, domain_id: str
) -> DomainBuildResult:
    snapshot = RunEngine(repository).execute(
        RunSpec(
            run_id=run_id,
            handler=_DomainResultHandler.name,
            semantic_input={"request": run_id},
        ),
        _DomainResultHandler(domain_id),
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    assert snapshot.result_ref is not None
    artifacts = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    return decode_domain_build_result(
        json.loads(artifacts.read_bytes(snapshot.result_ref).decode("utf-8"))
    )


def _result(artifacts: ImmutableArtifactStore, *, domain_id: str) -> DomainBuildResult:
    def publish(name: str, content: bytes, media_type: str):
        return artifacts.publish_bytes(name, content, media_type=media_type)

    return DomainBuildResult(
        domain_id=domain_id,
        foundation_selection=publish(
            "foundation-selection", b'{"schema_version":"foundation"}\n', "application/json"
        ),
        graph=publish("graph", b'{"schema_version":"graph"}\n', "application/json"),
        network_html=publish("network-html", b"<html>network</html>\n", "text/html"),
        paper_json_pack=publish(
            "paper-pack", b'{"schema_version":"paper-pack"}\n', "application/json"
        ),
        evidence_pack=publish(
            "evidence-pack", b'{"schema_version":"evidence-pack"}\n', "application/json"
        ),
        summary=publish("summary", b'{"schema_version":"summary"}\n', "application/json"),
        summary_markdown=publish("summary-markdown", b"# Summary\n", "text/markdown"),
    )


def _with_creation_times(
    monkeypatch: pytest.MonkeyPatch,
    repository: RunRepository,
    times: dict[str, str],
) -> None:
    original_inspect = repository.inspect

    def inspect(run_id: str) -> RunView:
        view = original_inspect(run_id)
        return RunView(
            snapshot=replace(view.snapshot, created_at=times[run_id]),
            stop_request=view.stop_request,
        )

    monkeypatch.setattr(repository, "inspect", inspect)


def test_publication_materializes_only_public_exports_and_writes_manifest_last(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    result = _succeeded_run(repository, "run-1", domain_id="domain-a")

    publication = publish_domain_result(
        repository, paths, run_id="run-1", result=result
    )

    generation = paths.export_generation("domain-a", "run-1")
    assert publication.active is True
    assert publication.manifest_path == generation / "export-manifest.json"
    assert (generation / "graph.json").read_bytes() == b'{"schema_version":"graph"}\n'
    assert (generation / "network.html").read_bytes() == b"<html>network</html>\n"
    assert (generation / "paper-pack.json").is_file()
    assert (generation / "evidence-pack.json").is_file()
    assert (generation / "summary.json").is_file()
    assert (generation / "summary.md").is_file()
    assert not (generation / "foundation-selection.json").exists()

    manifest = json.loads(publication.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == DOMAIN_EXPORT_MANIFEST_SCHEMA_VERSION
    assert manifest["domain_id"] == "domain-a"
    assert manifest["run_id"] == "run-1"
    assert manifest["result"]["foundation_selection"]["artifact_id"] == "foundation-selection"
    assert set(manifest["files"]) == {
        "graph.json",
        "network.html",
        "paper-pack.json",
        "evidence-pack.json",
        "summary.json",
        "summary.md",
    }

    catalog = read_domain_catalog(paths, domain_id="domain-a")
    assert catalog is not None
    assert catalog.latest == "run-1"
    assert catalog.active == "run-1"
    raw_catalog = json.loads(paths.catalog("domain-a").read_text(encoding="utf-8"))
    assert raw_catalog["contract_schema_version"] == DOMAIN_CATALOG_SCHEMA_VERSION


def test_newer_active_is_not_overwritten_when_an_older_run_finishes_late(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    old = _succeeded_run(repository, "old-run", domain_id="domain-a")
    new = _succeeded_run(repository, "new-run", domain_id="domain-a")
    _with_creation_times(
        monkeypatch,
        repository,
        {"old-run": "2026-07-24T10:00:00.000000Z", "new-run": "2026-07-24T11:00:00.000000Z"},
    )
    newest = publish_domain_result(repository, paths, run_id="new-run", result=new)
    older = publish_domain_result(repository, paths, run_id="old-run", result=old)

    assert newest.active is True
    assert older.active is False
    catalog = read_domain_catalog(paths, domain_id="domain-a")
    assert catalog is not None
    assert catalog.latest == "new-run"
    assert catalog.active == "new-run"
    assert (paths.export_generation("domain-a", "old-run") / "export-manifest.json").is_file()


def test_run_id_breaks_creation_time_ties_for_catalog_pointers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    for run_id in ("run-a", "run-b"):
        _succeeded_run(repository, run_id, domain_id="domain-a")
    _with_creation_times(
        monkeypatch,
        repository,
        {"run-a": "2026-07-24T10:00:00.000000Z", "run-b": "2026-07-24T10:00:00.000000Z"},
    )

    register_domain_run(repository, paths, domain_id="domain-a", run_id="run-a")
    catalog = register_domain_run(repository, paths, domain_id="domain-a", run_id="run-b")

    assert catalog.latest == "run-b"


def test_failed_publication_keeps_active_and_same_run_can_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    good = _succeeded_run(repository, "good-run", domain_id="domain-a")
    repair = _succeeded_run(repository, "repair-run", domain_id="domain-a")
    _with_creation_times(
        monkeypatch,
        repository,
        {"good-run": "2026-07-24T10:00:00.000000Z", "repair-run": "2026-07-24T11:00:00.000000Z"},
    )
    publish_domain_result(repository, paths, run_id="good-run", result=good)

    from arc_domain import catalog as catalog_module

    original_write = catalog_module._atomic_write_bytes

    def fail_network(path: Path, content: bytes) -> None:
        if path.name == "network.html":
            raise OSError("simulated export interruption")
        original_write(path, content)

    monkeypatch.setattr(catalog_module, "_atomic_write_bytes", fail_network)
    with pytest.raises(OSError, match="interruption"):
        publish_domain_result(repository, paths, run_id="repair-run", result=repair)

    partial_generation = paths.export_generation("domain-a", "repair-run")
    assert not (partial_generation / "export-manifest.json").exists()
    catalog = read_domain_catalog(paths, domain_id="domain-a")
    assert catalog is not None
    assert catalog.active == "good-run"
    assert catalog.latest == "repair-run"

    monkeypatch.setattr(catalog_module, "_atomic_write_bytes", original_write)
    repaired = publish_domain_result(repository, paths, run_id="repair-run", result=repair)

    assert repaired.active is True
    assert repaired.manifest_path.is_file()
    assert read_domain_catalog(paths, domain_id="domain-a").active == "repair-run"


def test_publication_rejects_pending_runs_before_touching_the_catalog(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    result = _succeeded_run(repository, "succeeded-run", domain_id="domain-a")
    repository.create(
        RunSpec(
            run_id="pending-run",
            handler="arc.domain.build.v1",
            semantic_input={"request": "pending-run"},
        )
    )

    with pytest.raises(DomainPublicationError, match="not succeeded"):
        publish_domain_result(repository, paths, run_id="pending-run", result=result)

    assert read_domain_catalog(paths, domain_id="domain-a") is None


def test_publication_rejects_a_result_that_differs_from_the_run_artifact(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    result = _succeeded_run(repository, "run-1", domain_id="domain-a")
    mismatched = replace(
        result,
        warnings=(DomainBuildWarning("unexpected", "different result", "test"),),
    )

    with pytest.raises(DomainPublicationError, match="does not match"):
        publish_domain_result(repository, paths, run_id="run-1", result=mismatched)

    assert read_domain_catalog(paths, domain_id="domain-a") is None


def test_publication_rejects_succeeded_runs_without_a_result_artifact(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    paths = DomainPaths(repository.root)
    snapshot = RunEngine(repository).execute(
        RunSpec(
            run_id="empty-result-run",
            handler=_NoResultHandler.name,
            semantic_input={"request": "empty-result-run"},
        ),
        _NoResultHandler(),
    )
    assert snapshot.status is RunStatus.SUCCEEDED
    result = _succeeded_run(repository, "reference-run", domain_id="domain-a")

    with pytest.raises(DomainPublicationError, match="no result artifact"):
        publish_domain_result(
            repository, paths, run_id="empty-result-run", result=result
        )
