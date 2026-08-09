from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest
from arc_paper import (
    RichDocument,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

from arc_companion.reviewed_supplements import (
    ReviewedCompanionSupplement,
    ReviewedOwnedResource,
    ReviewedSourceDraft,
    ReviewedSourceUnit,
    ReviewedSupplementEntry,
    decode_reviewed_companion_supplement,
    encode_reviewed_companion_supplement,
    reviewed_anchor_fingerprint,
    reviewed_source_inventory_digest,
    validate_reviewed_companion_supplement,
)
from arc_companion.request_contracts import (
    CompanionBuildRequest,
    CompanionGenerationRecipe,
    encode_handler_semantic_input,
    normalize_handler_semantic_input,
)


def _source(tmp_path: Path) -> RichDocument:
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        b"# Main source\n\nA stable paragraph.\n",
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT,
            locator="source.md",
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def _supplement(source: RichDocument) -> ReviewedCompanionSupplement:
    anchor = source.blocks[1]
    entry = ReviewedSupplementEntry(
        entry_id="entry-1",
        anchor_block_id=anchor.block_id,
        anchor_fingerprint=reviewed_anchor_fingerprint(anchor),
        title="Reviewed explanation",
        markdown=(
            "A reviewed explanation.\n\n"
            "![Reviewed diagram](reviewed/diagram.png)"
        ),
        source_draft_ids=("draft-1",),
        source_unit_ids=("unit-1",),
    )
    coverage = (
        ReviewedSourceUnit(
            unit_id="unit-1",
            kind="text",
            locator="notes.md#L10-L12",
            fingerprint="a" * 64,
            disposition="published",
            reason="This unit adds a missing derivation.",
            entry_ids=("entry-1",),
        ),
        ReviewedSourceUnit(
            unit_id="unit-2",
            kind="image",
            locator="figures/unused.png",
            fingerprint="b" * 64,
            disposition="excluded",
            reason="The image only repeats the source.",
        ),
    )
    return ReviewedCompanionSupplement(
        supplement_id="supplement-1",
        summary="One reviewed explanation from an exhaustively assessed source.",
        source_unit_count=len(coverage),
        source_inventory_digest=reviewed_source_inventory_digest(coverage),
        entries=(entry,),
        coverage=coverage,
        drafts=(
            ReviewedSourceDraft(
                draft_id="draft-1",
                disposition="published",
                reason="The reviewed draft was integrated.",
                source_unit_ids=("unit-1",),
                entry_ids=("entry-1",),
            ),
            ReviewedSourceDraft(
                draft_id="draft-2",
                disposition="excluded",
                reason="The draft only restated the source.",
                source_unit_ids=("unit-2",),
            ),
        ),
        resources=(
            ReviewedOwnedResource(
                artifact_digest="c" * 64,
                logical_name="reviewed/diagram.png",
                media_type="image/png",
                size=128,
            ),
        ),
    )


def test_reviewed_supplement_validates_and_round_trips(tmp_path: Path) -> None:
    source = _source(tmp_path)
    supplement = _supplement(source)

    validate_reviewed_companion_supplement(supplement, source)
    encoded = encode_reviewed_companion_supplement(supplement)

    assert decode_reviewed_companion_supplement(encoded) == supplement
    assert "content" not in encoded["resources"][0]
    with pytest.raises(ValueError, match="invalid fields"):
        decode_reviewed_companion_supplement({**encoded, "extra": True})


def test_legacy_supplement_entries_upgrade_direct_and_draft_provenance(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    encoded = encode_reviewed_companion_supplement(_supplement(source))
    encoded["schema_version"] = "arc.companion.reviewed_supplement.v1"
    for entry in encoded["entries"]:
        del entry["source_basis"]
        del entry["source_basis_reason"]

    direct = decode_reviewed_companion_supplement(encoded)
    assert direct.schema_version == "arc.companion.reviewed_supplement.v2"
    assert direct.entries[0].source_basis == "supplement_units"

    encoded["entries"][0]["source_unit_ids"] = []
    encoded["coverage"][0]["disposition"] = "excluded"
    encoded["coverage"][0]["entry_ids"] = []
    encoded["source_inventory_digest"] = reviewed_source_inventory_digest(
        direct.coverage
    )
    via_draft = decode_reviewed_companion_supplement(encoded)
    assert via_draft.entries[0].source_basis == "supplement_drafts"
    validate_reviewed_companion_supplement(via_draft, source)


def test_legacy_nested_supplement_binding_normalizes_for_durable_resume(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    request = CompanionBuildRequest(
        source, reviewed_supplements=(_supplement(source),)
    )
    current = encode_handler_semantic_input(
        request, CompanionGenerationRecipe()
    )
    legacy = copy.deepcopy(current)
    nested = legacy["request"]["reviewed_supplements"][0]
    nested["schema_version"] = "arc.companion.reviewed_supplement.v1"
    for entry in nested["entries"]:
        del entry["source_basis"]
        del entry["source_basis_reason"]

    assert normalize_handler_semantic_input(legacy) == current


@pytest.mark.parametrize("collection", ("entries", "coverage", "drafts"))
def test_reviewed_supplement_rejects_duplicate_ids(
    tmp_path: Path, collection: str
) -> None:
    source = _source(tmp_path)
    supplement = _supplement(source)
    duplicate = replace(
        supplement,
        **{
            collection: (
                *getattr(supplement, collection),
                getattr(supplement, collection)[0],
            )
        },
    )

    with pytest.raises(ValueError, match="duplicate"):
        validate_reviewed_companion_supplement(duplicate, source)


def test_reviewed_supplement_rejects_missing_or_unknown_mappings(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    supplement = _supplement(source)
    entry = supplement.entries[0]

    mismatched = replace(
        supplement,
        entries=(replace(entry, source_unit_ids=("unit-2",)),),
    )
    with pytest.raises(ValueError, match="mappings must match exactly"):
        validate_reviewed_companion_supplement(mismatched, source)

    unknown = replace(
        supplement,
        entries=(replace(entry, source_unit_ids=("unit-unknown",)),),
    )
    with pytest.raises(ValueError, match="unknown coverage source unit"):
        validate_reviewed_companion_supplement(unknown, source)

    with pytest.raises(ValueError, match="published coverage"):
        replace(supplement.coverage[0], entry_ids=())
    with pytest.raises(ValueError, match="excluded coverage"):
        replace(supplement.coverage[1], entry_ids=("entry-1",))

    with pytest.raises(ValueError, match="source_unit_ids must not be empty"):
        replace(entry, source_unit_ids=())

    primary = replace(
        entry,
        source_draft_ids=(),
        source_unit_ids=(),
        source_basis="primary_source",
        source_basis_reason="The reviewer added a derivation from the anchored source.",
    )
    primary_coverage = tuple(
        replace(item, disposition="excluded", entry_ids=())
        for item in supplement.coverage
    )
    primary_drafts = tuple(
        replace(item, disposition="excluded", entry_ids=())
        for item in supplement.drafts
    )
    validate_reviewed_companion_supplement(
        replace(
            supplement,
            entries=(primary,),
            coverage=primary_coverage,
            drafts=primary_drafts,
            source_inventory_digest=reviewed_source_inventory_digest(
                primary_coverage
            ),
        ),
        source,
    )
    with pytest.raises(ValueError, match="must explain"):
        replace(primary, source_basis_reason="")


def test_reviewed_supplement_rejects_unknown_anchor_or_fingerprint(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    supplement = _supplement(source)
    entry = supplement.entries[0]

    unknown = replace(
        supplement,
        entries=(replace(entry, anchor_block_id="block-unknown"),),
    )
    with pytest.raises(ValueError, match="absent from the source"):
        validate_reviewed_companion_supplement(unknown, source)

    changed = replace(
        supplement,
        entries=(replace(entry, anchor_fingerprint="d" * 64),),
    )
    with pytest.raises(ValueError, match="fingerprint differs"):
        validate_reviewed_companion_supplement(changed, source)


def test_reviewed_supplement_rejects_invalid_or_unresolved_resources(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    supplement = _supplement(source)

    with pytest.raises(ValueError, match="SHA-256"):
        replace(supplement.resources[0], artifact_digest="bad")
    with pytest.raises(ValueError, match="positive integer"):
        replace(supplement.resources[0], size=0)
    with pytest.raises(ValueError, match="canonical relative path"):
        replace(supplement.resources[0], logical_name="../diagram.png")

    missing = replace(supplement, resources=())
    with pytest.raises(ValueError, match="has no source or owned resource"):
        validate_reviewed_companion_supplement(missing, source)

    remote = replace(
        supplement.entries[0],
        markdown="![Remote diagram](https://example.test/diagram.png)",
    )
    with pytest.raises(ValueError, match="must use a source or owned resource"):
        validate_reviewed_companion_supplement(
            replace(supplement, entries=(remote,)), source
        )


def test_reviewed_supplement_rejects_duplicate_resource_identity(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    supplement = _supplement(source)
    resource = supplement.resources[0]
    duplicate_name = replace(resource, artifact_digest="d" * 64)
    duplicate_digest = replace(resource, logical_name="reviewed/copy.png")

    with pytest.raises(ValueError, match="duplicate resource logical_name"):
        validate_reviewed_companion_supplement(
            replace(supplement, resources=(resource, duplicate_name)), source
        )
    with pytest.raises(ValueError, match="duplicate resource artifact_digest"):
        validate_reviewed_companion_supplement(
            replace(supplement, resources=(resource, duplicate_digest)), source
        )
