from __future__ import annotations

from pathlib import Path

import pytest

from arc_domain.paths import DomainPaths, domain_id_for, safe_domain_id


def test_domain_paths_are_project_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARC_HOME", str(tmp_path / "legacy-arc-home"))
    paths = DomainPaths.for_project(tmp_path / "project")
    assert paths.root == tmp_path / "project" / ".arc" / "domain"
    assert not (tmp_path / "legacy-arc-home" / "cache" / "arc-domain").exists()


@pytest.mark.parametrize("value", ["", "   "])
def test_domain_paths_reject_an_empty_project_directory(value: str):
    with pytest.raises(ValueError, match="project_dir"):
        DomainPaths.for_project(value)


def test_domain_id_is_normalized_and_intent_sensitive():
    first = domain_id_for("arXiv:2401.01234v2", "  amplitudes ")
    assert first == domain_id_for("2401.01234", "amplitudes")
    assert first != domain_id_for("2401.01234", "cosmology")
    assert first.startswith("arXiv_2401.01234_")


def test_domain_paths_use_run_generations(tmp_path: Path):
    paths = DomainPaths.for_project(tmp_path)
    domain_id = safe_domain_id("arXiv:2401.01234 / amplitudes")
    assert paths.runs == tmp_path / ".arc" / "domain" / "runs"
    assert paths.catalog(domain_id) == (
        tmp_path / ".arc" / "domain" / "domains" / domain_id / "catalog.json"
    )
    assert paths.export_generation(domain_id, "run-1") == (
        tmp_path / ".arc" / "domain" / "domains" / domain_id / "exports" / "run-1"
    )
    for run_id in ("../escape", "/tmp/escape", "contains/slash"):
        with pytest.raises(ValueError):
            paths.export_generation(domain_id, run_id)


@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_domain_id_rejects_invalid_seed(value):
    with pytest.raises(ValueError, match="seed_paper"):
        domain_id_for(value)


@pytest.mark.parametrize("value", [None, 42])
def test_domain_id_rejects_invalid_intent(value):
    with pytest.raises(ValueError, match="intent"):
        domain_id_for("2401.01234", value)
