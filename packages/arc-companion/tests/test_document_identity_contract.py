from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from arc_paper import (
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

from arc_companion.request_contracts import (
    COMPANION_BUILD_REQUEST_SCHEMA,
    COMPANION_GENERATION_RECIPE_SCHEMA,
    CompanionBuildRequest,
    CompanionGenerationRecipe,
    decode_build_request,
    decode_generation_recipe,
    decode_handler_semantic_input,
    encode_build_request,
    encode_generation_recipe,
    encode_handler_semantic_input,
    normalize_handler_semantic_input,
)
from arc_companion.source_identity import resolve_document_identity


def _document(tmp_path: Path, text: str):
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        text.encode("utf-8"),
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT, locator="source.md"),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def test_document_identity_prefers_metadata_then_shallow_heading(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path, "## Deep\n\nBody.\n\n# Shallow\n\nMore.\n"
    )
    assert resolve_document_identity(document).title == "Shallow"
    with_metadata = replace(
        document, metadata={**document.metadata, "title": "Metadata"}
    )
    assert resolve_document_identity(with_metadata).title == "Metadata"


def test_current_request_and_recipe_round_trip_only(tmp_path: Path) -> None:
    request = CompanionBuildRequest(
        _document(tmp_path, "# Source\n\nBody.\n"),
        authors=("Author",),
    )
    request_document = encode_build_request(request)
    recipe = CompanionGenerationRecipe()
    recipe_document = encode_generation_recipe(recipe)
    assert request_document["schema_version"] == COMPANION_BUILD_REQUEST_SCHEMA
    assert request_document["reviewed_supplements"] == []
    assert recipe_document["schema_version"] == COMPANION_GENERATION_RECIPE_SCHEMA
    decoded_request = decode_build_request(request_document)
    assert decoded_request.source.document_digest == request.source.document_digest
    assert decoded_request.authors == request.authors
    assert decoded_request.target_language == request.target_language
    assert decode_generation_recipe(recipe_document) == recipe


def test_v7_request_decodes_without_reviewed_supplements(
    tmp_path: Path,
) -> None:
    request = encode_build_request(
        CompanionBuildRequest(_document(tmp_path, "# Source\n\nBody.\n"))
    )
    request["schema_version"] = "arc.companion.build_request.v7"
    del request["reviewed_supplements"]

    decoded = decode_build_request(request)

    assert decoded.reviewed_supplements == ()
    assert encode_build_request(decoded)["schema_version"] == (
        COMPANION_BUILD_REQUEST_SCHEMA
    )

    recipe = CompanionGenerationRecipe()
    legacy_binding = {
        "request": request,
        "generation_recipe": encode_generation_recipe(recipe),
    }
    normalized = normalize_handler_semantic_input(legacy_binding)
    normalized_request, normalized_recipe = decode_handler_semantic_input(
        normalized
    )
    assert normalized == encode_handler_semantic_input(
        normalized_request, normalized_recipe
    )


def test_old_request_and_recipe_schemas_are_rejected(tmp_path: Path) -> None:
    request = encode_build_request(
        CompanionBuildRequest(_document(tmp_path, "# Source\n\nBody.\n"))
    )
    request["schema_version"] = "arc.companion.build_request.v6"
    with pytest.raises(ValueError, match="unsupported"):
        decode_build_request(request)

    recipe = encode_generation_recipe(CompanionGenerationRecipe())
    recipe["schema_version"] = "arc.companion.generation_recipe.v15"
    with pytest.raises(ValueError, match="unsupported"):
        decode_generation_recipe(recipe)
