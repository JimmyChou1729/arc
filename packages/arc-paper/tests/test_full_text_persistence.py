from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from arc_paper import (
    ArcPaperService,
    PDFTextLayer,
    PaperParserService,
    PdftotextExtractor,
    SourceBundle,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    ValidationPolicy,
)
from arc_paper._full_text_catalog import FullTextCatalog
from arc_paper._parsed_document_cache import PARSER_CONTRACT, ParsedDocumentCache
from arc_paper.parse.parser import ParseError, parse_artifact_bytes
from arc_paper.providers import Ar5ivProvider
from arc_paper.providers.base import ProviderError
from arc_paper.rich_document import RichDocumentParserService


parser_service_module = importlib.import_module(
    "arc_document.parse.service"
)


class FakePDFTextExtractor:
    def __init__(
        self,
        contract_id: str,
        pages: tuple[str, ...],
        warning: str = "",
    ):
        self.contract_id = contract_id
        self.pages = pages
        self.warning = warning
        self.calls = 0

    def extract(self, payload: bytes) -> PDFTextLayer:
        self.calls += 1
        return PDFTextLayer(self.pages, self.warning)


def _store(
    repository: SourceRepository,
    payload: bytes,
    source_format: SourceFormat,
    *,
    arxiv_id: str = "",
):
    return repository.store_bytes(
        payload,
        source_format=source_format,
        origin=SourceOrigin(
            (
                SourceOriginKind.REMOTE_PROVIDER
                if arxiv_id
                else SourceOriginKind.LOCAL_IMPORT
            ),
            provider="fixture" if arxiv_id else "",
            metadata=(
                {
                    "arxiv_id": arxiv_id,
                    "document_id": f"arXiv:{arxiv_id}",
                }
                if arxiv_id
                else {}
            ),
        ),
    )


@pytest.mark.parametrize(
    ("source_format", "payload"),
    (
        (SourceFormat.HTML, b"<h1>HTML title</h1><p>body</p>"),
        (SourceFormat.MARKDOWN, b"# Markdown title\nbody\n"),
        (SourceFormat.TEX, b"\\section{TeX title}\nbody\n"),
        (SourceFormat.PDF, b"%PDF cache fixture"),
    ),
)
def test_public_parser_materializes_every_supported_format_and_reuses_across_services(
    tmp_path: Path,
    source_format: SourceFormat,
    payload: bytes,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, payload, source_format)
    first_extractor = FakePDFTextExtractor(
        "arc.paper.tests.persistence_pdf.v1", ("PDF title\nbody",)
    )
    first = PaperParserService(
        repository, pdf_text_extractor=first_extractor
    ).parse_source(source)
    second_extractor = FakePDFTextExtractor(
        "arc.paper.tests.persistence_pdf.v1", ("must not be used",)
    )
    second = PaperParserService(
        repository, pdf_text_extractor=second_extractor
    ).parse_source(source)

    assert first.document_digest == second.document_digest
    assert second_extractor.calls == 0
    assert first_extractor.calls == (1 if source_format is SourceFormat.PDF else 0)
    entry = FullTextCatalog(repository.root).current_entries()[0]
    assert entry.kind == "local"
    assert entry.representations[0].document_digest == first.document_digest


def test_parser_contract_rebuilds_from_legacy_derived_entry_without_removing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    payload = b"<h1>Current title</h1><p>body</p>"
    source = _store(repository, payload, SourceFormat.HTML)
    legacy = ParsedDocumentCache(
        repository=repository,
        parser_contract="arc.document.parser.v3",
    )
    legacy_document, _ = legacy.get_or_parse(
        source,
        lambda artifact: parse_artifact_bytes(
            artifact, repository.read_bytes(artifact)
        ),
    )
    legacy_key = legacy.cache_key(source)
    legacy_path = legacy._entry_dir(legacy_key)
    legacy_document_bytes = (legacy_path / "document.json").read_bytes()
    legacy_manifest_bytes = (legacy_path / "manifest.json").read_bytes()
    catalog = FullTextCatalog(repository.root)
    catalog.record(
        source,
        legacy_document,
        parser_contract=legacy.parser_contract,
        parsed_cache_key=legacy_key,
    )
    assert PARSER_CONTRACT == "arc.document.parser.v7"

    current_calls: list[str] = []
    original_parse = parser_service_module.parse_artifact_bytes

    def parse_current(artifact, payload, **kwargs):
        current_calls.append(artifact.artifact_digest)
        return original_parse(artifact, payload, **kwargs)

    monkeypatch.setattr(parser_service_module, "parse_artifact_bytes", parse_current)
    current = PaperParserService(repository).parse_source(source)
    current_cache = ParsedDocumentCache(repository=repository)
    current_key = current_cache.cache_key(source)
    representation = catalog.current_entries()[0].representations[0]

    assert current_calls == [source.artifact_digest]
    assert current_key != legacy_key
    assert current_cache._entry_dir(current_key).joinpath("document.json").is_file()
    assert (legacy_path / "document.json").read_bytes() == legacy_document_bytes
    assert (legacy_path / "manifest.json").read_bytes() == legacy_manifest_bytes
    assert repository.read_bytes(source) == payload
    assert current.document_digest == representation.document_digest
    assert representation.parser_contract == PARSER_CONTRACT
    assert representation.parsed_cache_key == current_key
    assert (
        json.loads(
            (current_cache._entry_dir(current_key) / "manifest.json").read_text()
        )["parser_contract"]
        == PARSER_CONTRACT
    )
    assert (
        current_cache.read_verified_by_key(
            current_key,
            expected_source_identity=representation.source_identity,
            expected_parser_contract=representation.parser_contract,
            expected_document_digest=representation.document_digest,
        ).document_digest
        == current.document_digest
    )


def test_pdf_extractor_contract_is_required_and_isolates_cached_documents(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"%PDF contract fixture", SourceFormat.PDF)

    class MissingContract:
        def extract(self, payload: bytes) -> PDFTextLayer:
            return PDFTextLayer(("unused",))

    with pytest.raises(ParseError) as error:
        PaperParserService(
            repository, pdf_text_extractor=MissingContract()
        ).parse_source(source)
    assert error.value.code == "pdf_extractor_contract_missing"
    assert FullTextCatalog(repository.root).current_entries() == ()

    first_extractor = FakePDFTextExtractor(
        "arc.paper.tests.extractor.first.v1", ("First text",)
    )
    second_extractor = FakePDFTextExtractor(
        "arc.paper.tests.extractor.second.v1", ("Second text",)
    )
    first_service = PaperParserService(
        repository, pdf_text_extractor=first_extractor
    )
    second_service = PaperParserService(
        repository, pdf_text_extractor=second_extractor
    )

    first = first_service.parse_source(source)
    second = second_service.parse_source(source)

    assert first.document_digest != second.document_digest
    assert first_extractor.calls == second_extractor.calls == 1
    contracts = {
        path.parent.name
        for path in (repository.root / "parsed-document-cache" / "v1").glob(
            "sha256/*/*/document.json"
        )
    }
    assert len(contracts) == 2


@pytest.mark.parametrize(
    ("raised", "code"),
    (
        (FileNotFoundError(), "pdf_text_extractor_unavailable"),
        (
            subprocess.TimeoutExpired(["pdftotext"], 1),
            "pdf_text_extraction_timeout",
        ),
    ),
)
def test_environmental_pdftotext_failure_is_typed_and_not_cached(
    tmp_path: Path,
    monkeypatch,
    raised: Exception,
    code: str,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"%PDF environment fixture", SourceFormat.PDF)

    def fail(*args, **kwargs):
        raise raised

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ParseError) as error:
        PaperParserService(
            repository, pdf_text_extractor=PdftotextExtractor()
        ).parse_source(source)

    assert error.value.code == code
    assert FullTextCatalog(repository.root).current_entries() == ()
    assert not tuple(
        (repository.root / "parsed-document-cache" / "v1").glob(
            "sha256/*/*/manifest.json"
        )
    )


def test_valid_pdf_without_text_is_cached_with_warning(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"%PDF image only", SourceFormat.PDF)
    extractor = FakePDFTextExtractor(
        "arc.paper.tests.no_text.v1",
        (),
        "PDF contains no extractable text layer",
    )

    document = PaperParserService(
        repository, pdf_text_extractor=extractor
    ).parse_source(source)

    assert document.metadata["text_layer"] is False
    assert "no extractable text layer" in document.warnings[0]
    assert len(FullTextCatalog(repository.root).current_entries()) == 1


def test_builtin_empty_pdftotext_output_is_a_cacheable_missing_text_layer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"%PDF empty output", SourceFormat.PDF)

    def empty_output(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", empty_output)
    document = PaperParserService(
        repository, pdf_text_extractor=PdftotextExtractor()
    ).parse_source(source)

    assert document.metadata["text_layer"] is False
    assert document.warnings == ("PDF contains no extractable text layer",)
    assert len(FullTextCatalog(repository.root).current_entries()) == 1


def test_catalog_refreshes_format_pointer_and_keeps_arxiv_representations_separate(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    service = PaperParserService(
        repository,
        pdf_text_extractor=FakePDFTextExtractor(
            "arc.paper.tests.catalog_pdf.v1", ("PDF title",)
        ),
    )
    stale_html = _store(
        repository,
        b"<h1>Old title</h1>",
        SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    fresh_html = _store(
        repository,
        b"<h1>New title</h1>",
        SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    pdf = _store(
        repository,
        b"%PDF catalog fixture",
        SourceFormat.PDF,
        arxiv_id="0911.3380",
    )
    stale_document = service.parse_source(stale_html)
    fresh_document = service.parse_source(fresh_html)
    pdf_document = service.parse_source(pdf)

    entries = FullTextCatalog(repository.root).current_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "arxiv"
    assert entry.paper_ids == ("arXiv:0911.3380",)
    by_format = {item.source_format: item for item in entry.representations}
    assert set(by_format) == {"html", "pdf"}
    assert by_format["html"].document_digest == fresh_document.document_digest
    assert by_format["html"].document_digest != stale_document.document_digest
    assert by_format["pdf"].document_digest == pdf_document.document_digest
    locator_text = next(
        (repository.root / "document-full-text-catalog" / "v2").glob(
            "entries/*/*/locator.json"
        )
    ).read_text(encoding="utf-8")
    assert "Old title" not in locator_text
    assert "New title" not in locator_text
    assert "document.json" not in locator_text
    assert str(repository.root) not in locator_text


def test_v1_catalog_layout_is_ignored(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(
        repository,
        b"# Current title\nbody\n",
        SourceFormat.MARKDOWN,
    )
    PaperParserService(repository).parse_source(source)
    current_root = repository.root / "document-full-text-catalog" / "v2"
    current_root.rename(current_root.with_name("v1"))

    catalog = FullTextCatalog(repository.root)
    assert catalog.current_entries() == ()
    assert catalog.admin_entries() == ()


def test_current_catalog_requires_admin_and_parse_repairs_it(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(
        repository,
        b"# Current title\nbody\n",
        SourceFormat.MARKDOWN,
    )
    service = PaperParserService(repository)
    service.parse_source(source)
    locator = next(
        (repository.root / "document-full-text-catalog" / "v2").glob(
            "entries/*/*/locator.json"
        )
    )
    (locator.parent / "admin.json").unlink()

    catalog = FullTextCatalog(repository.root)
    assert catalog.current_entries() == ()
    assert catalog.admin_entries() == ()

    service.parse_source(source)

    assert len(catalog.current_entries()) == 1
    assert len(catalog.admin_entries()) == 1


def test_catalog_entry_lock_preserves_concurrent_html_and_pdf_updates(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    html = _store(
        repository,
        b"<h1>Concurrent title</h1>",
        SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    pdf = _store(
        repository,
        b"%PDF concurrent fixture",
        SourceFormat.PDF,
        arxiv_id="0911.3380",
    )

    def materialize(source):
        PaperParserService(
            repository,
            pdf_text_extractor=FakePDFTextExtractor(
                "arc.paper.tests.concurrent_pdf.v1", ("Concurrent title",)
            ),
        ).parse_source(source)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(materialize, (html, pdf)))

    entry = FullTextCatalog(repository.root).current_entries()[0]
    assert {item.source_format for item in entry.representations} == {
        "html",
        "pdf",
    }


def test_catalog_selected_cache_read_revalidates_all_digests(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(
        repository, b"# Verified candidate\nbody\n", SourceFormat.MARKDOWN
    )
    service = PaperParserService(repository)
    document = service.parse_source(source)
    representation = (
        FullTextCatalog(repository.root)
        .current_entries()[0]
        .representations[0]
    )
    cache = ParsedDocumentCache(
        repository=repository,
        parser_contract=representation.parser_contract,
    )
    candidate_path = cache.candidate_document_path_by_key(
        representation.parsed_cache_key,
        expected_source_identity=representation.source_identity,
        expected_parser_contract=representation.parser_contract,
    )
    verified = cache.read_verified_by_key(
        representation.parsed_cache_key,
        expected_source_identity=representation.source_identity,
        expected_parser_contract=representation.parser_contract,
        expected_document_digest=representation.document_digest,
    )

    assert candidate_path.name == "document.json"
    assert verified.document_digest == document.document_digest
    candidate_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="verification"):
        cache.read_verified_by_key(
            representation.parsed_cache_key,
            expected_source_identity=representation.source_identity,
            expected_parser_contract=representation.parser_contract,
            expected_document_digest=representation.document_digest,
        )


def test_public_import_failure_retains_source_for_offline_retry(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    source_path = tmp_path / "project.tex"
    payload = b"\\input{chapter}\n"
    source_path.write_bytes(payload)
    service = ArcPaperService(cache_root=cache_root)

    with pytest.raises(ParseError) as error:
        service.import_source(source_path)

    assert error.value.code == "unsupported_tex_project"
    digest = hashlib.sha256(payload).hexdigest()
    retained = service.repository.get(SourceFormat.TEX, digest)
    assert service.repository.read_bytes(retained) == payload
    assert FullTextCatalog(cache_root).current_entries() == ()


def test_parse_local_preserves_nonfatal_and_disabled_validator_semantics(
    tmp_path: Path,
) -> None:
    primary_path = tmp_path / "primary.md"
    invalid_pdf_path = tmp_path / "invalid.pdf"
    primary_path.write_bytes(b"# Primary\nbody\n")
    invalid_pdf_path.write_bytes(b"not a PDF")

    nonfatal = ArcPaperService(cache_root=tmp_path / "nonfatal").parse_local(
        primary_path,
        validator_paths=(invalid_pdf_path,),
    )
    assert nonfatal.document.sections[0].title == "Primary"
    assert nonfatal.report.entries[0].status.value == "unreviewed"
    assert "pdf_invalid" in nonfatal.report.entries[0].message
    assert len(FullTextCatalog(tmp_path / "nonfatal").current_entries()) == 1

    disabled = ArcPaperService(cache_root=tmp_path / "disabled").parse_local(
        primary_path,
        validator_paths=(invalid_pdf_path,),
        policy=ValidationPolicy.NONE,
    )
    assert disabled.report.entries[0].message == "validation was explicitly disabled"
    assert len(FullTextCatalog(tmp_path / "disabled").current_entries()) == 1


def test_public_fetch_failure_retains_source_and_remote_mapping(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            content=b"\xff",
            headers={"content-type": "text/html"},
        )

    cache_root = tmp_path / "cache"
    repository = SourceRepository(cache_root)
    provider = Ar5ivProvider(
        cache_root=cache_root,
        source_repository=repository,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    class MissingOfficial:
        def fetch(self, paper_id: str, *, refresh: bool = False):
            raise ProviderError("arxiv_html_not_found", "fixture")

    service = ArcPaperService(
        repository=repository,
        arxiv_html=MissingOfficial(),  # type: ignore[arg-type]
        ar5iv=provider,
    )

    for _ in range(2):
        with pytest.raises(ParseError) as error:
            service.fetch_arxiv_auto("0911.3380")
        assert error.value.code == "source_encoding_invalid"

    assert calls == 1
    assert tuple(
        (cache_root / "source-repository" / "v1" / "html").glob(
            "sha256/*/*/manifest.json"
        )
    )
    assert FullTextCatalog(cache_root).current_entries() == ()


def test_metadata_reference_and_citer_operations_do_not_parse_full_text(
    tmp_path: Path,
) -> None:
    class MetadataOnly:
        def get_metadata(self, paper_id: str, *, refresh: bool = False):
            return {
                "title": "Metadata title",
                "abstract": "Metadata abstract",
                "authors": ["A. Author"],
            }

        def get_references(
            self,
            paper_id: str,
            *,
            refresh: bool = False,
            enrich: bool = False,
        ):
            return [{"paper_id": "arXiv:0911.3380"}]

        def get_citers(
            self,
            paper_id: str,
            *,
            refresh: bool = False,
            limit: int = 1000,
            sort: str = "mostrecent",
        ):
            return [{"paper_id": "arXiv:1201.0001"}]

        def get_citer_count(self, paper_id: str, *, refresh: bool = False):
            return 1

    cache_root = tmp_path / "cache"
    service = ArcPaperService(cache_root=cache_root, inspire=MetadataOnly())

    assert service.get_title("0911.3380") == "Metadata title"
    assert service.get_abstract("0911.3380") == "Metadata abstract"
    assert service.get_authors("0911.3380") == ["A. Author"]
    assert service.get_references("0911.3380")
    assert service.get_citers("0911.3380")
    assert service.get_citer_count("0911.3380") == 1
    assert FullTextCatalog(cache_root).current_entries() == ()
    assert not (cache_root / "parsed-document-cache").exists()


def test_rich_document_materializes_standard_primary_and_pdf_validator(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    primary = _store(
        repository, b"# Rich title\nBody text.\n", SourceFormat.MARKDOWN
    )
    no_validator = RichDocumentParserService(repository)
    no_validator.parse(SourceBundle(primary=primary))
    entries = FullTextCatalog(repository.root).current_entries()
    assert entries == ()

    pdf = _store(repository, b"%PDF rich fixture", SourceFormat.PDF)
    validated = RichDocumentParserService(
        repository,
        pdf_text_extractor=FakePDFTextExtractor(
            "arc.paper.tests.rich_pdf.v1", ("Rich title\nBody text.",)
        ),
    )
    validated.parse(SourceBundle(primary=primary, validators=(pdf,)))

    formats = {
        representation.source_format
        for entry in FullTextCatalog(repository.root).current_entries()
        for representation in entry.representations
    }
    assert formats == {"markdown", "pdf"}
