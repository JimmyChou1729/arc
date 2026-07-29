from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


ASSETS = (
    Path(__file__).parents[1]
    / "src"
    / "arc_render"
    / "web_assets"
)


def _text(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def test_reader_javascript_passes_node_syntax_check() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")

    subprocess.run(
        [node, "--check", str(ASSETS / "reader.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_reader_contract_helpers_execute_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    browserCreatedHistory: browserCreatedHistory,
    effectiveEquationLabel: effectiveEquationLabel,
    stableStringify: stableStringify,
    validateIntegerJson: validateIntegerJson,
    validateRevisionMetadata: validateRevisionMetadata
  };
}());
var helpers = globalThis.__arcReaderTest;
helpers.validateIntegerJson({value: 4, nested: [1, 2]}, "fixture");
var rejected = false;
try {
  helpers.validateIntegerJson({value: 1.5}, "fixture");
} catch (_error) {
  rejected = true;
}
if (!rejected) throw new Error("non-integer JSON was accepted");
if (helpers.stableStringify({b: 2, a: 1}) !== '{"a":1,"b":2}') {
  throw new Error("canonical JSON ordering changed");
}
if (!helpers.browserCreatedHistory([{
  revision: 1,
  parent_semantic_digest: null,
  semantic_digest: "d".repeat(64),
  provenance: {producer: "arc-render-browser"}
}])) {
  throw new Error("browser-created fragment history was not recognized");
}
if (helpers.browserCreatedHistory([{
  revision: 1,
  parent_semantic_digest: null,
  semantic_digest: "e".repeat(64),
  provenance: {producer: "arc-translate"}
}])) {
  throw new Error("stale machine fragment history was treated as user-owned");
}
helpers.state.payload = {
  source_identity: {
    source_format: "markdown",
    media_type: "text/markdown",
    artifact_digest: "a".repeat(64),
    size: 4,
    rich_document_digest: "b".repeat(64)
  },
  block_fingerprints: {"block-1": "c".repeat(64)},
  publication: {
    source_document: {
      blocks: [{
        block_id: "block-1",
        kind: "paragraph",
        ordinal: 0,
        locator: {line_start: 1}
      }],
      sections: [],
      metadata: {
        equation_label_reconciliation: {
          "eq-1": {effective_label: " (7) "}
        }
      }
    }
  }
};
helpers.validateRevisionMetadata({
  schema_version: "arc.render.fragment_revision.v1",
  source: helpers.state.payload.source_identity,
  fragment_id: "note-1",
  revision: 1,
  parent_semantic_digest: null,
  anchor: {
    kind: "block",
    target_id: "block-1",
    related_blocks: [{
      block_id: "block-1",
      kind: "paragraph",
      ordinal: 0,
      locator: {line_start: 1},
      content_fingerprint: "c".repeat(64)
    }]
  },
  priority: 110,
  role: "note",
  language: "en",
  title: null,
  citation_ids: [],
  provenance: {producer: "test"}
});
if (
  helpers.effectiveEquationLabel({block_id: "eq-1"}, {label: "(3)"}) !== "(7)"
) {
  throw new Error("effective equation label was not preferred");
}
if (
  helpers.effectiveEquationLabel({block_id: "eq-2"}, {label: " (3) "}) !== "(3)"
) {
  throw new Error("equation payload label fallback changed");
}
"""
    )

    subprocess.run(
        [node, "-"],
        input=instrumented,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reader_rebuilds_directory_state_and_guards_revision_lineage() -> None:
    javascript = _text("reader.js")

    assert (
        "state.embeddedRevisions = (state.payload.revisions || []).slice()"
        in javascript
    )
    assert "state.embeddedRevisions.forEach(addRevision)" in javascript
    assert "state.activeFragmentIds.has(fragmentId)" in javascript
    assert "browserCreatedHistory(values)" in javascript
    assert "resetRevisionState();\n    var fragments;" in javascript
    assert "await loadDirectoryRevisions();" in javascript
    assert "current.semantic_digest !== base.semantic_digest" in javascript
    assert "throw new Error(labels().historyChanged)" in javascript


def test_reader_enforces_strict_browser_revision_contract() -> None:
    javascript = _text("reader.js")

    assert "validateSourceIdentity(metadata.source)" in javascript
    assert "validateAnchor(metadata.anchor)" in javascript
    assert "validateIntegerJson(metadata, \"fragment revision\")" in javascript
    assert "Number.isSafeInteger(value)" in javascript
    assert "anchor related block differs from the rich source" in javascript
    assert "assertKnownCitations(metadata.citation_ids)" in javascript


def test_reader_waits_for_storage_and_exposes_history_and_section_notes() -> None:
    javascript = _text("reader.js")
    stylesheet = _text("reader.css")

    assert "await restoreDirectoryHandle();" in javascript
    assert "await setupEditor();" in javascript
    assert "arc-history-compare" in javascript
    assert "restoreHistoricalRevision" in javascript
    assert "openNewSectionEditor(section)" in javascript
    assert (
        "blocks.slice(section.block_start, section.block_end).map(anchorBlock)"
        in javascript
    )
    assert ".arc-history-compare" in stylesheet
    assert ".arc-section-note-button" in stylesheet


def test_reader_preserves_source_text_and_glossary_rendering_contracts() -> None:
    javascript = _text("reader.js")

    assert "removeVisibleHtmlTags(heading)" in javascript
    assert "removeVisibleHtmlTags(table)" in javascript
    assert "removeVisibleHtmlTags(figure)" in javascript
    assert 'decorateGlossary(original, "source")' in javascript
    assert 'decorateGlossary(translated, "target")' in javascript
    assert "equation_label_reconciliation" in javascript
    assert "reconciliation.effective_label.trim()" in javascript
