from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from arc_paper import (
    RICH_DOCUMENT_PARSER_CONTRACT,
    ArcPaperService,
    CachedRichDocumentError,
    RichDocumentParserService,
    cached_rich_document_ref_from_document,
    cached_rich_document_ref_to_document,
    open_cached_rich_document,
    read_cached_rich_asset,
)


def _illustrated(tmp_path: Path):
    source_path = tmp_path / "paper.md"
    asset_path = tmp_path / "plot.png"
    asset_payload = b"\x89PNG\r\n\x1a\nscoped-rich-asset"
    asset_path.write_bytes(asset_payload)
    source_path.write_text(
        "# Result\n\nA source paragraph.\n\n"
        '![Plot](plot.png "Measured result")\n',
        encoding="utf-8",
    )
    cache_root = tmp_path / "cache"
    service = ArcPaperService(cache_root=cache_root)
    reference = service.cache_rich_document(
        service.import_source(source_path)
    )
    return service, reference, asset_payload


def test_cached_rich_reference_round_trip_and_public_open(
    tmp_path: Path,
) -> None:
    service, reference, _ = _illustrated(tmp_path)
    encoded = cached_rich_document_ref_to_document(reference)

    assert cached_rich_document_ref_from_document(encoded) == reference
    assert encoded == {
        "primary": {
            "source_format": "markdown",
            "source_sha256": reference.primary.source_sha256,
            "source_size": reference.primary.source_size,
            "media_type": "text/markdown",
            "parser_contract": reference.primary.parser_contract,
            "parsed_document_sha256": (
                reference.primary.parsed_document_sha256
            ),
        },
        "validators": [],
        "rich_parser_contract": RICH_DOCUMENT_PARSER_CONTRACT,
        "rich_document_sha256": reference.rich_document_sha256,
        "asset_manifest_sha256": reference.asset_manifest_sha256,
    }
    document = service.open_cached_rich_document(reference)
    wrapped = open_cached_rich_document(
        reference, cache_root=tmp_path / "cache"
    )
    assert wrapped.document_digest == document.document_digest
    assert document.assets[0].logical_name == "plot.png"
    assert str(tmp_path) not in json.dumps(encoded)
    assert str(tmp_path) not in json.dumps(
        {
            "source": document.source.origin.locator,
            "document": document.document_digest,
        }
    )


def test_cache_accepts_a_verified_rich_document_without_source_paths(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "paper.md"
    (tmp_path / "figure.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        encoding="utf-8",
    )
    source_path.write_text(
        "# Figure\n\n![Diagram](figure.svg)\n",
        encoding="utf-8",
    )
    service = ArcPaperService(cache_root=tmp_path / "cache")
    source = service.import_source(source_path)
    document = RichDocumentParserService(service.repository).parse_source(
        source
    )

    reference = service.cache_rich_document(document)
    reopened = service.open_cached_rich_document(reference)

    assert reopened.document_digest == document.document_digest
    assert reopened.source.origin.locator.startswith("markdown/sha256/")
    assert str(tmp_path) not in reopened.source.origin.locator


def test_cached_rich_asset_read_is_scoped_and_path_free(
    tmp_path: Path,
) -> None:
    service, reference, asset_payload = _illustrated(tmp_path)
    document = service.open_cached_rich_document(reference)
    asset = document.assets[0]

    assert service.read_cached_rich_asset(
        reference, asset.artifact_digest
    ) == asset_payload
    assert read_cached_rich_asset(
        reference,
        asset.artifact_digest,
        cache_root=tmp_path / "cache",
    ) == asset_payload

    unrelated = service.repository.store_asset_bytes(
        b"other bytes", media_type="image/png"
    )
    with pytest.raises(CachedRichDocumentError) as error:
        service.read_cached_rich_asset(
            reference, unrelated.artifact_digest
        )
    assert error.value.code == "cached_rich_asset_not_scoped"


def test_cached_rich_document_repairs_from_scoped_asset_manifest(
    tmp_path: Path,
) -> None:
    service, reference, asset_payload = _illustrated(tmp_path)
    object_dir = next(
        (tmp_path / "cache" / "rich-document" / "v1" / "objects").glob(
            "*/*"
        )
    )
    document_path = object_dir / "document.json"
    manifest_path = object_dir / "asset-manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert asset_payload not in document_path.read_bytes()
    assert asset_payload not in manifest_path.read_bytes()

    document_path.write_text("corrupt", encoding="utf-8")
    repaired = service.open_cached_rich_document(reference)

    assert repaired.document_digest == reference.rich_document_sha256
    assert manifest_path.read_text(encoding="utf-8") == manifest_text
    assert json.loads(document_path.read_text(encoding="utf-8"))[
        "document_digest"
    ] == reference.rich_document_sha256


def test_cached_rich_document_restores_manifest_from_valid_document(
    tmp_path: Path,
) -> None:
    service, reference, _ = _illustrated(tmp_path)
    manifest_path = next(
        (tmp_path / "cache" / "rich-document" / "v1" / "objects").glob(
            "*/*/asset-manifest.json"
        )
    )
    manifest_path.write_text("corrupt", encoding="utf-8")

    document = service.open_cached_rich_document(reference)

    assert document.document_digest == reference.rich_document_sha256
    assert json.loads(manifest_path.read_text(encoding="utf-8"))[
        "schema_version"
    ] == "arc.paper.rich_asset_manifest.v1"


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (
            {"rich_parser_contract": "arc.paper.rich_document_parser.future"},
            "cached_rich_document_parser_contract_mismatch",
        ),
        (
            {"rich_document_sha256": "0" * 64},
            "cached_rich_document_digest_mismatch",
        ),
        (
            {"asset_manifest_sha256": "0" * 64},
            "cached_rich_asset_manifest_mismatch",
        ),
    ],
)
def test_cached_rich_open_revalidates_logical_identity(
    tmp_path: Path,
    replacement: dict[str, object],
    code: str,
) -> None:
    service, reference, _ = _illustrated(tmp_path)

    with pytest.raises(CachedRichDocumentError) as error:
        service.open_cached_rich_document(replace(reference, **replacement))

    assert error.value.code == code


def test_cached_rich_reference_codec_is_strict(tmp_path: Path) -> None:
    _, reference, _ = _illustrated(tmp_path)
    encoded = cached_rich_document_ref_to_document(reference)

    with pytest.raises(ValueError):
        cached_rich_document_ref_from_document(
            {**encoded, "physical_cache_path": str(tmp_path)}
        )
    with pytest.raises(ValueError):
        cached_rich_document_ref_from_document(
            {**encoded, "validators": ()}
        )
    with pytest.raises(ValueError):
        cached_rich_document_ref_from_document(
            {**encoded, "validators": ["not-a-reference"]}
        )
    with pytest.raises(ValueError):
        cached_rich_document_ref_from_document(
            {**encoded, "rich_parser_contract": 1}
        )
