from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from arc_paper import (
    RichDocument,
    SourceArtifact,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
)
from arc_render import (
    FRONT_MATTER_BEGIN,
    FRAGMENT_REVISION_SCHEMA,
    FRAGMENT_REVISION_SCHEMA_V1,
    AnchorBlock,
    FragmentAnchor,
    FragmentAppearance,
    FragmentRevision,
    decode_fragment_revision,
    encode_fragment_revision,
    extract_markdown_citation_ids,
    fragment_revision_filename,
    fragment_revision_to_document,
    source_identity_from_rich_document,
)


def make_revision(
    *,
    body: str = "One\r\nTwo\r",
    provenance: dict[str, object] | None = None,
) -> FragmentRevision:
    payload = b"# Source\n"
    document = RichDocument(
        SourceArtifact(
            SourceFormat.MARKDOWN,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "text/markdown",
            SourceOrigin(SourceOriginKind.REPOSITORY),
        ),
        (),
    )
    return FragmentRevision(
        source=source_identity_from_rich_document(document),
        fragment_id="fragment-1",
        revision=1,
        parent_semantic_digest=None,
        anchor=FragmentAnchor(
            "block",
            "b1",
            (
                AnchorBlock(
                    "b1",
                    "paragraph",
                    0,
                    {"line_start": 1, "line_end": 1},
                    "b" * 64,
                ),
            ),
        ),
        priority=10,
        role="translation",
        language="fr",
        title="Example",
        citation_ids=("ref-1",),
        provenance=provenance or {"z": 1, "a": 2},
        markdown_body=body,
    )


def test_json_front_matter_round_trip_and_markdown_normalization() -> None:
    revision = make_revision()
    encoded = encode_fragment_revision(revision)
    assert encoded.startswith(FRONT_MATTER_BEGIN + "\n{")
    metadata_line = encoded.splitlines()[1]
    assert json.loads(metadata_line)["schema_version"] == FRAGMENT_REVISION_SCHEMA
    assert json.loads(metadata_line)["appearance"] is None
    decoded = decode_fragment_revision(
        encoded, filename=fragment_revision_filename(revision)
    )
    assert decoded == revision
    assert decoded.markdown_body == "One\nTwo\n"


def test_v1_round_trip_retains_original_schema_and_digest() -> None:
    revision = replace(
        make_revision(),
        schema_version=FRAGMENT_REVISION_SCHEMA_V1,
        appearance=None,
    )
    encoded = encode_fragment_revision(revision)
    metadata = json.loads(encoded.splitlines()[1])
    assert metadata["schema_version"] == FRAGMENT_REVISION_SCHEMA_V1
    assert "appearance" not in metadata
    assert decode_fragment_revision(
        encoded, filename=fragment_revision_filename(revision)
    ) == revision


def test_v2_appearance_round_trip_is_canonical_and_digest_bound() -> None:
    plain = make_revision()
    styled = replace(
        plain,
        appearance=FragmentAppearance("#F9FAFB", "#111827"),
    )
    assert styled.appearance == FragmentAppearance("#f9fafb", "#111827")
    assert styled.semantic_digest != plain.semantic_digest
    assert decode_fragment_revision(encode_fragment_revision(styled)) == styled


def test_semantic_digest_is_independent_of_mapping_insertion_order() -> None:
    left = make_revision(provenance={"a": 2, "z": 1})
    right = make_revision(provenance={"z": 1, "a": 2})
    assert left.semantic_digest == right.semantic_digest


def test_v1_rejects_yaml_and_extra_front_matter_fields() -> None:
    yaml_value = (
        f"{FRONT_MATTER_BEGIN}\n"
        "schema_version: arc.render.fragment_revision.v1\n"
        "<!-- ARC:FRAGMENT-JSON:END -->\nBody"
    )
    with pytest.raises(ValueError, match="valid JSON"):
        decode_fragment_revision(yaml_value)

    revision = make_revision()
    metadata = fragment_revision_to_document(revision)
    metadata["extra"] = True
    value = (
        f"{FRONT_MATTER_BEGIN}\n"
        f"{json.dumps(metadata)}\n"
        "<!-- ARC:FRAGMENT-JSON:END -->\nBody"
    )
    with pytest.raises(ValueError, match="fields"):
        decode_fragment_revision(value)


def test_front_matter_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    revision = make_revision()
    metadata = fragment_revision_to_document(revision)
    encoded = json.dumps(metadata)
    duplicate = encoded[:-1] + ', "role": "duplicate"}'
    value = (
        f"{FRONT_MATTER_BEGIN}\n{duplicate}\n"
        "<!-- ARC:FRAGMENT-JSON:END -->\nBody"
    )
    with pytest.raises(ValueError, match="valid JSON"):
        decode_fragment_revision(value)

    metadata["provenance"] = {"score": float("nan")}
    value = (
        f"{FRONT_MATTER_BEGIN}\n{json.dumps(metadata)}\n"
        "<!-- ARC:FRAGMENT-JSON:END -->\nBody"
    )
    with pytest.raises(ValueError, match="valid JSON"):
        decode_fragment_revision(value)


def test_filename_binds_revision_number_and_semantic_digest() -> None:
    revision = make_revision()
    with pytest.raises(ValueError, match="digest"):
        decode_fragment_revision(
            encode_fragment_revision(revision),
            filename=f"revision-000001-{'0' * 64}.md",
        )


def test_markdown_citation_extractor_is_ordered_unique_and_literal() -> None:
    markdown = "[@first] [@first] [@second:part] [@ignored space] [@third]"

    assert extract_markdown_citation_ids(markdown) == (
        "first",
        "second:part",
        "third",
    )
