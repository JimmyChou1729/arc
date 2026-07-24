from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from arc_domain._cache_root import resolve_cache_root
from arc_domain.paths import DomainPaths, domain_id_for, safe_domain_id


@dataclass(frozen=True)
class _Repository:
    root: Path


def test_cache_root_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ARC_DOMAIN_CACHE", str(tmp_path / "domain"))
    monkeypatch.setenv("ARC_HOME", str(tmp_path / "arc-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert resolve_cache_root() == tmp_path / "domain"
    assert resolve_cache_root(repository=_Repository(tmp_path / "repository")) == (
        tmp_path / "repository"
    )
    assert resolve_cache_root(tmp_path / "explicit") == tmp_path / "explicit"


def test_cache_root_fallbacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("ARC_DOMAIN_CACHE", raising=False)
    monkeypatch.setenv("ARC_HOME", str(tmp_path / "arc-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert resolve_cache_root() == tmp_path / "arc-home" / "cache" / "arc-domain"

    monkeypatch.delenv("ARC_HOME")
    assert resolve_cache_root() == tmp_path / "xdg" / "arc" / "arc-domain"

    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert resolve_cache_root() == tmp_path / "home" / ".cache" / "arc" / "arc-domain"


def test_explicit_root_must_match_injected_repository(tmp_path: Path):
    repository = _Repository(tmp_path / "repository")
    assert resolve_cache_root(repository.root, repository=repository) == repository.root
    with pytest.raises(ValueError, match="must match"):
        resolve_cache_root(tmp_path / "other", repository=repository)


def test_domain_id_is_normalized_and_intent_sensitive():
    first = domain_id_for("arXiv:2401.01234v2", "  amplitudes ")
    assert first == domain_id_for("2401.01234", "amplitudes")
    assert first != domain_id_for("2401.01234", "cosmology")
    assert first.startswith("arXiv_2401.01234_")


def test_domain_paths_use_run_generations(tmp_path: Path):
    paths = DomainPaths(tmp_path)
    domain_id = safe_domain_id("arXiv:2401.01234 / amplitudes")
    assert paths.runs == tmp_path / "runs"
    assert paths.catalog(domain_id) == tmp_path / "domains" / domain_id / "catalog.json"
    assert paths.export_generation(domain_id, "run-1") == (
        tmp_path / "domains" / domain_id / "exports" / "run-1"
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
