from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

import arc_paper._cache_archive as cache_archive
from arc_paper import (
    ArcPaperService,
    CacheArchiveError,
    OPERATION_REGISTRY,
    OperationEffect,
    export_cache,
    import_cache,
)
from arc_paper.cli import main
from arc_paper._cache_admin import PaperCacheIndex
from arc_paper.providers.remote_cache import RemoteRequestCache
from arc_paper.source_repository import SourceRepositoryError


def _cache_two_sources(tmp_path: Path) -> tuple[ArcPaperService, object, object]:
    first_path = tmp_path / "first.md"
    second_path = tmp_path / "second.md"
    first_path.write_text("# First\n\nSelected cache content.\n", encoding="utf-8")
    second_path.write_text("# Second\n\nUnselected cache content.\n", encoding="utf-8")
    service = ArcPaperService(cache_root=tmp_path / "source-cache")
    first = service.import_source(first_path)
    second = service.import_source(second_path)
    return service, first, second


def _entry_for_digest(service: ArcPaperService, digest: str):
    return next(
        entry
        for entry in service.list_cache().entries
        if entry.local_source_identity is not None
        and entry.local_source_identity["artifact_digest"] == digest
    )


def _tree_payloads(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_selected_export_import_includes_only_requested_logical_entry(
    tmp_path: Path,
) -> None:
    service, selected_source, other_source = _cache_two_sources(tmp_path)
    entry = _entry_for_digest(service, selected_source.artifact_digest)
    archive = tmp_path / "selected.tar.gz"

    exported = service.export_cache(archive, entry_ids=(entry.entry_id,))

    assert exported.selection_mode == "entries"
    assert exported.entry_ids == (entry.entry_id,)
    assert exported.file_count > 0
    assert exported.total_bytes > selected_source.size
    assert len(exported.archive_sha256) == 64

    target = tmp_path / "target-cache"
    imported = import_cache(archive, cache_root=target)
    listed = ArcPaperService(cache_root=target).list_cache().entries

    assert imported.added_count == exported.file_count
    assert imported.reused_count == 0
    assert imported.replaced_count == 0
    assert [item.entry_id for item in listed] == [entry.entry_id]
    repository = ArcPaperService(cache_root=target).repository
    assert repository.read_bytes(selected_source).startswith(b"# First")
    with pytest.raises(SourceRepositoryError) as exc_info:
        repository.read_bytes(other_source)
    assert exc_info.value.code == "source_not_found"


def test_selected_paper_export_includes_remote_json_payload(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    remote = RemoteRequestCache(cache)
    remote.fetch_json(
        "inspire-record", "arXiv:0911.3380", fetch=lambda: {"title": "cached"}
    )
    storage = remote.admin_entry("json", "inspire-record", "arXiv:0911.3380")
    assert storage is not None
    entry = PaperCacheIndex(cache).record_paper_component(
        "0911.3380",
        "inspire-record",
        cached_at="2026-08-12T00:00:00Z",
        storage_entry_ids=(storage.entry_id,),
    )
    archive = tmp_path / "paper.tar.gz"

    exported = export_cache(
        archive, cache_root=cache, entry_ids=(entry.entry_id,)
    )
    target = tmp_path / "target"
    import_cache(archive, cache_root=target)

    assert exported.file_count == 4
    assert RemoteRequestCache(target).get_json(
        "inspire-record", "arXiv:0911.3380"
    ) == {"title": "cached"}


def test_whole_export_includes_stable_unknown_files_but_not_coordination_state(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    stable = cache / "future-component" / "v1" / "payload.bin"
    stable.parent.mkdir(parents=True)
    stable.write_bytes(b"portable")
    lock = cache / "future-component" / "locks" / "writer.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("pid", encoding="utf-8")
    hidden = cache / "future-component" / ".payload.tmp"
    hidden.write_bytes(b"partial")
    archive = tmp_path / "all.tar.gz"

    exported = export_cache(archive, cache_root=cache, all_entries=True)
    target = tmp_path / "target"
    imported = import_cache(archive, cache_root=target)

    assert exported.selection_mode == "all"
    assert exported.entry_ids == ()
    assert exported.file_count == 1
    assert imported.added_count == 1
    assert (target / stable.relative_to(cache)).read_bytes() == b"portable"
    assert not (target / lock.relative_to(cache)).exists()
    assert not (target / hidden.relative_to(cache)).exists()


def test_import_is_idempotent_and_conflicts_are_zero_write_until_replace(
    tmp_path: Path,
) -> None:
    service, selected_source, _ = _cache_two_sources(tmp_path)
    entry = _entry_for_digest(service, selected_source.artifact_digest)
    archive = tmp_path / "selected.tar.gz"
    service.export_cache(archive, entry_ids=(entry.entry_id,))
    target = tmp_path / "target"

    first = import_cache(archive, cache_root=target)
    second = import_cache(archive, cache_root=target)
    assert second.added_count == 0
    assert second.reused_count == first.added_count

    source_payload = next(
        path
        for path in target.rglob("*")
        if path.is_file()
        and path.name == "source"
        and "source-repository" in path.parts
    )
    source_payload.write_bytes(b"conflicting local bytes")
    before = _tree_payloads(target)

    with pytest.raises(CacheArchiveError) as exc_info:
        import_cache(archive, cache_root=target)
    assert exc_info.value.code == "cache_archive_conflict"
    assert source_payload.relative_to(target).as_posix() in exc_info.value.paths
    assert _tree_payloads(target) == before

    replaced = import_cache(archive, cache_root=target, replace_conflicts=True)
    assert replaced.replaced_count == 1
    assert ArcPaperService(cache_root=target).repository.read_bytes(selected_source).startswith(
        b"# First"
    )


def test_import_retry_completes_after_interrupted_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, selected_source, _ = _cache_two_sources(tmp_path)
    entry = _entry_for_digest(service, selected_source.artifact_digest)
    archive = tmp_path / "selected.tar.gz"
    exported = service.export_cache(archive, entry_ids=(entry.entry_id,))
    target = tmp_path / "target"
    original = cache_archive._atomic_copy
    calls = 0

    def interrupt_once(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        original(source, destination)
        if calls == 1:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(cache_archive, "_atomic_copy", interrupt_once)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        import_cache(archive, cache_root=target)
    monkeypatch.setattr(cache_archive, "_atomic_copy", original)

    result = import_cache(archive, cache_root=target)
    assert result.added_count + result.reused_count == exported.file_count
    assert ArcPaperService(cache_root=target).repository.read_bytes(selected_source)


def test_export_retries_when_a_cache_file_changes_during_archiving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    payload = cache / "component" / "payload.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"before")
    archive = tmp_path / "all.tar.gz"
    original = cache_archive._write_archive
    calls = 0

    def mutate_once(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            payload.write_bytes(b"after mutation")
        original(*args, **kwargs)

    monkeypatch.setattr(cache_archive, "_write_archive", mutate_once)
    result = export_cache(archive, cache_root=cache, all_entries=True)

    assert calls == 2
    assert result.file_count == 1
    target = tmp_path / "target"
    import_cache(archive, cache_root=target)
    assert (target / "component" / "payload.bin").read_bytes() == b"after mutation"


@pytest.mark.parametrize(
    "entry_ids,all_entries,code",
    [
        ((), False, "cache_archive_selection_invalid"),
        (("entry",), True, "cache_archive_selection_invalid"),
        (("missing",), False, "cache_archive_entry_not_found"),
    ],
)
def test_export_rejects_invalid_selections(
    tmp_path: Path,
    entry_ids: tuple[str, ...],
    all_entries: bool,
    code: str,
) -> None:
    with pytest.raises(CacheArchiveError) as exc_info:
        export_cache(
            tmp_path / "archive.tar.gz",
            cache_root=tmp_path / "cache",
            entry_ids=entry_ids,
            all_entries=all_entries,
        )
    assert exc_info.value.code == code


def test_export_refuses_existing_output_and_output_inside_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    existing = tmp_path / "existing.tar.gz"
    existing.write_bytes(b"keep")
    with pytest.raises(CacheArchiveError) as exc_info:
        export_cache(existing, cache_root=cache, all_entries=True)
    assert exc_info.value.code == "cache_archive_exists"
    assert existing.read_bytes() == b"keep"

    with pytest.raises(CacheArchiveError) as exc_info:
        export_cache(cache / "archive.tar.gz", cache_root=cache, all_entries=True)
    assert exc_info.value.code == "cache_archive_output_inside_cache"


def test_import_rejects_unsafe_member_and_digest_mismatch(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.tar.gz"
    _write_raw_archive(unsafe, "../outside", b"payload")
    with pytest.raises(CacheArchiveError) as exc_info:
        import_cache(unsafe, cache_root=tmp_path / "unsafe-target")
    assert exc_info.value.code == "cache_archive_invalid"
    assert not (tmp_path / "outside").exists()

    corrupt = tmp_path / "corrupt.tar.gz"
    _write_raw_archive(corrupt, "component/data", b"payload", digest="0" * 64)
    with pytest.raises(CacheArchiveError) as exc_info:
        import_cache(corrupt, cache_root=tmp_path / "corrupt-target")
    assert exc_info.value.code == "cache_archive_invalid"
    assert _tree_payloads(tmp_path / "corrupt-target") == {}


def test_import_rejects_destination_symlink_without_replacing_it(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    payload = cache / "component" / "data"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"archive")
    archive = tmp_path / "all.tar.gz"
    export_cache(archive, cache_root=cache, all_entries=True)

    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (target / "component").mkdir()
    (target / "component" / "data").symlink_to(outside)

    with pytest.raises(CacheArchiveError) as exc_info:
        import_cache(archive, cache_root=target, replace_conflicts=True)
    assert exc_info.value.code == "cache_archive_structural_conflict"
    assert outside.read_bytes() == b"outside"
    assert (target / "component" / "data").is_symlink()


def test_import_rejects_a_symlink_cache_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    payload = source / "component" / "data"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"archive")
    archive = tmp_path / "all.tar.gz"
    export_cache(archive, cache_root=source, all_entries=True)
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    linked_target = tmp_path / "linked-target"
    linked_target.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(CacheArchiveError) as exc_info:
        import_cache(archive, cache_root=linked_target)
    assert exc_info.value.code == "cache_archive_structural_conflict"
    assert _tree_payloads(real_target) == {}


def test_cli_exports_selected_entry_and_imports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service, selected_source, _ = _cache_two_sources(tmp_path)
    entry = _entry_for_digest(service, selected_source.artifact_digest)
    archive = tmp_path / "selected.tar.gz"
    target = tmp_path / "target"

    assert main(
        [
            "cache",
            "export",
            entry.entry_id,
            "--output",
            str(archive),
            "--cache-root",
            str(service.cache_root),
        ]
    ) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["data"]["entry_ids"] == [entry.entry_id]

    assert main(
        ["cache", "import", str(archive), "--cache-root", str(target)]
    ) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["data"]["added_count"] > 0


def test_cache_archive_registry_declares_path_and_write_effects() -> None:
    assert OPERATION_REGISTRY["cache-export"].effect_flags == frozenset(
        {OperationEffect.CACHE_ADMIN, OperationEffect.ARBITRARY_LOCAL_PATH}
    )
    assert OPERATION_REGISTRY["cache-import"].effect_flags == frozenset(
        {
            OperationEffect.CACHE_ADMIN,
            OperationEffect.CACHE_WRITE,
            OperationEffect.DESTRUCTIVE,
            OperationEffect.ARBITRARY_LOCAL_PATH,
        }
    )


def _write_raw_archive(
    output: Path, path: str, payload: bytes, *, digest: str | None = None
) -> None:
    manifest = {
        "schema_version": "arc.paper.cache_archive.v2",
        "selection": {"mode": "all", "entry_ids": []},
        "files": [
            {
                "path": path,
                "size": len(payload),
                "sha256": digest or hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    manifest_payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with tarfile.open(output, "w:gz") as archive:
        manifest_info = tarfile.TarInfo("arc-paper-cache/manifest.json")
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))
        payload_info = tarfile.TarInfo(f"arc-paper-cache/cache/{path}")
        payload_info.size = len(payload)
        archive.addfile(payload_info, io.BytesIO(payload))
