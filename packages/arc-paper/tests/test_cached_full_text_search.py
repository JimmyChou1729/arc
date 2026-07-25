from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import arc_paper.cached_full_text_search as cached_search_module
from arc_paper import (
    CachedFullTextContextStatus,
    CachedFullTextSearchError,
    CachedFullTextSearchMode,
    PDFTextLayer,
    PaperParserService,
    ParsedDocument,
    ParsedPage,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)
from arc_paper._full_text_catalog import FullTextCatalog
from arc_paper._parsed_document_cache import ParsedDocumentCache
from arc_paper._ripgrep import RipgrepCandidateSelector, RipgrepError
from arc_paper.cached_full_text_search import CachedFullTextSearcher
from arc_paper.parse.parser import parse_artifact_bytes
from arc_paper.registry import OperationRequestError, get_operation, to_json_value


class AllCandidates:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], tuple[Path, ...], bool]] = []

    def ensure_available(self) -> None:
        pass

    def files_with_matches(
        self,
        patterns,
        paths,
        *,
        case_sensitive: bool,
    ) -> tuple[Path, ...]:
        values = tuple(Path(item) for item in paths)
        self.calls.append((tuple(patterns), values, case_sensitive))
        return values


class NoCandidatesExpected:
    def ensure_available(self) -> None:
        pass

    def files_with_matches(self, patterns, paths, *, case_sensitive: bool):
        raise AssertionError("ripgrep must receive only current catalog candidates")


class FakePDFTextExtractor:
    contract_id = "arc.paper.tests.cached_search_pdf.v1"

    def __init__(self, pages: tuple[str, ...]):
        self.pages = pages

    def extract(self, payload: bytes) -> PDFTextLayer:
        return PDFTextLayer(self.pages)


def _store(
    repository: SourceRepository,
    payload: bytes,
    source_format: SourceFormat = SourceFormat.MARKDOWN,
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
            metadata={"arxiv_id": arxiv_id} if arxiv_id else {},
        ),
    )


def _materialize(
    repository: SourceRepository,
    payload: bytes,
    *,
    source_format: SourceFormat = SourceFormat.MARKDOWN,
    arxiv_id: str = "",
    pdf_pages: tuple[str, ...] = (),
):
    source = _store(
        repository,
        payload,
        source_format,
        arxiv_id=arxiv_id,
    )
    service = PaperParserService(
        repository,
        pdf_text_extractor=FakePDFTextExtractor(pdf_pages),
    )
    return source, service.parse_source(source)


def _searcher(repository: SourceRepository, selector=None) -> CachedFullTextSearcher:
    return CachedFullTextSearcher(
        repository.root,
        candidate_selector=selector or AllCandidates(),
    )


def test_literal_or_equivalent_whitespace_case_and_every_occurrence(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        (
            "# Paper Alpha\n"
            "heavy field first\n"
            "HEAVY FIELD second\n"
            "heavy\\nfield is literal\n"
            "heavy\nfield crosses a line\n"
            "massive    exchange and a+b\n"
        ).encode(),
    )
    selector = AllCandidates()

    result = CachedFullTextSearcher(
        repository.root, candidate_selector=selector
    ).search(("heavy field", "massive exchange", "a+b"), limit=20)

    assert result.mode is CachedFullTextSearchMode.OCCURRENCES
    assert result.total_occurrences == 5
    assert result.matched_document_count == 1
    assert [item.line for item in result.occurrences] == [2, 3, 5, 7, 7]
    assert [item.column for item in result.occurrences] == [1, 1, 1, 1, 25]
    assert [item.matched_terms for item in result.occurrences] == [
        ("heavy field",),
        ("heavy field",),
        ("heavy field",),
        ("massive exchange",),
        ("a+b",),
    ]
    assert all(item.context == "" for item in result.occurrences)
    patterns, _, case_sensitive = selector.calls[0]
    assert len(patterns) == 3
    assert r"\+" in patterns[2]
    assert case_sensitive is False

    exact = _searcher(repository).search(
        ("HEAVY FIELD",), case_sensitive=True
    )
    assert exact.total_occurrences == 1
    assert exact.occurrences[0].line == 3


def test_case_insensitive_term_deduplication_preserves_distinct_regex_terms(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(repository, "# Case Folding\nß ss\n".encode())

    result = _searcher(repository).search(("ß", "ss", "ß"))

    assert result.terms == ("ß", "ss")
    assert result.total_occurrences == 2
    assert [item.column for item in result.occurrences] == [1, 3]
    assert [item.matched_terms for item in result.occurrences] == [
        ("ß",),
        ("ss",),
    ]


def test_overlapping_occurrences_are_all_returned(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(repository, b"# Overlap\nbanana\n")

    result = _searcher(repository).search(("ana",))

    assert result.total_occurrences == 2
    assert [item.line for item in result.occurrences] == [2, 2]
    assert [item.column for item in result.occurrences] == [2, 4]


def test_nested_html_child_projection_is_not_counted_in_ancestors(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        b"""
        <article>
          <h1>Paper Title</h1>
          <section>
            <h2>Parent</h2>
            <p>parent-only phrase</p>
            <section>
              <h3>Child</h3>
              <p>child-only needle and legitimate repeat</p>
            </section>
            <p>parent-tail phrase</p>
          </section>
          <section>
            <h2>Sibling</h2>
            <p>legitimate repeat</p>
          </section>
        </article>
        """,
        source_format=SourceFormat.HTML,
        arxiv_id="0911.3380",
    )

    child = _searcher(repository).search(("child-only needle",))
    repeated = _searcher(repository).search(("legitimate repeat",))
    parent = _searcher(repository).search(
        ("parent-only phrase", "parent-tail phrase")
    )

    assert child.total_occurrences == 1
    assert child.occurrences[0].title == "Child"
    assert repeated.total_occurrences == 2
    assert [item.title for item in repeated.occurrences] == [
        "Child",
        "Sibling",
    ]
    assert parent.total_occurrences == 2
    assert [item.title for item in parent.occurrences] == [
        "Parent",
        "Parent",
    ]


def test_nested_html_tokenizes_each_parent_projection_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        b"""
        <article>
          <h1>Paper</h1>
          <section>
            <h2>Parent</h2>
            <section><h3>One</h3><p>first child needle</p></section>
            <section><h3>Two</h3><p>second child needle</p></section>
            <section><h3>Three</h3><p>third child needle</p></section>
          </section>
        </article>
        """,
        source_format=SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    original_finditer = cached_search_module.re.finditer
    tokenized_texts: list[str] = []

    def tracked_finditer(pattern: str, text: str):
        tokenized_texts.append(text)
        return original_finditer(pattern, text)

    monkeypatch.setattr(
        cached_search_module.re,
        "finditer",
        tracked_finditer,
    )

    result = _searcher(repository).search(("child needle",))

    assert result.total_occurrences == 3
    assert len(tokenized_texts) == 2


def test_non_html_sections_keep_legitimate_repeated_child_projection(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        b"""# Parent
```
## Child
legitimate repeat
```
## Child
legitimate repeat
""",
    )

    result = _searcher(repository).search(("legitimate repeat",))

    assert result.total_occurrences == 2
    assert [item.title for item in result.occurrences] == [
        "Parent",
        "Child",
    ]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_real_ripgrep_filters_canonical_json_for_literal_and_cross_line_terms(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        (
            "# Real Adapter\nheavy\nfield and a+b\n"
            "nonbreaking\u00a0space\nİstanbul marker\n"
        ).encode(),
    )
    candidate = next(
        repository.root.glob("parsed-document-cache/v1/sha256/*/*/document.json")
    )

    result = CachedFullTextSearcher(repository.root).search(
        (
            "heavy field",
            "a+b",
            "nonbreaking space",
            "istanbul marker",
        )
    )
    case_sensitive = CachedFullTextSearcher(repository.root).search(
        ("istanbul marker",),
        case_sensitive=True,
    )

    assert b"nonbreaking\xc2\xa0space" in candidate.read_bytes()
    assert result.total_occurrences == 4
    assert {item.matched_terms for item in result.occurrences} == {
        ("heavy field",),
        ("a+b",),
        ("nonbreaking space",),
        ("istanbul marker",),
    }
    assert case_sensitive.total_occurrences == 0


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_real_ripgrep_filters_json_escaped_vertical_tab(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"source bytes")
    document = ParsedDocument(
        source=source,
        pages=(ParsedPage(1, "vertical\vtab"),),
    )
    cache = ParsedDocumentCache(repository=repository)
    cached, _ = cache.get_or_parse(source, lambda artifact: document)
    FullTextCatalog(repository.root).record(
        source,
        cached,
        parser_contract=cache.parser_contract,
        parsed_cache_key=cache.cache_key(source),
    )
    candidate = cache.candidate_document_path_by_key(
        cache.cache_key(source),
        expected_source_identity={
            "source_format": source.source_format.value,
            "media_type": source.media_type,
            "artifact_digest": source.artifact_digest,
            "size": source.size,
        },
        expected_parser_contract=cache.parser_contract,
    )

    result = CachedFullTextSearcher(repository.root).search(("vertical tab",))

    assert b"vertical\\u000btab" in candidate.read_bytes()
    assert result.total_occurrences == 1
    assert result.occurrences[0].matched_terms == ("vertical tab",)


def test_context_threshold_multiline_range_and_400_character_cap(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    long_line = "x" * 600 + " focused phrase " + "y" * 600
    _materialize(
        repository,
        f"# Context Paper\nbefore\n{long_line}\nafter\n".encode(),
    )

    included = _searcher(repository).search(
        ("focused phrase",), context_lines=1
    )

    assert included.context_status is CachedFullTextContextStatus.INCLUDED
    assert len(included.occurrences[0].context) <= 400
    assert "focused phrase" in included.occurrences[0].context

    repository_many = SourceRepository(tmp_path / "many")
    _materialize(
        repository_many,
        ("# Many\n" + " ".join(["term"] * 21)).encode(),
    )
    broad = _searcher(repository_many).search(
        ("term",), limit=100, context_lines=2
    )
    assert broad.total_occurrences == 21
    assert broad.context_status is CachedFullTextContextStatus.OMITTED_TOO_BROAD
    assert all(item.context == "" for item in broad.occurrences)

    repository_multiline = SourceRepository(tmp_path / "multiline")
    _materialize(
        repository_multiline,
        b"# Multi\nbefore\nheavy\nfield\nafter\n",
    )
    multiline = _searcher(repository_multiline).search(
        ("heavy field",), context_lines=1
    )
    assert "heavy\nfield" in multiline.occurrences[0].context
    assert "before" in multiline.occurrences[0].context
    assert "after" in multiline.occurrences[0].context


def test_sectionless_document_searches_pages_and_uses_page_location(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"# source bytes\n")
    document = ParsedDocument(
        source=source,
        pages=(ParsedPage(1, "Page Display Title\npage-only phrase"),),
    )
    cache = ParsedDocumentCache(repository=repository)
    cached, _ = cache.get_or_parse(source, lambda artifact: document)
    FullTextCatalog(repository.root).record(
        source,
        cached,
        parser_contract=cache.parser_contract,
        parsed_cache_key=cache.cache_key(source),
    )

    result = _searcher(repository).search(("page-only phrase",))

    assert result.total_occurrences == 1
    occurrence = result.occurrences[0]
    assert occurrence.location.value == "page"
    assert occurrence.location_id == "page-1"
    assert occurrence.title == "Page 1"
    assert occurrence.page_number == 1
    assert occurrence.line == 2


def test_refinement_counts_exactly_and_returns_only_top_50_titles(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    for number in range(51):
        repeated = (
            "common mechanism common mechanism common mechanism"
            if number == 50
            else "common mechanism common mechanism"
        )
        _materialize(
            repository,
            (
                f"# Paper {number:02d}\n"
                f"{repeated} unique-{number}\n"
            ).encode(),
        )

    result = _searcher(repository).search(
        ("common mechanism",), context_lines=1
    )

    assert result.mode is CachedFullTextSearchMode.REFINEMENT_REQUIRED
    assert result.total_occurrences == 103
    assert result.matched_document_count == 51
    assert result.occurrences == ()
    assert len(result.top_paper_titles) == 50
    assert result.top_paper_titles[0] == "Paper 50"
    assert result.top_paper_titles[-1] == "Paper 48"
    encoded = to_json_value(result)
    assert all(isinstance(item, str) for item in encoded["top_paper_titles"])
    assert "abstract" not in json.dumps(encoded).casefold()
    assert "summary" not in json.dumps(encoded).casefold()
    assert (
        result.context_status
        is CachedFullTextContextStatus.OMITTED_REFINEMENT_REQUIRED
    )


def test_limit_500_is_supported_and_overflow_storage_stays_empty(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        ("# Large\n" + " ".join(["needle"] * 501)).encode(),
    )

    result = _searcher(repository).search(("needle",), limit=500)

    assert result.mode is CachedFullTextSearchMode.REFINEMENT_REQUIRED
    assert result.total_occurrences == 501
    assert result.occurrences == ()
    with pytest.raises(CachedFullTextSearchError, match="between 1 and 500"):
        _searcher(repository).search(("needle",), limit=501)


def test_current_refresh_html_preference_alias_merge_and_local_identity(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        b"<h1>Old Paper</h1><p>stale phrase</p>",
        source_format=SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    fresh_source, fresh_document = _materialize(
        repository,
        b"<h1>Fresh Paper</h1><p>current phrase</p>",
        source_format=SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    _materialize(
        repository,
        b"%PDF html preferred",
        source_format=SourceFormat.PDF,
        arxiv_id="0911.3380",
        pdf_pages=("PDF Paper\npdf-only phrase",),
    )
    same_source = repository.store_bytes(
        repository.read_bytes(fresh_source),
        source_format=SourceFormat.HTML,
        origin=SourceOrigin(
            SourceOriginKind.REMOTE_PROVIDER,
            provider="fixture",
            metadata={"arxiv_id": "hep-th/0601001"},
        ),
    )
    PaperParserService(repository).parse_source(same_source)
    local_source, local_document = _materialize(
        repository,
        b"# Local Note\nlocal phrase\n",
    )

    stale = _searcher(repository).search(("stale phrase",))
    pdf = _searcher(repository).search(("pdf-only phrase",))
    current = _searcher(repository).search(("current phrase",))
    local = _searcher(repository).search(("local phrase",))

    assert stale.total_occurrences == 0
    assert pdf.total_occurrences == 0
    assert current.matched_document_count == 1
    occurrence = current.occurrences[0]
    assert occurrence.arxiv_ids == (
        "arXiv:0911.3380",
        "arXiv:hep-th/0601001",
    )
    assert occurrence.source_format == "html"
    assert occurrence.source_digest == fresh_source.artifact_digest
    assert occurrence.document_digest == fresh_document.document_digest
    assert local.matched_document_count == 1
    local_occurrence = local.occurrences[0]
    assert local_occurrence.source_kind == "local"
    assert local_occurrence.arxiv_ids == ()
    assert local_occurrence.source_format == "markdown"
    assert local_occurrence.source_digest == local_source.artifact_digest
    assert local_occurrence.document_digest == local_document.document_digest


def test_corrupt_current_candidate_is_skipped_without_path_leak(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "private-cache")
    _materialize(repository, b"# Secret\nneedle\n")
    candidate = next(
        repository.root.glob("parsed-document-cache/v1/sha256/*/*/document.json")
    )
    candidate.write_bytes(b'{"needle":"corrupt"}')

    result = _searcher(repository).search(("needle",))
    encoded = json.dumps(to_json_value(result))

    assert result.total_occurrences == 0
    assert result.warnings
    assert str(repository.root) not in encoded
    assert "document.json" not in encoded
    assert "session" not in encoded.casefold()
    assert "path" not in encoded.casefold()


def test_missing_candidate_and_unreferenced_old_cache_are_not_sent_to_rg(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"# Old\nneedle\n")
    cache = ParsedDocumentCache(repository=repository)
    cache.get_or_parse(
        source,
        lambda artifact: parse_artifact_bytes(
            artifact, repository.read_bytes(artifact)
        ),
    )

    ignored = CachedFullTextSearcher(
        repository.root, candidate_selector=NoCandidatesExpected()
    ).search(("needle",))
    assert ignored.total_occurrences == 0

    PaperParserService(repository).parse_source(source)
    next(
        repository.root.glob("parsed-document-cache/v1/sha256/*/*/document.json")
    ).unlink()
    missing = CachedFullTextSearcher(
        repository.root, candidate_selector=NoCandidatesExpected()
    ).search(("needle",))
    assert missing.total_occurrences == 0
    assert missing.warnings


def test_stale_parser_contract_cache_is_skipped_without_reparsing(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    source = _store(repository, b"# Old projection\nstale-only phrase\n")
    legacy = ParsedDocumentCache(
        repository=repository,
        parser_contract="arc.paper.parser.v2",
    )
    document, _ = legacy.get_or_parse(
        source,
        lambda artifact: parse_artifact_bytes(
            artifact, repository.read_bytes(artifact)
        ),
    )
    legacy_key = legacy.cache_key(source)
    legacy_path = legacy._entry_dir(legacy_key) / "document.json"
    legacy_bytes = legacy_path.read_bytes()
    catalog = FullTextCatalog(repository.root)
    catalog.record(
        source,
        document,
        parser_contract=legacy.parser_contract,
        parsed_cache_key=legacy_key,
    )

    result = CachedFullTextSearcher(
        repository.root, candidate_selector=NoCandidatesExpected()
    ).search(("stale-only phrase",))

    assert result.total_occurrences == 0
    assert result.matched_document_count == 0
    assert any("stale parser contract" in warning for warning in result.warnings)
    assert legacy_path.read_bytes() == legacy_bytes
    assert (
        catalog.current_entries()[0].representations[0].parser_contract
        == "arc.paper.parser.v2"
    )


def test_current_pdf_parser_contract_remains_searchable(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "cache")
    _materialize(
        repository,
        b"%PDF current projection",
        source_format=SourceFormat.PDF,
        pdf_pages=("PDF title\ncurrent PDF phrase",),
    )

    result = _searcher(repository).search(("current PDF phrase",))

    assert result.total_occurrences == 1
    assert result.occurrences[0].source_format == "pdf"
    assert not result.warnings


def test_current_pdf_falls_back_when_html_catalog_projection_is_stale(
    tmp_path: Path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    html = _store(
        repository,
        b"<h1>Old HTML</h1><p>stale HTML phrase</p>",
        SourceFormat.HTML,
        arxiv_id="0911.3380",
    )
    legacy = ParsedDocumentCache(
        repository=repository,
        parser_contract="arc.paper.parser.v2",
    )
    legacy_document, _ = legacy.get_or_parse(
        html,
        lambda artifact: parse_artifact_bytes(
            artifact, repository.read_bytes(artifact)
        ),
    )
    catalog = FullTextCatalog(repository.root)
    catalog.record(
        html,
        legacy_document,
        parser_contract=legacy.parser_contract,
        parsed_cache_key=legacy.cache_key(html),
    )
    _materialize(
        repository,
        b"%PDF current projection",
        source_format=SourceFormat.PDF,
        arxiv_id="0911.3380",
        pdf_pages=("Current PDF\ncurrent PDF phrase",),
    )

    result = _searcher(repository).search(("current PDF phrase",))

    assert result.total_occurrences == 1
    assert result.occurrences[0].source_format == "pdf"
    assert not result.warnings


def test_registry_contract_is_safe_path_free_and_titles_are_strings_only() -> None:
    spec = get_operation("search-cached-full-text")
    assert spec is not None
    assert spec.operation_id == "arc-paper.search-cached-full-text.v1"
    assert spec.effect_flags == frozenset()
    assert set(spec.input_codec.schema["properties"]) == {
        "terms",
        "limit",
        "context_lines",
        "case_sensitive",
    }
    assert "cache_root" not in spec.input_codec.schema["properties"]
    assert "path" not in spec.input_codec.schema["properties"]

    invalid = {
        "mode": "refinement_required",
        "terms": ["specific phrase"],
        "limit": 100,
        "context_lines": 0,
        "case_sensitive": False,
        "total_occurrences": 101,
        "matched_document_count": 1,
        "occurrences": [],
        "top_paper_titles": [{"title": "Too detailed"}],
        "context_status": "not_requested",
        "message": "refine",
        "warnings": [],
    }
    with pytest.raises(OperationRequestError) as error:
        spec.output_codec.encode(invalid)
    assert error.value.code == "invalid_result"


def test_ripgrep_adapter_uses_argv_batches_and_accepts_only_known_nul_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = tuple((tmp_path / f"{number}.json").resolve() for number in range(3))
    for path in paths:
        path.write_text("body", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        selected = command[-1]
        return subprocess.CompletedProcess(
            command, 0, stdout=selected.encode() + b"\0", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", run)
    result = RipgrepCandidateSelector(max_paths_per_call=2).files_with_matches(
        ("specific", "synonym"),
        paths,
        case_sensitive=False,
    )

    assert result == (paths[1], paths[2])
    assert len(calls) == 2
    for command, kwargs in calls:
        assert isinstance(command, list)
        assert "--no-config" in command
        assert "--files-with-matches" in command
        assert "--null" in command
        assert "--ignore-case" in command
        assert command.count("--regexp") == 2
        assert "--" in command
        assert "shell" not in kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    (
        (2, b""),
        (0, b""),
        (0, b"not-nul-terminated"),
        (1, b"unexpected"),
        (0, b"/unknown/path\0"),
        (0, b"\xff\0"),
    ),
)
def test_ripgrep_adapter_rejects_failure_and_bad_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
) -> None:
    candidate = (tmp_path / "candidate.json").resolve()
    candidate.write_text("body", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, stdout=stdout, stderr=b""
        ),
    )

    with pytest.raises(RipgrepError) as error:
        RipgrepCandidateSelector().files_with_matches(
            ("term",), (candidate,), case_sensitive=True
        )

    assert error.value.code == "rg_failed"
    assert str(tmp_path) not in error.value.message


def test_ripgrep_unavailable_is_typed_and_has_install_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (tmp_path / "candidate.json").resolve()
    candidate.write_text("body", encoding="utf-8")

    def unavailable(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(RipgrepError) as error:
        RipgrepCandidateSelector(executable="missing-rg").files_with_matches(
            ("term",), (candidate,), case_sensitive=False
        )

    assert error.value.code == "rg_unavailable"
    assert "install" in error.value.message
    assert "ripgrep" in error.value.message


@pytest.mark.parametrize("invalid_catalog", (False, True))
def test_ripgrep_availability_is_checked_before_catalog_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_catalog: bool,
) -> None:
    repository = SourceRepository(
        tmp_path / ("invalid" if invalid_catalog else "empty")
    )
    if invalid_catalog:
        locator = (
            repository.root
            / "full-text-catalog"
            / "v1"
            / "entries"
            / "00"
            / ("0" * 64)
            / "locator.json"
        )
        locator.parent.mkdir(parents=True)
        locator.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda executable: None)

    with pytest.raises(RipgrepError) as error:
        CachedFullTextSearcher(
            repository.root,
            candidate_selector=RipgrepCandidateSelector(),
        ).search(("specific phrase",))

    assert error.value.code == "rg_unavailable"
