from __future__ import annotations

import importlib
import sys
from pathlib import Path
from arc_llm import InteractionRequest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"


def _module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("_arc_workflows.evidence")
    finally:
        sys.path.remove(str(SCRIPTS))


def test_evidence_contracts_are_exactly_the_paper_allowlist() -> None:
    module = _module()

    contracts = module.evidence_operation_contracts()

    assert tuple(contracts) == module.EVIDENCE_OPERATION_NAMES
    assert len(contracts) == 9
    assert all("cache_root" not in contract.arguments_schema["properties"] for name, contract in contracts.items() if "arxiv" in name)
    assert {
        "get-arxiv-table-of-contents",
        "get-arxiv-section",
        "search-arxiv-full-text",
        "search-arxiv-equations",
        "search-cached-full-text",
    } <= set(contracts)
    cached_properties = contracts[
        "search-cached-full-text"
    ].arguments_schema["properties"]
    assert set(cached_properties) == {
        "terms",
        "limit",
        "context_lines",
        "case_sensitive",
    }
    assert "cache_root" not in cached_properties
    assert "path" not in cached_properties
    cached_output = contracts[
        "search-cached-full-text"
    ].response_schema["properties"]["data"]
    title_items = cached_output["properties"]["top_paper_titles"]["items"]
    assert title_items["type"] == "string"
    assert title_items["minLength"] == 1
    assert "abstracts" not in cached_output["properties"]
    assert "summaries" not in cached_output["properties"]
    assert not hasattr(module, "_SERVICE_METHODS")


def test_evidence_resolver_routes_multi_term_cached_search() -> None:
    module = _module()

    class CachedSearchService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def search_cached_full_text(
            self,
            terms: list[str],
            *,
            limit: int = 100,
            context_lines: int = 0,
            case_sensitive: bool = False,
        ) -> dict[str, object]:
            self.calls.append(
                {
                    "terms": terms,
                    "limit": limit,
                    "context_lines": context_lines,
                    "case_sensitive": case_sensitive,
                }
            )
            return {
                "mode": "occurrences",
                "terms": terms,
                "limit": limit,
                "context_lines": context_lines,
                "case_sensitive": case_sensitive,
                "total_occurrences": 0,
                "matched_document_count": 0,
                "occurrences": [],
                "top_paper_titles": [],
                "context_status": "not_requested",
                "message": "Add synonymous multiword terms.",
                "warnings": [],
            }

    service = CachedSearchService()
    resolver = module.ArcPaperEvidenceResolver(service=service)  # type: ignore[arg-type]
    response = resolver.resolve(
        InteractionRequest(
            "cached-search",
            "search-cached-full-text",
            {
                "terms": ["heavy field", "massive exchange"],
                "limit": 50,
                "context_lines": 0,
                "case_sensitive": True,
            },
        )
    )

    assert response.error is None
    assert service.calls == [
        {
            "terms": ["heavy field", "massive exchange"],
            "limit": 50,
            "context_lines": 0,
            "case_sensitive": True,
        }
    ]
    assert response.result["operation_id"] == (
        "arc-paper.search-cached-full-text.v1"
    )
    assert response.result["data"]["top_paper_titles"] == []


def test_evidence_resolver_reuses_one_service_document_memo_and_records_provenance() -> None:
    module = _module()

    class MemoizedService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, bool]] = []
            self.parsed_documents: set[str] = set()
            self.parse_count = 0

        def get_arxiv_section(
            self,
            arxiv_id: str,
            selector: str,
            *,
            refresh: bool = False,
        ) -> dict[str, object]:
            self.calls.append((arxiv_id, selector, refresh))
            if arxiv_id not in self.parsed_documents:
                self.parsed_documents.add(arxiv_id)
                self.parse_count += 1
            return {
                "provenance": {
                    "canonical_arxiv_id": "arXiv:0911.3380",
                    "provider": "arxiv-html",
                    "source_format": "html",
                    "source_digest": "a" * 64,
                    "document_digest": "b" * 64,
                },
                "section_id": "intro",
                "title": "Introduction",
                "text": "Body",
                "level": 1,
                "ordinal": 0,
                "page_start": None,
                "page_end": None,
                "warnings": [],
            }

    service = MemoizedService()
    resolver = module.ArcPaperEvidenceResolver(service=service)  # type: ignore[arg-type]
    responses = tuple(
        resolver.resolve(
            InteractionRequest(
                f"request-{number}",
                "get-arxiv-section",
                {
                    "arxiv_id": "https://arxiv.org/abs/0911.3380v3",
                    "selector": "Introduction",
                },
            )
        )
        for number in (1, 2)
    )

    assert all(response.error is None for response in responses)
    assert resolver.service is service
    assert service.calls == [
        ("arXiv:0911.3380", "Introduction", False),
        ("arXiv:0911.3380", "Introduction", False),
    ]
    assert service.parse_count == 1
    response = responses[0]
    assert response.result["operation_id"] == "arc-paper.get-arxiv-section.v3"
    provenance = response.result["provenance"]
    assert provenance["canonical_arxiv_id"] == "arXiv:0911.3380"
    assert provenance["source_digest"] == "a" * 64
    assert provenance["document_digest"] == "b" * 64
    assert resolver.records[0]["parameters"]["selector"] == "Introduction"


def test_evidence_resolver_has_no_batch_cap_and_enforces_allowlist() -> None:
    module = _module()

    class SearchService:
        def search_metadata(self, query: str, *, limit: int = 20) -> list[object]:
            return []

    resolver = module.ArcPaperEvidenceResolver(  # type: ignore[arg-type]
        service=SearchService(),
    )

    for number in range(50):
        response = resolver.resolve(
            InteractionRequest(
                f"request-{number}",
                "search-metadata",
                {"query": f"research query {number}", "limit": 1},
            )
        )
        assert response.error is None
    forbidden = resolver.resolve(
        InteractionRequest("request-50", "import-source", {"path": "/tmp/paper"})
    )
    aliased = resolver.resolve(
        InteractionRequest(
            "request-51",
            "arc-paper.search-metadata.v1",
            {"query": "alias must be explicit", "limit": 1},
        )
    )

    assert forbidden.error["code"] == "evidence_operation_not_allowed"
    assert forbidden.error["message"] == (
        "operation is not in the ARC evidence allowlist: import-source"
    )
    assert aliased.error["code"] == "evidence_operation_not_allowed"
    assert aliased.error["message"] == (
        "operation is not in the ARC evidence allowlist: "
        "arc-paper.search-metadata.v1"
    )
    assert resolver.request_count == 52
    assert [record["request_number"] for record in resolver.records] == [
        *range(1, 53),
    ]


def test_evidence_ledger_reports_current_process_outcomes_by_loop() -> None:
    module = _module()

    class SearchService:
        def search_metadata(
            self,
            query: str,
            *,
            limit: int = 20,
        ) -> list[object]:
            if query == "provider failure":
                raise RuntimeError("provider unavailable")
            return []

    ledger = module.IdeasEvidenceLedger(
        module.ArcPaperEvidenceResolver(  # type: ignore[arg-type]
            service=SearchService(),
        ),
        ["loop-a", "loop-b"],
    )
    loop_a = ledger.scoped("loop-a")
    loop_b = ledger.scoped("loop-b")

    successful = InteractionRequest(
        "success-a",
        "search-metadata",
        {"query": "same raw request", "limit": 1},
    )
    repeated = InteractionRequest(
        "success-b",
        "search-metadata",
        {"query": "same raw request", "limit": 1},
    )
    failed = InteractionRequest(
        "failed",
        "search-metadata",
        {"query": "provider failure", "limit": 1},
    )
    forbidden = InteractionRequest(
        "forbidden",
        "import-source",
        {"path": "/tmp/paper"},
    )

    assert loop_a.resolve(successful).error is None
    assert loop_b.resolve(repeated).error is None
    assert loop_b.resolve(failed).error["code"] == "evidence_operation_failed"
    assert loop_a.resolve(forbidden).error["code"] == (
        "evidence_operation_not_allowed"
    )

    document = ledger.to_document()

    assert document["observation_scope"] == "current_process"
    assert document["attempted"] == 4
    assert document["succeeded"] == 2
    assert document["failed"] == 2
    assert document["errors_by_code"] == {
        "evidence_operation_failed": 1,
        "evidence_operation_not_allowed": 1,
    }
    assert document["repeated_raw_signature"] == 1
    assert [record["request_number"] for record in document["records"]] == [
        1,
        2,
        3,
        4,
    ]
    assert document["per_loop"] == {
        "loop-a": {
            "attempted": 2,
            "succeeded": 1,
            "failed": 1,
            "errors_by_code": {
                "evidence_operation_not_allowed": 1,
            },
            "repeated_raw_signature": 0,
        },
        "loop-b": {
            "attempted": 2,
            "succeeded": 1,
            "failed": 1,
            "errors_by_code": {
                "evidence_operation_failed": 1,
            },
            "repeated_raw_signature": 1,
        },
    }


def test_evidence_adapter_delegates_package_mechanics() -> None:
    module = _module()
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "threading" not in source
    assert "_service_lock" not in source
    assert "_normalize_parameters" not in source
    assert "normalize_paper_id" not in source


def test_evidence_adapter_preserves_ideas_fallback_error() -> None:
    module = _module()

    class FailingService:
        def search_metadata(self, query: str, *, limit: int = 20) -> list[object]:
            raise RuntimeError("provider unavailable")

    resolver = module.ArcPaperEvidenceResolver(
        service=FailingService(),  # type: ignore[arg-type]
    )
    response = resolver.resolve(
        InteractionRequest(
            "failed",
            "search-metadata",
            {"query": "bounded query", "limit": 1},
        )
    )

    assert response.error["code"] == "evidence_operation_failed"
    assert response.error["message"] == "provider unavailable"
    assert resolver.records == [
        {
            "request_id": "failed",
            "ok": False,
            "source": "arc-paper",
            "operation_id": "arc-paper.search-metadata.v1",
            "parameters": {"query": "bounded query", "limit": 1},
            "canonical_arxiv_id": "",
            "source_digest": "",
            "document_digest": "",
            "request_number": 1,
            "error": {
                "code": "evidence_operation_failed",
                "message": "provider unavailable",
            },
        }
    ]
