from __future__ import annotations

from pathlib import Path

import pytest

from arc_paper import ArcPaperService, PaperInputError, SourceRepository
from arc_paper.providers import RemoteRequestCache
from arc_paper.service import default_cache_root


def _clear_cache_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ARC_PAPER_CACHE", "ARC_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_cache_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_cache_environment(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_root() == tmp_path / "xdg" / "arc" / "arc-paper"

    monkeypatch.setenv("ARC_HOME", str(tmp_path / "arc-home"))
    assert default_cache_root() == tmp_path / "arc-home" / "cache" / "arc-paper"

    monkeypatch.setenv("ARC_PAPER_CACHE", str(tmp_path / "paper"))
    assert default_cache_root() == tmp_path / "paper"


def test_explicit_and_injected_roots_override_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_PAPER_CACHE", str(tmp_path / "environment"))
    explicit = tmp_path / "explicit"
    assert RemoteRequestCache(explicit).root == explicit

    repository = SourceRepository(tmp_path / "repository")
    assert RemoteRequestCache(source_repository=repository).root == repository.root
    assert ArcPaperService(repository=repository).repository is repository


def test_explicit_root_must_match_injected_repository(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "repository")
    with pytest.raises(ValueError, match="injected repository"):
        RemoteRequestCache(tmp_path / "other", source_repository=repository)
    with pytest.raises(PaperInputError, match="injected SourceRepository"):
        ArcPaperService(cache_root=tmp_path / "other", repository=repository)
