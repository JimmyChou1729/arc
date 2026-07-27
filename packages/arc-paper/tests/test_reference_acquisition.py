from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from arc_paper import (
    AcquiredReferenceResource,
    EPUB_MEDIA_TYPE,
    CachedResourceRef,
    ReferenceAcquisitionService,
    ReferenceCacheError,
    ReferenceIdentity,
    ReferenceMaterialCache,
    cached_reference_material_from_document,
    cached_reference_material_to_document,
    normalize_reference_title,
)
from arc_paper.providers.base import ProviderError
from arc_paper.providers.crossref import CrossrefProvider
from arc_paper.providers.http import AcquiredHttpResource
from arc_paper.workflows.reference import REFERENCE_OUTPUT_SCHEMA


def test_reference_identity_preserves_every_normalized_doi_and_legacy_first() -> None:
    identity = ReferenceIdentity(
        arxiv_id="arXiv:2401.00001v2",
        dois=(
            "doi:10.1000/PRIMARY",
            "https://doi.org/10.1000/secondary",
            "10.1000/primary",
        ),
        urls=("HTTPS://EXAMPLE.TEST:443/paper#section",),
        title="  A   Paper  ",
    )

    assert identity.arxiv_id == "2401.00001"
    assert identity.dois == ("10.1000/primary", "10.1000/secondary")
    assert identity.doi == "10.1000/primary"
    assert identity.urls == ("https://example.test/paper",)
    assert identity.title == "A Paper"


def test_arbitrary_resource_round_trip_and_all_exact_lookup_aliases(tmp_path) -> None:
    cache = ReferenceMaterialCache(tmp_path)
    identity = ReferenceIdentity(
        dois=("10.1000/one", "10.1000/two"),
        urls=("https://example.test/reference",),
        title="Quantum—Reference: A Test",
    )
    resource = cache.store_resource(
        b"arbitrary bytes",
        media_type="application/x-research-data",
        source_locator="https://example.test/reference",
        filename="data.bin",
    )
    material = cache.store_material(identity, (resource,))

    assert cache.lookup(doi="doi:10.1000/TWO") == material
    assert cache.lookup(url="https://EXAMPLE.test:443/reference#part") == material
    assert cache.lookup(title="quantum reference, a test") == material
    assert cache.lookup(arxiv_id="2401.00001") is None
    assert cache.read_resource(resource) == b"arbitrary bytes"
    encoded = cached_reference_material_to_document(material)
    assert cached_reference_material_from_document(encoded) == material


def test_exact_title_normalization_is_not_fuzzy() -> None:
    assert normalize_reference_title("A—B:  Café") == "a b café"
    assert normalize_reference_title("A B Cafe") != normalize_reference_title(
        "A B Café"
    )


def test_conflicting_strong_identities_are_retained_as_ambiguous(tmp_path) -> None:
    cache = ReferenceMaterialCache(tmp_path)
    first = cache.store_resource(b"first", media_type="text/plain")
    second = cache.store_resource(b"second", media_type="text/plain")
    cache.store_material(
        ReferenceIdentity(arxiv_id="2401.00001", dois=("10.1000/shared",)),
        (first,),
    )
    cache.store_material(
        ReferenceIdentity(arxiv_id="2401.00002", dois=("10.1000/shared",)),
        (second,),
    )

    with pytest.raises(ReferenceCacheError) as error:
        cache.lookup(doi="10.1000/shared")
    assert error.value.code == "reference_ambiguous"
    assert cache.lookup(arxiv_id="2401.00001").resources == (first,)


def test_concurrent_admission_publishes_one_verified_material(tmp_path) -> None:
    cache = ReferenceMaterialCache(tmp_path)
    identity = ReferenceIdentity(dois=("10.1000/concurrent",))

    def store(_: int):
        resource = cache.store_resource(
            b"same bytes", media_type="application/octet-stream"
        )
        return cache.store_material(identity, (resource,))

    with ThreadPoolExecutor(max_workers=8) as pool:
        materials = list(pool.map(store, range(24)))

    assert len({item.resources[0].content_identity for item in materials}) == 1
    found = cache.lookup(doi="10.1000/concurrent")
    assert found is not None
    assert cache.read_resource(found.resources[0]) == b"same bytes"


def test_fabricated_media_type_and_corrupt_bytes_are_rejected(tmp_path) -> None:
    cache = ReferenceMaterialCache(tmp_path)
    resource = cache.store_resource(b"payload", media_type="text/plain")
    fabricated = CachedResourceRef(
        resource.resource_sha256,
        resource.resource_size,
        "application/pdf",
    )
    with pytest.raises(ReferenceCacheError) as mismatch:
        cache.store_material(ReferenceIdentity(title="Mismatch"), (fabricated,))
    assert mismatch.value.code == "reference_resource_mismatch"

    resource_path = (
        tmp_path
        / "reference-material-cache"
        / "v1"
        / "resources"
        / "sha256"
        / resource.resource_sha256[:2]
        / resource.resource_sha256
        / "resource"
    )
    resource_path.write_bytes(b"tampered")
    with pytest.raises(ReferenceCacheError) as corrupt:
        cache.read_resource(resource)
    assert corrupt.value.code == "reference_resource_corrupt"


def test_local_epub_admission_keeps_raw_and_derives_spine_order(tmp_path) -> None:
    epub_path = tmp_path / "book.epub"
    payload = _epub_fixture()
    epub_path.write_bytes(payload)
    service = ReferenceAcquisitionService(cache_root=tmp_path / "cache")

    material = service.admit_reference_file(
        epub_path, ReferenceIdentity(dois=("10.1000/book",))
    )

    assert [item.media_type for item in material.resources] == [
        EPUB_MEDIA_TYPE,
        "text/html",
    ]
    assert material.readable_resource is not None
    assert service.cache.read_resource(material.resources[0]) == payload
    readable = service.cache.read_resource(material.readable_resource).decode()
    assert readable.index("Chapter Two first") < readable.index("Chapter One second")
    assert material.identity.title == "Spine Test"


def test_http_acquisition_is_cache_first_and_preserves_resolved_url(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            content=b"<html><body>paper</body></html>",
            headers={"content-type": "text/html"},
        )

    first = ReferenceAcquisitionService(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    ).acquire_reference("https://example.test/paper#section")
    replay = ReferenceAcquisitionService(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("cache hit must not use HTTP")
                )
            )
        ),
    ).acquire_reference("https://example.test/paper")

    assert replay == first
    assert first.readable_resource is not None
    assert calls == ["https://example.test/paper"]


def test_arxiv_acquisition_enriches_identity_and_replays_reference_cache(
    tmp_path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "inspirehep.net":
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "123",
                    "metadata": {
                        "control_number": 123,
                        "titles": [{"title": "arXiv work"}],
                        "arxiv_eprints": [{"value": "0911.3380"}],
                        "dois": [{"value": "10.1000/arxiv-work"}],
                    },
                },
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            request=request,
            content=b"<html><body>arXiv work</body></html>",
            headers={"content-type": "text/html"},
        )

    first = ReferenceAcquisitionService(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).acquire_reference("arXiv:0911.3380v2")
    replay = ReferenceAcquisitionService(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("reference cache hit must not use HTTP")
                )
            )
        ),
    ).acquire_reference("0911.3380")

    assert replay == first
    assert first.identity.title == "arXiv work"
    assert first.identity.dois == ("10.1000/arxiv-work",)
    assert first.readable_resource is not None
    assert calls == [
        "https://inspirehep.net/api/arxiv/0911.3380",
        "https://arxiv.org/html/0911.3380",
    ]


def test_doi_uses_crossref_fulltext_links_and_caches_all_aliases(tmp_path) -> None:
    class MissingInspire:
        def get_metadata(self, paper_id: str, *, refresh: bool = False):
            raise ProviderError("inspire_not_found", "not in discipline index")

    class FakeCrossref:
        def get_metadata(self, doi: str, *, refresh: bool = False):
            return {
                "doi": "10.1000/work",
                "dois": ["10.1000/work"],
                "title": "Generic DOI work",
                "landing_url": "https://publisher.test/article",
                "links": [
                    {
                        "url": "https://publisher.test/full.epub",
                        "media_type": EPUB_MEDIA_TYPE,
                    }
                ],
            }

    class FakeHttp:
        calls: list[str] = []

        def fetch(self, url: str):
            self.calls.append(url)
            return AcquiredHttpResource(
                payload=_epub_fixture(),
                media_type=EPUB_MEDIA_TYPE,
                requested_url=url,
                resolved_url=url,
                filename="full.epub",
            )

    http = FakeHttp()
    service = ReferenceAcquisitionService(
        cache_root=tmp_path,
        inspire=MissingInspire(),
        crossref=FakeCrossref(),
        http=http,
    )
    material = service.acquire_reference("doi:10.1000/WORK")

    assert material.identity.dois == ("10.1000/work",)
    assert "https://publisher.test/full.epub" in material.identity.urls
    assert "https://publisher.test/article" in material.identity.urls
    assert material.readable_resource is not None
    assert http.calls == ["https://publisher.test/full.epub"]
    assert service.lookup_cached_reference(
        url="https://publisher.test/article"
    ) == material


def test_custom_backend_is_minimal_and_cache_replay_skips_backend(tmp_path) -> None:
    class Backend:
        calls = 0

        def acquire(self, identity: ReferenceIdentity, *, refresh: bool = False):
            self.calls += 1
            return AcquiredReferenceResource(
                payload=b"authorized result",
                media_type="text/plain",
                source_locator="backend:item",
                identity=ReferenceIdentity(
                    dois=identity.dois,
                    title="Resolved title",
                ),
            )

    backend = Backend()
    service = ReferenceAcquisitionService(
        cache_root=tmp_path, backends=(backend,)
    )
    first = service.acquire_reference("10.1000/backend")
    second = service.acquire_reference("doi:10.1000/BACKEND")

    assert first == second
    assert backend.calls == 1


def test_crossref_provider_normalizes_metadata_links_and_replays_cache(
    tmp_path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            json={
                "message": {
                    "DOI": "10.1000/WORK",
                    "title": ["Generic work"],
                    "URL": "https://publisher.test/work",
                    "author": [{"given": "A.", "family": "Researcher"}],
                    "published": {"date-parts": [[2025, 3, 4]]},
                    "link": [
                        {
                            "URL": "https://publisher.test/work.epub",
                            "content-type": EPUB_MEDIA_TYPE,
                        }
                    ],
                }
            },
            headers={"content-type": "application/json"},
        )

    provider = CrossrefProvider(
        cache_root=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    first = provider.get_metadata("doi:10.1000/work")
    replay = CrossrefProvider(
        cache_root=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("Crossref cache hit must not use HTTP")
                )
            )
        ),
    ).get_metadata("10.1000/WORK")

    assert replay == first
    assert first["doi"] == "10.1000/work"
    assert first["dois"] == ["10.1000/work"]
    assert first["authors"] == ["A. Researcher"]
    assert first["published"] == "2025-03-04"
    assert first["links"] == [
        {
            "url": "https://publisher.test/work.epub",
            "media_type": EPUB_MEDIA_TYPE,
        }
    ]
    assert calls == ["https://api.crossref.org/works/10.1000%2Fwork"]


def test_reference_collection_schemas_have_no_maximum_item_quota() -> None:
    from arc_paper import get_operation

    references = get_operation("get-references").output_codec.schema
    candidates = REFERENCE_OUTPUT_SCHEMA["properties"]["candidates"]

    assert references["type"] == "array"
    assert "maxItems" not in references
    assert "maxItems" not in references["items"]["properties"]["dois"]
    assert candidates["type"] == "array"
    assert "maxItems" not in candidates


def _epub_fixture() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", EPUB_MEDIA_TYPE)
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles>
                <rootfile full-path="OPS/package.opf"
                  media-type="application/oebps-package+xml"/>
              </rootfiles>
            </container>""",
        )
        archive.writestr(
            "OPS/package.opf",
            """<?xml version="1.0"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
              <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                <dc:title>Spine Test</dc:title>
              </metadata>
              <manifest>
                <item id="one" href="one.xhtml"
                  media-type="application/xhtml+xml"/>
                <item id="two" href="two.xhtml"
                  media-type="application/xhtml+xml"/>
              </manifest>
              <spine><itemref idref="two"/><itemref idref="one"/></spine>
            </package>""",
        )
        archive.writestr(
            "OPS/one.xhtml",
            "<html><body><h2>Chapter One second</h2></body></html>",
        )
        archive.writestr(
            "OPS/two.xhtml",
            "<html><body><h2>Chapter Two first</h2></body></html>",
        )
    return output.getvalue()
