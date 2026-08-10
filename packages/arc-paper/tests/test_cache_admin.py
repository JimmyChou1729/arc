from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc_paper import ArcPaperService, OPERATION_REGISTRY, OperationEffect
from arc_paper._cache_admin import CacheAdministrator, PaperCacheIndex
from arc_paper.cli import main
from arc_paper.providers.arxiv_html import ARXIV_HTML_AVAILABILITY_NAMESPACE
from arc_paper.providers.remote_cache import RemoteCacheError, RemoteRequestCache
from arc_paper.providers.inspire import describe_inspire_citer_request
from arc_paper.source_repository import SourceRepositoryError
from arc_paper.sources import SourceFormat, SourceOrigin, SourceOriginKind


def test_remote_admin_timestamp_is_written_only_on_fetch(
    tmp_path: Path,
) -> None:
    cache = RemoteRequestCache(tmp_path)
    cache.fetch_json("inspire-record", "arXiv:0911.3380", fetch=lambda: {"v": 1})
    entry = cache.admin_entry("json", "inspire-record", "arXiv:0911.3380")
    assert entry is not None

    assert cache.get_json("inspire-record", "arXiv:0911.3380") == {"v": 1}
    unchanged = cache.admin_entry("json", "inspire-record", "arXiv:0911.3380")
    assert unchanged is not None
    assert unchanged.cached_at == entry.cached_at

    cache.fetch_json(
        "inspire-record",
        "arXiv:0911.3380",
        refresh=True,
        fetch=lambda: {"v": 2},
    )
    refreshed = cache.admin_entry("json", "inspire-record", "arXiv:0911.3380")
    assert refreshed is not None
    assert refreshed.cached_at >= entry.cached_at


def test_list_since_uses_inclusive_rolling_utc_and_latest_first(
    tmp_path: Path,
) -> None:
    index = PaperCacheIndex(tmp_path)
    index.record_paper_component(
        "0911.3380",
        "inspire-record",
        cached_at="2026-07-24T12:00:00Z",
    )
    index.record_paper_component(
        "1201.0001",
        "inspire-record",
        cached_at="2026-07-24T11:59:59Z",
    )
    index.record_paper_component(
        "1301.0001",
        "inspire-record",
        cached_at="2026-07-25T11:00:00Z",
    )

    result = CacheAdministrator(tmp_path).list(
        since_seconds=86400,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )

    assert result.threshold_at == "2026-07-24T12:00:00Z"
    assert [item.paper_id for item in result.entries] == [
        "arXiv:1301.0001",
        "arXiv:0911.3380",
    ]


def test_list_combines_paper_and_entry_selectors_as_a_union(
    tmp_path: Path,
) -> None:
    index = PaperCacheIndex(tmp_path)
    index.record_paper_component(
        "0911.3380",
        "inspire-record",
        cached_at="2026-07-25T12:00:00Z",
    )
    index.record_paper_component(
        "1201.0001",
        "inspire-record",
        cached_at="2026-07-25T11:00:00Z",
    )
    cache = RemoteRequestCache(tmp_path)
    cache.fetch_json("inspire-search", "unmapped query", fetch=lambda: {"hits": []})

    result = CacheAdministrator(tmp_path).list(
        paper_ids=("0911.3380",),
        entry_ids=("paper:arXiv:1201.0001",),
    )

    assert {entry.entry_id for entry in result.entries} == {
        "paper:arXiv:0911.3380",
        "paper:arXiv:1201.0001",
    }
    assert all(entry.kind in {"paper", "local"} for entry in result.entries)


def test_v1_remote_layout_is_ignored(
    tmp_path: Path,
) -> None:
    cache = RemoteRequestCache(tmp_path)
    cache.fetch_json("inspire-search", "query", fetch=lambda: {"hits": []})
    current_root = tmp_path / "remote-request-cache" / "v2"
    current_root.rename(current_root.with_name("v1"))

    assert cache.admin_entries() == ()
    assert CacheAdministrator(tmp_path).list().entries == ()


def test_v1_cache_index_layout_is_ignored(tmp_path: Path) -> None:
    index = PaperCacheIndex(tmp_path)
    index.record_paper_component(
        "0911.3380",
        "inspire-record",
        cached_at="2026-07-25T12:00:00Z",
    )
    current_root = tmp_path / "cache-admin" / "v2"
    current_root.rename(current_root.with_name("v1"))

    assert index.entries() == ()
    assert CacheAdministrator(tmp_path).list().entries == ()


def test_current_remote_mapping_requires_admin_and_fetch_repairs_it(
    tmp_path: Path,
) -> None:
    cache = RemoteRequestCache(tmp_path)
    cache.fetch_json("inspire-record", "arXiv:0911.3380", fetch=lambda: {"v": 1})
    entry = cache.admin_entries()[0]
    entry_dir = (
        tmp_path
        / "remote-request-cache"
        / "v2"
        / entry.kind
        / entry.namespace
        / entry.request_digest[:2]
        / entry.request_digest
    )
    (entry_dir / "admin.json").unlink()

    assert cache.admin_entries() == ()
    assert CacheAdministrator(tmp_path).list().entries == ()
    assert cache.fetch_json(
        "inspire-record",
        "arXiv:0911.3380",
        fetch=lambda: {"v": 2},
    ) == {"v": 2}
    assert cache.get_json("inspire-record", "arXiv:0911.3380") == {"v": 2}
    assert len(cache.admin_entries()) == 1


def test_remote_remove_deletes_mapping_and_source_object(tmp_path: Path) -> None:
    cache = RemoteRequestCache(tmp_path)
    origin = SourceOrigin(
        SourceOriginKind.REMOTE_PROVIDER,
        provider="ar5iv",
        locator="https://ar5iv.labs.arxiv.org/html/0911.3380",
    )
    source = cache.fetch_source(
        "ar5iv-html",
        "0911.3380",
        source_format=SourceFormat.HTML,
        media_type="text/html",
        origin=origin,
        fetch=lambda: b"<html>cached</html>",
    )
    entry = cache.admin_entries()[0]

    assert cache.remove_admin_entry(entry.entry_id) is True
    assert cache.admin_entries() == ()
    with pytest.raises(SourceRepositoryError) as exc_info:
        cache.source_repository.read_bytes(source)
    assert exc_info.value.code == "source_not_found"


def test_official_html_remote_component_is_listed_and_removed(tmp_path: Path) -> None:
    cache = RemoteRequestCache(tmp_path)
    origin = SourceOrigin(
        SourceOriginKind.REMOTE_PROVIDER,
        provider="arxiv-html",
        locator="https://arxiv.org/html/0911.3380",
    )
    cache.fetch_source(
        "arxiv-html",
        "0911.3380",
        source_format=SourceFormat.HTML,
        media_type="text/html",
        origin=origin,
        fetch=lambda: b"<html>official</html>",
    )
    cache.fetch_json(
        ARXIV_HTML_AVAILABILITY_NAMESPACE,
        "0911.3380",
        fetch=lambda: {"status": "not_found"},
    )
    service = ArcPaperService(cache_root=tmp_path)

    entry = service.list_cache(paper_ids=("arXiv:0911.3380",)).entries[0]
    assert entry.paper_id == "arXiv:0911.3380"
    assert [component.name for component in entry.components] == [
        "arxiv-html",
        "arxiv-html-availability",
    ]

    removed = service.remove_cache(entry_ids=(entry.entry_id,), dry_run=False)
    assert removed.removed_entry_ids == (entry.entry_id,)
    assert service.list_cache(entry_ids=(entry.entry_id,)).entries == ()
    assert (
        cache.get_json(ARXIV_HTML_AVAILABILITY_NAMESPACE, "0911.3380") is None
    )


def test_shared_source_mapping_refetches_after_other_entry_deletes_object(
    tmp_path: Path,
) -> None:
    cache = RemoteRequestCache(tmp_path)
    origin = SourceOrigin(
        SourceOriginKind.REMOTE_PROVIDER,
        provider="ar5iv",
        locator="https://ar5iv.labs.arxiv.org/html/0911.3380",
    )
    first = cache.fetch_source(
        "ar5iv-html",
        "0911.3380",
        source_format=SourceFormat.HTML,
        media_type="text/html",
        origin=origin,
        fetch=lambda: b"<html>shared</html>",
    )
    second = cache.fetch_source(
        "validator-html",
        "0911.3380",
        source_format=SourceFormat.HTML,
        media_type="text/html",
        origin=origin,
        fetch=lambda: b"<html>shared</html>",
    )
    assert first.artifact_digest == second.artifact_digest

    first_entry = next(
        item for item in cache.admin_entries() if item.namespace == "ar5iv-html"
    )
    assert cache.remove_admin_entry(first_entry.entry_id) is True

    fetch_count = 0

    def refetch() -> bytes:
        nonlocal fetch_count
        fetch_count += 1
        return b"<html>shared</html>"

    repaired = cache.fetch_source(
        "validator-html",
        "0911.3380",
        source_format=SourceFormat.HTML,
        media_type="text/html",
        origin=origin,
        fetch=refetch,
    )

    assert fetch_count == 1
    assert cache.source_repository.read_bytes(repaired) == b"<html>shared</html>"


def test_fetch_json_repairs_payload_corruption_but_not_manifest_contract(
    tmp_path: Path,
) -> None:
    cache = RemoteRequestCache(tmp_path)
    cache.fetch_json("metadata", "request", fetch=lambda: {"generation": 1})
    entry = cache.admin_entries()[0]
    entry_dir = (
        tmp_path
        / "remote-request-cache"
        / "v2"
        / entry.kind
        / entry.namespace
        / entry.request_digest[:2]
        / entry.request_digest
    )
    manifest_path = entry_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (entry_dir / manifest["payload_file"]).write_bytes(b"corrupt")

    assert cache.fetch_json(
        "metadata",
        "request",
        fetch=lambda: {"generation": 2},
    ) == {"generation": 2}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RemoteCacheError) as exc_info:
        cache.fetch_json(
            "metadata",
            "request",
            fetch=lambda: {"generation": 3},
        )
    assert exc_info.value.code == "remote_cache_manifest_invalid"


def test_local_remove_previews_then_removes_locator_and_cache_objects(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "note.md"
    source_path.write_text("# Note\nbody\n", encoding="utf-8")
    service = ArcPaperService(cache_root=tmp_path / "cache")
    source = service.import_source(source_path)
    entry = service.list_cache().entries[0]
    assert {item.name for item in entry.components} == {"full-text", "full-text:markdown"}
    cached_at = entry.cached_at

    service.parser.parse_source(source)
    assert service.list_cache(entry_ids=(entry.entry_id,)).entries[0].cached_at == cached_at

    preview = service.remove_cache(entry_ids=(entry.entry_id,))
    assert preview.dry_run is True
    assert service.list_cache(entry_ids=(entry.entry_id,)).entries

    removed = service.remove_cache(
        entry_ids=(entry.entry_id,),
        dry_run=False,
    )

    assert removed.removed_entry_ids == (entry.entry_id,)
    assert service.list_cache(entry_ids=(entry.entry_id,)).entries == ()
    with pytest.raises(SourceRepositoryError) as exc_info:
        service.repository.read_bytes(source)
    assert exc_info.value.code == "source_not_found"


def test_update_runs_all_fixed_components_and_collects_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ArcPaperService(cache_root=tmp_path)
    service.cache_index.record_paper_component(
        "0911.3380",
        "inspire-record",
        cached_at="2026-07-25T00:00:00Z",
    )
    calls: list[tuple[str, object]] = []

    def metadata(paper_id: str, *, refresh: bool = False):
        calls.append(("metadata", refresh))
        return {"arxiv_id": "0911.3380"}

    def citers(
        paper_id: str,
        *,
        refresh: bool = False,
        limit: int = 1000,
        sort: str = "mostrecent",
    ):
        calls.append((sort, (refresh, limit)))
        if sort == "mostcited":
            raise RuntimeError("temporary")
        return []

    def arxiv_auto(paper_id: str, *, refresh: bool = False):
        calls.append(("arxiv-auto", refresh))
        return SimpleNamespace(
            report=SimpleNamespace(
                primary=SimpleNamespace(
                    origin=SimpleNamespace(provider="arxiv-html")
                )
            )
        )

    def pdf(paper_id: str, *, refresh: bool = False):
        calls.append(("pdf", refresh))
        return object()

    monkeypatch.setattr(service, "get_metadata", metadata)
    monkeypatch.setattr(service, "get_citers", citers)
    monkeypatch.setattr(service, "parse_arxiv_auto", arxiv_auto)
    monkeypatch.setattr(service, "parse_arxiv_pdf", pdf)

    result = service.update_cache(paper_ids=("0911.3380",))

    assert [(item.component, item.status) for item in result.records] == [
        ("inspire-record", "updated"),
        ("inspire-citers:mostrecent:1000", "updated"),
        ("inspire-citers:mostcited:1000", "failed"),
        ("arxiv-html", "updated"),
        ("arxiv-pdf", "updated"),
    ]
    assert calls == [
        ("metadata", True),
        ("mostrecent", (True, 1000)),
        ("mostcited", (True, 1000)),
        ("arxiv-auto", True),
        ("pdf", True),
    ]


def test_citer_admin_component_uses_provider_canonical_request(tmp_path: Path) -> None:
    service = ArcPaperService(cache_root=tmp_path)
    request = describe_inspire_citer_request(
        "123", sort="MostRecent", limit=1001
    )
    service.inspire.cache.fetch_json(
        "inspire-record",
        "arXiv:0911.3380",
        fetch=lambda: {
            "id": "123",
            "metadata": {
                "control_number": 123,
                "arxiv_eprints": [{"value": "0911.3380"}],
            },
        },
    )
    service.inspire.cache.fetch_json(
        "inspire-citers",
        request.request_key,
        fetch=lambda: {"hits": {"hits": []}},
    )

    assert service.get_citers(
        "0911.3380", sort="MostRecent", limit=1001
    ) == []
    entry = service.list_cache(paper_ids=("0911.3380",)).entries[0]
    component = next(
        item
        for item in entry.components
        if item.name == "inspire-citers:mostrecent:1000"
    )
    remote = service.inspire.cache.admin_entry(
        "json", "inspire-citers", request.request_key
    )
    assert remote is not None
    assert component.storage_entry_ids == (remote.entry_id,)


def test_cache_registry_effects_are_excluded_from_safe_projection() -> None:
    assert OPERATION_REGISTRY["cache-list"].effect_flags == frozenset(
        {OperationEffect.CACHE_ADMIN}
    )
    assert OPERATION_REGISTRY["cache-remove"].effect_flags == frozenset(
        {OperationEffect.CACHE_ADMIN, OperationEffect.DESTRUCTIVE}
    )
    assert OPERATION_REGISTRY["cache-update"].effect_flags == frozenset(
        {
            OperationEffect.CACHE_ADMIN,
            OperationEffect.NETWORK,
            OperationEffect.CACHE_WRITE,
        }
    )
    assert OPERATION_REGISTRY["cache-list"].operation_id == "arc-paper.cache-list.v2"
    component_schema = OPERATION_REGISTRY["cache-list"].output_codec.schema[
        "properties"
    ]["entries"]["items"]["properties"]["components"]["items"]
    assert "time_basis" not in component_schema["properties"]


@pytest.mark.parametrize("duration", ["0d", "-1d", "1.5h", "1 day", "1D", "1h30m"])
def test_cli_rejects_noncanonical_since(
    duration: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "cache",
                "list",
                "--since",
                duration,
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 2
    )
    value = json.loads(capsys.readouterr().out)
    assert value["error"]["code"] == "invalid_request"


def test_cli_cache_list_and_remove_preview_are_nested_protocol_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["cache", "list", "--since", "1d", "--cache-root", str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["data"]["since_seconds"] == 86400

    assert (
        main(
            [
                "cache",
                "remove",
                "--entry-id",
                "paper:arXiv:missing",
                "--cache-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["data"]["dry_run"] is True
