from __future__ import annotations

from pathlib import Path

import pytest

from arc_paper.document_access import DocumentTarget
from arc_paper.reference_cache import ReferenceIdentity, ReferenceMaterialCache
from arc_paper.service import ArcPaperService, PaperInputError


def test_general_read_resolves_reference_and_returns_exact_document_ref(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    source = tmp_path / "paper.md"
    source.write_text("# Introduction\nAlpha.\n\n# Conclusion\nOmega.\n")
    service = ArcPaperService(cache_root=cache_root)
    service.admit_reference(source, title="Exact Paper")

    toc = service.get_table_of_contents(
        DocumentTarget(kind="reference", reference="Exact Paper")
    )
    section = service.get_section(
        DocumentTarget(kind="reference", reference="Exact Paper"), "Conclusion"
    )

    assert [item.title for item in toc.entries] == ["Introduction", "Conclusion"]
    assert toc.source.document.source_format.value == "markdown"
    assert toc.source.identity == section.source.identity
    assert section.text.endswith("Omega.")


def test_general_read_exact_document_never_accepts_provider_controls(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.md"
    source.write_text("# One\nText.\n")
    service = ArcPaperService(cache_root=tmp_path / "cache")
    artifact = service.import_source(source)
    reference = service.cache_document(artifact)
    target = DocumentTarget(kind="document", document=reference)

    assert service.get_table_of_contents(target).source.identity is None
    with pytest.raises(PaperInputError, match="apply only to reference"):
        service.get_table_of_contents(target, refresh=True)


def test_reference_cache_supports_exact_inspire_identity(tmp_path: Path) -> None:
    cache = ReferenceMaterialCache(tmp_path / "cache")
    resource = cache.store_resource(b"# Paper", media_type="text/markdown")
    cache.store_material(ReferenceIdentity(inspire_recid="12345"), (resource,))

    found = cache.lookup(inspire_recid="12345")
    assert found is not None
    assert found.identity.inspire_recid == "12345"


def test_full_text_search_supports_literal_or_dedup_and_partial_failures(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.md"
    source.write_text("# Body\nAlpha here. Omega there.\n")
    service = ArcPaperService(cache_root=tmp_path / "cache")
    artifact = service.import_source(source)
    reference = service.cache_document(artifact)
    exact = DocumentTarget(kind="document", document=reference)

    result = service.search_full_text_targets(
        ("Alpha", "Omega"),
        targets=(exact, DocumentTarget(kind="reference", reference="Missing"), exact),
    )

    assert result.scope == "targets"
    assert result.terms == ("Alpha", "Omega")
    assert {item.matched_terms for item in result.occurrences} == {
        ("Alpha",),
        ("Omega",),
    }
    assert result.documents[0].target_indices == (0, 2)
    assert result.failures[0].target_index == 1
    assert result.failures[0].code == "reference_acquisition_unavailable"


def test_equation_target_search_fails_only_when_no_target_resolves(
    tmp_path: Path,
) -> None:
    service = ArcPaperService(cache_root=tmp_path / "cache")
    with pytest.raises(PaperInputError) as error:
        service.search_equation_targets(
            (DocumentTarget(kind="reference", reference="Missing"),),
            ("2.30",),
        )
    assert error.value.code == "no_document_target_resolved"
