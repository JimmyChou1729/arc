from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arc_paper import (
    ArcPaperService,
    DocumentTarget,
    PaperInputError,
    ReferenceAcquisitionError,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)
from arc_paper.providers.base import ProviderError


HTML = b"""
<html><body>
  <section id="S1"><h2>Introduction</h2>
    <p>The Hamiltonian constraint fixes expansion.</p>
  </section>
  <section id="S2"><h2>Dynamics</h2>
    <p>The Friedmann equation follows.</p>
    <table class="ltx_equation" id="E1">
      <tr><td><math alttext="H^2 = 8\\pi G \\rho/3"></math></td></tr>
    </table>
  </section>
</body></html>
"""


class FakeHtmlProvider:
    def __init__(
        self,
        repository: SourceRepository,
        *,
        provider: str,
        payloads: tuple[bytes, ...] = (HTML,),
        missing: bool = False,
    ):
        self.repository = repository
        self.cache = SimpleNamespace(source_repository=repository)
        self.provider = provider
        self.payloads = payloads
        self.missing = missing
        self.calls: list[tuple[str, bool]] = []

    def fetch(self, paper_id: str, *, refresh: bool = False):
        self.calls.append((paper_id, refresh))
        if self.missing:
            code = (
                "arxiv_html_not_found"
                if self.provider == "arxiv-html"
                else "ar5iv_not_found"
            )
            raise ProviderError(code, "missing")
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return self.repository.store_bytes(
            self.payloads[index],
            source_format=SourceFormat.HTML,
            origin=SourceOrigin(
                SourceOriginKind.REMOTE_PROVIDER,
                provider=self.provider,
                locator=f"https://fixture.invalid/html/{paper_id}",
            ),
        )


class ForbiddenPDF:
    def fetch(self, paper_id: str, *, refresh: bool = False):
        raise AssertionError("deep arXiv document operations must not fetch PDF")


class FakeInspire:
    def get_metadata(self, paper_id: str, *, refresh: bool = False):
        return {}


def _target(reference: str) -> DocumentTarget:
    return DocumentTarget("reference", reference=reference)


def _service(
    tmp_path: Path,
    *,
    official_payloads: tuple[bytes, ...] = (HTML,),
    fallback_payloads: tuple[bytes, ...] = (HTML,),
    official_missing: bool = False,
):
    repository = SourceRepository(tmp_path / "cache")
    official = FakeHtmlProvider(
        repository,
        provider="arxiv-html",
        payloads=official_payloads,
        missing=official_missing,
    )
    ar5iv = FakeHtmlProvider(
        repository,
        provider="ar5iv",
        payloads=fallback_payloads,
    )
    return (
        ArcPaperService(
            repository=repository,
            inspire=FakeInspire(),  # type: ignore[arg-type]
            arxiv_html=official,  # type: ignore[arg-type]
            ar5iv=ar5iv,
            arxiv_pdf=ForbiddenPDF(),
        ),
        official,
        ar5iv,
    )


@pytest.mark.parametrize(
    "identifier",
    (
        "0911.3380",
        "arXiv:0911.3380v2",
        "https://arxiv.org/abs/0911.3380v3",
        "https://arxiv.org/pdf/0911.3380.pdf",
    ),
)
def test_arxiv_toc_normalizes_ids_and_returns_path_free_provenance(
    tmp_path: Path, identifier: str
) -> None:
    service, official, ar5iv = _service(tmp_path)

    result = service.get_table_of_contents(
        _target(identifier), source_format=SourceFormat.HTML
    )

    assert result.source.identity is not None
    assert result.source.identity.arxiv_id == "0911.3380"
    assert result.source.document.source_format is SourceFormat.HTML
    assert len(result.source.document.source_sha256) == 64
    assert len(result.source.document.parsed_document_sha256) == 64
    assert [item.title for item in result.entries] == ["Introduction", "Dynamics"]
    assert official.calls == [("arXiv:0911.3380", False)]
    assert ar5iv.calls == []
    assert "path" not in repr(result).casefold()


def test_arxiv_section_and_search_return_locations_and_digests(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    target = _target("0911.3380")
    section = service.get_section(
        target, "dynamics", source_format=SourceFormat.HTML
    )
    text = service.search_full_text_targets(
        ("Hamiltonian constraint",),
        targets=(target,),
        source_format=SourceFormat.HTML,
        context_lines=0,
    )
    equations = service.search_equation_targets(
        (target,), (r"8\pi G",), source_format=SourceFormat.HTML
    )

    assert section.title == "Dynamics"
    assert section.ordinal == 1
    assert "Friedmann" in section.text
    assert text.occurrences[0].title == "Introduction"
    assert text.occurrences[0].location_id
    assert text.occurrences[0].source_digest == text.documents[0].source.document.source_sha256
    assert text.occurrences[0].document_digest == text.documents[0].source.document.parsed_document_sha256
    assert equations.matches[0].span_id
    assert equations.matches[0].source_digest == equations.documents[0].source.document.source_sha256


def test_arxiv_document_operations_reuse_service_memo_and_refresh_by_content(
    tmp_path: Path,
) -> None:
    changed = HTML.replace(b"fixes expansion", b"determines expansion")
    service, official, ar5iv = _service(
        tmp_path, official_payloads=(HTML, HTML, changed)
    )

    target = _target("0911.3380")
    first = service.get_table_of_contents(target, source_format="html")
    same = service.get_table_of_contents(
        target, source_format="html", refresh=True
    )
    changed_result = service.get_table_of_contents(
        target, source_format="html", refresh=True
    )

    assert first.source.document.parsed_document_sha256 == same.source.document.parsed_document_sha256
    assert first.source.document.parsed_document_sha256 != changed_result.source.document.parsed_document_sha256
    assert official.calls == [
        ("arXiv:0911.3380", False),
        ("arXiv:0911.3380", True),
        ("arXiv:0911.3380", True),
    ]
    assert ar5iv.calls == []


def test_arxiv_document_operations_fall_back_only_after_official_not_found(
    tmp_path: Path,
) -> None:
    service, official, ar5iv = _service(tmp_path, official_missing=True)

    result = service.get_table_of_contents(
        _target("0911.3380"), source_format="html"
    )

    assert result.source.document.source_format is SourceFormat.HTML
    assert official.calls == [("arXiv:0911.3380", False)]
    assert ar5iv.calls == [("arXiv:0911.3380", False)]


def test_arxiv_document_operations_do_not_fall_back_after_official_failure(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")

    class TransientOfficial:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def fetch(self, paper_id: str, *, refresh: bool = False):
            self.calls.append((paper_id, refresh))
            raise ProviderError("arxiv_html_fetch_failed", "temporary", status_code=503)

    official = TransientOfficial()
    fallback = FakeHtmlProvider(repository, provider="ar5iv")
    service = ArcPaperService(
        repository=repository,
        arxiv_html=official,  # type: ignore[arg-type]
        ar5iv=fallback,  # type: ignore[arg-type]
        arxiv_pdf=ForbiddenPDF(),
    )

    with pytest.raises(ProviderError) as error:
        service.get_table_of_contents(_target("0911.3380"), source_format="html")

    assert error.value.code == "arxiv_html_fetch_failed"
    assert official.calls == [("arXiv:0911.3380", False)]
    assert fallback.calls == []


def test_reference_document_errors_are_typed_and_never_fall_back_to_pdf(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    with pytest.raises(ReferenceAcquisitionError) as invalid:
        service.get_table_of_contents(_target("not an exact reference"))
    assert invalid.value.code == "reference_acquisition_unavailable"

    repository = SourceRepository(tmp_path / "missing")
    missing_html = FakeHtmlProvider(
        repository, provider="arxiv-html", missing=True
    )
    missing_ar5iv = FakeHtmlProvider(repository, provider="ar5iv", missing=True)
    missing = ArcPaperService(
        repository=repository,
        inspire=FakeInspire(),  # type: ignore[arg-type]
        arxiv_html=missing_html,  # type: ignore[arg-type]
        ar5iv=missing_ar5iv,  # type: ignore[arg-type]
        arxiv_pdf=ForbiddenPDF(),
    )
    with pytest.raises(PaperInputError) as error:
        missing.search_full_text_targets(
            ("query",),
            targets=(_target("0911.3380"),),
            source_format="html",
        )
    assert error.value.code == "no_document_target_resolved"
    assert "ar5iv_not_found" in str(error.value)
