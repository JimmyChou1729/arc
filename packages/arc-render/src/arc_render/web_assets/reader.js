(function () {
  "use strict";

  var FRONT_BEGIN = "<!-- ARC:FRAGMENT-JSON:BEGIN -->";
  var FRONT_END = "<!-- ARC:FRAGMENT-JSON:END -->";
  var FRAGMENT_SCHEMA = "arc.render.fragment_revision.v1";
  var MAX_BLOCKS_PER_RENDER_CHUNK = 36;
  var CHUNK_BLOCK_HEIGHT_ESTIMATE = 220;
  var DIRECTORY_READ_CONCURRENCY = 8;
  var STATUS_EXPIRY_MS = 10000;
  var state = {
    payload: null,
    md: null,
    revisions: new Map(),
    revisionDigests: new Map(),
    selected: new Map(),
    embeddedRevisions: [],
    activeFragmentIds: new Set(),
    diagnostics: [],
    fileDiagnostics: [],
    resolutionDiagnostics: new Map(),
    directory: null,
    directoryCacheHandle: null,
    directoryFileCache: new Map(),
    directoryLoadGeneration: 0,
    directorySelectionInProgress: false,
    saveInProgress: false,
    exportInProgress: false,
    activeDraft: null,
    editorGeneration: 0,
    editorPreviewDirty: true,
    editorPreviewTimer: null,
    statusTimer: null,
    editorBase: null,
    editorAnchor: null,
    editorHistorical: null,
    citationNumberCache: null,
    glossarySurfaceCache: {source: null, target: null},
    indexedPayload: null,
    sourceIndexes: null,
    sourceIdentityJson: null,
    sourceIdentityValidatedPayload: null,
    fragmentGroups: new Map(),
    renderPlan: [],
    renderedChunkIds: new Set(),
    chunkNodes: new Map(),
    chunkByTargetId: new Map(),
    chunkObserver: null,
    laneObserver: null,
    laneFallbackListener: null,
    idleRenderHandle: null,
    idleRenderUsesCallback: false,
    idleRenderGeneration: 0,
    hydrationQuietTimer: null,
    hydrationActivityReady: false,
    hydrationOrder: [],
    hashCalibration: null,
    diagnosticsRoot: null,
    readerShellReady: false,
    navigationReady: false,
    printReady: false,
    initialSelectedDigests: new Map(),
    exportHtmlTemplate: null,
    exportStandaloneSupported: false
  };

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function iconButton(className, symbol, accessibleLabel) {
    var button = element("button", className, symbol);
    button.type = "button";
    button.setAttribute("aria-label", accessibleLabel);
    button.title = accessibleLabel;
    return button;
  }

  function readPayload() {
    var node = document.getElementById("arc-render-payload");
    if (!node) throw new Error("ARC render payload is missing");
    var value = JSON.parse(node.textContent || "");
    if (!value || value.schema_version !== "arc.render.reader_payload.v1") {
      throw new Error("unsupported ARC reader payload");
    }
    if (!value.publication || !value.publication.source_document) {
      throw new Error("ARC reader payload has no source document");
    }
    return value;
  }

  function buildSourceIndexes() {
    var publication = state.payload.publication;
    var documentValue = publication.source_document;
    var blocksById = new Map();
    (documentValue.blocks || []).forEach(function (block) {
      blocksById.set(block.block_id, block);
    });
    var sectionsById = new Map();
    var sectionAnchors = new Map();
    (publication.outline || []).forEach(function (section) {
      sectionsById.set(section.section_id, section);
      sectionAnchors.set(section.section_id, section.anchor_block_id);
    });
    var resourcesByDigest = new Map();
    var resourcesByLogicalName = new Map();
    var resources = state.payload.resources || [];
    resources.forEach(function (resource) {
      var digest = resource.artifact_digest || resource.digest;
      if (digest) resourcesByDigest.set(digest, resource);
      if (resource.logical_name) {
        resourcesByLogicalName.set(resource.logical_name, resource);
      }
    });
    state.sourceIndexes = {
      blocksById: blocksById,
      sectionsById: sectionsById,
      sectionAnchors: sectionAnchors,
      resourcesByDigest: resourcesByDigest,
      resourcesByLogicalName: resourcesByLogicalName,
      resourceCount: resources.length
    };
    state.sourceIdentityJson = state.payload.source_identity ?
      stableStringify(state.payload.source_identity) : null;
    state.indexedPayload = state.payload;
  }

  function ensureSourceIndexes() {
    if (
      state.indexedPayload !== state.payload ||
      !state.sourceIndexes ||
      state.sourceIndexes.resourceCount !== (state.payload.resources || []).length
    ) {
      buildSourceIndexes();
    }
    return state.sourceIndexes;
  }

  function labels() {
    var publication = state.payload.publication;
    var language = String(
      (publication.reader_profile || {}).target_language || ""
    ).toLowerCase();
    var chinese = language === "zh" || language.indexOf("zh-") === 0;
    var traditional = chinese && /(?:^|-)hant(?:-|$)|(?:^|-)(?:tw|hk|mo)(?:-|$)/.test(
      language
    );
    var defaults = chinese ? {
      contents: "目录",
      collapse: "收起目录",
      expand: "展开目录",
      newSaveLocation: "新建保存位置",
      changeSaveLocation: "更改保存位置",
      connected: "保存位置已设置",
      edit: "编辑",
      addNote: "添加",
      editor: "编辑",
      newNote: "添加",
      title: "标题",
      role: "类型",
      priority: "优先级",
      advanced: "预览与更多设置",
      advancedAction: "高级",
      markdown: "Markdown",
      preview: "预览",
      save: "保存",
      cancel: "取消",
      close: "关闭",
      history: "版本历史",
      source: "原文",
      translation: traditional ? "譯文" : "译文",
      companion: "伴读",
      guide: "导读",
      note: "笔记",
      glossary: "术语表",
      references: "参考文献",
      originalTerm: "原文术语",
      translatedTerm: traditional ? "譯文" : "译文",
      definition: "释义",
      export: "导出",
      markdownScope: "Markdown 内容",
      allLatest: "全部最新版",
      changedLatest: "仅最新版改动",
      noExportChanges: "没有可导出的改动",
      fullHtml: "全文 => 单个 HTML",
      exportUnavailable: "当前页面不是完整的单文件阅读器，无法导出单个 HTML。",
      exportLoading: "正在同步最新版内容……",
      exportSyncFailed: "未能同步最新版内容，导出已取消。",
      exportStarted: "已开始导出。",
      revisionBusy: "正在保存或同步最新版内容，请稍候。",
      noDirectoryApi: "当前浏览器不支持本地目录编辑；阅读功能不受影响。",
      saveSuccess: "新版本已保存。",
      saveUnchanged: "内容没有变化。",
      draftActive: "请先保存或取消当前编辑。",
      editContent: "编辑这段 Markdown",
      loading: "正在读取版本……",
      historyChanged: "目录中的当前版本已变化；请关闭编辑器并重新打开后再保存。",
      unknownCitation: "引用不在当前参考文献中：",
      compareCurrent: "当前版本",
      compareHistorical: "历史版本",
      restore: "恢复为新版本",
      imageOmitted: "图片未加载"
    } : {
      contents: "Contents",
      collapse: "Collapse contents",
      expand: "Expand contents",
      newSaveLocation: "New save location",
      changeSaveLocation: "Change save location",
      connected: "Save location ready",
      edit: "Edit",
      addNote: "Add",
      editor: "Edit",
      newNote: "Add",
      title: "Title",
      role: "Role",
      priority: "Priority",
      advanced: "Preview and more settings",
      advancedAction: "Advanced",
      markdown: "Markdown",
      preview: "Preview",
      save: "Save",
      cancel: "Cancel",
      close: "Close",
      history: "Revision history",
      source: "Source",
      translation: "Translation",
      companion: "Companion",
      guide: "Guide",
      note: "Note",
      glossary: "Glossary",
      references: "References",
      originalTerm: "Original term",
      translatedTerm: "Translation",
      definition: "Definition",
      export: "Export",
      markdownScope: "Markdown content",
      allLatest: "All latest",
      changedLatest: "Latest changes only",
      noExportChanges: "No changed content to export",
      fullHtml: "Full text => Single HTML",
      exportUnavailable: "This page is not a complete standalone reader and cannot export a single HTML file.",
      exportLoading: "Synchronizing latest content…",
      exportSyncFailed: "Latest content could not be synchronized; export was cancelled.",
      exportStarted: "Export started.",
      revisionBusy: "A save or latest-content sync is already in progress.",
      noDirectoryApi: "This browser cannot edit a local directory; reading is unaffected.",
      saveSuccess: "A new revision was saved.",
      saveUnchanged: "Content is unchanged.",
      draftActive: "Save or cancel the current edit first.",
      editContent: "Edit this Markdown",
      loading: "Loading revisions…",
      historyChanged: "The current directory revision changed; close and reopen the editor before saving.",
      unknownCitation: "Citation is absent from the bibliography: ",
      compareCurrent: "Current revision",
      compareHistorical: "Historical revision",
      restore: "Restore as new revision",
      imageOmitted: "Image not loaded"
    };
    Object.keys(publication.labels || {}).forEach(function (key) {
      defaults[key] = publication.labels[key];
    });
    if (chinese && ["译名", "翻译", "譯名", "翻譯"].indexOf(defaults.translation) >= 0) {
      defaults.translation = traditional ? "譯文" : "译文";
    }
    if (chinese && ["译名", "翻译", "譯名", "翻譯"].indexOf(defaults.translatedTerm) >= 0) {
      defaults.translatedTerm = traditional ? "譯文" : "译文";
    }
    return defaults;
  }

  function setupMarkdown() {
    if (typeof window.markdownit !== "function") {
      throw new Error("embedded markdown-it is unavailable");
    }
    var md = window.markdownit("commonmark", {
      html: false,
      linkify: false,
      typographer: false,
      breaks: false
    });

    md.inline.ruler.before("escape", "arc_math_inline", function (parserState, silent) {
      var source = parserState.src;
      var position = parserState.pos;
      var open;
      var close;
      if (source.slice(position, position + 2) === "\\(") {
        open = "\\(";
        close = "\\)";
      } else if (
        source.charAt(position) === "$" &&
        source.slice(position, position + 2) !== "$$" &&
        (position === 0 || source.charAt(position - 1) !== "\\")
      ) {
        open = "$";
        close = "$";
      } else {
        return false;
      }
      var start = position + open.length;
      var end = source.indexOf(close, start);
      while (
        end >= 0 &&
        close === "$" &&
        source.charAt(end - 1) === "\\"
      ) {
        end = source.indexOf(close, end + 1);
      }
      if (end < 0 || end === start || source.slice(start, end).indexOf("\n") >= 0) {
        return false;
      }
      if (!silent) {
        var token = parserState.push("arc_math_inline", "span", 0);
        token.content = source.slice(start, end).trim();
      }
      parserState.pos = end + close.length;
      return true;
    });

    md.inline.ruler.before("text", "arc_citation", function (parserState, silent) {
      var match = /^\[@([A-Za-z0-9][A-Za-z0-9._:-]*)\]/.exec(
        parserState.src.slice(parserState.pos)
      );
      if (!match) return false;
      if (!silent) {
        var token = parserState.push("arc_citation", "a", 0);
        token.content = match[1];
      }
      parserState.pos += match[0].length;
      return true;
    });

    md.block.ruler.before("fence", "arc_math_block", function (
      parserState, startLine, endLine, silent
    ) {
      var begin = parserState.bMarks[startLine] + parserState.tShift[startLine];
      var maximum = parserState.eMarks[startLine];
      var opening = parserState.src.slice(begin, maximum).trim();
      var closing = opening === "$$" ? "$$" : opening === "\\[" ? "\\]" : null;
      if (!closing) return false;
      var line = startLine + 1;
      while (line < endLine) {
        var lineStart = parserState.bMarks[line] + parserState.tShift[line];
        var lineEnd = parserState.eMarks[line];
        if (parserState.src.slice(lineStart, lineEnd).trim() === closing) break;
        line += 1;
      }
      if (line >= endLine) return false;
      if (silent) return true;
      var token = parserState.push("arc_math_block", "div", 0);
      token.block = true;
      token.map = [startLine, line + 1];
      token.content = parserState.getLines(
        startLine + 1, line, parserState.blkIndent, false
      ).trim();
      parserState.line = line + 1;
      return true;
    });

    md.renderer.rules.arc_math_inline = function (tokens, index) {
      return '<span class="math math-inline" data-tex="' +
        md.utils.escapeHtml(tokens[index].content) + '">' +
        md.utils.escapeHtml(tokens[index].content) + "</span>";
    };
    md.renderer.rules.arc_math_block = function (tokens, index) {
      return '<div class="math math-display" data-tex="' +
        md.utils.escapeHtml(tokens[index].content) + '">' +
        md.utils.escapeHtml(tokens[index].content) + "</div>";
    };
    md.renderer.rules.arc_citation = function (tokens, index, _options, env) {
      var citationId = tokens[index].content;
      var number = (env.citationNumbers || {})[citationId];
      var visible = number === undefined ? "?" : String(number);
      return '<a class="arc-citation" href="#reference-' +
        md.utils.escapeHtml(citationId) + '">[' +
        md.utils.escapeHtml(visible) + "]</a>";
    };
    md.renderer.rules.image = function (tokens, index) {
      var token = tokens[index];
      var alternative = md.utils.escapeHtml(token.content || "");
      var source = token.attrGet("src") || "";
      var resource = resourceForLogicalName(source);
      if (resource && typeof resource.data_uri === "string") {
        return '<img class="arc-markdown-image" src="' +
          md.utils.escapeHtml(resource.data_uri) + '" alt="' + alternative +
          '" loading="lazy" decoding="async">';
      }
      var text = md.utils.escapeHtml(labels().imageOmitted);
      return '<span class="arc-markdown-image" role="note">[' + text +
        (alternative ? ": " + alternative : "") + "]</span>";
    };
    var defaultLinkOpen = md.renderer.rules.link_open || function (
      tokens, index, options, _env, renderer
    ) {
      return renderer.renderToken(tokens, index, options);
    };
    md.renderer.rules.link_open = function (tokens, index, options, env, renderer) {
      tokens[index].attrSet("rel", "noopener noreferrer");
      return defaultLinkOpen(tokens, index, options, env, renderer);
    };
    state.md = md;
  }

  function citationNumbers() {
    if (state.citationNumberCache) return state.citationNumberCache;
    var values = {};
    (state.payload.publication.bibliography || []).forEach(function (item, index) {
      var id = item.evidence_id || item.citation_id || item.id;
      if (id) values[id] = index + 1;
    });
    state.citationNumberCache = values;
    return state.citationNumberCache;
  }

  function renderMarkdown(markdown) {
    var wrapper = element("div", "arc-markdown");
    wrapper.innerHTML = state.md.render(normalizeMarkdown(markdown), {
      citationNumbers: citationNumbers()
    });
    removeVisibleHtmlTags(wrapper);
    decorateGlossary(wrapper, "target");
    typeset(wrapper);
    return wrapper;
  }

  function normalizeMarkdown(value) {
    var normalized = String(value || "").replace(/\r\n?/g, "\n").normalize("NFC");
    if (normalized.indexOf("\u0000") >= 0) {
      throw new Error("Markdown body cannot contain NUL");
    }
    return normalized;
  }

  function removeVisibleHtmlTags(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [];
    var current;
    while ((current = walker.nextNode())) nodes.push(current);
    nodes.forEach(function (node) {
      if (node.parentElement && node.parentElement.closest("code, pre, .math")) return;
      node.nodeValue = node.nodeValue.replace(/<\/?[A-Za-z][^>]*>/g, "");
    });
  }

  function initialRevisions() {
    ensureSourceIndexes();
    state.embeddedRevisions = (state.payload.revisions || []).slice();
    state.activeFragmentIds = new Set(state.embeddedRevisions.map(function (raw) {
      return raw.metadata ? raw.metadata.fragment_id : raw.fragment_id;
    }));
    resetRevisionState();
    resolveAll();
  }

  function resetRevisionState() {
    state.revisions = new Map();
    state.revisionDigests = new Map();
    state.fileDiagnostics = [];
    state.embeddedRevisions.forEach(addRevision);
  }

  function addRevision(raw) {
    addRevisionTo(
      raw, state.revisions, state.fileDiagnostics, state.revisionDigests
    );
  }

  function addRevisionTo(raw, revisions, fileDiagnostics, revisionDigests) {
    var revision = raw.metadata ? Object.assign({}, raw.metadata) : Object.assign({}, raw);
    revision.markdown_body = raw.markdown_body === undefined ?
      revision.markdown_body || "" : raw.markdown_body;
    revision.semantic_digest = raw.semantic_digest || revision.semantic_digest || "";
    revision._origin = raw._origin || "embedded";
    try {
      validateRevisionMetadata(metadataOnly(revision));
    } catch (error) {
      fileDiagnostics.push(
        "Ignored invalid fragment " + (revision.fragment_id || "(unknown)") + ": " +
        String(error.message || error)
      );
      return;
    }
    var values = revisions.get(revision.fragment_id) || [];
    var digests = revisionDigests && (
      revisionDigests.get(revision.fragment_id) || new Set()
    );
    if (!digests || !digests.has(revision.semantic_digest)) {
      values.push(revision);
      revisions.set(revision.fragment_id, values);
      if (digests) {
        digests.add(revision.semantic_digest);
        revisionDigests.set(revision.fragment_id, digests);
      }
    }
  }

  function resolveAll() {
    state.selected.clear();
    state.resolutionDiagnostics = new Map();
    state.revisions.forEach(function (values, fragmentId) {
      resolveFragmentInState(fragmentId, values);
    });
    rebuildDiagnostics();
  }

  function resolveOne(fragmentId) {
    resolveFragmentInState(fragmentId, state.revisions.get(fragmentId) || []);
    rebuildDiagnostics();
  }

  function resolveFragmentInState(fragmentId, values) {
    state.selected.delete(fragmentId);
    state.resolutionDiagnostics.delete(fragmentId);
    if (
      !state.activeFragmentIds.has(fragmentId) &&
      !browserCreatedHistory(values)
    ) {
      return;
    }
    var resolved = resolveFragment(values, fragmentId);
    if (resolved.selected) state.selected.set(fragmentId, resolved.selected);
    state.resolutionDiagnostics.set(fragmentId, resolved.diagnostics);
  }

  function rebuildDiagnostics() {
    state.diagnostics = (state.payload.diagnostics || []).slice().concat(
      state.fileDiagnostics
    );
    state.resolutionDiagnostics.forEach(function (values) {
      state.diagnostics = state.diagnostics.concat(values);
    });
    state.diagnostics = state.diagnostics.concat(anchorDiagnostics());
  }

  function anchorDiagnostics() {
    var diagnostics = [];
    state.selected.forEach(function (fragment) {
      if (!fragmentTargetId(fragment)) {
        diagnostics.push(
          "Fragment " + fragment.fragment_id + " has an unknown anchor."
        );
      }
    });
    return diagnostics;
  }

  function browserCreatedHistory(values) {
    var roots = values.filter(function (item) {
      return item.revision === 1 && item.parent_semantic_digest === null;
    });
    roots = Array.from(new Map(roots.map(function (item) {
      return [item.semantic_digest, item];
    })).values());
    return roots.length === 1 &&
      roots[0].provenance &&
      roots[0].provenance.producer === "arc-render-browser";
  }

  function resolveFragment(values, fragmentId) {
    var diagnostics = [];
    var byDigest = new Map();
    var childrenByParent = new Map();
    values.forEach(function (item) {
      if (item.semantic_digest) byDigest.set(item.semantic_digest, item);
      if (item.revision <= 1 || !item.parent_semantic_digest) return;
      var children = childrenByParent.get(item.parent_semantic_digest) || new Map();
      children.set(item.semantic_digest, item);
      childrenByParent.set(item.parent_semantic_digest, children);
    });
    var roots = values.filter(function (item) {
      return item.revision === 1 && item.parent_semantic_digest === null;
    });
    var uniqueRoots = Array.from(new Map(roots.map(function (item) {
      return [item.semantic_digest, item];
    })).values());
    if (uniqueRoots.length !== 1) {
      diagnostics.push(
        "Fragment " + fragmentId + " has " + uniqueRoots.length +
        " initial revisions; no revision was selected."
      );
      return {selected: null, diagnostics: diagnostics};
    }
    var current = uniqueRoots[0];
    while (true) {
      var children = Array.from(
        (childrenByParent.get(current.semantic_digest) || new Map()).values()
      ).filter(function (item) {
        return item.revision === current.revision + 1;
      });
      if (children.length === 0) break;
      if (children.length > 1) {
        diagnostics.push(
          "Fragment " + fragmentId + " forks after revision " +
          current.revision + "; the common parent remains visible."
        );
        break;
      }
      if (
        stableStringify(children[0].source) !== stableStringify(current.source) ||
        stableStringify(children[0].anchor) !== stableStringify(current.anchor)
      ) {
        diagnostics.push(
          "Fragment " + fragmentId +
          " changes its immutable source or anchor at revision " +
          children[0].revision + "."
        );
        break;
      }
      current = children[0];
    }
    values.forEach(function (item) {
      if (item.revision > 1 && !byDigest.has(item.parent_semantic_digest)) {
        diagnostics.push(
          "Fragment " + fragmentId + " contains dangling revision " +
          item.revision + "."
        );
      }
    });
    return {selected: current, diagnostics: diagnostics};
  }

  function renderReader() {
    stopProgressiveRendering();
    state.citationNumberCache = null;
    state.glossarySurfaceCache = {source: null, target: null};
    var publication = state.payload.publication;
    var documentValue = publication.source_document;
    var profile = publication.reader_profile || {};
    var strings = labels();
    var title = readerTitle();
    document.title = title;
    document.documentElement.lang = profile.target_language ||
      profile.source_language || "und";

    var header = document.getElementById("arc-book-header");
    var main = document.getElementById("arc-document");
    var contents = document.getElementById("arc-contents-list");
    header.replaceChildren();
    main.replaceChildren();
    contents.replaceChildren();

    var heading = element("h1", "", title);
    removeVisibleHtmlTags(heading);
    header.appendChild(heading);
    decorateGlossary(heading, "source");
    decorateGlossary(heading, "target");
    if (Array.isArray(profile.authors) && profile.authors.length) {
      header.appendChild(element("p", "arc-authors", profile.authors.join(", ")));
    }

    state.fragmentGroups = groupedFragments(documentValue);
    state.renderPlan = buildRenderChunks(
      documentValue.blocks || [], publication.outline || []
    );
    state.renderedChunkIds = new Set();
    state.chunkNodes = new Map();
    state.chunkByTargetId = new Map();
    state.diagnosticsRoot = element("div", "arc-reader-diagnostics");
    main.appendChild(state.diagnosticsRoot);
    renderDiagnostics(state.diagnosticsRoot);

    state.renderPlan.forEach(function (chunk) {
      var node = element("div", "arc-render-chunk");
      node.dataset.chunkId = chunk.chunk_id;
      node.dataset.chunkKind = chunk.kind;
      node.setAttribute("aria-busy", "true");
      node.style.setProperty(
        "--arc-chunk-placeholder-height",
        String(chunkHeightEstimate(chunk)) + "px"
      );
      state.chunkNodes.set(chunk.chunk_id, node);
      main.appendChild(node);
      if (chunk.kind === "content") {
        for (
          var index = chunk.block_start;
          index < chunk.block_end;
          index += 1
        ) {
          var block = documentValue.blocks[index];
          state.chunkByTargetId.set(
            "block-" + safeToken(block.block_id), chunk
          );
          if (block.kind === "heading") {
            state.chunkByTargetId.set(
              "heading-" + safeToken(block.block_id), chunk
            );
          }
        }
      }
    });
    var appendix = state.renderPlan.find(function (chunk) {
      return chunk.kind === "appendices";
    });
    if (appendix) {
      state.chunkByTargetId.set("arc-glossary", appendix);
      state.chunkByTargetId.set("arc-references", appendix);
    }

    renderContents(contents, publication.outline || [], strings);
    setupContents();
    setupProgressiveNavigation();
    setupPrintRendering();
    state.readerShellReady = true;

    var initialChunk = initialRenderChunk();
    state.hydrationOrder = hydrationOrderFrom(initialChunk);
    updateRenderComplete();
  }

  function buildRenderChunks(blocks, outline) {
    var blockCount = blocks.length;
    var roots = (outline || []).filter(function (section) {
      return Array.isArray(section.path) && section.path.length === 1;
    });
    if (!roots.length && outline.length) {
      var minimumLevel = Math.min.apply(null, outline.map(function (section) {
        return Number(section.level);
      }));
      roots = outline.filter(function (section) {
        return Number(section.level) === minimumLevel;
      });
    }
    roots = roots.slice().sort(function (left, right) {
      return Number(left.block_start) - Number(right.block_start) ||
        Number(left.block_end) - Number(right.block_end) ||
        Number(left.ordinal || 0) - Number(right.ordinal || 0);
    });

    var ranges = [];
    var cursor = 0;
    roots.forEach(function (section) {
      var start = Math.max(0, Math.min(blockCount, Number(section.block_start)));
      var end = Math.max(start, Math.min(blockCount, Number(section.block_end)));
      if (start > cursor) ranges.push({start: cursor, end: start});
      if (end > cursor) {
        ranges.push({
          start: Math.max(cursor, start),
          end: end,
          title: section.title || null
        });
        cursor = end;
      }
    });
    if (cursor < blockCount) ranges.push({start: cursor, end: blockCount});
    if (!ranges.length && blockCount) ranges.push({start: 0, end: blockCount});

    var chunks = [];
    ranges.forEach(function (range) {
      var start = range.start;
      while (start < range.end) {
        var end = Math.min(range.end, start + MAX_BLOCKS_PER_RENDER_CHUNK);
        chunks.push({
          chunk_id: "chunk-" + String(chunks.length).padStart(4, "0"),
          kind: "content",
          block_start: start,
          block_end: end,
          title: start === range.start ? range.title || null : null
        });
        start = end;
      }
    });
    chunks.push({
      chunk_id: "chunk-" + String(chunks.length).padStart(4, "0"),
      kind: "appendices",
      block_start: blockCount,
      block_end: blockCount,
      title: null
    });
    return chunks;
  }

  function chunkHeightEstimate(chunk) {
    if (chunk.kind === "appendices") {
      var publication = state.payload.publication;
      return Math.max(
        320,
        ((publication.glossary || []).length +
          (publication.bibliography || []).length) * 72
      );
    }
    return Math.max(
      320,
      (chunk.block_end - chunk.block_start) * CHUNK_BLOCK_HEIGHT_ESTIMATE
    );
  }

  function initialRenderChunk() {
    var target = chunkForTargetId(hashTargetId(window.location.hash));
    if (target) {
      renderChunk(target);
      armHashCalibration(window.location.hash);
      return target;
    }
    var contentChunks = state.renderPlan.filter(function (chunk) {
      return chunk.kind === "content";
    });
    if (!contentChunks.length) {
      var appendix = state.renderPlan[0] || null;
      if (appendix) renderChunk(appendix);
      return appendix;
    }
    var renderedHeight = 0;
    var minimumHeight = Math.max(window.innerHeight * 1.5, 720);
    var last = contentChunks[0];
    for (var index = 0; index < contentChunks.length; index += 1) {
      last = contentChunks[index];
      var node = renderChunk(last);
      renderedHeight += node.getBoundingClientRect().height;
      if (renderedHeight >= minimumHeight) break;
    }
    return last;
  }

  function hydrationOrderFrom(initialChunk) {
    if (!state.renderPlan.length) return [];
    var initialIndex = Math.max(0, state.renderPlan.indexOf(initialChunk));
    if (initialIndex === 0) {
      return state.renderPlan.map(function (chunk) { return chunk.chunk_id; });
    }
    var values = [state.renderPlan[initialIndex].chunk_id];
    for (var distance = 1; values.length < state.renderPlan.length; distance += 1) {
      if (initialIndex + distance < state.renderPlan.length) {
        values.push(state.renderPlan[initialIndex + distance].chunk_id);
      }
      if (initialIndex - distance >= 0) {
        values.push(state.renderPlan[initialIndex - distance].chunk_id);
      }
    }
    return values;
  }

  function renderChunk(chunk) {
    if (!chunk) return null;
    var node = state.chunkNodes.get(chunk.chunk_id);
    if (!node) throw new Error("render chunk has no document shell");
    if (state.renderedChunkIds.has(chunk.chunk_id)) return node;
    if (state.chunkObserver) state.chunkObserver.unobserve(node);
    renderChunkBody(chunk, node);
    node.classList.add("is-rendered");
    node.setAttribute("aria-busy", "false");
    state.renderedChunkIds.add(chunk.chunk_id);
    setupLaneResponsiveness(node);
    updateRenderComplete();
    recalibrateHashTarget();
    return node;
  }

  function rerenderChunk(chunk) {
    if (!chunk || !state.renderedChunkIds.has(chunk.chunk_id)) return;
    var node = state.chunkNodes.get(chunk.chunk_id);
    teardownLaneResponsiveness(node);
    renderChunkBody(chunk, node);
    setupLaneResponsiveness(node);
  }

  function renderChunkBody(chunk, node) {
    var publication = state.payload.publication;
    var documentValue = publication.source_document;
    var content = document.createDocumentFragment();
    if (chunk.kind === "content") {
      for (
        var index = chunk.block_start;
        index < chunk.block_end;
        index += 1
      ) {
        var block = documentValue.blocks[index];
        if (isPdfPageMarkerBlock(block) || isStandaloneHtmlCommentBlock(block)) {
          continue;
        }
        content.appendChild(renderSourceRow(
          block, state.fragmentGroups.get(block.block_id) || []
        ));
      }
    } else {
      renderGlossary(content, publication.glossary || [], labels());
      renderBibliography(content, publication.bibliography || [], labels());
    }
    node.replaceChildren(content);
  }

  function renderAllChunks() {
    cancelIdleHydration();
    state.renderPlan.forEach(renderChunk);
    updateRenderComplete();
  }

  function refreshChangedSelections(previousSelected) {
    if (!state.readerShellReady) return;
    var fragmentIds = new Set(previousSelected.keys());
    state.selected.forEach(function (_revision, fragmentId) {
      fragmentIds.add(fragmentId);
    });
    var changedAnchors = [];
    fragmentIds.forEach(function (fragmentId) {
      var previous = previousSelected.get(fragmentId);
      var current = state.selected.get(fragmentId);
      if (
        (previous && previous.semantic_digest) ===
        (current && current.semantic_digest)
      ) {
        return;
      }
      if (previous && previous.anchor) changedAnchors.push(previous.anchor);
      if (current && current.anchor) changedAnchors.push(current.anchor);
    });
    if (changedAnchors.length) {
      state.fragmentGroups = groupedFragments(
        state.payload.publication.source_document
      );
    }
    renderDiagnostics(state.diagnosticsRoot);
    var chunks = new Set();
    changedAnchors.forEach(function (anchor) {
      var chunk = chunkForAnchor(anchor);
      if (chunk) chunks.add(chunk);
    });
    chunks.forEach(rerenderChunk);
  }

  function refreshChunkForAnchor(anchor) {
    if (!state.readerShellReady) return;
    renderDiagnostics(state.diagnosticsRoot);
    var chunk = chunkForAnchor(anchor);
    rerenderChunk(chunk);
  }

  function refreshFragmentGroup(fragmentId, anchor) {
    updateFragmentGroup(fragmentId, anchor);
  }

  function updateFragmentGroup(fragmentId, anchor) {
    var target = fragmentTargetId({anchor: anchor});
    if (!target) return;
    var values = (state.fragmentGroups.get(target) || []).filter(function (item) {
      return item.fragment_id !== fragmentId;
    });
    var selected = state.selected.get(fragmentId);
    if (selected) values.push(selected);
    values.sort(function (left, right) {
      return left.priority - right.priority ||
        left.fragment_id.localeCompare(right.fragment_id);
    });
    state.fragmentGroups.set(target, values);
  }

  function chunkForAnchor(anchor) {
    if (!anchor) return null;
    var target = anchor.target_id;
    if (anchor.kind === "section") {
      target = ensureSourceIndexes().sectionAnchors.get(target) || null;
    }
    return target ?
      state.chunkByTargetId.get("block-" + safeToken(target)) || null :
      null;
  }

  function chunkForTargetId(targetId) {
    if (!targetId) return null;
    if (targetId.indexOf("reference-") === 0) {
      return state.chunkByTargetId.get("arc-references") || null;
    }
    return state.chunkByTargetId.get(targetId) || null;
  }

  function hashTargetId(hash) {
    if (!hash || hash.charAt(0) !== "#") return "";
    try {
      return decodeURIComponent(hash.slice(1));
    } catch (_error) {
      return hash.slice(1);
    }
  }

  function sourceTitle(documentValue) {
    var first = (documentValue.blocks || []).find(function (block) {
      return block.kind === "heading" && Number(block.payload.level) === 1;
    });
    return first ? first.payload.text : "";
  }

  function isPdfPageMarkerBlock(block) {
    if (!block || block.kind !== "paragraph") return false;
    var text = String((block.payload || {}).text || "");
    return /^<!-- PDF_PAGE: [1-9][0-9]* -->$/.test(text);
  }

  function isStandaloneHtmlCommentBlock(block) {
    if (!block || block.kind !== "paragraph") return false;
    var payload = block.payload || {};
    if (Array.isArray(payload.inline_spans) && payload.inline_spans.length) {
      return false;
    }
    return /^\s*<!--[\s\S]*?-->\s*$/.test(String(payload.text || ""));
  }

  function groupedFragments(documentValue) {
    var groups = new Map();
    state.selected.forEach(function (fragment) {
      var target = fragmentTargetId(fragment);
      if (!target) return;
      var values = groups.get(target) || [];
      values.push(fragment);
      groups.set(target, values);
    });
    groups.forEach(function (values) {
      values.sort(function (left, right) {
        return left.priority - right.priority ||
          left.fragment_id.localeCompare(right.fragment_id);
      });
    });
    return groups;
  }

  function fragmentTargetId(fragment) {
    var anchor = fragment && fragment.anchor;
    var target = anchor && anchor.target_id;
    if (anchor && anchor.kind === "section") {
      target = ensureSourceIndexes().sectionAnchors.get(target) || null;
    }
    return target || null;
  }

  function renderSourceRow(block, fragments) {
    var row = element("article", "arc-source-row");
    row.id = "block-" + safeToken(block.block_id);
    row.dataset.blockId = block.block_id;
    var lanes = element("div", "arc-lanes");
    var source = element("section", "arc-source-card");
    source.dataset.role = "source";
    source.appendChild(renderSourceBlock(block));
    lanes.appendChild(source);

    fragments.filter(function (item) {
      return item.priority <= 100;
    }).forEach(function (item) {
      lanes.appendChild(renderFragment(item));
    });
    lanes.style.setProperty("--arc-lane-count", String(lanes.children.length));
    row.appendChild(lanes);

    var full = fragments.filter(function (item) {
      return item.priority >= 101;
    });
    if (full.length) {
      var fullRows = element("div", "arc-full-rows");
      full.forEach(function (item) { fullRows.appendChild(renderFragment(item)); });
      row.appendChild(fullRows);
    }
    var noteButton = iconButton(
      "arc-note-button arc-icon-button", "+", labels().addNote
    );
    noteButton.addEventListener("click", function () {
      openNewEditor(block);
    });
    source.appendChild(noteButton);
    return row;
  }

  function renderSourceBlock(block) {
    var payload = block.payload || {};
    var container = document.createDocumentFragment();
    if (block.kind === "heading") {
      var level = Math.max(2, Math.min(6, Number(payload.level) + 1));
      var heading = element("h" + level, "", payload.text || "");
      heading.id = "heading-" + safeToken(block.block_id);
      container.appendChild(heading);
      removeVisibleHtmlTags(heading);
      decorateGlossary(heading, "source");
      return container;
    }
    if (block.kind === "paragraph") {
      var paragraph = element("p");
      appendInlineSpans(paragraph, payload.inline_spans, payload.text);
      container.appendChild(paragraph);
      return container;
    }
    if (block.kind === "list") {
      var list = element(payload.ordered ? "ol" : "ul");
      (payload.items || []).forEach(function (item) {
        var listItem = element("li");
        appendInlineSpans(listItem, item.inline_spans, item.text);
        list.appendChild(listItem);
      });
      container.appendChild(list);
      return container;
    }
    if (block.kind === "code") {
      var pre = element("pre");
      var code = element("code", "", payload.text || "");
      if (payload.language) code.dataset.language = payload.language;
      pre.appendChild(code);
      container.appendChild(pre);
      return container;
    }
    if (block.kind === "equation") {
      var math = element("div", "math math-display", payload.tex || "");
      math.dataset.tex = payload.tex || "";
      container.appendChild(math);
      var equationLabel = effectiveEquationLabel(block, payload);
      if (equationLabel) {
        container.appendChild(element("span", "arc-equation-label", equationLabel));
      }
      removeVisibleHtmlTags(container);
      typeset(container);
      return container;
    }
    if (block.kind === "table") {
      var table = element("table");
      if (payload.caption) table.appendChild(element("caption", "", payload.caption));
      if ((payload.headers || []).length) {
        var head = element("thead");
        var headerRow = element("tr");
        payload.headers.forEach(function (value) {
          headerRow.appendChild(element("th", "", value));
        });
        head.appendChild(headerRow);
        table.appendChild(head);
      }
      var body = element("tbody");
      (payload.rows || []).forEach(function (values) {
        var tr = element("tr");
        values.forEach(function (value) {
          tr.appendChild(element("td", "", value));
        });
        body.appendChild(tr);
      });
      table.appendChild(body);
      container.appendChild(table);
      removeVisibleHtmlTags(table);
      decorateGlossary(table, "source");
      return container;
    }
    if (block.kind === "figure") {
      var figure = element("figure");
      var digest = payload.asset_digest || "";
      var resource = resourceForDigest(digest);
      if (resource) {
        var image = element("img");
        image.src = resource.data_uri;
        image.alt = payload.alt_text || "";
        figure.appendChild(image);
      } else {
        figure.appendChild(element(
          "p", "arc-figure-note", payload.alt_text || payload.logical_name || ""
        ));
      }
      if (payload.caption) figure.appendChild(element("figcaption", "", payload.caption));
      container.appendChild(figure);
      removeVisibleHtmlTags(figure);
      decorateGlossary(figure, "source");
      return container;
    }
    container.appendChild(element("p", "", JSON.stringify(payload)));
    removeVisibleHtmlTags(container);
    return container;
  }

  function effectiveEquationLabel(block, payload) {
    var documentValue = state.payload.publication.source_document;
    var reconciliations = (
      (documentValue.metadata || {}).equation_label_reconciliation || {}
    );
    var reconciliation = reconciliations[block.block_id];
    if (
      reconciliation &&
      typeof reconciliation.effective_label === "string" &&
      reconciliation.effective_label.trim()
    ) {
      return reconciliation.effective_label.trim();
    }
    return typeof payload.label === "string" ? payload.label.trim() : "";
  }

  function appendInlineSpans(parent, spans, fallback) {
    if (!Array.isArray(spans) || spans.length === 0) {
      parent.textContent = fallback || "";
      removeVisibleHtmlTags(parent);
      decorateGlossary(parent, "source");
      return;
    }
    spans.forEach(function (span) {
      if (span.kind === "math") {
        var math = element("span", "math math-inline", span.source || span.tex || "");
        math.dataset.tex = span.tex || span.source || "";
        parent.appendChild(math);
      } else if (span.kind === "link") {
        var link = element("a", "", span.text || span.target || "");
        link.href = span.target || "";
        link.rel = "noopener noreferrer";
        parent.appendChild(link);
      } else {
        parent.appendChild(document.createTextNode(span.text || ""));
      }
    });
    removeVisibleHtmlTags(parent);
    decorateGlossary(parent, "source");
    typeset(parent);
  }

  function resourceForDigest(digest) {
    return ensureSourceIndexes().resourcesByDigest.get(digest) || null;
  }

  function resourceForLogicalName(logicalName) {
    return ensureSourceIndexes().resourcesByLogicalName.get(logicalName) || null;
  }

  function fragmentMetaText(fragment, editing) {
    var role = roleLabel(fragment.role);
    if (editing) {
      return role + " · " + fragment.priority + " · v" + fragment.revision;
    }
    return fragment.revision === 1
      ? role
      : role + " · v" + fragment.revision;
  }

  function renderFragment(fragment) {
    var card = element("aside", "arc-fragment");
    card.dataset.fragmentId = fragment.fragment_id;
    card.dataset.revision = String(fragment.revision);
    card.dataset.role = fragment.role;
    card.dataset.priority = String(fragment.priority);
    var header = element("header", "arc-fragment-header");
    var title = fragment.title ? element("h4", "", fragment.title) : element("span");
    decorateGlossary(title, "target");
    header.appendChild(title);
    var actions = element("div", "arc-fragment-actions");
    var draft = state.activeDraft;
    var editing = Boolean(
      draft && draft.base && draft.base.fragment_id === fragment.fragment_id
    );
    actions.appendChild(element(
      "span", "arc-fragment-meta", fragmentMetaText(fragment, editing)
    ));
    if (editing) {
      actions.classList.add("arc-inline-actions");
      appendInlineActions(actions);
    } else {
      var accessibleEdit = element(
        "button", "arc-edit-accessible", labels().editContent
      );
      accessibleEdit.type = "button";
      accessibleEdit.addEventListener("click", function () {
        beginInlineEdit(fragment);
      });
      actions.appendChild(accessibleEdit);
    }
    header.appendChild(actions);
    card.appendChild(header);
    var saved = element("div", "arc-fragment-saved-content");
    saved.appendChild(renderMarkdown(fragment.markdown_body));
    saved.addEventListener("click", function (event) {
      if (interactiveFragmentTarget(event.target)) return;
      beginInlineEdit(fragment);
    });
    card.appendChild(saved);
    if (editing) {
      card.classList.add("is-inline-editing");
      card.appendChild(renderInlineEditor());
    }
    return card;
  }

  function interactiveFragmentTarget(target) {
    return Boolean(target && target.closest && target.closest(
      "a, button, input, textarea, select, .glossary-term"
    ));
  }

  function appendInlineActions(actions) {
    var strings = labels();
    var advanced = element("button", "arc-inline-advanced", strings.advancedAction);
    advanced.type = "button";
    advanced.addEventListener("click", openAdvancedEditor);
    var cancel = element("button", "arc-inline-cancel", strings.cancel);
    cancel.type = "button";
    cancel.addEventListener("click", cancelActiveDraft);
    var save = element("button", "arc-inline-save", strings.save);
    save.type = "button";
    save.disabled = !activeDraftHasChanges();
    save.addEventListener("click", saveEditor);
    actions.appendChild(advanced);
    actions.appendChild(cancel);
    actions.appendChild(save);
  }

  function renderInlineEditor() {
    var root = element("div", "arc-inline-editor");
    var textarea = element("textarea", "arc-inline-markdown");
    textarea.value = state.activeDraft ? state.activeDraft.markdown_body : "";
    textarea.setAttribute("aria-label", labels().markdown);
    textarea.spellcheck = true;
    textarea.addEventListener("input", function () {
      if (!state.activeDraft) return;
      state.activeDraft.markdown_body = textarea.value;
      resizeInlineTextarea(textarea);
      updateDraftSaveButtons(textarea.closest(".arc-fragment"));
    });
    textarea.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        if (activeDraftHasChanges()) saveEditor(event);
      }
    });
    root.appendChild(textarea);
    window.requestAnimationFrame(function () {
      resizeInlineTextarea(textarea);
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    });
    return root;
  }

  function resizeInlineTextarea(textarea) {
    textarea.style.height = "auto";
    var maximum = Math.max(160, Math.round(window.innerHeight * 0.6));
    textarea.style.height = Math.min(textarea.scrollHeight, maximum) + "px";
  }

  function roleLabel(role) {
    var strings = labels();
    return strings[role] || role;
  }

  function renderContents(list, sections, strings) {
    var contentsHeading = document.getElementById("arc-contents-heading");
    contentsHeading.textContent = strings.contents;
    sections.forEach(function (section) {
      var item = element("li");
      item.dataset.level = String(section.level);
      var link = element("a");
      link.href = "#block-" + safeToken(section.anchor_block_id);
      appendTocTitle(link, section.title);
      item.appendChild(link);
      list.appendChild(item);
    });
    if ((state.payload.publication.glossary || []).length) {
      appendContentsLink(list, strings.glossary, "#arc-glossary");
    }
    if ((state.payload.publication.bibliography || []).length) {
      appendContentsLink(list, strings.references, "#arc-references");
    }
  }

  function appendTocTitle(parent, value) {
    var text = String(value || "");
    var position = 0;
    while (position < text.length) {
      var match = /\\\(([^\n]+?)\\\)|\$([^$\n]+?)\$/.exec(
        text.slice(position)
      );
      if (!match) {
        parent.appendChild(document.createTextNode(text.slice(position)));
        break;
      }
      var start = position + match.index;
      if (start > position) {
        parent.appendChild(document.createTextNode(text.slice(position, start)));
      }
      var tex = match[1] === undefined ? match[2] : match[1];
      var math = element("span", "math math-inline", tex);
      math.dataset.tex = tex;
      parent.appendChild(math);
      position = start + match[0].length;
    }
    removeVisibleHtmlTags(parent);
    typeset(parent);
  }

  function appendContentsLink(list, text, href) {
    var item = element("li");
    var link = element("a", "", text);
    link.href = href;
    item.appendChild(link);
    list.appendChild(item);
  }

  function renderGlossary(main, glossary, strings) {
    if (!glossary.length) return;
    var section = element("section", "arc-appendix");
    section.id = "arc-glossary";
    section.appendChild(element("h2", "", strings.glossary));
    var dl = element("dl");
    glossary.forEach(function (entry) {
      var row = element("div", "arc-glossary-row");
      var original = element("dt", "", entry.term || entry.source_term || "");
      var translated = element(
        "dd", "", entry.translated_term || entry.translation || ""
      );
      decorateGlossary(original, "source");
      decorateGlossary(translated, "target");
      row.appendChild(original);
      row.appendChild(translated);
      row.appendChild(element("dd", "", entry.definition || ""));
      dl.appendChild(row);
    });
    section.appendChild(dl);
    main.appendChild(section);
  }

  function renderBibliography(main, bibliography, strings) {
    if (!bibliography.length) return;
    var section = element("section", "arc-appendix");
    section.id = "arc-references";
    section.appendChild(element("h2", "", strings.references));
    var list = element("ol", "arc-reference-list");
    bibliography.forEach(function (entry) {
      var item = element("li");
      var id = entry.evidence_id || entry.citation_id || entry.id || "";
      if (id) item.id = "reference-" + id;
      var title = entry.title || entry.source || id;
      var source = entry.source || entry.url || "";
      if (/^https?:\/\//i.test(source)) {
        var link = element("a", "", title);
        link.href = source;
        link.rel = "noopener noreferrer";
        item.appendChild(link);
      } else {
        item.appendChild(element("strong", "", title));
      }
      if (source && source !== title) {
        item.appendChild(document.createTextNode(" — " + source));
      }
      (entry.dois || []).forEach(function (doi) {
        item.appendChild(document.createTextNode(" DOI: " + doi));
      });
      (entry.arxiv_ids || []).forEach(function (identifier) {
        item.appendChild(document.createTextNode(" arXiv: " + identifier));
      });
      list.appendChild(item);
    });
    section.appendChild(list);
    main.appendChild(section);
  }

  function renderDiagnostics(main) {
    main.replaceChildren();
    state.diagnostics.forEach(function (value) {
      main.appendChild(element("p", "arc-diagnostic", value));
    });
  }

  function glossarySurfaces(layer) {
    if (state.glossarySurfaceCache[layer]) {
      return state.glossarySurfaceCache[layer];
    }
    var values = [];
    (state.payload.publication.glossary || []).forEach(function (entry) {
      var surface = layer === "source" ?
        (entry.term || entry.source_term) :
        (entry.translated_term || entry.translation);
      if (!surface) return;
      values.push({
        surface: String(surface),
        folded: String(surface).toLocaleLowerCase(),
        entry: entry,
        latin: /^[A-Za-z0-9_ -]+$/.test(String(surface))
      });
    });
    values.sort(function (left, right) {
      return right.surface.length - left.surface.length ||
        left.surface.localeCompare(right.surface);
    });
    state.glossarySurfaceCache[layer] = values;
    return state.glossarySurfaceCache[layer];
  }

  function decorateGlossary(root, layer) {
    var surfaces = glossarySurfaces(layer);
    if (!surfaces.length) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) nodes.push(node);
    nodes.forEach(function (textNode) {
      var parent = textNode.parentElement;
      if (!parent || parent.closest(
        "code, pre, a, .math, .glossary-term, .arc-reference-list"
      )) return;
      replaceGlossaryText(textNode, surfaces);
    });
  }

  function replaceGlossaryText(textNode, surfaces) {
    var value = textNode.nodeValue || "";
    var folded = value.toLocaleLowerCase();
    var matches = [];
    var cursor = 0;
    while (cursor < value.length) {
      var found = null;
      surfaces.some(function (item) {
        if (folded.slice(cursor, cursor + item.folded.length) !== item.folded) {
          return false;
        }
        if (item.latin && !hasLatinBoundaries(value, cursor, item.surface.length)) {
          return false;
        }
        found = item;
        return true;
      });
      if (found) {
        var same = surfaces.filter(function (item) {
          return item.folded === found.folded;
        });
        matches.push({start: cursor, end: cursor + found.surface.length, entries: same});
        cursor += found.surface.length;
      } else {
        cursor += 1;
      }
    }
    if (!matches.length) return;
    var fragment = document.createDocumentFragment();
    cursor = 0;
    matches.forEach(function (match) {
      if (match.start > cursor) {
        fragment.appendChild(document.createTextNode(value.slice(cursor, match.start)));
      }
      var term = element("span", "glossary-term", value.slice(match.start, match.end));
      term.tabIndex = 0;
      term.dataset.glossaryTooltip = tooltipText(
        match.entries.map(function (item) { return item.entry; })
      );
      fragment.appendChild(term);
      cursor = match.end;
    });
    if (cursor < value.length) fragment.appendChild(document.createTextNode(value.slice(cursor)));
    textNode.replaceWith(fragment);
  }

  function hasLatinBoundaries(value, start, length) {
    var before = start > 0 ? value.charAt(start - 1) : "";
    var after = start + length < value.length ? value.charAt(start + length) : "";
    return !/[A-Za-z0-9_]/.test(before) && !/[A-Za-z0-9_]/.test(after);
  }

  function tooltipText(entries) {
    var strings = labels();
    return entries.map(function (entry) {
      return strings.originalTerm + ": " + (entry.term || entry.source_term || "") +
        "\n" + strings.translatedTerm + ": " +
        (entry.translated_term || entry.translation || "") +
        "\n" + strings.definition + ": " + (entry.definition || "");
    }).join("\n\n");
  }

  function katexTex(value) {
    var tex = String(value || "");
    if (/\\begin\s*\{[^{}]+\}/.test(tex)) return tex;
    for (var index = 0; index < tex.length; index += 1) {
      if (tex.charAt(index) !== "&") continue;
      var slashCount = 0;
      var cursor = index - 1;
      while (cursor >= 0 && tex.charAt(cursor) === "\\") {
        slashCount += 1;
        cursor -= 1;
      }
      if (slashCount % 2 === 0) {
        return "\\begin{aligned}" + tex + "\\end{aligned}";
      }
    }
    return tex;
  }

  function repairMatrixShorthand(value) {
    var tex = String(value || "");
    var opening = /\\left\s*(\[|\()\s*\{\s*([clr](?:\s*[clr])*)\s*\}/g;
    var edits = [];
    var match;
    while ((match = opening.exec(tex))) {
      var depth = 1;
      var closingIndex = -1;
      var token = /\\(?:left|right)\b/g;
      token.lastIndex = opening.lastIndex;
      var delimiter;
      while ((delimiter = token.exec(tex))) {
        if (delimiter[0] === "\\left") {
          depth += 1;
        } else {
          depth -= 1;
          if (depth === 0) {
            closingIndex = delimiter.index;
            break;
          }
        }
      }
      if (closingIndex < 0) continue;
      edits.push({
        start: match.index,
        end: opening.lastIndex,
        value: "\\left" + match[1] + "\\begin{array}{" +
          match[2].replace(/\s+/g, "") + "}"
      });
      edits.push({
        start: closingIndex,
        end: closingIndex,
        value: "\\end{array}"
      });
    }
    edits.sort(function (left, right) {
      return right.start - left.start || right.end - left.end;
    });
    edits.forEach(function (edit) {
      tex = tex.slice(0, edit.start) + edit.value + tex.slice(edit.end);
    });
    return tex;
  }

  function katexCandidates(value) {
    var primary = katexTex(value);
    var repaired = repairMatrixShorthand(primary);
    var fixedSizeDelimiters = repaired
      .replace(/\\left\b/g, "\\bigl")
      .replace(/\\right\b/g, "\\bigr");
    return [primary, repaired, fixedSizeDelimiters].filter(function (
      candidate, index, values
    ) {
      return values.indexOf(candidate) === index;
    });
  }

  function typeset(root) {
    if (!window.katex || typeof window.katex.render !== "function") return;
    var scope = root.querySelectorAll ? root : document;
    scope.querySelectorAll(".math[data-tex]").forEach(function (node) {
      if (node.dataset.arcTypeset === "true") return;
      try {
        var settings = {
          displayMode: node.classList.contains("math-display"),
          throwOnError: true,
          strict: "warn"
        };
        var candidates = katexCandidates(node.dataset.tex);
        var rendered = candidates.some(function (candidate) {
          try {
            window.katex.render(candidate, node, settings);
            return true;
          } catch (_candidateError) {
            return false;
          }
        });
        if (!rendered) {
          settings.throwOnError = false;
          window.katex.render(candidates[0] || "", node, settings);
          node.classList.add("math-error");
        }
        node.dataset.arcTypeset = "true";
      } catch (_error) {
        node.textContent = node.dataset.tex || "";
        node.classList.add("math-error");
      }
    });
  }

  function updateLaneResponsiveness(lanes) {
    var count = Number(lanes.style.getPropertyValue("--arc-lane-count")) || 1;
    var horizontal = window.innerWidth >= 900 &&
      lanes.getBoundingClientRect().width / count >= 275;
    lanes.classList.toggle("lanes-horizontal", horizontal);
  }

  function setupLaneResponsiveness(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var lanes = Array.prototype.slice.call(
      scope.querySelectorAll(".arc-lanes")
    );
    lanes.forEach(updateLaneResponsiveness);
    if ("ResizeObserver" in window) {
      if (!state.laneObserver) {
        state.laneObserver = new ResizeObserver(function (entries) {
          entries.forEach(function (entry) {
            updateLaneResponsiveness(entry.target);
          });
        });
      }
      lanes.forEach(function (item) { state.laneObserver.observe(item); });
    } else if (!state.laneFallbackListener) {
      state.laneFallbackListener = function () {
        Array.prototype.forEach.call(
          document.querySelectorAll(".arc-lanes"),
          updateLaneResponsiveness
        );
      };
      window.addEventListener("resize", state.laneFallbackListener);
    }
  }

  function teardownLaneResponsiveness(root) {
    if (!state.laneObserver || !root) return;
    Array.prototype.forEach.call(
      root.querySelectorAll(".arc-lanes"),
      function (item) { state.laneObserver.unobserve(item); }
    );
  }

  function startProgressiveRendering() {
    setupChunkObserver();
    setupHydrationActivity();
    scheduleIdleHydration();
  }

  function setupChunkObserver() {
    if (!("IntersectionObserver" in window) || state.chunkObserver) return;
    state.chunkObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var chunk = state.renderPlan.find(function (item) {
          return item.chunk_id === entry.target.dataset.chunkId;
        });
        try {
          renderChunk(chunk);
        } catch (error) {
          failProgressiveRender(error);
        }
      });
    }, {rootMargin: "150% 0px", threshold: 0});
    state.renderPlan.forEach(function (chunk) {
      if (state.renderedChunkIds.has(chunk.chunk_id)) return;
      state.chunkObserver.observe(state.chunkNodes.get(chunk.chunk_id));
    });
  }

  function scheduleIdleHydration() {
    if (
      state.idleRenderHandle !== null ||
      state.hydrationQuietTimer !== null ||
      document.body.dataset.arcRenderComplete === "true"
    ) {
      return;
    }
    state.hydrationQuietTimer = window.setTimeout(function () {
      state.hydrationQuietTimer = null;
      requestIdleHydration();
    }, 2000);
  }

  function requestIdleHydration() {
    if (
      state.idleRenderHandle !== null ||
      document.body.dataset.arcRenderComplete === "true"
    ) {
      return;
    }
    var generation = state.idleRenderGeneration;
    var work = function (deadline) {
      state.idleRenderHandle = null;
      if (generation !== state.idleRenderGeneration) return;
      if (deadline && deadline.timeRemaining() < 12) {
        requestIdleHydration();
        return;
      }
      var chunk = nextHydrationChunk();
      if (!chunk) {
        updateRenderComplete();
        return;
      }
      try {
        renderChunk(chunk);
      } catch (error) {
        failProgressiveRender(error);
        return;
      }
      requestIdleHydration();
    };
    if (typeof window.requestIdleCallback === "function") {
      state.idleRenderUsesCallback = true;
      state.idleRenderHandle = window.requestIdleCallback(work);
    } else {
      state.idleRenderUsesCallback = false;
      state.idleRenderHandle = window.setTimeout(work, 250);
    }
  }

  function noteHydrationActivity() {
    if (state.hashCalibration) state.hashCalibration = null;
    cancelIdleHydration();
    scheduleIdleHydration();
  }

  function setupHydrationActivity() {
    if (state.hydrationActivityReady) return;
    ["wheel", "touchstart", "keydown", "pointerdown"].forEach(function (name) {
      window.addEventListener(name, noteHydrationActivity, {passive: true});
    });
    state.hydrationActivityReady = true;
  }

  function nextHydrationChunk() {
    for (var index = 0; index < state.hydrationOrder.length; index += 1) {
      var id = state.hydrationOrder[index];
      if (state.renderedChunkIds.has(id)) continue;
      return state.renderPlan.find(function (chunk) {
        return chunk.chunk_id === id;
      }) || null;
    }
    return state.renderPlan.find(function (chunk) {
      return !state.renderedChunkIds.has(chunk.chunk_id);
    }) || null;
  }

  function cancelIdleHydration() {
    state.idleRenderGeneration += 1;
    if (state.hydrationQuietTimer !== null) {
      window.clearTimeout(state.hydrationQuietTimer);
      state.hydrationQuietTimer = null;
    }
    if (state.idleRenderHandle === null) return;
    if (
      state.idleRenderUsesCallback &&
      typeof window.cancelIdleCallback === "function"
    ) {
      window.cancelIdleCallback(state.idleRenderHandle);
    } else {
      window.clearTimeout(state.idleRenderHandle);
    }
    state.idleRenderHandle = null;
  }

  function stopProgressiveRendering() {
    if (state.chunkObserver) state.chunkObserver.disconnect();
    state.chunkObserver = null;
    cancelIdleHydration();
    if (state.laneObserver) state.laneObserver.disconnect();
    state.laneObserver = null;
    if (state.laneFallbackListener) {
      window.removeEventListener("resize", state.laneFallbackListener);
      state.laneFallbackListener = null;
    }
    state.readerShellReady = false;
  }

  function updateRenderComplete() {
    var complete = state.renderPlan.length > 0 &&
      state.renderPlan.every(function (chunk) {
        return state.renderedChunkIds.has(chunk.chunk_id);
      });
    document.body.dataset.arcRenderComplete = String(complete);
    if (!complete) return;
    if (state.chunkObserver) state.chunkObserver.disconnect();
    state.chunkObserver = null;
    cancelIdleHydration();
  }

  function failProgressiveRender(error) {
    stopProgressiveRendering();
    document.body.dataset.arcRenderReady = "error";
    var message = String(error.message || error);
    if (state.diagnosticsRoot) {
      state.diagnosticsRoot.prepend(
        element("p", "arc-diagnostic", message)
      );
    }
  }

  function setupProgressiveNavigation() {
    if (state.navigationReady) return;
    document.addEventListener("click", function (event) {
      var link = event.target.closest && event.target.closest('a[href^="#"]');
      if (!link) return;
      var href = link.getAttribute("href") || "";
      var targetId = hashTargetId(href);
      if (!chunkForTargetId(targetId)) return;
      event.preventDefault();
      activateHashTarget(href, true);
    });
    window.addEventListener("hashchange", function () {
      activateHashTarget(window.location.hash, false);
    });
    window.addEventListener("popstate", function () {
      activateHashTarget(window.location.hash, false);
    });
    state.navigationReady = true;
  }

  function activateHashTarget(hash, updateHistory) {
    var targetId = hashTargetId(hash);
    var chunk = chunkForTargetId(targetId);
    if (!chunk) return false;
    renderChunk(chunk);
    armHashCalibration(hash);
    if (updateHistory) {
      if (window.location.hash === hash) {
        window.history.replaceState(null, "", hash);
      } else {
        window.history.pushState(null, "", hash);
      }
    }
    scrollToHashTarget(targetId);
    return true;
  }

  function armHashCalibration(hash) {
    var targetId = hashTargetId(hash);
    if (!targetId || !chunkForTargetId(targetId)) return;
    state.hashCalibration = {targetId: targetId};
    recalibrateHashTarget();
  }

  function recalibrateHashTarget() {
    var calibration = state.hashCalibration;
    if (!calibration) return;
    scrollToHashTarget(calibration.targetId);
  }

  function scrollToHashTarget(targetId) {
    window.requestAnimationFrame(function () {
      var target = document.getElementById(targetId);
      if (!target) return;
      var root = document.documentElement;
      var previousBehavior = root.style.scrollBehavior;
      root.style.scrollBehavior = "auto";
      target.scrollIntoView({block: "start", behavior: "auto"});
      root.style.scrollBehavior = previousBehavior;
    });
  }

  function setupPrintRendering() {
    if (state.printReady) return;
    window.addEventListener("beforeprint", renderAllChunks);
    state.printReady = true;
  }

  function setupContents() {
    var shell = document.getElementById("arc-shell");
    var contents = document.getElementById("arc-contents");
    var toggle = document.getElementById("arc-contents-toggle");
    var mobile = window.matchMedia("(max-width: 899px)");
    var strings = labels();
    var open = !mobile.matches;
    function setOpen(value) {
      open = Boolean(value);
      shell.classList.toggle("contents-collapsed", !open);
      contents.setAttribute("aria-hidden", String(!open));
      toggle.setAttribute("aria-expanded", String(open));
      toggle.title = open ? strings.collapse : strings.expand;
    }
    setOpen(open);
    toggle.onclick = function () { setOpen(!open); };
    contents.onclick = function (event) {
      if (mobile.matches && event.target.closest("a")) setOpen(false);
    };
  }

  function setupTooltip() {
    var tooltip = document.getElementById("arc-tooltip");
    var active = null;
    function close() {
      tooltip.hidden = true;
      tooltip.textContent = "";
      active = null;
    }
    function open(term) {
      var content = term && term.dataset.glossaryTooltip;
      if (!content) return;
      active = term;
      tooltip.textContent = content;
      tooltip.hidden = false;
      var termRect = term.getBoundingClientRect();
      var tipRect = tooltip.getBoundingClientRect();
      var gap = 8;
      var top = termRect.bottom + gap;
      if (top + tipRect.height > window.innerHeight - gap) {
        top = Math.max(gap, termRect.top - tipRect.height - gap);
      }
      var left = Math.min(
        Math.max(gap, termRect.left),
        Math.max(gap, window.innerWidth - tipRect.width - gap)
      );
      tooltip.style.top = Math.round(top) + "px";
      tooltip.style.left = Math.round(left) + "px";
    }
    document.addEventListener("mouseover", function (event) {
      var term = event.target.closest && event.target.closest(".glossary-term");
      if (term) open(term);
    });
    document.addEventListener("mouseout", function (event) {
      var term = event.target.closest && event.target.closest(".glossary-term");
      if (term && (!event.relatedTarget || !term.contains(event.relatedTarget))) close();
    });
    document.addEventListener("focusin", function (event) {
      if (event.target.matches && event.target.matches(".glossary-term")) open(event.target);
    });
    document.addEventListener("focusout", function (event) {
      if (event.target === active) close();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") close();
    });
    window.addEventListener("scroll", close, {passive: true});
  }

  function captureExportTemplate() {
    state.exportStandaloneSupported = !document.querySelector(
      'script[src], link[rel~="stylesheet"][href]'
    );
    if (!state.exportStandaloneSupported) return;
    var root = document.documentElement.cloneNode(true);
    var body = root.querySelector("body");
    var readingArea = root.querySelector("#arc-document");
    var header = root.querySelector("#arc-book-header");
    var contents = root.querySelector("#arc-contents-list");
    if (readingArea) readingArea.replaceChildren();
    if (header) header.replaceChildren();
    if (contents) contents.replaceChildren();
    if (body) {
      delete body.dataset.arcRenderReady;
      delete body.dataset.arcRenderComplete;
    }
    state.exportHtmlTemplate = "<!doctype html>\n" + root.outerHTML;
  }

  function captureInitialSelection() {
    state.initialSelectedDigests = new Map();
    state.selected.forEach(function (revision, fragmentId) {
      state.initialSelectedDigests.set(fragmentId, revision.semantic_digest);
    });
  }

  function setupExport() {
    var strings = labels();
    var control = document.querySelector(".arc-export-control");
    var trigger = document.getElementById("arc-export");
    var panel = document.getElementById("arc-export-panel");
    trigger.textContent = strings.export;
    document.getElementById("arc-export-scope-label").textContent =
      strings.markdownScope;
    document.getElementById("arc-export-all-label").textContent =
      strings.allLatest;
    document.getElementById("arc-export-changed-label").textContent =
      strings.changedLatest;
    document.getElementById("arc-export-empty").textContent =
      strings.noExportChanges;
    var htmlButton = document.getElementById("arc-export-html");
    htmlButton.textContent = strings.fullHtml;
    htmlButton.title = state.exportStandaloneSupported ? "" :
      strings.exportUnavailable;
    trigger.addEventListener("click", function () {
      if (panel.hidden) {
        openExportPanel();
      } else {
        closeExportPanel(false);
      }
    });
    document.getElementById("arc-export-scope").addEventListener(
      "change", renderExportOptions
    );
    htmlButton.addEventListener("click", function () {
      runExport({kind: "html"});
    });
    document.addEventListener("click", function (event) {
      if (!panel.hidden && !control.contains(event.target)) closeExportPanel(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) {
        event.preventDefault();
        closeExportPanel(true);
      }
    });
    renderExportOptions();
  }

  async function openExportPanel() {
    if (state.activeDraft) {
      setStatus(labels().draftActive, "error");
      return;
    }
    if (
      state.saveInProgress ||
      state.exportInProgress ||
      state.directorySelectionInProgress
    ) {
      setStatus(labels().revisionBusy, "error");
      return;
    }
    var trigger = document.getElementById("arc-export");
    var panel = document.getElementById("arc-export-panel");
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    if (!state.directory) {
      renderExportOptions();
      return;
    }
    state.exportInProgress = true;
    renderExportOptions();
    try {
      setStatus(labels().exportLoading);
      if (!await loadDirectoryRevisions(state.directory)) {
        throw new Error(labels().exportSyncFailed);
      }
    } catch (error) {
      setStatus(String(error.message || error), "error");
    } finally {
      state.exportInProgress = false;
      renderExportOptions();
    }
  }

  function closeExportPanel(restoreFocus) {
    var trigger = document.getElementById("arc-export");
    document.getElementById("arc-export-panel").hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  }

  function exportScope() {
    var checked = document.querySelector(
      'input[name="arc-export-scope"]:checked'
    );
    return checked && checked.value === "changed" ? "changed" : "all";
  }

  function selectedForMarkdown(scope) {
    var values = Array.from(state.selected.values());
    if (scope === "changed") {
      values = values.filter(function (revision) {
        return state.initialSelectedDigests.get(revision.fragment_id) !==
          revision.semantic_digest;
      });
    }
    return values;
  }

  function renderExportOptions() {
    var root = document.getElementById("arc-export-role-options");
    var empty = document.getElementById("arc-export-empty");
    root.replaceChildren();
    var roles = Array.from(new Set(selectedForMarkdown(exportScope()).map(
      function (revision) { return revision.role; }
    )));
    var preferred = ["translation", "companion", "guide", "note"];
    roles.sort(function (left, right) {
      var leftIndex = preferred.indexOf(left);
      var rightIndex = preferred.indexOf(right);
      if (leftIndex < 0) leftIndex = preferred.length;
      if (rightIndex < 0) rightIndex = preferred.length;
      return leftIndex - rightIndex || left.localeCompare(right);
    });
    roles.forEach(function (role) {
      var button = element("button", "", roleLabel(role) + " => MD");
      button.type = "button";
      button.disabled = state.exportInProgress;
      button.addEventListener("click", function () {
        runExport({kind: "markdown", role: role});
      });
      root.appendChild(button);
    });
    empty.hidden = roles.length > 0 || exportScope() !== "changed";
    var scopeControls = document.querySelectorAll(
      'input[name="arc-export-scope"]'
    );
    Array.prototype.forEach.call(scopeControls, function (input) {
      input.disabled = state.exportInProgress;
    });
    var htmlButton = document.getElementById("arc-export-html");
    var changedOnly = exportScope() === "changed";
    htmlButton.hidden = changedOnly;
    htmlButton.disabled = changedOnly || state.exportInProgress ||
      !state.exportStandaloneSupported;
  }

  async function runExport(request) {
    if (state.activeDraft) {
      setStatus(labels().draftActive, "error");
      return;
    }
    if (
      state.saveInProgress ||
      state.exportInProgress ||
      state.directorySelectionInProgress
    ) {
      setStatus(labels().revisionBusy, "error");
      return;
    }
    state.exportInProgress = true;
    renderExportOptions();
    try {
      if (state.directory) {
        setStatus(labels().exportLoading);
        if (!await loadDirectoryRevisions(state.directory)) {
          throw new Error(labels().exportSyncFailed);
        }
      }
      if (request.kind === "markdown") {
        var markdown = buildRoleMarkdown(request.role, exportScope());
        if (!markdown) {
          setStatus(labels().noExportChanges);
          return;
        }
        downloadText(
          exportFilename(request.role, "md"),
          markdown,
          "text/markdown;charset=utf-8"
        );
      } else {
        if (!state.exportStandaloneSupported) {
          throw new Error(labels().exportUnavailable);
        }
        downloadText(
          exportFilename("latest", "html"),
          buildStandaloneExportHtml(),
          "text/html;charset=utf-8"
        );
      }
      closeExportPanel(false);
      setStatus(labels().exportStarted);
    } catch (error) {
      setStatus(String(error.message || error), "error");
    } finally {
      state.exportInProgress = false;
      renderExportOptions();
    }
  }

  function buildRoleMarkdown(role, scope) {
    var revisions = selectedForMarkdown(scope).filter(function (revision) {
      return revision.role === role;
    });
    if (!revisions.length) return "";
    var blockOrder = new Map(
      (state.payload.publication.source_document.blocks || []).map(
        function (block, index) { return [block.block_id, index]; }
      )
    );
    revisions.sort(function (left, right) {
      var leftTarget = fragmentTargetId(left);
      var rightTarget = fragmentTargetId(right);
      var leftOrder = blockOrder.has(leftTarget) ? blockOrder.get(leftTarget) : Infinity;
      var rightOrder = blockOrder.has(rightTarget) ? blockOrder.get(rightTarget) : Infinity;
      return leftOrder - rightOrder || left.priority - right.priority ||
        left.fragment_id.localeCompare(right.fragment_id);
    });
    var parts = ["# " + markdownHeading(readerTitle() + " — " + roleLabel(role))];
    revisions.forEach(function (revision) {
      if (revision.title) parts.push("## " + markdownHeading(revision.title));
      parts.push(normalizeMarkdown(revision.markdown_body).replace(/\n+$/, ""));
    });
    return parts.filter(function (part) { return part !== ""; }).join("\n\n") + "\n";
  }

  function markdownHeading(value) {
    return String(value || "")
      .replace(/<\/?[A-Za-z][^>]*>/g, "")
      .replace(/[\r\n]+/g, " ")
      .trim() || "Untitled";
  }

  function readerTitle() {
    var publication = state.payload.publication;
    var profile = publication.reader_profile || {};
    return profile.title || publication.labels.document_title ||
      sourceTitle(publication.source_document) ||
      publication.labels.untitled_document || "Untitled document";
  }

  function exportFilename(suffix, extension) {
    var title = String(readerTitle()).normalize("NFC")
      .replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ")
      .trim()
      .replace(/[. ]+$/g, "")
      .replace(/\s+/g, "-")
      .slice(0, 80) || "arc-render";
    var role = String(suffix || "export").replace(/[^A-Za-z0-9_-]+/g, "-");
    return title + "-" + role + "." + extension;
  }

  function downloadText(filename, value, mediaType) {
    var url = URL.createObjectURL(new Blob([value], {type: mediaType}));
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    setTimeout(function () {
      link.remove();
      URL.revokeObjectURL(url);
    }, 0);
  }

  function exportRevisionState() {
    var records = [];
    var fragmentOrder = [];
    state.revisions.forEach(function (values, fragmentId) {
      if (
        !state.activeFragmentIds.has(fragmentId) &&
        !browserCreatedHistory(values)
      ) {
        return;
      }
      fragmentOrder.push(fragmentId);
      values.slice().sort(function (left, right) {
        return left.revision - right.revision ||
          left.semantic_digest.localeCompare(right.semantic_digest);
      }).forEach(function (revision) {
        records.push({
          metadata: metadataOnly(revision),
          markdown_body: normalizeMarkdown(revision.markdown_body),
          semantic_digest: revision.semantic_digest
        });
      });
    });
    var positions = new Map(fragmentOrder.map(function (fragmentId, index) {
      return [fragmentId, index];
    }));
    var selected = Array.from(state.selected.values()).filter(function (revision) {
      return positions.has(revision.fragment_id);
    });
    selected.sort(function (left, right) {
      return left.priority - right.priority ||
        positions.get(left.fragment_id) - positions.get(right.fragment_id);
    });
    return {
      revisions: records,
      selected_revision_digests: selected.map(function (revision) {
        return revision.semantic_digest;
      })
    };
  }

  function buildStandaloneExportHtml() {
    if (!state.exportHtmlTemplate) captureExportTemplate();
    if (!state.exportStandaloneSupported || !state.exportHtmlTemplate) {
      throw new Error(labels().exportUnavailable);
    }
    var payload = JSON.parse(JSON.stringify(state.payload));
    var revisionState = exportRevisionState();
    payload.revisions = revisionState.revisions;
    payload.selected_revision_digests = revisionState.selected_revision_digests;
    var encoded = JSON.stringify(payload).replace(/<\/script/gi, "<\\/script");
    var pattern = /(<script[^>]*\bid=["']arc-render-payload["'][^>]*>)[\s\S]*?(<\/script>)/i;
    if (!pattern.test(state.exportHtmlTemplate)) {
      throw new Error(labels().exportUnavailable);
    }
    return state.exportHtmlTemplate.replace(pattern, function (_match, open, close) {
      return open + encoded + close;
    });
  }

  async function setupEditor() {
    var strings = labels();
    var connect = document.getElementById("arc-connect");
    updateDirectoryControl();
    if (!window.showDirectoryPicker) {
      connect.disabled = true;
      setStatus(strings.noDirectoryApi, "error");
    } else {
      connect.disabled = true;
      await restoreDirectoryHandle();
      updateDirectoryControl();
      connect.disabled = false;
      connect.addEventListener("click", connectDirectory);
    }
    var dialog = document.getElementById("arc-editor-dialog");
    document.getElementById("arc-editor-title-label").textContent =
      strings.title;
    document.getElementById("arc-editor-markdown-label").textContent =
      strings.markdown;
    document.getElementById("arc-editor-preview-label").textContent =
      strings.preview;
    document.getElementById("arc-editor-advanced-label").textContent =
      strings.advanced;
    document.getElementById("arc-editor-role-label").textContent =
      strings.role;
    document.getElementById("arc-editor-priority-label").textContent =
      strings.priority;
    document.getElementById("arc-editor-save").textContent = strings.save;
    document.getElementById("arc-editor-cancel").textContent = strings.cancel;
    var close = document.getElementById("arc-editor-close");
    close.setAttribute("aria-label", strings.close);
    close.title = strings.close;
    Array.prototype.forEach.call(
      document.getElementById("arc-editor-role").options,
      function (option) { option.textContent = strings[option.value] || option.value; }
    );
    close.onclick = closeEditorDialog;
    document.getElementById("arc-editor-cancel").onclick = closeEditorDialog;
    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      if (!state.saveInProgress) closeEditorDialog();
    });
    document.getElementById("arc-editor-title").addEventListener(
      "input", syncDraftAndSaveState
    );
    document.getElementById("arc-editor-role").addEventListener(
      "change", syncDraftAndSaveState
    );
    document.getElementById("arc-editor-priority").addEventListener(
      "input", syncDraftAndSaveState
    );
    document.getElementById("arc-editor-markdown").addEventListener("input", function () {
      syncDraftAndSaveState();
      markEditorPreviewDirty();
    });
    document.getElementById("arc-editor-save").addEventListener("click", saveEditor);
  }

  function updateDirectoryControl() {
    var connect = document.getElementById("arc-connect");
    if (!connect) return;
    var strings = labels();
    connect.textContent = state.directory ?
      strings.changeSaveLocation : strings.newSaveLocation;
  }

  async function connectDirectory() {
    if (state.activeDraft && !state.saveInProgress) {
      setStatus(labels().draftActive, "error");
      return false;
    }
    if (
      state.exportInProgress ||
      state.directorySelectionInProgress
    ) {
      setStatus(labels().revisionBusy, "error");
      return false;
    }
    state.directorySelectionInProgress = true;
    var previousDirectory = state.directory;
    try {
      var handle = await window.showDirectoryPicker({
        id: "arc-render-project",
        mode: "readwrite"
      });
      var permission = await handle.requestPermission({mode: "readwrite"});
      if (permission !== "granted") throw new Error("read/write permission was not granted");
      setStatus(labels().loading);
      if (!await loadDirectoryRevisions(handle)) return false;
      await rememberDirectoryHandle(handle);
      updateDirectoryControl();
      setStatus(labels().connected);
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") return false;
      state.directory = previousDirectory;
      updateDirectoryControl();
      setStatus(String(error.message || error), "error");
      return false;
    } finally {
      state.directorySelectionInProgress = false;
    }
  }

  async function loadDirectoryRevisions(directory) {
    var handle = directory || state.directory;
    if (!handle) return false;
    var generation = state.directoryLoadGeneration + 1;
    state.directoryLoadGeneration = generation;
    var revisions = new Map();
    var revisionDigests = new Map();
    var fileDiagnostics = [];
    state.embeddedRevisions.forEach(function (revision) {
      addRevisionTo(revision, revisions, fileDiagnostics, revisionDigests);
    });
    var previousCache = handle === state.directoryCacheHandle ?
      state.directoryFileCache : new Map();
    var nextCache = new Map();
    var fragments;
    try {
      fragments = await handle.getDirectoryHandle("fragments");
    } catch (error) {
      if (error.name === "NotFoundError") {
        return commitDirectorySnapshot(
          handle, revisions, revisionDigests, fileDiagnostics, nextCache, generation
        );
      }
      throw error;
    }
    var files = await collectMarkdownFiles(fragments);
    var outcomes = await loadDirectoryRevisionFiles(
      files, embeddedRevisionFilenames(), previousCache, nextCache
    );
    outcomes.forEach(function (outcome) {
      if (!outcome) return;
      if (outcome.revision) {
        addRevisionTo(
          outcome.revision, revisions, fileDiagnostics, revisionDigests
        );
      } else if (outcome.diagnostic) {
        fileDiagnostics.push(outcome.diagnostic);
      }
    });
    return commitDirectorySnapshot(
      handle, revisions, revisionDigests, fileDiagnostics, nextCache, generation
    );
  }

  function commitDirectorySnapshot(
    handle, revisions, revisionDigests, fileDiagnostics, fileCache, generation
  ) {
    if (generation !== state.directoryLoadGeneration) return false;
    var previousSelected = new Map(state.selected);
    state.directory = handle;
    state.revisions = revisions;
    state.revisionDigests = revisionDigests;
    state.fileDiagnostics = fileDiagnostics;
    state.directoryCacheHandle = handle;
    state.directoryFileCache = fileCache;
    resolveAll();
    refreshChangedSelections(previousSelected);
    return true;
  }

  function embeddedRevisionFilenames() {
    var filenames = new Set();
    state.embeddedRevisions.forEach(function (raw) {
      var metadata = raw.metadata || raw;
      var digest = raw.semantic_digest || metadata.semantic_digest;
      if (
        positiveInteger(metadata.revision) &&
        typeof digest === "string" &&
        /^[0-9a-f]{64}$/.test(digest)
      ) {
        filenames.add(revisionFilename(metadata.revision, digest));
      }
    });
    return filenames;
  }

  async function loadDirectoryRevisionFiles(
    files, embeddedFilenames, previousCache, nextCache
  ) {
    var outcomes = new Array(files.length);
    var nextIndex = 0;
    async function worker() {
      while (true) {
        var index = nextIndex;
        nextIndex += 1;
        if (index >= files.length) return;
        outcomes[index] = await loadDirectoryRevisionFile(
          files[index], embeddedFilenames, previousCache, nextCache
        );
      }
    }
    var workers = [];
    var workerCount = Math.min(DIRECTORY_READ_CONCURRENCY, files.length);
    for (var index = 0; index < workerCount; index += 1) {
      workers.push(worker());
    }
    await Promise.all(workers);
    return outcomes;
  }

  async function loadDirectoryRevisionFile(
    entry, embeddedFilenames, previousCache, nextCache
  ) {
    if (embeddedFilenames.has(entry.name)) return null;
    var key = JSON.stringify(entry.path);
    var file;
    try {
      file = await entry.handle.getFile();
    } catch (error) {
      return {
        diagnostic: "Ignored invalid fragment file " + entry.name + ": " +
          String(error.message || error)
      };
    }
    var stamp = String(file.size) + ":" + String(file.lastModified);
    var cached = previousCache.get(key);
    if (cached && cached.stamp === stamp) {
      nextCache.set(key, cached);
      return cached.outcome;
    }
    var outcome;
    try {
      var revision = await parseRevisionFile(await file.text(), entry.name);
      revision._origin = "directory";
      outcome = {revision: revision};
    } catch (error) {
      outcome = {
        diagnostic: "Ignored invalid fragment file " + entry.name + ": " +
          String(error.message || error)
      };
    }
    nextCache.set(key, {stamp: stamp, outcome: outcome});
    return outcome;
  }

  async function collectMarkdownFiles(directory) {
    var output = [];
    var pending = [{handle: directory, path: []}];
    while (pending.length) {
      var batch = pending.splice(0, DIRECTORY_READ_CONCURRENCY);
      var scans = await Promise.all(batch.map(scanMarkdownDirectory));
      scans.forEach(function (scan) {
        output = output.concat(scan.files);
        pending = pending.concat(scan.directories);
      });
    }
    output.sort(function (left, right) {
      return JSON.stringify(left.path).localeCompare(JSON.stringify(right.path));
    });
    return output;
  }

  async function scanMarkdownDirectory(item) {
    var directories = [];
    var files = [];
    for await (var entry of item.handle.values()) {
      var path = item.path.concat([entry.name]);
      if (entry.kind === "directory") {
        directories.push({handle: entry, path: path});
      } else if (entry.kind === "file" && entry.name.endsWith(".md")) {
        files.push({handle: entry, name: entry.name, path: path});
      }
    }
    directories.sort(function (left, right) {
      return JSON.stringify(left.path).localeCompare(JSON.stringify(right.path));
    });
    files.sort(function (left, right) {
      return JSON.stringify(left.path).localeCompare(JSON.stringify(right.path));
    });
    return {directories: directories, files: files};
  }

  function openEditEditor(fragment) {
    beginInlineEdit(fragment);
  }

  function beginInlineEdit(fragment) {
    if (state.exportInProgress || state.directorySelectionInProgress) {
      setStatus(labels().revisionBusy, "error");
      return;
    }
    if (state.saveInProgress) return;
    if (state.activeDraft) {
      if (
        state.activeDraft.base &&
        state.activeDraft.base.fragment_id === fragment.fragment_id
      ) {
        focusInlineEditor(fragment.fragment_id);
      } else {
        setStatus(labels().draftActive, "error");
      }
      return;
    }
    state.activeDraft = draftFromFragment(fragment);
    state.editorBase = fragment;
    state.editorAnchor = fragment.anchor;
    state.editorHistorical = fragment;
    replaceFragmentCard(fragment.fragment_id, fragment.anchor);
    focusInlineEditor(fragment.fragment_id);
  }

  function draftFromFragment(fragment) {
    return {
      base: fragment,
      anchor: JSON.parse(JSON.stringify(fragment.anchor)),
      title: fragment.title || null,
      role: fragment.role,
      priority: fragment.priority,
      markdown_body: fragment.markdown_body || ""
    };
  }

  function focusInlineEditor(fragmentId) {
    window.requestAnimationFrame(function () {
      var card = document.querySelector(
        '.arc-fragment[data-fragment-id="' + cssString(fragmentId) + '"]'
      );
      var textarea = card && card.querySelector(".arc-inline-markdown");
      if (textarea) textarea.focus();
    });
  }

  function cssString(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(String(value));
    }
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function openNewEditor(block) {
    openNewEditorForAnchor({
      kind: "block",
      target_id: block.block_id,
      related_blocks: [anchorBlock(block)]
    });
  }

  function openNewEditorForAnchor(anchor) {
    if (state.exportInProgress || state.directorySelectionInProgress) {
      setStatus(labels().revisionBusy, "error");
      return;
    }
    if (state.activeDraft) {
      setStatus(labels().draftActive, "error");
      return;
    }
    state.activeDraft = {
      base: null,
      anchor: JSON.parse(JSON.stringify(anchor)),
      title: null,
      role: "note",
      priority: 110,
      markdown_body: ""
    };
    state.editorBase = null;
    state.editorHistorical = null;
    state.editorAnchor = anchor;
    openAdvancedEditor(null, labels().newNote);
  }

  function openAdvancedEditor(event, heading) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (!state.activeDraft || state.saveInProgress) return;
    var dialog = document.getElementById("arc-editor-dialog");
    var draft = state.activeDraft;
    state.editorGeneration += 1;
    document.getElementById("arc-editor-heading").textContent =
      heading || labels().editor;
    document.getElementById("arc-editor-title").value = draft.title || "";
    document.getElementById("arc-editor-role").value = draft.role || "note";
    document.getElementById("arc-editor-priority").value = String(draft.priority || 110);
    document.getElementById("arc-editor-markdown").value = draft.markdown_body || "";
    state.editorPreviewDirty = true;
    renderHistory(draft.base && draft.base.fragment_id);
    updatePreview();
    updateDraftSaveButtons();
    dialog.showModal();
  }

  function closeEditorDialog() {
    if (state.saveInProgress) return;
    var dialog = document.getElementById("arc-editor-dialog");
    if (!state.activeDraft) {
      if (dialog.open) dialog.close();
      return;
    }
    syncDraftFromDialog();
    if (!state.activeDraft.base) {
      state.activeDraft = null;
      state.editorBase = null;
      state.editorAnchor = null;
      state.editorHistorical = null;
      if (dialog.open) dialog.close();
      return;
    }
    var anchor = state.activeDraft.anchor;
    if (dialog.open) dialog.close();
    replaceFragmentCard(state.activeDraft.base.fragment_id, anchor);
  }

  function cancelActiveDraft(event) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (!state.activeDraft || state.saveInProgress) return;
    var anchor = state.activeDraft.anchor;
    var fragmentId = state.activeDraft.base && state.activeDraft.base.fragment_id;
    state.activeDraft = null;
    state.editorBase = null;
    state.editorAnchor = null;
    state.editorHistorical = null;
    var dialog = document.getElementById("arc-editor-dialog");
    if (dialog && dialog.open) dialog.close();
    if (fragmentId) replaceFragmentCard(fragmentId, anchor);
  }

  function replaceFragmentCard(fragmentId, anchor) {
    var current = state.selected.get(fragmentId);
    var card = document.querySelector(
      '.arc-fragment[data-fragment-id="' + cssString(fragmentId) + '"]'
    );
    if (current && card && typeof card.replaceWith === "function") {
      card.replaceWith(renderFragment(current));
    }
  }

  function renderHistory(fragmentId) {
    var root = document.getElementById("arc-editor-history");
    root.replaceChildren();
    if (!fragmentId) return;
    var strings = labels();
    var revisions = (state.revisions.get(fragmentId) || []).slice().sort(function (a, b) {
      return a.revision - b.revision;
    });
    var toolbar = element("div", "arc-history-toolbar");
    toolbar.appendChild(element("span", "", strings.history + ": "));
    revisions.forEach(function (revision) {
      var button = element("button", "", "v" + revision.revision);
      button.type = "button";
      button.classList.toggle(
        "is-selected",
        Boolean(
          state.editorHistorical &&
          state.editorHistorical.semantic_digest === revision.semantic_digest
        )
      );
      button.onclick = function () {
        state.editorHistorical = revision;
        renderHistory(fragmentId);
      };
      toolbar.appendChild(button);
    });
    root.appendChild(toolbar);
    if (!state.editorBase || !state.editorHistorical) return;

    var compare = element("div", "arc-history-compare");
    compare.appendChild(historyPane(
      strings.compareCurrent + " · v" + state.editorBase.revision,
      state.editorBase.markdown_body
    ));
    compare.appendChild(historyPane(
      strings.compareHistorical + " · v" + state.editorHistorical.revision,
      state.editorHistorical.markdown_body
    ));
    root.appendChild(compare);
    var restore = element("button", "arc-history-restore", strings.restore);
    restore.type = "button";
    restore.onclick = restoreHistoricalRevision;
    root.appendChild(restore);
  }

  function historyPane(title, markdown) {
    var pane = element("section", "arc-history-pane");
    pane.appendChild(element("h3", "", title));
    pane.appendChild(element("pre", "", markdown || ""));
    return pane;
  }

  function restoreHistoricalRevision() {
    var revision = state.editorHistorical;
    if (!revision || !state.activeDraft) return;
    state.activeDraft.title = revision.title || null;
    state.activeDraft.role = revision.role;
    state.activeDraft.priority = revision.priority;
    state.activeDraft.markdown_body = revision.markdown_body;
    document.getElementById("arc-editor-title").value = revision.title || "";
    document.getElementById("arc-editor-role").value = revision.role;
    document.getElementById("arc-editor-priority").value = String(revision.priority);
    document.getElementById("arc-editor-markdown").value = revision.markdown_body;
    updateDraftSaveButtons();
    markEditorPreviewDirty();
  }

  function syncDraftAndSaveState() {
    syncDraftFromDialog();
    updateDraftSaveButtons();
  }

  function syncDraftFromDialog() {
    if (!state.activeDraft) return;
    var dialog = document.getElementById("arc-editor-dialog");
    if (!dialog || !dialog.open) return;
    state.activeDraft.title =
      document.getElementById("arc-editor-title").value;
    state.activeDraft.role =
      document.getElementById("arc-editor-role").value;
    state.activeDraft.priority =
      document.getElementById("arc-editor-priority").value;
    state.activeDraft.markdown_body =
      document.getElementById("arc-editor-markdown").value;
  }

  function markEditorPreviewDirty() {
    state.editorPreviewDirty = true;
    if (state.editorPreviewTimer !== null) {
      window.clearTimeout(state.editorPreviewTimer);
    }
    state.editorPreviewTimer = window.setTimeout(function () {
      state.editorPreviewTimer = null;
      var dialog = document.getElementById("arc-editor-dialog");
      if (dialog && dialog.open && state.editorPreviewDirty) updatePreview();
    }, 150);
  }

  function updatePreview() {
    var preview = document.getElementById("arc-editor-preview");
    preview.replaceChildren(renderMarkdown(
      document.getElementById("arc-editor-markdown").value
    ));
    state.editorPreviewDirty = false;
  }

  async function saveEditor(event) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (state.exportInProgress || state.directorySelectionInProgress) {
      setStatus(labels().revisionBusy, "error");
      return;
    }
    if (state.saveInProgress || !state.activeDraft) return;
    syncDraftFromDialog();
    var saveButton = document.getElementById("arc-editor-save");
    var dialog = document.getElementById("arc-editor-dialog");
    var controls = dialog ? Array.prototype.slice.call(
      dialog.querySelectorAll("button, input, select, textarea")
    ) : [];
    if (typeof document.querySelectorAll === "function") {
      Array.prototype.forEach.call(document.querySelectorAll(
        ".arc-inline-editor button, .arc-inline-editor textarea, " +
        ".arc-inline-actions button"
      ), function (control) {
        if (controls.indexOf(control) < 0) controls.push(control);
      });
    }
    var disabledStates = controls.map(function (control) {
      return control.disabled;
    });
    state.saveInProgress = true;
    controls.forEach(function (control) { control.disabled = true; });
    if (saveButton) saveButton.disabled = true;
    try {
      var draft = state.activeDraft;
      var base = draft.base;
      var editorGeneration = state.editorGeneration;
      var editorAnchor = JSON.parse(JSON.stringify(draft.anchor));
      var editable = editableDraftState(draft);
      assertKnownCitations(editable.citation_ids);
      if (base) assertEditorBaseCurrent(base);
      if (
        base &&
        stableStringify(editable) ===
          stableStringify(editableRevisionState(base))
      ) {
        finishActiveDraft(base.fragment_id, editorAnchor, editorGeneration);
        setStatus(labels().saveUnchanged);
        return;
      }
      if (!state.directory) {
        if (!await connectDirectory()) return;
      }
      if (!state.directory) return;
      if (base) assertEditorBaseCurrent(base);
      var metadata = base ? metadataOnly(base) : newNoteMetadata(editorAnchor);
      metadata.revision = base ? base.revision + 1 : 1;
      metadata.parent_semantic_digest = base ? base.semantic_digest : null;
      metadata.title = editable.title;
      metadata.role = editable.role;
      metadata.priority = editable.priority;
      metadata.citation_ids = editable.citation_ids;
      metadata.provenance = Object.assign({}, metadata.provenance || {}, {
        last_editor: "arc-render-browser",
        edited_at: new Date().toISOString()
      });
      validateRevisionMetadata(metadata);
      var digest = await semanticDigest(metadata, editable.markdown_body);
      var encoded = FRONT_BEGIN + "\n" + stableStringify(metadata) + "\n" +
        FRONT_END + "\n" + editable.markdown_body;
      var filename = revisionFilename(metadata.revision, digest);
      var folder = await fragmentsDirectory(true);
      await writeImmutableRevision(folder, filename, encoded);
      var revision = Object.assign({}, metadata, {
        markdown_body: editable.markdown_body,
        semantic_digest: digest,
        _origin: "directory"
      });
      state.activeDraft = null;
      state.editorBase = null;
      state.editorAnchor = null;
      state.editorHistorical = null;
      var uiError = null;
      try {
        addRevision(revision);
        resolveOne(revision.fragment_id);
        refreshFragmentGroup(revision.fragment_id, revision.anchor);
      } catch (error) {
        uiError = error;
      }
      try {
        refreshChunkForAnchor(revision.anchor);
      } catch (error) {
        uiError = uiError || error;
      }
      if (state.editorGeneration === editorGeneration) {
        try {
          if (dialog && dialog.open) dialog.close();
        } catch (error) {
          uiError = uiError || error;
        }
      }
      if (uiError && base) {
        try {
          replaceFragmentCard(revision.fragment_id, revision.anchor);
        } catch (_replaceError) {
          /* The saved revision and original UI error remain authoritative. */
        }
      }
      if (uiError) {
        try {
          updateFragmentGroup(revision.fragment_id, revision.anchor);
          rerenderChunk(chunkForAnchor(revision.anchor));
        } catch (_recoveryError) {
          /* The status below reports the first post-commit UI error. */
        }
      }
      setStatus(
        labels().saveSuccess +
          (uiError ? " " + String(uiError.message || uiError) : "")
      );
    } catch (error) {
      setStatus(String(error.message || error), "error");
    } finally {
      state.saveInProgress = false;
      controls.forEach(function (control, index) {
        control.disabled = disabledStates[index];
      });
    }
  }

  function editableDraftState(draft) {
    var markdown = normalizeMarkdown(draft.markdown_body);
    var priority = Number(draft.priority);
    if (!Number.isInteger(priority) || priority < 1) {
      throw new Error("priority must be a positive integer");
    }
    return {
      title: String(draft.title || "").normalize("NFC").trim() || null,
      markdown_body: markdown,
      role: String(draft.role || ""),
      priority: priority,
      citation_ids: citationIds(markdown)
    };
  }

  function editableRevisionState(revision) {
    var markdown = normalizeMarkdown(revision.markdown_body);
    return {
      title: String(revision.title || "").normalize("NFC").trim() || null,
      markdown_body: markdown,
      role: revision.role,
      priority: revision.priority,
      citation_ids: citationIds(markdown)
    };
  }

  function activeDraftHasChanges() {
    var draft = state.activeDraft;
    if (!draft) return false;
    if (!draft.base) return true;
    try {
      return stableStringify(editableDraftState(draft)) !==
        stableStringify(editableRevisionState(draft.base));
    } catch (_error) {
      return true;
    }
  }

  function updateDraftSaveButtons(scope) {
    var disabled = !activeDraftHasChanges() || state.saveInProgress;
    var dialogSave = document.getElementById("arc-editor-save");
    if (dialogSave) dialogSave.disabled = disabled;
    var localSave = scope && typeof scope.querySelector === "function" ?
      scope.querySelector(".arc-inline-save") : null;
    if (localSave) localSave.disabled = disabled;
    if (typeof document.querySelectorAll !== "function") return;
    Array.prototype.forEach.call(
      document.querySelectorAll(".arc-inline-save"),
      function (button) { button.disabled = disabled; }
    );
  }

  function assertEditorBaseCurrent(base) {
    var current = state.selected.get(base.fragment_id);
    var nextChildren = (
      state.revisions.get(base.fragment_id) || []
    ).filter(function (revision) {
      return revision.parent_semantic_digest === base.semantic_digest &&
        revision.revision === base.revision + 1;
    });
    if (
      !current ||
      current.semantic_digest !== base.semantic_digest ||
      nextChildren.length > 0
    ) {
      throw new Error(labels().historyChanged);
    }
  }

  function finishActiveDraft(fragmentId, anchor, editorGeneration) {
    state.activeDraft = null;
    state.editorBase = null;
    state.editorAnchor = null;
    state.editorHistorical = null;
    var dialog = document.getElementById("arc-editor-dialog");
    if (
      dialog &&
      dialog.open &&
      state.editorGeneration === editorGeneration
    ) {
      dialog.close();
    }
    replaceFragmentCard(fragmentId, anchor);
  }

  async function writeImmutableRevision(folder, filename, encoded) {
    var existing = null;
    try {
      existing = await folder.getFileHandle(filename);
    } catch (error) {
      if (error.name !== "NotFoundError") throw error;
    }
    if (existing) {
      if (await revisionFileMatches(existing, encoded)) return;
      throw new Error("revision file already exists; no file was overwritten");
    }
    var handle = await folder.getFileHandle(filename, {create: true});
    var created = await handle.getFile();
    if (created.size > 0) {
      if (await created.text() === encoded) return;
      throw new Error("revision file already exists; no file was overwritten");
    }
    var writable = await handle.createWritable();
    try {
      await writable.write(encoded);
      await writable.close();
    } catch (error) {
      if (await revisionFileMatches(handle, encoded)) return;
      if (typeof writable.abort === "function") {
        try {
          await writable.abort();
        } catch (_abortError) {
          /* The original write error is the useful failure. */
        }
      }
      throw error;
    }
    if (!await revisionFileMatches(handle, encoded)) {
      throw new Error("saved revision bytes could not be verified");
    }
  }

  async function revisionFileMatches(handle, encoded) {
    try {
      return await (await handle.getFile()).text() === encoded;
    } catch (_error) {
      return false;
    }
  }

  function revisionFilename(revision, digest) {
    return "revision-" + String(revision).padStart(6, "0") +
      "-" + digest + ".md";
  }

  function metadataOnly(revision) {
    var keys = [
      "schema_version", "source", "fragment_id", "revision",
      "parent_semantic_digest", "anchor", "priority", "role", "language",
      "title", "citation_ids", "provenance"
    ];
    var value = {};
    keys.forEach(function (key) { value[key] = revision[key]; });
    return JSON.parse(JSON.stringify(value));
  }

  function newNoteMetadata(anchor) {
    var publication = state.payload.publication;
    var profile = publication.reader_profile || {};
    return {
      schema_version: FRAGMENT_SCHEMA,
      source: state.payload.source_identity,
      fragment_id: "user-" + crypto.randomUUID().toLowerCase(),
      revision: 1,
      parent_semantic_digest: null,
      anchor: anchor,
      priority: 110,
      role: "note",
      language: profile.target_language || profile.source_language || "und",
      title: null,
      citation_ids: [],
      provenance: {
        producer: "arc-render-browser",
        created_at: new Date().toISOString()
      }
    };
  }

  function anchorBlock(block) {
    var fingerprints = state.payload.block_fingerprints || {};
    return {
      block_id: block.block_id,
      kind: block.kind,
      ordinal: block.ordinal,
      locator: block.locator,
      content_fingerprint: fingerprints[block.block_id]
    };
  }

  function citationIds(markdown) {
    var values = [];
    var seen = new Set();
    var pattern = /\[@([A-Za-z0-9][A-Za-z0-9._:-]*)\]/g;
    var match;
    while ((match = pattern.exec(markdown))) {
      if (!seen.has(match[1])) {
        seen.add(match[1]);
        values.push(match[1]);
      }
    }
    return values;
  }

  function bibliographyIdSet() {
    var values = new Set();
    (state.payload.publication.bibliography || []).forEach(function (item) {
      var id = item.evidence_id || item.citation_id || item.id;
      if (typeof id === "string" && id) values.add(id);
    });
    return values;
  }

  function assertKnownCitations(citations) {
    var known = bibliographyIdSet();
    var unknown = citations.find(function (citation) {
      return !known.has(citation);
    });
    if (unknown !== undefined) {
      throw new Error(labels().unknownCitation + unknown);
    }
  }

  async function fragmentsDirectory(create) {
    return state.directory.getDirectoryHandle(
      "fragments", {create: Boolean(create)}
    );
  }

  async function parseRevisionFile(value, filename) {
    var prefix = FRONT_BEGIN + "\n";
    var separator = "\n" + FRONT_END + "\n";
    if (value.slice(0, prefix.length) !== prefix) {
      throw new Error("missing JSON front matter");
    }
    var split = value.indexOf(separator, prefix.length);
    if (split < 0) throw new Error("unterminated JSON front matter");
    var metadata = JSON.parse(value.slice(prefix.length, split));
    if (stableStringify(metadata) !== value.slice(prefix.length, split)) {
      throw new Error("JSON front matter is not canonical or has duplicate keys");
    }
    validateRevisionMetadata(metadata);
    var markdown = normalizeMarkdown(value.slice(split + separator.length));
    var digest = await semanticDigest(metadata, markdown);
    var expected = /^revision-([0-9]{6,})-([0-9a-f]{64})[.]md$/.exec(filename);
    if (!expected || Number(expected[1]) !== metadata.revision || expected[2] !== digest) {
      throw new Error("filename identity does not match content");
    }
    return Object.assign({}, metadata, {
      markdown_body: markdown,
      semantic_digest: digest
    });
  }

  async function semanticDigest(metadata, markdown) {
    if (!crypto.subtle) throw new Error("Web Crypto is required to save revisions");
    var material = stableStringify({
      metadata: metadata,
      markdown_body: normalizeMarkdown(markdown)
    });
    var bytes = new TextEncoder().encode(material);
    var digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest)).map(function (value) {
      return value.toString(16).padStart(2, "0");
    }).join("");
  }

  function validateRevisionMetadata(metadata) {
    ensureSourceIndexes();
    validateCurrentSourceIdentity();
    var fields = [
      "anchor", "citation_ids", "fragment_id", "language",
      "parent_semantic_digest", "priority", "provenance", "revision", "role",
      "schema_version", "source", "title"
    ];
    requireExactObject(metadata, fields, "fragment revision");
    validateIntegerJson(metadata, "fragment revision");
    if (
      metadata.schema_version !== FRAGMENT_SCHEMA ||
      !portableIdentifier(metadata.fragment_id) ||
      !positiveInteger(metadata.revision) ||
      !positiveInteger(metadata.priority) ||
      !normalizedNonblank(metadata.role) ||
      !normalizedNonblank(metadata.language) ||
      (
        metadata.title !== null &&
        !normalizedNonblank(metadata.title)
      )
    ) {
      throw new Error("fragment revision metadata is invalid");
    }
    if (
      (metadata.revision === 1 && metadata.parent_semantic_digest !== null) ||
      (metadata.revision > 1 &&
        !/^[0-9a-f]{64}$/.test(metadata.parent_semantic_digest || ""))
    ) {
      throw new Error("fragment revision parent identity is invalid");
    }
    if (stableStringify(metadata.source) !== state.sourceIdentityJson) {
      throw new Error("fragment revision binds another rich source");
    }
    validateAnchor(metadata.anchor);
    if (!Array.isArray(metadata.citation_ids)) {
      throw new Error("fragment citation_ids must be an array");
    }
    var citationSet = new Set();
    metadata.citation_ids.forEach(function (citation) {
      if (!normalizedNonblank(citation) || citationSet.has(citation)) {
        throw new Error("fragment citation IDs must be unique non-empty strings");
      }
      citationSet.add(citation);
    });
    if (!plainObject(metadata.provenance)) {
      throw new Error("fragment provenance must be an object");
    }
  }

  function validateCurrentSourceIdentity() {
    if (state.sourceIdentityValidatedPayload === state.payload) return;
    validateSourceIdentity(state.payload.source_identity);
    state.sourceIdentityValidatedPayload = state.payload;
  }

  function validateSourceIdentity(source) {
    requireExactObject(source, [
      "artifact_digest", "media_type", "rich_document_digest",
      "size", "source_format"
    ], "source identity");
    if (
      !["html", "markdown", "tex"].includes(source.source_format) ||
      typeof source.media_type !== "string" ||
      source.media_type !== source.media_type.trim().toLowerCase() ||
      source.media_type.indexOf("/") < 1 ||
      source.media_type.indexOf(";") >= 0 ||
      !digestValue(source.artifact_digest) ||
      !digestValue(source.rich_document_digest) ||
      !nonnegativeInteger(source.size)
    ) {
      throw new Error("fragment source identity is invalid");
    }
  }

  function validateAnchor(anchor) {
    requireExactObject(
      anchor, ["kind", "related_blocks", "target_id"], "fragment anchor"
    );
    if (
      !["block", "section"].includes(anchor.kind) ||
      !normalizedNonblank(anchor.target_id) ||
      !Array.isArray(anchor.related_blocks)
    ) {
      throw new Error("fragment anchor is invalid");
    }
    var indexes = ensureSourceIndexes();
    var blocks = indexes.blocksById;
    var sections = indexes.sectionsById;
    if (
      (anchor.kind === "block" && !blocks.has(anchor.target_id)) ||
      (anchor.kind === "section" && !sections.has(anchor.target_id))
    ) {
      throw new Error("fragment anchor target is absent from the rich source");
    }
    var blockIds = new Set();
    var ordinals = new Set();
    anchor.related_blocks.forEach(function (frozen) {
      requireExactObject(frozen, [
        "block_id", "content_fingerprint", "kind", "locator", "ordinal"
      ], "anchor related block");
      if (
        !normalizedNonblank(frozen.block_id) ||
        !["heading", "paragraph", "list", "code", "equation", "table", "figure"]
          .includes(frozen.kind) ||
        !nonnegativeInteger(frozen.ordinal) ||
        !plainObject(frozen.locator) ||
        !digestValue(frozen.content_fingerprint) ||
        blockIds.has(frozen.block_id) ||
        ordinals.has(frozen.ordinal)
      ) {
        throw new Error("anchor related block provenance is invalid");
      }
      var current = blocks.get(frozen.block_id);
      if (
        !current ||
        current.kind !== frozen.kind ||
        current.ordinal !== frozen.ordinal ||
        stableStringify(current.locator) !== stableStringify(frozen.locator) ||
        (state.payload.block_fingerprints || {})[frozen.block_id] !==
          frozen.content_fingerprint
      ) {
        throw new Error("anchor related block differs from the rich source");
      }
      blockIds.add(frozen.block_id);
      ordinals.add(frozen.ordinal);
    });
    if (anchor.kind === "block" && !blockIds.has(anchor.target_id)) {
      throw new Error("block anchor target must be a related block");
    }
  }

  function validateIntegerJson(value, description) {
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value)) {
        throw new Error(description + " contains a non-integer JSON number");
      }
      return;
    }
    if (value === null || ["string", "boolean"].includes(typeof value)) return;
    if (Array.isArray(value)) {
      value.forEach(function (item) { validateIntegerJson(item, description); });
      return;
    }
    if (plainObject(value)) {
      Object.keys(value).forEach(function (key) {
        validateIntegerJson(value[key], description);
      });
      return;
    }
    throw new Error(description + " is not JSON-compatible");
  }

  function requireExactObject(value, fields, description) {
    if (
      !plainObject(value) ||
      stableStringify(Object.keys(value).sort()) !==
        stableStringify(fields.slice().sort())
    ) {
      throw new Error(description + " has invalid fields");
    }
  }

  function plainObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function normalizedNonblank(value) {
    return typeof value === "string" && Boolean(value) && value === value.trim();
  }

  function portableIdentifier(value) {
    return normalizedNonblank(value) &&
      /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value);
  }

  function digestValue(value) {
    return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  }

  function positiveInteger(value) {
    return Number.isSafeInteger(value) && value >= 1;
  }

  function nonnegativeInteger(value) {
    return Number.isSafeInteger(value) && value >= 0;
  }

  function stableStringify(value) {
    if (typeof value === "number" && !Number.isSafeInteger(value)) {
      throw new Error("canonical JSON contains a non-integer number");
    }
    if (value === null || typeof value !== "object") {
      var primitive = JSON.stringify(value);
      if (primitive === undefined) throw new Error("value is not JSON-compatible");
      return primitive;
    }
    if (Array.isArray(value)) {
      return "[" + value.map(stableStringify).join(",") + "]";
    }
    return "{" + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ":" + stableStringify(value[key]);
    }).join(",") + "}";
  }

  function safeToken(value) {
    return String(value).replace(/[^A-Za-z0-9_.:-]/g, "-");
  }

  function setStatus(value, kind) {
    var status = document.getElementById("arc-storage-status");
    if (state.statusTimer !== null) {
      window.clearTimeout(state.statusTimer);
      state.statusTimer = null;
    }
    status.textContent = value || "";
    status.dataset.kind = kind || "info";
    status.hidden = !value;
    if (!value) return;
    var timer = window.setTimeout(function () {
      if (state.statusTimer !== timer) return;
      state.statusTimer = null;
      status.textContent = "";
      status.hidden = true;
    }, STATUS_EXPIRY_MS);
    state.statusTimer = timer;
    if (timer && typeof timer.unref === "function") timer.unref();
  }

  function openDatabase() {
    return new Promise(function (resolve, reject) {
      var request = indexedDB.open("arc-render", 1);
      request.onupgradeneeded = function () {
        if (!request.result.objectStoreNames.contains("handles")) {
          request.result.createObjectStore("handles");
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  async function rememberDirectoryHandle(handle) {
    try {
      var database = await openDatabase();
      var transaction = database.transaction("handles", "readwrite");
      transaction.objectStore("handles").put(handle, "project");
    } catch (_error) {
      /* Handle persistence is a convenience, never a reading requirement. */
    }
  }

  async function restoreDirectoryHandle() {
    try {
      var database = await openDatabase();
      var transaction = database.transaction("handles", "readonly");
      var handle = await new Promise(function (resolve, reject) {
        var request = transaction.objectStore("handles").get("project");
        request.onsuccess = function () { resolve(request.result); };
        request.onerror = function () { reject(request.error); };
      });
      if (!handle) return;
      var permission = await handle.queryPermission({mode: "readwrite"});
      if (permission === "granted") {
        if (await loadDirectoryRevisions(handle)) {
          updateDirectoryControl();
          setStatus(labels().connected);
        }
      }
    } catch (_error) {
      state.directory = null;
      /* Opaque file origins may not persist IndexedDB; reconnect still works. */
    }
  }

  async function initialize() {
    try {
      state.exportStandaloneSupported = !document.querySelector(
        'script[src], link[rel~="stylesheet"][href]'
      );
      document.body.dataset.arcRenderReady = "loading";
      document.body.dataset.arcRenderComplete = "false";
      state.payload = readPayload();
      setupMarkdown();
      initialRevisions();
      captureInitialSelection();
      renderReader();
      setupTooltip();
      await setupEditor();
      setupExport();
      if (document.fonts && document.fonts.ready) await document.fonts.ready;
      await Promise.all(Array.prototype.slice.call(document.images).map(function (image) {
        if (image.complete) return Promise.resolve();
        if (typeof image.decode === "function") return image.decode().catch(function () {});
        return new Promise(function (resolve) {
          image.addEventListener("load", resolve, {once: true});
          image.addEventListener("error", resolve, {once: true});
        });
      }));
      activateHashTarget(window.location.hash, false);
      document.body.dataset.arcRenderReady = "true";
      startProgressiveRendering();
    } catch (error) {
      stopProgressiveRendering();
      document.body.dataset.arcRenderReady = "error";
      var root = document.getElementById("arc-document") || document.body;
      root.prepend(element("p", "arc-diagnostic", String(error.message || error)));
      throw error;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
}());
