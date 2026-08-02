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
    markdown_it = _text("markdown-it/markdown-it.min.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        """
globalThis.window = globalThis;
module = undefined;
exports = undefined;
define = undefined;
"""
        + markdown_it
        + "\n"
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    browserCreatedHistory: browserCreatedHistory,
    effectiveEquationLabel: effectiveEquationLabel,
    fragmentsDirectory: fragmentsDirectory,
    labels: labels,
    updateDirectoryControl: updateDirectoryControl,
    stableStringify: stableStringify,
    setupMarkdown: setupMarkdown,
    buildRenderChunks: buildRenderChunks,
    isPdfPageMarkerBlock: isPdfPageMarkerBlock,
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
if (!helpers.isPdfPageMarkerBlock({
  kind: "paragraph",
  payload: {text: "<!-- PDF_PAGE: 1 -->"}
})) {
  throw new Error("PDF page marker remained visible");
}
if (helpers.isPdfPageMarkerBlock({
  kind: "paragraph",
  payload: {text: "<!-- note --> visible text"}
})) {
  throw new Error("visible paragraph text was hidden with its comment");
}
if (helpers.isPdfPageMarkerBlock({
  kind: "paragraph",
  payload: {text: "<!-- note -->"}
})) {
  throw new Error("ordinary HTML comment was treated as a PDF page marker");
}
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
    },
    outline: [],
    bibliography: [],
    labels: {},
    reader_profile: {target_language: "zh-CN"}
  }
};
helpers.state.payload.publication.labels.translation = "译名";
if (
  helpers.labels().translation !== "译文" ||
  helpers.labels().translatedTerm !== "译文"
) {
  throw new Error("legacy simplified Chinese translation labels were not normalized");
}
helpers.state.payload.publication.reader_profile.target_language = "zh-TW";
helpers.state.payload.publication.labels.translation = "譯名";
if (
  helpers.labels().translation !== "譯文" ||
  helpers.labels().translatedTerm !== "譯文"
) {
  throw new Error("legacy traditional Chinese translation labels were not normalized");
}
helpers.state.payload.publication.reader_profile.target_language = "zh-CN";
helpers.state.payload.publication.labels = {};
var connectControl = {textContent: ""};
globalThis.document = {
  getElementById: function (id) {
    return id === "arc-connect" ? connectControl : null;
  }
};
helpers.state.directory = null;
helpers.updateDirectoryControl();
if (connectControl.textContent !== "新建保存位置") {
  throw new Error("missing-directory button label changed");
}
helpers.state.directory = {};
helpers.updateDirectoryControl();
if (connectControl.textContent !== "更改保存位置") {
  throw new Error("connected-directory button label changed");
}
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
helpers.setupMarkdown();
var imageMarkup = helpers.state.md.render(
  "![remote](https://example.test/image.png)"
);
if (!imageMarkup.includes("arc-markdown-image")) {
  throw new Error("Markdown image placeholder is missing");
}
if (/\\b(?:src|href)=/.test(imageMarkup)) {
  throw new Error("Markdown image placeholder retained a fetchable URL");
}
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
var requestedDirectory = null;
helpers.state.directory = {
  getDirectoryHandle: function (name, options) {
    requestedDirectory = {name: name, create: options.create};
    return Promise.resolve({name: name});
  }
};
helpers.fragmentsDirectory(true).then(function (handle) {
  if (
    requestedDirectory.name !== "fragments" ||
    requestedDirectory.create !== true ||
    handle.name !== "fragments"
  ) {
    throw new Error("fragment storage used a semantic ID as a directory name");
  }
});
var chunkBlocks = Array.from({length: 95}, function (_value, index) {
  return {block_id: "chunk-block-" + index};
});
var chunks = helpers.buildRenderChunks(chunkBlocks, [{
  block_start: 0,
  block_end: 10,
  level: 1,
  ordinal: 0,
  path: ["front"],
  title: "Front"
}, {
  block_start: 10,
  block_end: 95,
  level: 1,
  ordinal: 1,
  path: ["body"],
  title: "Body"
}]);
var contentChunks = chunks.filter(function (item) {
  return item.kind === "content";
});
if (
  contentChunks[0].block_start !== 0 ||
  contentChunks[contentChunks.length - 1].block_end !== 95 ||
  contentChunks.some(function (item) {
    return item.block_end - item.block_start > 36;
  }) ||
  chunks[chunks.length - 1].kind !== "appendices"
) {
  throw new Error("progressive render chunks lost coverage or bounds");
}
for (var chunkIndex = 1; chunkIndex < contentChunks.length; chunkIndex += 1) {
  if (
    contentChunks[chunkIndex - 1].block_end !==
    contentChunks[chunkIndex].block_start
  ) {
    throw new Error("progressive render chunks contain a gap");
  }
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


def test_reader_steady_save_uses_constant_filesystem_work_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        """
globalThis.window = globalThis;
"""
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    saveEditor: saveEditor,
    installRenderSpies: function (rerender) {
      renderDiagnostics = function () {};
      rerenderChunk = rerender;
    }
  };
}());

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

var source = {
  source_format: "markdown",
  media_type: "text/markdown",
  artifact_digest: "a".repeat(64),
  size: 4,
  rich_document_digest: "b".repeat(64)
};
var anchor = {
  kind: "block",
  target_id: "block-1",
  related_blocks: [{
    block_id: "block-1",
    kind: "paragraph",
    ordinal: 0,
    locator: {line_start: 1},
    content_fingerprint: "c".repeat(64)
  }]
};
var base = {
  schema_version: "arc.render.fragment_revision.v1",
  source: source,
  fragment_id: "note-1",
  revision: 1,
  parent_semantic_digest: null,
  anchor: anchor,
  priority: 110,
  role: "note",
  language: "en",
  title: null,
  citation_ids: [],
  provenance: {producer: "arc-render-browser"},
  markdown_body: "old",
  semantic_digest: "d".repeat(64),
  _origin: "embedded"
};
var nodes = {
  "arc-editor-markdown": {value: "updated"},
  "arc-editor-title": {value: "Updated"},
  "arc-editor-role": {value: "note"},
  "arc-editor-priority": {value: "110"},
  "arc-editor-save": {disabled: false},
  "arc-editor-dialog": {
    close: function () { this.closeCalls += 1; },
    closeCalls: 0,
    querySelectorAll: function () {
      return [
        nodes["arc-editor-title"],
        nodes["arc-editor-role"],
        nodes["arc-editor-priority"],
        nodes["arc-editor-markdown"],
        nodes["arc-editor-save"]
      ];
    }
  },
  "arc-storage-status": {textContent: "", dataset: {}, hidden: true}
};
globalThis.document = {
  getElementById: function (id) {
    if (!nodes[id]) throw new Error("unexpected DOM lookup: " + id);
    return nodes[id];
  }
};

var directoryCalls = [];
var fileCalls = [];
var writes = [];
var closeCalls = 0;
var failClose = false;
var commitOnCloseFailure = false;
var savedContent = "";
var revisionHandle = {
  getFile: function () {
    return Promise.resolve({
      size: savedContent.length,
      text: function () { return Promise.resolve(savedContent); }
    });
  },
  createWritable: function () {
    var pending = "";
    return Promise.resolve({
      write: function (value) { pending = value; writes.push(value); },
      close: function () {
        closeCalls += 1;
        if (failClose) {
          if (commitOnCloseFailure) savedContent = pending;
          return Promise.reject(new Error("close failed"));
        }
        savedContent = pending;
      }
    });
  }
};
var folder = {
  values: function () { throw new Error("steady save scanned revision history"); },
  getFileHandle: function (name, options) {
    fileCalls.push({name: name, create: Boolean(options && options.create)});
    if (!options || !options.create) {
      var missing = new Error("missing");
      missing.name = "NotFoundError";
      return Promise.reject(missing);
    }
    savedContent = "";
    return Promise.resolve(revisionHandle);
  }
};
var helpers = globalThis.__arcReaderTest;
helpers.state.payload = {
  source_identity: source,
  block_fingerprints: {"block-1": "c".repeat(64)},
  revisions: [],
  diagnostics: [],
  publication: {
    source_document: {
      blocks: [{
        block_id: "block-1",
        kind: "paragraph",
        ordinal: 0,
        locator: {line_start: 1},
        payload: {text: "source"}
      }]
    },
    outline: [],
    bibliography: [],
    glossary: [],
    labels: {},
    reader_profile: {source_language: "en", target_language: "en"}
  }
};
helpers.state.directory = {
  getDirectoryHandle: function (name, options) {
    directoryCalls.push({name: name, create: Boolean(options && options.create)});
    if (!options) throw new Error("steady save reloaded the directory");
    return Promise.resolve(folder);
  }
};
helpers.state.revisions = new Map([[base.fragment_id, [base]]]);
helpers.state.selected = new Map([[base.fragment_id, base]]);
helpers.state.fragmentGroups = new Map([[anchor.target_id, [base]]]);
helpers.state.editorBase = base;
helpers.state.editorAnchor = anchor;
helpers.state.readerShellReady = true;
helpers.state.chunkByTargetId = new Map([["block-" + anchor.target_id, {chunk_id: "chunk-1"}]]);
var renderedChunks = [];
helpers.installRenderSpies(function (chunk) { renderedChunks.push(chunk); });

(async function () {
  helpers.state.exportInProgress = true;
  var fileCallsBeforeBlockedSave = fileCalls.length;
  await helpers.saveEditor({preventDefault: function () {}});
  assert(
    fileCalls.length === fileCallsBeforeBlockedSave,
    "save overlapped a latest-content export sync"
  );
  assert(
    nodes["arc-storage-status"].textContent.includes("sync is already in progress"),
    "export/save overlap did not report the shared revision guard"
  );
  helpers.state.exportInProgress = false;
  var first = helpers.saveEditor({preventDefault: function () {}});
  assert(
    nodes["arc-editor-dialog"].querySelectorAll().every(function (control) {
      return control.disabled;
    }),
    "editor controls remained enabled during save"
  );
  var duplicate = helpers.saveEditor({preventDefault: function () {}});
  await Promise.all([first, duplicate]);
  assert(directoryCalls.length === 1, "steady save did not use constant directory work");
  assert(
    directoryCalls[0].name === "fragments" && directoryCalls[0].create,
    "steady save requested the wrong revision directory"
  );
  assert(fileCalls.length === 2, "immutable existence probe/create count changed");
  assert(fileCalls[0].name === fileCalls[1].name, "probe and create names differ");
  assert(!fileCalls[0].create && fileCalls[1].create, "immutable write order changed");
  assert(writes.length === 1 && closeCalls === 1, "duplicate save wrote more than once");
  assert(
    renderedChunks.length === 1 && renderedChunks[0].chunk_id === "chunk-1",
    "steady save did not rerender exactly the affected chunk"
  );
  assert(nodes["arc-editor-dialog"].closeCalls === 1, "successful save did not close once");
  assert(!nodes["arc-editor-save"].disabled, "save button remained disabled");
  var selected = helpers.state.selected.get(base.fragment_id);
  assert(selected.revision === 2, "saved revision was not selected");
  assert(
    helpers.state.revisions.get(base.fragment_id).length === 2,
    "saved revision was not added once"
  );
  assert(
    helpers.state.fragmentGroups.get(anchor.target_id).length === 1 &&
      helpers.state.fragmentGroups.get(anchor.target_id)[0].revision === 2,
    "saved fragment group was not updated in place"
  );
  assert(
    writes[0].includes("Updated") && writes[0].endsWith("updated"),
    "saved bytes do not contain the editor value"
  );

  helpers.state.editorBase = selected;
  nodes["arc-editor-markdown"].value = "failed update";
  failClose = true;
  await helpers.saveEditor({preventDefault: function () {}});
  assert(
    helpers.state.selected.get(base.fragment_id).revision === 2 &&
      helpers.state.revisions.get(base.fragment_id).length === 2,
    "failed close changed in-memory revision state"
  );
  assert(nodes["arc-editor-dialog"].closeCalls === 1, "failed save closed the editor");
  assert(
    nodes["arc-storage-status"].dataset.kind === "error" &&
      nodes["arc-storage-status"].textContent === "close failed",
    "failed save did not report the close error"
  );
  assert(!nodes["arc-editor-save"].disabled, "failed save did not release its guard");

  failClose = false;
  await helpers.saveEditor({preventDefault: function () {}});
  assert(
    helpers.state.selected.get(base.fragment_id).revision === 3,
    "save guard was not reusable after failure"
  );
  var latest = helpers.state.selected.get(base.fragment_id);
  helpers.state.editorBase = latest;
  nodes["arc-editor-markdown"].value = "committed despite close error";
  failClose = true;
  commitOnCloseFailure = true;
  await helpers.saveEditor({preventDefault: function () {}});
  assert(
    helpers.state.selected.get(base.fragment_id).revision === 4,
    "exact readback did not recover an ambiguous close"
  );
  assert(
    nodes["arc-storage-status"].dataset.kind === "info",
    "verified close recovery was reported as a failure"
  );
  failClose = false;
  commitOnCloseFailure = false;
  latest = helpers.state.selected.get(base.fragment_id);
  helpers.state.editorBase = latest;
  helpers.state.revisions.get(base.fragment_id).push(Object.assign({}, latest, {
    revision: 5,
    parent_semantic_digest: latest.semantic_digest,
    semantic_digest: "e".repeat(64),
    anchor: {kind: "block", target_id: "other", related_blocks: []}
  }));
  var fileCallsBeforeStaleSave = fileCalls.length;
  await helpers.saveEditor({preventDefault: function () {}});
  assert(
    fileCalls.length === fileCallsBeforeStaleSave,
    "known next revision was not rejected before filesystem access"
  );
  assert(
    nodes["arc-storage-status"].textContent.includes("current directory revision changed"),
    "known next revision did not report stale editor state"
  );
})().catch(function (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""
    )

    subprocess.run(
        [node, "-"],
        input=instrumented,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reader_directory_sync_skips_embedded_and_caches_external_files() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        """
globalThis.window = globalThis;
"""
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    loadDirectoryRevisions: loadDirectoryRevisions,
    revisionFilename: revisionFilename,
    semanticDigest: semanticDigest,
    stableStringify: stableStringify
  };
}());

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
var helpers = globalThis.__arcReaderTest;
var source = {
  source_format: "markdown",
  media_type: "text/markdown",
  artifact_digest: "a".repeat(64),
  size: 4,
  rich_document_digest: "b".repeat(64)
};
var anchor = {
  kind: "block",
  target_id: "block-1",
  related_blocks: [{
    block_id: "block-1",
    kind: "paragraph",
    ordinal: 0,
    locator: {line_start: 1},
    content_fingerprint: "c".repeat(64)
  }]
};
var baseMetadata = {
  schema_version: "arc.render.fragment_revision.v1",
  source: source,
  fragment_id: "note-1",
  revision: 1,
  parent_semantic_digest: null,
  anchor: anchor,
  priority: 110,
  role: "note",
  language: "en",
  title: null,
  citation_ids: [],
  provenance: {producer: "arc-render-browser"}
};
var baseDigest = "d".repeat(64);
helpers.state.payload = {
  source_identity: source,
  block_fingerprints: {"block-1": "c".repeat(64)},
  diagnostics: [],
  publication: {
    source_document: {
      blocks: [{
        block_id: "block-1",
        kind: "paragraph",
        ordinal: 0,
        locator: {line_start: 1},
        payload: {text: "source"}
      }]
    },
    outline: [],
    bibliography: [],
    labels: {},
    reader_profile: {source_language: "en", target_language: "en"}
  }
};
helpers.state.embeddedRevisions = [{
  metadata: baseMetadata,
  markdown_body: "base",
  semantic_digest: baseDigest
}];
helpers.state.activeFragmentIds = new Set([baseMetadata.fragment_id]);
helpers.state.readerShellReady = false;

var embeddedReads = 0;
var externalGetFileCalls = 0;
var externalTextCalls = 0;
var externalValue = null;
var externalStamp = 1;
var entries = [];
var failEnumeration = false;
var embeddedEntry = {
  kind: "file",
  name: helpers.revisionFilename(1, baseDigest),
  getFile: function () {
    embeddedReads += 1;
    throw new Error("embedded revision bytes should not be read");
  }
};
var externalEntry = {
  kind: "file",
  name: "",
  getFile: function () {
    externalGetFileCalls += 1;
    var captured = externalValue;
    return Promise.resolve({
      size: captured.length,
      lastModified: externalStamp,
      text: function () {
        externalTextCalls += 1;
        return Promise.resolve(captured);
      }
    });
  }
};
var fragments = {
  values: async function* () {
    if (failEnumeration) throw new Error("enumeration failed");
    for (var index = 0; index < entries.length; index += 1) yield entries[index];
  }
};
var directory = {
  getDirectoryHandle: function (name) {
    if (name !== "fragments") throw new Error("unexpected directory: " + name);
    return Promise.resolve(fragments);
  }
};

function encode(metadata, markdown) {
  return "<!-- ARC:FRAGMENT-JSON:BEGIN -->\\n" +
    helpers.stableStringify(metadata) +
    "\\n<!-- ARC:FRAGMENT-JSON:END -->\\n" + markdown;
}

(async function () {
  var childMetadata = JSON.parse(JSON.stringify(baseMetadata));
  childMetadata.revision = 2;
  childMetadata.parent_semantic_digest = baseDigest;
  childMetadata.title = "child";
  var childMarkdown = "external child";
  var childDigest = await helpers.semanticDigest(childMetadata, childMarkdown);
  externalEntry.name = helpers.revisionFilename(2, childDigest);
  externalValue = encode(childMetadata, childMarkdown);
  entries = [embeddedEntry, externalEntry];

  assert(await helpers.loadDirectoryRevisions(directory), "first sync did not commit");
  assert(embeddedReads === 0, "embedded revision was read from disk");
  assert(externalGetFileCalls === 1 && externalTextCalls === 1, "external revision was not read once");
  assert(helpers.state.selected.get("note-1").revision === 2, "external child was not selected");

  assert(await helpers.loadDirectoryRevisions(directory), "cached sync did not commit");
  assert(externalGetFileCalls === 2, "cached sync did not refresh file metadata");
  assert(externalTextCalls === 1, "unchanged external bytes were read again");

  externalValue = "broken";
  externalStamp = 2;
  assert(await helpers.loadDirectoryRevisions(directory), "broken-file sync did not commit");
  assert(externalTextCalls === 2, "changed external bytes were not reread");
  assert(helpers.state.selected.get("note-1").revision === 1, "broken child remained selected");
  assert(
    helpers.state.diagnostics.some(function (value) {
      return value.includes("Ignored invalid fragment file");
    }),
    "broken external file did not produce a diagnostic"
  );

  externalValue = encode(childMetadata, childMarkdown);
  externalStamp = 3;
  assert(await helpers.loadDirectoryRevisions(directory), "repaired-file sync did not commit");
  assert(externalTextCalls === 3, "repaired external bytes were not reread");
  assert(helpers.state.selected.get("note-1").revision === 2, "repaired child was not selected");

  var selectedBeforeFailure = helpers.state.selected.get("note-1").semantic_digest;
  var cacheSizeBeforeFailure = helpers.state.directoryFileCache.size;
  failEnumeration = true;
  var enumerationFailed = false;
  try {
    await helpers.loadDirectoryRevisions(directory);
  } catch (error) {
    enumerationFailed = error.message === "enumeration failed";
  }
  failEnumeration = false;
  assert(enumerationFailed, "directory enumeration failure was hidden");
  assert(
    helpers.state.selected.get("note-1").semantic_digest === selectedBeforeFailure &&
      helpers.state.directoryFileCache.size === cacheSizeBeforeFailure,
    "failed directory sync replaced the last complete snapshot"
  );

  var siblingMetadata = JSON.parse(JSON.stringify(childMetadata));
  siblingMetadata.title = "sibling";
  var siblingMarkdown = "external sibling";
  var siblingDigest = await helpers.semanticDigest(siblingMetadata, siblingMarkdown);
  var siblingValue = encode(siblingMetadata, siblingMarkdown);
  var siblingEntry = {
    kind: "file",
    name: helpers.revisionFilename(2, siblingDigest),
    getFile: function () {
      return Promise.resolve({
        size: siblingValue.length,
        lastModified: 1,
        text: function () { return Promise.resolve(siblingValue); }
      });
    }
  };
  entries = [embeddedEntry, externalEntry, siblingEntry];
  assert(await helpers.loadDirectoryRevisions(directory), "fork sync did not commit");
  assert(helpers.state.selected.get("note-1").revision === 1, "fork did not retain its common parent");
  assert(
    helpers.state.diagnostics.some(function (value) { return value.includes("forks after revision 1"); }),
    "external fork was not diagnosed"
  );

  entries = [embeddedEntry];
  assert(await helpers.loadDirectoryRevisions(directory), "deletion sync did not commit");
  assert(helpers.state.selected.get("note-1").revision === 1, "deleted external files remained selected");
  assert(helpers.state.directoryFileCache.size === 0, "deleted external cache entries survived");
})().catch(function (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""
    )

    subprocess.run(
        [node, "-"],
        input=instrumented,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reader_selection_sync_rerenders_only_changed_anchor_chunk() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        """
globalThis.window = globalThis;
"""
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    refreshChangedSelections: refreshChangedSelections,
    rebuildDiagnostics: rebuildDiagnostics
  };
}());

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
var helpers = globalThis.__arcReaderTest;
var firstRenders = 0;
var secondRenders = 0;
var firstNode = {
  replaceChildren: function () { firstRenders += 1; },
  querySelectorAll: function () { return []; }
};
var secondNode = {
  replaceChildren: function () { secondRenders += 1; },
  querySelectorAll: function () { return []; }
};
var diagnosticsRoot = {
  replaceChildren: function () {},
  appendChild: function () {}
};
globalThis.document = {
  createDocumentFragment: function () { return {}; }
};
var firstChunk = {
  chunk_id: "chunk-1",
  kind: "content",
  block_start: 0,
  block_end: 0
};
var secondChunk = {
  chunk_id: "chunk-2",
  kind: "content",
  block_start: 0,
  block_end: 0
};
var previous = {
  fragment_id: "note-1",
  semantic_digest: "a".repeat(64),
  priority: 110,
  anchor: {kind: "section", target_id: "section-1"}
};
var current = {
  fragment_id: "note-1",
  semantic_digest: "b".repeat(64),
  priority: 110,
  anchor: {kind: "section", target_id: "section-1"}
};
helpers.state.payload = {
  publication: {
    source_document: {blocks: []},
    outline: [{section_id: "section-1", anchor_block_id: "block-1"}]
  }
};
helpers.state.selected = new Map([["note-1", current]]);
helpers.state.diagnostics = [];
helpers.state.readerShellReady = true;
helpers.state.diagnosticsRoot = diagnosticsRoot;
helpers.state.renderPlan = [firstChunk, secondChunk];
helpers.state.renderedChunkIds = new Set(["chunk-1", "chunk-2"]);
helpers.state.chunkNodes = new Map([
  ["chunk-1", firstNode],
  ["chunk-2", secondNode]
]);
helpers.state.chunkByTargetId = new Map([
  ["block-block-1", firstChunk],
  ["block-block-2", secondChunk]
]);
helpers.state.laneFallbackListener = function () {};
helpers.refreshChangedSelections(new Map([["note-1", previous]]));
assert(firstRenders === 1, "changed section anchor chunk was not rerendered once");
assert(secondRenders === 0, "unchanged chunk was rerendered");
current.anchor = {kind: "section", target_id: "missing-section"};
helpers.rebuildDiagnostics();
helpers.rebuildDiagnostics();
assert(
  helpers.state.diagnostics.filter(function (value) {
    return value.includes("unknown anchor");
  }).length === 1,
  "incremental diagnostics lost or duplicated the unknown-anchor warning"
);
"""
    )

    subprocess.run(
        [node, "-"],
        input=instrumented,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reader_markdown_export_uses_latest_or_embedded_change_scope() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        """
globalThis.window = globalThis;
"""
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    buildRoleMarkdown: buildRoleMarkdown,
    captureInitialSelection: captureInitialSelection,
    exportRevisionState: exportRevisionState
  };
}());

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
var helpers = globalThis.__arcReaderTest;
var source = {
  source_format: "markdown",
  media_type: "text/markdown",
  artifact_digest: "a".repeat(64),
  size: 4,
  rich_document_digest: "b".repeat(64)
};
var blocks = [
  {block_id: "block-1", kind: "paragraph", ordinal: 0, locator: {}, payload: {text: "one"}},
  {block_id: "block-2", kind: "paragraph", ordinal: 1, locator: {}, payload: {text: "two"}}
];
function revision(fragmentId, digest, role, target, body, priority, number, parent, title) {
  return {
    schema_version: "arc.render.fragment_revision.v1",
    source: source,
    fragment_id: fragmentId,
    revision: number,
    parent_semantic_digest: parent,
    anchor: {kind: "block", target_id: target, related_blocks: []},
    priority: priority,
    role: role,
    language: "zh-CN",
    title: title || null,
    citation_ids: [],
    provenance: {producer: "arc-render-browser"},
    markdown_body: body,
    semantic_digest: digest
  };
}
var first = revision("translation-1", "1".repeat(64), "translation", "block-1", "旧译文", 10, 1, null);
var unchanged = revision("translation-2", "2".repeat(64), "translation", "block-2", "未改译文", 10, 1, null);
var companion = revision("companion-1", "3".repeat(64), "companion", "block-1", "未改伴读", 20, 1, null);
helpers.state.payload = {
  publication: {
    source_document: {blocks: blocks},
    outline: [],
    labels: {document_title: "Fearful Symmetry", translation: "译名"},
    reader_profile: {target_language: "zh-CN"}
  }
};
helpers.state.selected = new Map([
  [first.fragment_id, first],
  [unchanged.fragment_id, unchanged],
  [companion.fragment_id, companion]
]);
helpers.captureInitialSelection();
var revised = revision(
  first.fragment_id, "4".repeat(64), "translation", "block-1",
  "最新译文", 10, 2, first.semantic_digest, "改动标题"
);
var note = revision("note-1", "5".repeat(64), "note", "block-2", "新增笔记", 110, 1, null);
helpers.state.selected = new Map([
  [first.fragment_id, revised],
  [unchanged.fragment_id, unchanged],
  [companion.fragment_id, companion],
  [note.fragment_id, note]
]);
helpers.state.revisions = new Map([
  [first.fragment_id, [first, revised]],
  [unchanged.fragment_id, [unchanged]],
  [companion.fragment_id, [companion]],
  [note.fragment_id, [note]]
]);
helpers.state.activeFragmentIds = new Set([
  first.fragment_id, unchanged.fragment_id, companion.fragment_id
]);
var all = helpers.buildRoleMarkdown("translation", "all");
var changed = helpers.buildRoleMarkdown("translation", "changed");
assert(all.includes("# Fearful Symmetry — 译文"), "role label did not use 译文");
assert(all.includes("最新译文") && all.includes("未改译文"), "all-latest Markdown omitted content");
assert(changed.includes("最新译文"), "changed Markdown omitted the revised fragment");
assert(!changed.includes("未改译文"), "changed Markdown included an unchanged fragment");
assert(changed.includes("## 改动标题"), "fragment title was not exported");
assert(
  helpers.buildRoleMarkdown("companion", "changed") === "",
  "unchanged companion content was exported as changed"
);
assert(
  helpers.buildRoleMarkdown("note", "changed").includes("新增笔记"),
  "new browser fragment was not exported as changed"
);
var exported = helpers.exportRevisionState();
assert(exported.revisions.length === 5, "full export omitted a revision history entry");
assert(
  exported.selected_revision_digests.join(",") === [
    revised.semantic_digest,
    unchanged.semantic_digest,
    companion.semantic_digest,
    note.semantic_digest
  ].join(","),
  "full export selected digest order changed"
);
"""
    )

    subprocess.run(
        [node, "-"],
        input=instrumented,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reader_scans_nested_revision_directories_with_bounded_concurrency() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        """
globalThis.window = globalThis;
"""
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {collectMarkdownFiles: collectMarkdownFiles};
}());

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
var active = 0;
var maximum = 0;
function directory(name) {
  return {
    kind: "directory",
    name: name,
    values: async function* () {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise(function (resolve) { setTimeout(resolve, 5); });
      yield {
        kind: "file",
        name: "revision-" + name + ".md",
        getFile: function () { throw new Error("file bytes were read during enumeration"); }
      };
      active -= 1;
    }
  };
}
var directories = Array.from({length: 24}, function (_value, index) {
  return directory(String(24 - index).padStart(2, "0"));
});
var root = {
  values: async function* () {
    for (var index = 0; index < directories.length; index += 1) {
      yield directories[index];
    }
  }
};
(async function () {
  var files = await globalThis.__arcReaderTest.collectMarkdownFiles(root);
  assert(maximum === 8, "directory enumeration was not bounded at eight");
  assert(files.length === 24, "directory enumeration lost files");
  var paths = files.map(function (entry) { return entry.path.join("/"); });
  var sorted = paths.slice().sort();
  assert(paths.join(",") === sorted.join(","), "directory results were not deterministic");
})().catch(function (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""
    )

    subprocess.run(
        [node, "-"],
        input=instrumented,
        check=True,
        capture_output=True,
        text=True,
    )


def test_export_panel_syncs_external_changes_before_building_roles() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    javascript = _text("reader.js")
    startup = javascript.rfind("\n  if (document.readyState")
    assert startup > 0
    instrumented = (
        "globalThis.window = globalThis;\n"
        + javascript[:startup]
        + """
  globalThis.__arcReaderTest = {
    state: state,
    openExportPanel: openExportPanel,
    semanticDigest: semanticDigest,
    stableStringify: stableStringify
  };
}());

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
var helpers = globalThis.__arcReaderTest;
var source = {
  source_format: "markdown",
  media_type: "text/markdown",
  artifact_digest: "a".repeat(64),
  size: 4,
  rich_document_digest: "b".repeat(64)
};
var anchor = {
  kind: "block",
  target_id: "block-1",
  related_blocks: [{
    block_id: "block-1",
    kind: "paragraph",
    ordinal: 0,
    locator: {line_start: 1},
    content_fingerprint: "c".repeat(64)
  }]
};
helpers.state.payload = {
  source_identity: source,
  block_fingerprints: {"block-1": "c".repeat(64)},
  revisions: [],
  diagnostics: [],
  publication: {
    source_document: {
      blocks: [{
        block_id: "block-1",
        kind: "paragraph",
        ordinal: 0,
        locator: {line_start: 1},
        payload: {text: "source"}
      }]
    },
    outline: [],
    bibliography: [],
    labels: {},
    reader_profile: {title: "Reader", target_language: "en"}
  }
};
helpers.state.embeddedRevisions = [];
helpers.state.activeFragmentIds = new Set();
helpers.state.readerShellReady = false;
helpers.state.initialSelectedDigests = new Map();

var roleButtons = [];
var scopeInput = {value: "changed", checked: true, disabled: false};
var otherScopeInput = {value: "all", checked: false, disabled: false};
var nodes = {
  "arc-export": {
    attrs: {},
    setAttribute: function (name, value) { this.attrs[name] = value; }
  },
  "arc-export-panel": {hidden: true},
  "arc-export-role-options": {
    replaceChildren: function () { roleButtons = []; },
    appendChild: function (child) { roleButtons.push(child); }
  },
  "arc-export-empty": {hidden: true},
  "arc-export-html": {disabled: false},
  "arc-storage-status": {textContent: "", dataset: {}, hidden: true}
};
globalThis.document = {
  getElementById: function (id) { return nodes[id]; },
  querySelector: function (selector) {
    if (selector.includes("arc-export-scope")) return scopeInput;
    throw new Error("unexpected selector: " + selector);
  },
  querySelectorAll: function (selector) {
    if (selector.includes("arc-export-scope")) {
      return [scopeInput, otherScopeInput];
    }
    throw new Error("unexpected selector: " + selector);
  },
  createElement: function (tag) {
    return {
      tagName: tag,
      disabled: false,
      addEventListener: function () {}
    };
  }
};

(async function () {
  var metadata = {
    schema_version: "arc.render.fragment_revision.v1",
    source: source,
    fragment_id: "external-note",
    revision: 1,
    parent_semantic_digest: null,
    anchor: anchor,
    priority: 110,
    role: "note",
    language: "en",
    title: "External note",
    citation_ids: [],
    provenance: {producer: "arc-render-browser"}
  };
  var markdown = "new external change";
  var digest = await helpers.semanticDigest(metadata, markdown);
  var encoded = "<!-- ARC:FRAGMENT-JSON:BEGIN -->\\n" +
    helpers.stableStringify(metadata) +
    "\\n<!-- ARC:FRAGMENT-JSON:END -->\\n" + markdown;
  var file = {
    kind: "file",
    name: "revision-000001-" + digest + ".md",
    getFile: function () {
      return Promise.resolve({
        size: encoded.length,
        lastModified: 1,
        text: function () { return Promise.resolve(encoded); }
      });
    }
  };
  var fragments = {
    values: async function* () { yield file; }
  };
  helpers.state.directory = {
    getDirectoryHandle: function (name) {
      assert(name === "fragments", "export sync requested the wrong directory");
      return Promise.resolve(fragments);
    }
  };
  assert(roleButtons.length === 0, "fixture unexpectedly started with a role");
  await helpers.openExportPanel();
  assert(
    helpers.state.selected.get("external-note").semantic_digest === digest,
    "opening export did not synchronize the external latest revision"
  );
  assert(
    roleButtons.length === 1 && roleButtons[0].textContent === "Note => MD",
    "export roles were rendered before the synchronized selection"
  );
  assert(
    nodes["arc-export"].attrs["aria-expanded"] === "true",
    "synchronized export panel did not remain open"
  );
})().catch(function (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
});
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
    assert "var revisions = new Map();" in javascript
    assert "var DIRECTORY_READ_CONCURRENCY = 8;" in javascript
    assert "Math.min(DIRECTORY_READ_CONCURRENCY, files.length)" in javascript
    assert "commitDirectorySnapshot(" in javascript
    assert "await loadDirectoryRevisions(handle)" in javascript
    assert "current.semantic_digest !== base.semantic_digest" in javascript
    assert "nextChildren.length > 0" in javascript
    assert "throw new Error(labels().historyChanged)" in javascript
    assert "var folder = await fragmentsDirectory(true);" in javascript
    assert "async function fragmentsDirectory(create)" in javascript
    assert "getDirectoryHandle(fragmentId" not in javascript
    assert "fragmentDirectory(metadata.fragment_id" not in javascript


def test_reader_enforces_strict_browser_revision_contract() -> None:
    javascript = _text("reader.js")

    assert "validateSourceIdentity(metadata.source)" in javascript
    assert "validateAnchor(metadata.anchor)" in javascript
    assert "validateIntegerJson(metadata, \"fragment revision\")" in javascript
    assert "Number.isSafeInteger(value)" in javascript
    assert "anchor related block differs from the rich source" in javascript
    assert "assertKnownCitations(metadata.citation_ids)" in javascript


def test_reader_uses_low_distraction_controls_and_collapsed_advanced_editor() -> None:
    javascript = _text("reader.js")
    stylesheet = _text("reader.css")

    for reader_facing_term in (
        "添加外挂",
        "编辑外挂",
        "新建外挂",
        "另存为新版本",
        "Add overlay",
        "Edit overlay",
        "New overlay",
        "Save as new revision",
    ):
        assert reader_facing_term not in javascript
    assert '"arc-editor-cancel").textContent = strings.cancel' in javascript
    assert '"arc-editor-save").textContent = strings.save' in javascript
    assert "await restoreDirectoryHandle();" in javascript
    assert "await setupEditor();" in javascript
    assert "arc-history-compare" in javascript
    assert "restoreHistoricalRevision" in javascript
    assert (
        '"arc-note-button arc-icon-button", "+", labels().addNote'
        in javascript
    )
    assert (
        '"arc-edit-button arc-icon-button", "✎", labels().edit'
        in javascript
    )
    assert "openNewSectionEditor" not in javascript
    assert (
        'document.getElementById("arc-editor-advanced").open = false'
        in javascript
    )
    assert ".arc-history-compare" in stylesheet
    assert ".arc-section-note-button" not in stylesheet
    assert ".arc-icon-button" in stylesheet
    assert "position: fixed" in stylesheet
    assert 'content: "▸"' in stylesheet
    assert ".arc-editor-advanced[open]" in stylesheet
    assert "width: min(29rem, calc(100vw - 2rem))" in stylesheet
    assert "height: min(39.5rem, calc(100dvh - 2rem))" in stylesheet
    assert ".arc-source-row {\n  position: relative;\n  margin: .35rem 0" in stylesheet
    assert "background: transparent;" in stylesheet
    assert "border: 0;\n  border-radius: 0;" in stylesheet
    assert "newSaveLocation" in javascript
    assert "changeSaveLocation" in javascript
    assert "buildStandaloneExportHtml" in javascript
    assert "collectMarkdownFiles" in javascript


def test_reader_progressively_hydrates_navigation_find_and_print_content() -> None:
    javascript = _text("reader.js")
    stylesheet = _text("reader.css")

    assert "MAX_BLOCKS_PER_RENDER_CHUNK = 36" in javascript
    assert "buildRenderChunks(" in javascript
    assert "new IntersectionObserver" in javascript
    assert "window.requestIdleCallback" in javascript
    assert 'window.addEventListener("beforeprint", renderAllChunks)' in javascript
    assert "activateHashTarget(href, true)" in javascript
    assert "refreshChangedSelections(previousSelected);" in javascript
    assert "refreshChunkForAnchor(revision.anchor);" in javascript
    assert 'document.body.dataset.arcRenderComplete = String(complete)' in javascript
    assert "state.citationNumberCache" in javascript
    assert "state.glossarySurfaceCache[layer]" in javascript
    assert javascript.count("renderReader();") == 1
    assert ".arc-render-chunk:not(.is-rendered)" in stylesheet
    assert "content-visibility: auto" in stylesheet
    assert "content-visibility: visible !important" in stylesheet


def test_reader_preserves_source_text_and_glossary_rendering_contracts() -> None:
    javascript = _text("reader.js")

    assert "removeVisibleHtmlTags(heading)" in javascript
    assert "removeVisibleHtmlTags(table)" in javascript
    assert "removeVisibleHtmlTags(figure)" in javascript
    assert 'decorateGlossary(original, "source")' in javascript
    assert 'decorateGlossary(translated, "target")' in javascript
    assert "equation_label_reconciliation" in javascript
    assert "reconciliation.effective_label.trim()" in javascript


def test_reader_uses_explicit_outline_for_navigation_and_section_anchors() -> None:
    javascript = _text("reader.js")

    assert (
        "renderContents(contents, publication.outline || [], strings)"
        in javascript
    )
    assert "state.payload.publication.outline || []" in javascript
    assert 'target = section ? section.anchor_block_id : null' in javascript
    assert 'safeToken(section.anchor_block_id)' in javascript
