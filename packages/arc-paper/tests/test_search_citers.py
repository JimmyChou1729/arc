from __future__ import annotations

from typing import Any

import pytest

import arc_paper
from arc_paper import ArcPaperService, PaperInputError


def _record(
    paper_id: str,
    *,
    title: str,
    abstract: str = "",
    citation_count: int = 0,
    published: str = "",
) -> dict[str, Any]:
    arxiv_id = paper_id.removeprefix("arXiv:")
    return {
        "paper_id": paper_id,
        "title": title,
        "abstract": abstract,
        "authors": [],
        "arxiv_id": arxiv_id,
        "inspire_recid": "",
        "doi": "",
        "identifiers": {"paper_id": paper_id, "arxiv_id": arxiv_id},
        "year": int(published[:4]) if published else None,
        "published": published,
        "citation_count": citation_count,
    }


class _FakeInspire:
    def __init__(
        self,
        *,
        total: int,
        recent: list[dict[str, Any]],
        cited: list[dict[str, Any]] | None = None,
    ):
        self.total = total
        self.recent = recent
        self.cited = cited if cited is not None else recent
        self.calls: list[tuple[str, int, bool]] = []

    def get_citer_count(self, paper_id: str, *, refresh: bool = False) -> int:
        return self.total

    def get_citers(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
        limit: int = 1000,
        sort: str = "mostrecent",
    ) -> list[dict[str, Any]]:
        self.calls.append((sort, limit, refresh))
        records = self.recent if sort == "mostrecent" else self.cited
        return records[:limit]


def _service(tmp_path: Any, inspire: _FakeInspire) -> ArcPaperService:
    return ArcPaperService(cache_root=tmp_path, inspire=inspire)


def test_search_citers_is_exported_as_public_python_api() -> None:
    assert "search_citers" in arc_paper.__all__
    assert callable(arc_paper.search_citers)


def test_search_citers_matches_normalized_literal_or_terms_and_missing_abstract(
    tmp_path,
) -> None:
    records = [
        _record(
            "arXiv:2401.00001",
            title="ULTRA—slow-roll signatures",
            abstract="A non-attractor phase.",
            citation_count=10,
            published="2024-01-02",
        ),
        _record(
            "arXiv:2401.00002",
            title="A different title",
            abstract="Signals from massive-exchange are derived.",
            citation_count=100,
            published="2025-01-02",
        ),
        _record(
            "arXiv:2401.00003",
            title="No overlap",
            abstract="",
            citation_count=1000,
            published="2026-01-02",
        ),
    ]
    inspire = _FakeInspire(total=3, recent=records)

    result = _service(tmp_path, inspire).search_citers(
        "1503.08043",
        ["ultra slow roll", "MASSIVE EXCHANGE", "non-attractor"],
    )

    assert result["paper_id"] == "arXiv:1503.08043"
    assert result["scanned_count"] == 3
    assert result["scan_complete"] is True
    assert result["scan_strategy"] == "all-mostrecent"
    assert result["matched_count"] == 2
    assert [item["paper_id"] for item in result["matches"]] == [
        "arXiv:2401.00001",
        "arXiv:2401.00002",
    ]
    assert result["matches"][0]["matched_terms"] == [
        "ultra slow roll",
        "non-attractor",
    ]
    assert result["matches"][0]["matched_fields"] == ["title", "abstract"]
    assert result["matches"][1]["matched_fields"] == ["abstract"]
    assert inspire.calls == [("mostrecent", 1000, False)]


def test_search_citers_sorting_limits_and_control_sample_are_deterministic(
    tmp_path,
) -> None:
    records = [
        _record(
            f"arXiv:24{index:02d}.00001",
            title=(
                "heavy field massive exchange"
                if index == 0
                else "heavy field"
                if index < 4
                else f"Control {index}"
            ),
            abstract="heavy field massive exchange" if index == 4 else "",
            citation_count=100 - index,
            published=f"2024-{index + 1:02d}-01",
        )
        for index in range(12)
    ]
    inspire = _FakeInspire(total=12, recent=records)

    result = _service(tmp_path, inspire).search_citers(
        "1503.08043",
        ["heavy field", "massive exchange"],
        limit=2,
    )

    assert [item["paper_id"] for item in result["matches"]] == [
        "arXiv:2400.00001",
        "arXiv:2401.00001",
    ]
    assert result["matched_count"] == 5
    assert result["returned_count"] == 2
    assert result["matches_truncated"] is True
    assert len(result["control_sample"]) <= 10
    assert sum(
        "newest" in item["control_reasons"] for item in result["control_sample"]
    ) == 5
    assert sum(
        "most-cited" in item["control_reasons"]
        for item in result["control_sample"]
    ) == 5


def test_search_citers_splits_large_neighborhood_and_deduplicates(tmp_path) -> None:
    overlap = _record(
        "arXiv:2401.00003",
        title="Overlap",
        abstract="target mechanism",
    )
    recent = [
        _record("arXiv:2401.00001", title="Recent one"),
        _record("arXiv:2401.00002", title="Recent two"),
        overlap,
    ]
    cited = [
        overlap,
        _record("arXiv:1901.00001", title="Highly cited"),
    ]
    inspire = _FakeInspire(total=2000, recent=recent, cited=cited)

    result = _service(tmp_path, inspire).search_citers(
        "1503.08043",
        ["target mechanism"],
        scan_limit=5,
    )

    assert result["scan_complete"] is False
    assert result["scan_strategy"] == "split-mostrecent-mostcited"
    assert result["scanned_count"] == 4
    assert result["matched_count"] == 1
    assert inspire.calls == [
        ("mostrecent", 3, False),
        ("mostcited", 2, False),
    ]


@pytest.mark.parametrize(
    "terms",
    [[], [""], ["  "], ["---"]],
)
def test_search_citers_rejects_empty_terms(tmp_path, terms: list[str]) -> None:
    service = _service(tmp_path, _FakeInspire(total=0, recent=[]))

    with pytest.raises(PaperInputError, match="term"):
        service.search_citers("1503.08043", terms)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("scan_limit", 0), ("scan_limit", 1001), ("limit", 0), ("limit", 51)],
)
def test_search_citers_rejects_out_of_range_limits(
    tmp_path, keyword: str, value: int
) -> None:
    service = _service(tmp_path, _FakeInspire(total=0, recent=[]))

    with pytest.raises(PaperInputError, match=keyword):
        service.search_citers(
            "1503.08043",
            ["specific phrase"],
            **{keyword: value},
        )


def test_search_citers_zero_count_does_not_fetch_citer_page(tmp_path) -> None:
    inspire = _FakeInspire(total=0, recent=[])

    result = _service(tmp_path, inspire).search_citers(
        "1503.08043", ["specific phrase"]
    )

    assert result["scan_complete"] is True
    assert result["scanned_count"] == 0
    assert result["control_sample"] == []
    assert inspire.calls == []
