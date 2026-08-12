from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from arc_paper import (
    RichDocumentExportError,
    RichBlockKind,
    export_rich_document,
    rich_document_from_document,
)
from arc_paper.cli import main


def test_export_rich_document_copies_verified_figure_for_arc_render(tmp_path: Path) -> None:
    asset_payload = b"\x89PNG\r\nportable figure"
    (tmp_path / "figure.png").write_bytes(asset_payload)
    source = tmp_path / "paper.md"
    source.write_text(
        "# Result\n\n![Measured result](figure.png)\n",
        encoding="utf-8",
    )
    output = tmp_path / "handoff"

    result = export_rich_document(
        source,
        output_dir=output,
        cache_root=tmp_path / "cache",
    )

    assert Path(result["source"]) == output / "rich-source.json"
    assert Path(result["metadata"]) == output / "metadata.json"
    assert result["warnings"] == [
        "no PDF validator was supplied; rich source structure remains authoritative"
    ]
    document = rich_document_from_document(
        json.loads((output / "rich-source.json").read_text(encoding="utf-8"))
    )
    assert document.document_digest == result["document_digest"]
    assert [block.kind for block in document.blocks] == [
        RichBlockKind.HEADING,
        RichBlockKind.FIGURE,
    ]

    digest = hashlib.sha256(asset_payload).hexdigest()
    resource_path = output / "resources" / digest
    assert resource_path.read_bytes() == asset_payload
    assert result["resources"] == [
        {
            "artifact_digest": digest,
            "media_type": "image/png",
            "logical_name": "figure.png",
            "size": len(asset_payload),
            "path": str(resource_path),
        }
    ]
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    assert set(metadata) == {
        "glossary",
        "bibliography",
        "labels",
        "resources",
        "reader_profile",
    }
    assert metadata["resources"] == [
        {
            "artifact_digest": digest,
            "media_type": "image/png",
            "logical_name": "figure.png",
            "size": len(asset_payload),
            "path": f"resources/{digest}",
        }
    ]


@pytest.mark.parametrize(
    ("name", "source_format", "content"),
    [
        ("paper.md", None, "# Heading\n\nBody.\n"),
        ("paper.data", "html", "<h1>Heading</h1><p>Body.</p>"),
        ("paper.tex", None, "\\section{Heading}\nBody.\n"),
    ],
)
def test_export_rich_document_supports_all_rich_source_formats(
    tmp_path: Path,
    name: str,
    source_format: str | None,
    content: str,
) -> None:
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")

    result = export_rich_document(
        source,
        output_dir=tmp_path / "handoff",
        cache_root=tmp_path / "cache",
        source_format=source_format,
    )

    document = rich_document_from_document(
        json.loads(Path(result["source"]).read_text(encoding="utf-8"))
    )
    assert document.blocks[0].kind is RichBlockKind.HEADING


def test_export_refuses_nonempty_output_before_cache_or_source_reads(
    tmp_path: Path,
) -> None:
    output = tmp_path / "handoff"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    cache = tmp_path / "cache"

    with pytest.raises(RichDocumentExportError) as error:
        export_rich_document(
            tmp_path / "missing.md",
            output_dir=output,
            cache_root=cache,
        )

    assert error.value.code == "rich_document_output_not_empty"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not cache.exists()


def test_export_accepts_empty_output_and_cli_returns_public_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "paper.md"
    source.write_text("# Heading\n", encoding="utf-8")
    output = tmp_path / "handoff"
    output.mkdir()

    assert main(
        [
            "export-rich-document",
            str(source),
            "--output-dir",
            str(output),
            "--cache-root",
            str(tmp_path / "cache"),
        ]
    ) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "completed"
    assert envelope["data"]["source"] == str(output / "rich-source.json")
    assert envelope["data"]["metadata"] == str(output / "metadata.json")
    assert envelope["warnings"][0]["code"] == "paper_warning"
    assert (output / "resources").is_dir()
