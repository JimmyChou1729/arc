from __future__ import annotations

import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from arc_paper.providers import (
    Ar5ivProvider,
    ArxivPdfProvider,
    InspireProvider,
    RemoteCacheError,
    RemoteRequestCache,
)
from arc_paper.providers.ar5iv import MAX_HTML_BYTES
from arc_paper.providers.arxiv_pdf import arxiv_pdf_url
from arc_paper.source_repository import SourceRepository
from arc_paper.sources import SourceFormat


def _response(
    request: httpx.Request,
    *,
    content: bytes,
    media_type: str,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=request,
        content=content,
        headers={"content-type": media_type},
    )


def test_ar5iv_fetches_html_into_source_repository_and_replays_without_network(
    tmp_path,
):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _response(request, content=b"<html>paper</html>", media_type="text/html")

    first_provider = Ar5ivProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    first = first_provider.fetch("arXiv:0911.3380")
    second_provider = Ar5ivProvider(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("cache hit must not access the network")
                )
            )
        ),
    )
    second = second_provider.fetch("0911.3380")

    assert first.content_identity == second.content_identity
    assert first.source_format is SourceFormat.HTML
    assert first.origin.provider == "ar5iv"
    assert SourceRepository(tmp_path).read_bytes(second) == b"<html>paper</html>"
    assert calls == ["https://ar5iv.labs.arxiv.org/html/0911.3380"]
    assert all("/pdf/" not in url for url in calls)


def test_ar5iv_refresh_replaces_request_mapping_without_changing_content_identity(
    tmp_path,
):
    payloads = iter((b"<html>stale</html>", b"<html>fresh</html>"))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _response(
            request,
            content=next(payloads),
            media_type="text/html",
        )

    provider = Ar5ivProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    stale = provider.fetch("0911.3380")
    fresh = provider.fetch("0911.3380", refresh=True)
    replay = provider.fetch("0911.3380")

    assert stale.content_identity != fresh.content_identity
    assert replay.content_identity == fresh.content_identity
    assert SourceRepository(tmp_path).read_bytes(replay) == b"<html>fresh</html>"
    assert calls == [
        "https://ar5iv.labs.arxiv.org/html/0911.3380",
        "https://ar5iv.labs.arxiv.org/html/0911.3380",
    ]


def test_ar5iv_concurrent_cache_fill_fetches_once(tmp_path):
    call_count = 0
    count_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.02)
        return _response(request, content=b"<html>paper</html>", media_type="text/html")

    provider = Ar5ivProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        artifacts = list(
            executor.map(lambda _: provider.fetch("0911.3380"), range(8))
        )

    assert call_count == 1
    assert len({artifact.content_identity for artifact in artifacts}) == 1


def test_ar5iv_rejects_wrong_media_and_oversized_header(tmp_path):
    wrong_media = Ar5ivProvider(
        cache_root=tmp_path / "wrong",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: _response(
                    request, content=b"not html", media_type="text/plain"
                )
            )
        ),
    )
    with pytest.raises(Exception) as wrong_error:
        wrong_media.fetch("0911.3380")
    assert getattr(wrong_error.value, "code", "") == "ar5iv_media_type_invalid"

    def oversized(request: httpx.Request) -> httpx.Response:
        response = _response(request, content=b"", media_type="text/html")
        response.headers["content-length"] = str(MAX_HTML_BYTES + 1)
        return response

    too_large = Ar5ivProvider(
        cache_root=tmp_path / "large",
        client=httpx.Client(transport=httpx.MockTransport(oversized)),
    )
    with pytest.raises(Exception) as size_error:
        too_large.fetch("0911.3380")
    assert getattr(size_error.value, "code", "") == "ar5iv_html_too_large"


def test_arxiv_pdf_is_explicit_content_addressed_source(tmp_path):
    calls: list[str] = []
    pdf = b"%PDF-1.7\nfixture\n%%EOF\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _response(request, content=pdf, media_type="application/pdf")

    provider = ArxivPdfProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    artifact = provider.fetch("hep-th/0601001")

    assert arxiv_pdf_url("arXiv:hep-th/0601001") == (
        "https://arxiv.org/pdf/hep-th/0601001"
    )
    assert artifact.source_format is SourceFormat.PDF
    assert artifact.origin.provider == "arxiv-pdf"
    assert SourceRepository(tmp_path).read_bytes(artifact) == pdf
    assert calls == ["https://arxiv.org/pdf/hep-th/0601001"]


def test_inspire_metadata_cache_is_atomic_integrity_checked_and_concurrent(tmp_path):
    call_count = 0
    count_lock = threading.Lock()
    record = {
        "id": "123",
        "metadata": {
            "control_number": 123,
            "titles": [{"title": "Cached paper"}],
            "arxiv_eprints": [{"value": "0911.3380"}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.02)
        return _response(
            request,
            content=json.dumps(record).encode(),
            media_type="application/json",
        )

    provider = InspireProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(
            executor.map(lambda _: provider.get_metadata("0911.3380"), range(8))
        )

    assert call_count == 1
    assert {value["title"] for value in values} == {"Cached paper"}

    manifest = next(
        (tmp_path / "remote-request-cache" / "v1" / "json").glob(
            "inspire-record/*/*/manifest.json"
        )
    )
    entry_dir = manifest.parent
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    entry_dir.joinpath(manifest_value["payload_file"]).write_bytes(
        b'{"tampered":true}'
    )
    cached = RemoteRequestCache(tmp_path)
    with pytest.raises(RemoteCacheError) as error:
        cached.get_json("inspire-record", "arXiv:0911.3380")
    assert error.value.code == "remote_cache_json_corrupt"


def test_json_payload_is_not_published_without_manifest(tmp_path, monkeypatch):
    cache = RemoteRequestCache(tmp_path)
    original = cache._atomic_write

    def interrupted(path, payload):
        if path.name == "manifest.json":
            raise OSError("simulated interruption")
        original(path, payload)

    monkeypatch.setattr(cache, "_atomic_write", interrupted)
    with pytest.raises(OSError, match="simulated"):
        cache.fetch_json("metadata", "request", fetch=lambda: {"ok": True})

    assert cache.get_json("metadata", "request") is None


def test_json_refresh_interruption_preserves_previous_manifest_generation(
    tmp_path, monkeypatch
):
    cache = RemoteRequestCache(tmp_path)
    assert cache.fetch_json(
        "metadata", "request", fetch=lambda: {"generation": "old"}
    ) == {"generation": "old"}

    manifest_path = next(
        (tmp_path / "remote-request-cache" / "v1" / "json").glob(
            "metadata/*/*/manifest.json"
        )
    )
    entry_dir = manifest_path.parent
    old_manifest = manifest_path.read_bytes()
    old_manifest_value = json.loads(old_manifest)
    old_payload_path = entry_dir / old_manifest_value["payload_file"]
    old_payload = old_payload_path.read_bytes()
    original = cache._atomic_write

    def interrupted(path, payload):
        if path.name == "manifest.json":
            raise OSError("simulated refresh interruption")
        original(path, payload)

    monkeypatch.setattr(cache, "_atomic_write", interrupted)
    with pytest.raises(OSError, match="simulated refresh"):
        cache.fetch_json(
            "metadata",
            "request",
            fetch=lambda: {"generation": "new"},
            refresh=True,
        )

    assert manifest_path.read_bytes() == old_manifest
    assert old_payload_path.read_bytes() == old_payload
    assert cache.get_json("metadata", "request") == {"generation": "old"}


def test_remote_source_manifest_revalidates_repository_bytes(tmp_path):
    provider = Ar5ivProvider(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: _response(
                    request, content=b"<html>paper</html>", media_type="text/html"
                )
            )
        ),
    )
    artifact = provider.fetch("0911.3380")
    source_path = next(
        (tmp_path / "source-repository" / "v1" / "html" / "sha256").glob(
            "*/*/source"
        )
    )
    source_path.write_bytes(b"corrupt")

    cache = RemoteRequestCache(tmp_path)
    with pytest.raises(RemoteCacheError) as error:
        cache.get_source(
            "ar5iv-html",
            "0911.3380",
            source_format=SourceFormat.HTML,
            media_type="text/html",
            origin=artifact.origin,
        )
    assert error.value.code == "remote_cache_source_corrupt"


def test_inspire_search_preserves_query_limit_and_normalizes_mathml(tmp_path):
    observed: dict[str, str] = {}
    math = (
        '<math display="inline"><mi>P</mi><mi>l</mi><mi>a</mi>'
        "<mi>n</mi><mi>c</mi><mi>k</mi></math>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.url.params))
        return _response(
            request,
            content=json.dumps(
                {
                    "hits": {
                        "hits": [
                            {
                                "id": "123",
                                "metadata": {
                                    "control_number": 123,
                                    "titles": [{"title": f"Constraints from {math}"}],
                                    "arxiv_eprints": [{"value": "0911.3380v2"}],
                                },
                            }
                        ]
                    }
                }
            ).encode(),
            media_type="application/json",
        )

    provider = InspireProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    values = provider.search_metadata("specific mechanism", limit=7)

    assert observed["q"] == "specific mechanism"
    assert observed["size"] == "7"
    assert values[0]["paper_id"] == "arXiv:0911.3380"
    assert values[0]["title"] == "Constraints from Planck"


def test_inspire_references_are_normalized_enriched_and_cached(tmp_path):
    record = {
        "id": "123",
        "metadata": {
            "control_number": 123,
            "titles": [{"title": "Paper"}],
            "arxiv_eprints": [{"value": "0911.3380v2"}],
            "references": [
                {
                    "record": {
                        "$ref": "https://inspirehep.net/api/literature/456"
                    },
                    "reference": {
                        "title": "Reference",
                        "arxiv_eprint": "0801.0001v3",
                    },
                }
            ],
        },
    }
    reference = {
        "id": "456",
        "metadata": {
            "control_number": 456,
            "titles": [{"title": "Enriched reference"}],
            "authors": [{"full_name": "Ref Author"}],
            "abstracts": [{"value": "Reference abstract."}],
            "arxiv_eprints": [{"value": "0801.0001"}],
            "citation_count": 11,
        },
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        payload = reference if request.url.path.endswith("/456") else record
        return _response(
            request,
            content=json.dumps(payload).encode(),
            media_type="application/json",
        )

    provider = InspireProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    values = provider.get_references("arXiv:0911.3380v2", enrich=True)

    assert values[0]["paper_id"] == "arXiv:0801.0001"
    assert values[0]["title"] == "Enriched reference"
    assert values[0]["authors"] == ["Ref Author"]
    assert values[0]["metadata_enriched"] is True
    assert len(calls) == 2

    cached = InspireProvider(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("cached reference lookup must not use the network")
                )
            )
        ),
    )
    assert cached.get_references("0911.3380", enrich=True)[0]["citation_count"] == 11


def test_inspire_doi_lookup_uses_normalized_content_addressed_request_cache(tmp_path):
    calls: list[str] = []
    record = {
        "id": "123",
        "metadata": {
            "control_number": 123,
            "titles": [{"title": "DOI paper"}],
            "arxiv_eprints": [{"value": "0911.3380"}],
            "dois": [{"value": "10.1000/example"}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path == "/api/literature"
        assert request.url.params["q"] == "doi:10.1000/example"
        return _response(
            request,
            content=json.dumps({"hits": {"hits": [record]}}).encode(),
            media_type="application/json",
        )

    provider = InspireProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    first = provider.get_metadata("doi:10.1000/EXAMPLE")
    second = provider.get_metadata("10.1000/example")

    assert first == second
    assert first["paper_id"] == "arXiv:0911.3380"
    assert first["identifiers"]["doi"] == "10.1000/example"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("limit", "sort"),
    [(1, "mostrecent"), (3, "mostcited")],
)
def test_inspire_citers_use_recid_query_and_request_specific_cache(
    tmp_path, limit, sort
):
    calls: list[str] = []
    record = {
        "id": "123",
        "metadata": {
            "control_number": 123,
            "titles": [{"title": "Paper"}],
            "arxiv_eprints": [{"value": "0911.3380"}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/arxiv/0911.3380"):
            payload = record
        else:
            query = urllib.parse.parse_qs(request.url.query.decode())
            assert query["q"] == ["refersto:recid:123"]
            assert query["size"] == [str(limit)]
            assert query["sort"] == [sort]
            payload = {
                "hits": {
                    "hits": [
                        {
                            "id": str(index),
                            "metadata": {
                                "control_number": index,
                                "titles": [{"title": f"Citer {index}"}],
                            },
                        }
                        for index in range(limit)
                    ]
                }
            }
        return _response(
            request,
            content=json.dumps(payload).encode(),
            media_type="application/json",
        )

    provider = InspireProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    values = provider.get_citers("0911.3380", limit=limit, sort=sort)
    assert [item["title"] for item in values] == [
        f"Citer {index}" for index in range(limit)
    ]
    assert len(calls) == 2

    calls_before_replay = len(calls)
    replay = provider.get_citers("0911.3380", limit=limit, sort=sort)
    assert replay == values
    assert len(calls) == calls_before_replay
