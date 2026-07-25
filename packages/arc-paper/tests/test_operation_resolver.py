from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from arc_paper import PaperOperationResolver


ARXIV_PROVENANCE = {
    "canonical_arxiv_id": "arXiv:0911.3380",
    "provider": "ar5iv",
    "source_format": "html",
    "source_digest": "a" * 64,
    "document_digest": "b" * 64,
}


class SectionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.parsed_documents: set[str] = set()
        self.parse_count = 0

    def get_arxiv_section(
        self,
        arxiv_id: str,
        selector: str | int,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        self.calls.append((arxiv_id, str(selector), refresh))
        if arxiv_id not in self.parsed_documents:
            self.parsed_documents.add(arxiv_id)
            self.parse_count += 1
        return {
            "provenance": ARXIV_PROVENANCE,
            "section_id": "intro",
            "title": "Introduction",
            "text": "Body",
            "level": 1,
            "ordinal": 0,
            "page_start": None,
            "page_end": None,
            "warnings": [],
        }


def test_resolver_reuses_service_normalizes_ids_and_uses_registry_codecs() -> None:
    service = SectionService()
    resolver = PaperOperationResolver(
        allowed_operations=("get-arxiv-section",),
        request_limit=2,
        service=service,  # type: ignore[arg-type]
    )

    results = tuple(
        resolver.resolve(
            "get-arxiv-section",
            {
                "arxiv_id": "https://arxiv.org/abs/0911.3380v3",
                "selector": "Introduction",
            },
            request_id=f"request-{number}",
        )
        for number in (1, 2)
    )

    assert all(result.ok for result in results)
    assert resolver.service is service
    assert service.calls == [
        ("arXiv:0911.3380", "Introduction", False),
        ("arXiv:0911.3380", "Introduction", False),
    ]
    assert service.parse_count == 1
    assert results[0].to_document() == {
        "ok": True,
        "operation_id": "arc-paper.get-arxiv-section.v1",
        "parameters": {
            "arxiv_id": "arXiv:0911.3380",
            "selector": "Introduction",
        },
        "data": {
            "provenance": ARXIV_PROVENANCE,
            "section_id": "intro",
            "title": "Introduction",
            "text": "Body",
            "level": 1,
            "ordinal": 0,
            "page_start": None,
            "page_end": None,
            "warnings": [],
        },
        "provenance": {
            "source": "arc-paper",
            "operation_id": "arc-paper.get-arxiv-section.v1",
            "parameters": {
                "arxiv_id": "arXiv:0911.3380",
                "selector": "Introduction",
            },
            "canonical_arxiv_id": "arXiv:0911.3380",
            "source_digest": "a" * 64,
            "document_digest": "b" * 64,
            "request_number": 1,
        },
    }
    assert [record.request_id for record in resolver.records] == [
        "request-1",
        "request-2",
    ]


def test_resolver_validates_configuration_without_workflow_policy() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PaperOperationResolver(
            allowed_operations=("search-metadata",),
            request_limit=True,
        )
    with pytest.raises(ValueError, match="must not be empty"):
        PaperOperationResolver(allowed_operations=(), request_limit=1)
    with pytest.raises(ValueError, match="unknown arc-paper operation"):
        PaperOperationResolver(
            allowed_operations=("missing-operation",),
            request_limit=1,
        )
    with pytest.raises(ValueError, match="not supported"):
        PaperOperationResolver(
            allowed_operations=("import-source",),
            request_limit=1,
        )


def test_resolver_enforces_allowlist_and_limit_and_records_codec_failures() -> None:
    class SearchService:
        def search_metadata(
            self, query: str, *, limit: int = 20
        ) -> list[dict[str, Any]]:
            return []

    resolver = PaperOperationResolver(
        allowed_operations=("search-metadata",),
        request_limit=2,
        service=SearchService(),  # type: ignore[arg-type]
    )

    invalid = resolver.resolve(
        "search-metadata",
        {"query": "", "limit": 1},
        request_id="invalid",
    )
    success = resolver.resolve(
        "search-metadata",
        {"query": "bounded query", "limit": 1},
        request_id="success",
    )
    exhausted = resolver.resolve(
        "search-metadata",
        {"query": "one too many", "limit": 1},
        request_id="exhausted",
    )
    forbidden = resolver.resolve(
        "import-source",
        {"path": "/tmp/paper"},
        request_id="forbidden",
    )

    assert invalid.error is not None
    assert invalid.error.code == "invalid_parameters"
    assert success.ok
    assert exhausted.error is not None
    assert exhausted.error.code == "request_limit_exceeded"
    assert forbidden.error is not None
    assert forbidden.error.code == "operation_not_allowed"
    assert resolver.request_count == 4
    assert [
        record.result.provenance.request_number for record in resolver.records
    ] == [1, 2, 3, 4]
    assert resolver.records[0].to_document()["error"]["code"] == (
        "invalid_parameters"
    )
    with pytest.raises(TypeError):
        invalid.parameters["query"] = "mutated"  # type: ignore[index]


def test_resolver_allowlist_matches_only_explicitly_configured_tokens() -> None:
    class SearchService:
        def search_metadata(
            self, query: str, *, limit: int = 20
        ) -> list[dict[str, Any]]:
            return []

    short_name_resolver = PaperOperationResolver(
        allowed_operations=("search-metadata",),
        request_limit=2,
        service=SearchService(),  # type: ignore[arg-type]
    )
    operation_id_resolver = PaperOperationResolver(
        allowed_operations=("arc-paper.search-metadata.v1",),
        request_limit=2,
        service=SearchService(),  # type: ignore[arg-type]
    )

    rejected_id = short_name_resolver.resolve(
        "arc-paper.search-metadata.v1",
        {"query": "query", "limit": 1},
    )
    accepted_name = short_name_resolver.resolve(
        "search-metadata",
        {"query": "query", "limit": 1},
    )
    rejected_name = operation_id_resolver.resolve(
        "search-metadata",
        {"query": "query", "limit": 1},
    )
    accepted_id = operation_id_resolver.resolve(
        "arc-paper.search-metadata.v1",
        {"query": "query", "limit": 1},
    )

    assert rejected_id.error is not None
    assert rejected_id.error.code == "operation_not_allowed"
    assert accepted_name.ok
    assert rejected_name.error is not None
    assert rejected_name.error.code == "operation_not_allowed"
    assert accepted_id.ok


def test_resolver_serializes_one_service_and_keeps_concurrent_records_safe() -> None:
    class ConcurrentService:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.state_lock = threading.Lock()

        def search_metadata(
            self, query: str, *, limit: int = 20
        ) -> list[dict[str, Any]]:
            with self.state_lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            threading.Event().wait(0.002)
            with self.state_lock:
                self.active -= 1
            return []

    service = ConcurrentService()
    resolver = PaperOperationResolver(
        allowed_operations=("search-metadata",),
        request_limit=12,
        service=service,  # type: ignore[arg-type]
    )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = tuple(
            executor.map(
                lambda number: resolver.resolve(
                    "search-metadata",
                    {"query": f"query {number}", "limit": 1},
                    request_id=str(number),
                ),
                range(12),
            )
        )

    assert all(result.ok for result in results)
    assert service.maximum_active == 1
    assert resolver.request_count == 12
    assert sorted(
        record.result.provenance.request_number for record in resolver.records
    ) == list(range(1, 13))
