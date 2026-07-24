from __future__ import annotations

from pathlib import Path

import pytest

from arc_paper import (
    ArcPaperService,
    PaperInputError,
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


class FakeAr5iv:
    def __init__(self, repository: SourceRepository, payloads: tuple[bytes, ...] = (HTML,)):
        self.repository = repository
        self.payloads = payloads
        self.calls: list[tuple[str, bool]] = []

    def fetch(self, paper_id: str, *, refresh: bool = False):
        self.calls.append((paper_id, refresh))
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return self.repository.store_bytes(
            self.payloads[index],
            source_format=SourceFormat.HTML,
            origin=SourceOrigin(
                SourceOriginKind.REMOTE_PROVIDER,
                provider="ar5iv",
                locator=f"https://ar5iv.labs.arxiv.org/html/{paper_id}",
            ),
        )


class ForbiddenPDF:
    def fetch(self, paper_id: str, *, refresh: bool = False):
        raise AssertionError("deep arXiv document operations must not fetch PDF")


def _service(tmp_path: Path, *, payloads: tuple[bytes, ...] = (HTML,)):
    repository = SourceRepository(tmp_path / "cache")
    ar5iv = FakeAr5iv(repository, payloads)
    return (
        ArcPaperService(
            repository=repository,
            ar5iv=ar5iv,
            arxiv_pdf=ForbiddenPDF(),
        ),
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
    service, ar5iv = _service(tmp_path)

    result = service.get_arxiv_table_of_contents(identifier)

    assert result.provenance.canonical_arxiv_id == "arXiv:0911.3380"
    assert result.provenance.provider == "ar5iv"
    assert result.provenance.source_format == "html"
    assert len(result.provenance.source_digest) == 64
    assert len(result.provenance.document_digest) == 64
    assert [item.title for item in result.entries] == ["Introduction", "Dynamics"]
    assert ar5iv.calls == [("arXiv:0911.3380", False)]
    assert "path" not in repr(result).casefold()


def test_arxiv_section_and_search_return_locations_and_digests(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    section = service.get_arxiv_section("0911.3380", "dynamics")
    text = service.search_arxiv_full_text(
        "0911.3380", "Hamiltonian constraint", context_lines=0
    )
    equations = service.search_arxiv_equations("0911.3380", r"8\pi G")

    assert section.title == "Dynamics"
    assert section.ordinal == 1
    assert "Friedmann" in section.text
    assert text.matches[0].title == "Introduction"
    assert text.matches[0].location_id
    assert text.matches[0].source_digest == text.provenance.source_digest
    assert text.matches[0].document_digest == text.provenance.document_digest
    assert equations.matches[0].span_id
    assert equations.matches[0].source_digest == equations.provenance.source_digest


def test_arxiv_document_operations_reuse_service_memo_and_refresh_by_content(
    tmp_path: Path,
) -> None:
    changed = HTML.replace(b"fixes expansion", b"determines expansion")
    service, ar5iv = _service(tmp_path, payloads=(HTML, HTML, changed))

    first = service.get_arxiv_table_of_contents("0911.3380")
    same = service.get_arxiv_table_of_contents("0911.3380", refresh=True)
    changed_result = service.get_arxiv_table_of_contents(
        "0911.3380", refresh=True
    )

    assert first.provenance.document_digest == same.provenance.document_digest
    assert first.provenance.document_digest != changed_result.provenance.document_digest
    assert ar5iv.calls == [
        ("arXiv:0911.3380", False),
        ("arXiv:0911.3380", True),
        ("arXiv:0911.3380", True),
    ]


def test_arxiv_document_errors_are_typed_and_never_fall_back_to_pdf(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(PaperInputError) as invalid:
        service.get_arxiv_table_of_contents("doi:10.1000/example")
    assert invalid.value.code == "not_arxiv_id"

    class MissingAr5iv:
        def fetch(self, paper_id: str, *, refresh: bool = False):
            raise ProviderError("ar5iv_not_found", "missing")

    repository = SourceRepository(tmp_path / "missing")
    missing = ArcPaperService(
        repository=repository,
        ar5iv=MissingAr5iv(),
        arxiv_pdf=ForbiddenPDF(),
    )
    with pytest.raises(ProviderError) as error:
        missing.search_arxiv_full_text("0911.3380", "query")
    assert error.value.code == "ar5iv_not_found"
