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
