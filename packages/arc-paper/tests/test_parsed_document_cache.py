from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from arc_paper._parsed_document_cache import (
    DERIVED_CACHE_REBUILT_WARNING,
    PARSER_CONTRACT,
    PARSED_DOCUMENT_CACHE_SCHEMA,
    ParsedDocumentCache,
)
from arc_paper.parse.parser import parse_artifact_bytes
from arc_paper.source_repository import SourceRepository, SourceRepositoryError
from arc_paper.sources import SourceFormat, SourceOrigin, SourceOriginKind


def _source(repository: SourceRepository, body: bytes = b"<h1>Intro</h1><p>text</p>"):
    return repository.store_bytes(
        body,
        source_format=SourceFormat.HTML,
        media_type="text/html",
        origin=SourceOrigin(
            SourceOriginKind.REMOTE_PROVIDER,
            provider="fixture",
            locator="https://fixture.invalid/paper",
        ),
    )


def _parser(repository: SourceRepository, calls: list[int]):
    def parse(source):
        calls.append(1)
        return parse_artifact_bytes(source, repository.read_bytes(source))

    return parse


def test_cache_miss_hit_and_closed_manifest(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path)
    source = _source(repository)
    cache = ParsedDocumentCache(repository=repository)
    calls: list[int] = []

    first, first_warnings = cache.get_or_parse(source, _parser(repository, calls))
    second, second_warnings = cache.get_or_parse(source, _parser(repository, calls))

    assert first.document_digest == second.document_digest
    assert calls == [1]
    assert first_warnings == second_warnings == ()
    entry_dir = cache._entry_dir(cache._key(source))
    manifest = json.loads((entry_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version",
        "source_identity",
        "parser_contract",
        "payload_digest",
        "payload_size",
        "document_digest",
    }
    assert manifest["schema_version"] == PARSED_DOCUMENT_CACHE_SCHEMA
    assert manifest["parser_contract"] == PARSER_CONTRACT
    assert manifest["source_identity"] == {
        "source_format": "html",
        "media_type": "text/html",
        "artifact_digest": source.artifact_digest,
        "size": source.size,
    }
    assert (entry_dir / "document.json").is_file()


def test_cache_reuses_entry_across_instances_and_same_source_content(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path)
    first_source = _source(repository)
    second_source = _source(repository)
    first_calls: list[int] = []

    first_cache = ParsedDocumentCache(repository=repository)
    first, _ = first_cache.get_or_parse(first_source, _parser(repository, first_calls))

    second_cache = ParsedDocumentCache(repository=repository)
    second_calls: list[int] = []
    second, warnings = second_cache.get_or_parse(
        second_source, _parser(repository, second_calls)
    )

    assert first_source.content_identity == second_source.content_identity
    assert first.document_digest == second.document_digest
    assert first_calls == [1]
    assert second_calls == []
    assert warnings == ()


def test_cache_locks_concurrent_first_parse(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path)
    source = _source(repository)
    cache = ParsedDocumentCache(repository=repository)
    calls: list[int] = []
    calls_lock = threading.Lock()

    def parse(artifact):
        with calls_lock:
            calls.append(1)
        time.sleep(0.02)
        return parse_artifact_bytes(artifact, repository.read_bytes(artifact))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(lambda _: cache.get_or_parse(source, parse), range(8))
        )

    assert calls == [1]
    assert {document.document_digest for document, _ in results}
    assert all(warnings == () for _, warnings in results)


def test_cache_key_changes_for_source_content_and_parser_contract(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path)
    first_source = _source(repository, b"<h1>First</h1>")
    second_source = _source(repository, b"<h1>Second</h1>")
    cache = ParsedDocumentCache(repository=repository)
    calls: list[int] = []

    cache.get_or_parse(first_source, _parser(repository, calls))
    cache.get_or_parse(second_source, _parser(repository, calls))
    alternate = ParsedDocumentCache(
        repository=repository,
        parser_contract="arc.paper.parser.v2",
    )
    alternate.get_or_parse(first_source, _parser(repository, calls))

    assert calls == [1, 1, 1]
    assert cache._key(first_source) != cache._key(second_source)
    assert cache._key(first_source) != alternate._key(first_source)


@pytest.mark.parametrize("target", ("document.json", "manifest.json"))
def test_cache_repairs_corrupt_derived_entries_with_warning(
    tmp_path: Path, target: str
) -> None:
    repository = SourceRepository(tmp_path)
    source = _source(repository)
    cache = ParsedDocumentCache(repository=repository)
    calls: list[int] = []
    parser = _parser(repository, calls)
    cache.get_or_parse(source, parser)
    entry_dir = cache._entry_dir(cache._key(source))
    (entry_dir / target).write_bytes(b"not valid cache data")

    document, warnings = cache.get_or_parse(source, parser)

    assert document.source.content_identity == source.content_identity
    assert calls == [1, 1]
    assert warnings == (DERIVED_CACHE_REBUILT_WARNING,)
    assert json.loads((entry_dir / "manifest.json").read_text(encoding="utf-8"))[
        "document_digest"
    ] == document.document_digest


def test_cache_does_not_mask_source_corruption(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path)
    source = _source(repository)
    cache = ParsedDocumentCache(repository=repository)
    cache.get_or_parse(source, _parser(repository, []))
    payload_path = (
        tmp_path
        / "source-repository"
        / "v1"
        / source.source_format.value
        / "sha256"
        / source.artifact_digest[:2]
        / source.artifact_digest
        / "source"
    )
    payload_path.write_bytes(b"tampered source")

    with pytest.raises(SourceRepositoryError) as error:
        cache.get_or_parse(source, _parser(repository, []))

    assert error.value.code == "source_corrupt"


def test_cache_rejects_blank_parser_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parser_contract"):
        ParsedDocumentCache(tmp_path, parser_contract=" ")
