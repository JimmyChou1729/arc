import os

import httpx
import pytest

from arc_paper import ArcPaperService, DocumentTarget


pytestmark = pytest.mark.skipif(
    os.environ.get("ARC_RUN_NET_TESTS") != "1",
    reason="set ARC_RUN_NET_TESTS=1 to run network integration tests",
)


@pytest.mark.parametrize("paper_id", ["arXiv:0911.3380", "arXiv:hep-th/0601001"])
def test_paper_metadata_html_section_and_cache(paper_id, tmp_path, monkeypatch):
    service = ArcPaperService(cache_root=tmp_path)
    metadata = service.get_metadata(paper_id)
    assert metadata["title"]
    toc = service.get_table_of_contents(
        DocumentTarget("reference", reference=paper_id), source_format="html"
    )
    assert toc.entries
    document = toc.source.document
    assert document.source_format == "html"
    assert len(document.source_sha256) == len(document.parsed_document_sha256) == 64
    target = DocumentTarget("document", document=document)
    section = service.get_section(target, toc.entries[0].section_id)
    assert section.text.strip()

    def forbidden(*args, **kwargs):
        raise AssertionError("exact cached document read attempted HTTP")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    warm = ArcPaperService(cache_root=tmp_path)
    assert warm.get_table_of_contents(target).source.document == document
    assert warm.get_section(target, section.section_id).text == section.text
