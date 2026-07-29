(function () {
  "use strict";

  var FRONT_BEGIN = "<!-- ARC:FRAGMENT-JSON:BEGIN -->";
  var FRONT_END = "<!-- ARC:FRAGMENT-JSON:END -->";
  var FRAGMENT_SCHEMA = "arc.render.fragment_revision.v1";
  var state = {
    payload: null,
    md: null,
    revisions: new Map(),
    selected: new Map(),
    embeddedRevisions: [],
    activeFragmentIds: new Set(),
    diagnostics: [],
    fileDiagnostics: [],
    directory: null,
    editorBase: null,
    editorAnchor: null,
    editorHistorical: null
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

  function labels() {
    var publication = state.payload.publication;
    var language = String(
      (publication.reader_profile || {}).target_language || ""
    ).toLowerCase();
    var chinese = language === "zh" || language.indexOf("zh-") === 0;
    var defaults = chinese ? {
      contents: "目录",
      collapse: "收起目录",
      expand: "展开目录",
      connect: "连接项目目录",
      connected: "已连接项目目录",
      edit: "编辑",
      addNote: "添加外挂",
      editor: "编辑外挂",
      newNote: "新建外挂",
      title: "标题",
      role: "类型",
      priority: "优先级",
      advanced: "更多设置",
      markdown: "Markdown",
      preview: "预览",
      save: "另存为新版本",
      close: "关闭",
      history: "版本历史",
      source: "原文",
      translation: "翻译",
      companion: "伴读",
      guide: "导读",
      note: "笔记",
      glossary: "术语表",
      references: "参考文献",
      originalTerm: "原文术语",
      translatedTerm: "译名",
      definition: "释义",
      noDirectoryApi: "当前浏览器不支持本地目录编辑；阅读功能不受影响。",
      saveSuccess: "新版本已保存。",
      loading: "正在读取外挂版本……",
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
      connect: "Connect project directory",
      connected: "Project directory connected",
      edit: "Edit",
      addNote: "Add overlay",
      editor: "Edit overlay",
      newNote: "New overlay",
      title: "Title",
      role: "Role",
      priority: "Priority",
      advanced: "More options",
      markdown: "Markdown",
      preview: "Preview",
      save: "Save as new revision",
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
      noDirectoryApi: "This browser cannot edit a local directory; reading is unaffected.",
      saveSuccess: "A new revision was saved.",
      loading: "Loading overlay revisions…",
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
    var values = {};
    (state.payload.publication.bibliography || []).forEach(function (item, index) {
      var id = item.evidence_id || item.citation_id || item.id;
      if (id) values[id] = index + 1;
    });
    return values;
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
    state.embeddedRevisions = (state.payload.revisions || []).slice();
    state.activeFragmentIds = new Set(state.embeddedRevisions.map(function (raw) {
      return raw.metadata ? raw.metadata.fragment_id : raw.fragment_id;
    }));
    resetRevisionState();
    resolveAll();
  }

  function resetRevisionState() {
    state.revisions = new Map();
    state.fileDiagnostics = [];
    state.embeddedRevisions.forEach(addRevision);
  }

  function addRevision(raw) {
    var revision = raw.metadata ? Object.assign({}, raw.metadata) : Object.assign({}, raw);
    revision.markdown_body = raw.markdown_body === undefined ?
      revision.markdown_body || "" : raw.markdown_body;
    revision.semantic_digest = raw.semantic_digest || revision.semantic_digest || "";
    revision._origin = raw._origin || "embedded";
    try {
      validateRevisionMetadata(metadataOnly(revision));
    } catch (error) {
      state.fileDiagnostics.push(
        "Ignored invalid fragment " + (revision.fragment_id || "(unknown)") + ": " +
        String(error.message || error)
      );
      return;
    }
    var values = state.revisions.get(revision.fragment_id) || [];
    if (!values.some(function (item) {
      return item.semantic_digest &&
        item.semantic_digest === revision.semantic_digest;
    })) {
      values.push(revision);
      state.revisions.set(revision.fragment_id, values);
    }
  }

  function resolveAll() {
    state.selected.clear();
    state.diagnostics = (state.payload.diagnostics || []).slice().concat(
      state.fileDiagnostics
    );
    state.revisions.forEach(function (values, fragmentId) {
      if (
        !state.activeFragmentIds.has(fragmentId) &&
        !browserCreatedHistory(values)
      ) {
        return;
      }
      var resolved = resolveFragment(values, fragmentId);
      if (resolved.selected) state.selected.set(fragmentId, resolved.selected);
      state.diagnostics = state.diagnostics.concat(resolved.diagnostics);
    });
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
    values.forEach(function (item) {
      if (item.semantic_digest) byDigest.set(item.semantic_digest, item);
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
      var children = values.filter(function (item) {
        return item.parent_semantic_digest === current.semantic_digest &&
          item.revision === current.revision + 1;
      });
      children = Array.from(new Map(children.map(function (item) {
        return [item.semantic_digest, item];
      })).values());
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
    var publication = state.payload.publication;
    var documentValue = publication.source_document;
    var profile = publication.reader_profile || {};
    var strings = labels();
    var title = profile.title || publication.labels.document_title ||
      sourceTitle(documentValue) || publication.labels.untitled_document ||
      "Untitled document";
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

    var anchorMap = groupedFragments(documentValue);
    (documentValue.blocks || []).forEach(function (block) {
      main.appendChild(renderSourceRow(block, anchorMap.get(block.block_id) || []));
    });
    renderGlossary(main, publication.glossary || [], strings);
    renderBibliography(main, publication.bibliography || [], strings);
    renderDiagnostics(main);
    renderContents(contents, publication.outline || [], strings);
    setupLaneResponsiveness();
    setupContents();
  }

  function sourceTitle(documentValue) {
    var first = (documentValue.blocks || []).find(function (block) {
      return block.kind === "heading" && Number(block.payload.level) === 1;
    });
    return first ? first.payload.text : "";
  }

  function groupedFragments(documentValue) {
    var groups = new Map();
    var sections = new Map(
      (state.payload.publication.outline || []).map(function (section) {
        return [section.section_id, section];
      })
    );
    state.selected.forEach(function (fragment) {
      var target = fragment.anchor && fragment.anchor.target_id;
      if (fragment.anchor && fragment.anchor.kind === "section") {
        var section = sections.get(target);
        target = section ? section.anchor_block_id : null;
      }
      if (!target) {
        state.diagnostics.push(
          "Fragment " + fragment.fragment_id + " has an unknown anchor."
        );
        return;
      }
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
    row.appendChild(noteButton);
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
    return (state.payload.resources || []).find(function (item) {
      return item.artifact_digest === digest || item.digest === digest;
    });
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
    actions.appendChild(element(
      "span", "arc-fragment-meta",
      roleLabel(fragment.role) + " · " + fragment.priority + " · v" + fragment.revision
    ));
    var edit = iconButton(
      "arc-edit-button arc-icon-button", "✎", labels().edit
    );
    edit.addEventListener("click", function () { openEditEditor(fragment); });
    actions.appendChild(edit);
    header.appendChild(actions);
    card.appendChild(header);
    card.appendChild(renderMarkdown(fragment.markdown_body));
    return card;
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
      var link = element("a", "", section.title);
      link.href = "#block-" + safeToken(section.anchor_block_id);
      removeVisibleHtmlTags(link);
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
    state.diagnostics.forEach(function (value) {
      main.insertBefore(element("p", "arc-diagnostic", value), main.firstChild);
    });
  }

  function glossarySurfaces(layer) {
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
    return values;
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

  function typeset(root) {
    if (!window.katex || typeof window.katex.render !== "function") return;
    var scope = root.querySelectorAll ? root : document;
    scope.querySelectorAll(".math[data-tex]").forEach(function (node) {
      if (node.dataset.arcTypeset === "true") return;
      try {
        window.katex.render(node.dataset.tex || "", node, {
          displayMode: node.classList.contains("math-display"),
          throwOnError: false,
          strict: "warn"
        });
        node.dataset.arcTypeset = "true";
      } catch (_error) {
        node.textContent = node.dataset.tex || "";
        node.classList.add("math-error");
      }
    });
  }

  function setupLaneResponsiveness() {
    function update(lanes) {
      var count = Number(lanes.style.getPropertyValue("--arc-lane-count")) || 1;
      var horizontal = window.innerWidth >= 900 &&
        lanes.getBoundingClientRect().width / count >= 275;
      lanes.classList.toggle("lanes-horizontal", horizontal);
    }
    var lanes = Array.prototype.slice.call(document.querySelectorAll(".arc-lanes"));
    lanes.forEach(update);
    if ("ResizeObserver" in window) {
      var observer = new ResizeObserver(function (entries) {
        entries.forEach(function (entry) { update(entry.target); });
      });
      lanes.forEach(function (item) { observer.observe(item); });
    } else {
      window.addEventListener("resize", function () {
        lanes.forEach(update);
      });
    }
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

  async function setupEditor() {
    var strings = labels();
    var connect = document.getElementById("arc-connect");
    connect.textContent = strings.connect;
    connect.addEventListener("click", connectDirectory);
    if (!window.showDirectoryPicker) {
      connect.disabled = true;
      setStatus(strings.noDirectoryApi, "error");
    } else {
      await restoreDirectoryHandle();
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
    document.getElementById("arc-editor-cancel").textContent = strings.close;
    var close = document.getElementById("arc-editor-close");
    close.setAttribute("aria-label", strings.close);
    close.title = strings.close;
    Array.prototype.forEach.call(
      document.getElementById("arc-editor-role").options,
      function (option) { option.textContent = strings[option.value] || option.value; }
    );
    close.onclick = function () { dialog.close(); };
    document.getElementById("arc-editor-cancel").onclick = function () { dialog.close(); };
    document.getElementById("arc-editor-markdown").addEventListener("input", updatePreview);
    document.getElementById("arc-editor-save").addEventListener("click", saveEditor);
  }

  async function connectDirectory() {
    try {
      var handle = await window.showDirectoryPicker({
        id: "arc-render-project",
        mode: "readwrite"
      });
      var permission = await handle.requestPermission({mode: "readwrite"});
      if (permission !== "granted") throw new Error("read/write permission was not granted");
      state.directory = handle;
      await rememberDirectoryHandle(handle);
      setStatus(labels().loading);
      await loadDirectoryRevisions();
      setStatus(labels().connected);
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") return false;
      state.directory = null;
      setStatus(String(error.message || error), "error");
      return false;
    }
  }

  async function loadDirectoryRevisions() {
    if (!state.directory) return;
    resetRevisionState();
    var fragments;
    try {
      fragments = await state.directory.getDirectoryHandle("fragments");
    } catch (error) {
      if (error.name === "NotFoundError") {
        resolveAll();
        renderReader();
        return;
      }
      resolveAll();
      renderReader();
      throw error;
    }
    var files = [];
    await collectMarkdownFiles(fragments, files);
    for (var index = 0; index < files.length; index += 1) {
      try {
        var file = await files[index].getFile();
        var revision = await parseRevisionFile(await file.text(), file.name);
        revision._origin = "directory";
        addRevision(revision);
      } catch (error) {
        state.fileDiagnostics.push(
          "Ignored invalid fragment file " + files[index].name + ": " +
          String(error.message || error)
        );
      }
    }
    resolveAll();
    renderReader();
  }

  async function collectMarkdownFiles(directory, output) {
    for await (var entry of directory.values()) {
      if (entry.kind === "directory") {
        await collectMarkdownFiles(entry, output);
      } else if (entry.kind === "file" && entry.name.endsWith(".md")) {
        output.push(entry);
      }
    }
  }

  function openEditEditor(fragment) {
    state.editorBase = fragment;
    state.editorAnchor = fragment.anchor;
    state.editorHistorical = fragment;
    fillEditor(fragment, labels().editor);
  }

  function openNewEditor(block) {
    openNewEditorForAnchor({
      kind: "block",
      target_id: block.block_id,
      related_blocks: [anchorBlock(block)]
    });
  }

  function openNewEditorForAnchor(anchor) {
    state.editorBase = null;
    state.editorHistorical = null;
    state.editorAnchor = anchor;
    fillEditor({
      title: null,
      role: "note",
      priority: 110,
      markdown_body: "",
      revision: 0
    }, labels().newNote);
  }

  function fillEditor(fragment, heading) {
    var dialog = document.getElementById("arc-editor-dialog");
    document.getElementById("arc-editor-heading").textContent = heading;
    document.getElementById("arc-editor-title").value = fragment.title || "";
    document.getElementById("arc-editor-role").value = fragment.role || "note";
    document.getElementById("arc-editor-priority").value = String(fragment.priority || 110);
    document.getElementById("arc-editor-markdown").value = fragment.markdown_body || "";
    document.getElementById("arc-editor-advanced").open = false;
    renderHistory(fragment.fragment_id);
    updatePreview();
    dialog.showModal();
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
    if (!revision) return;
    document.getElementById("arc-editor-title").value = revision.title || "";
    document.getElementById("arc-editor-role").value = revision.role;
    document.getElementById("arc-editor-priority").value = String(revision.priority);
    document.getElementById("arc-editor-markdown").value = revision.markdown_body;
    updatePreview();
  }

  function updatePreview() {
    var preview = document.getElementById("arc-editor-preview");
    preview.replaceChildren(renderMarkdown(
      document.getElementById("arc-editor-markdown").value
    ));
  }

  async function saveEditor(event) {
    event.preventDefault();
    try {
      if (!state.directory) {
        if (!await connectDirectory()) return;
      } else {
        setStatus(labels().loading);
        await loadDirectoryRevisions();
      }
      if (!state.directory) return;
      var base = state.editorBase;
      if (base) {
        var current = state.selected.get(base.fragment_id);
        var eligibleChildren = (
          state.revisions.get(base.fragment_id) || []
        ).filter(function (revision) {
          return revision.parent_semantic_digest === base.semantic_digest &&
            revision.revision === base.revision + 1 &&
            stableStringify(revision.source) === stableStringify(base.source) &&
            stableStringify(revision.anchor) === stableStringify(base.anchor);
        });
        if (
          !current ||
          current.semantic_digest !== base.semantic_digest ||
          eligibleChildren.length > 1
        ) {
          throw new Error(labels().historyChanged);
        }
      }
      var markdown = normalizeMarkdown(
        document.getElementById("arc-editor-markdown").value
      );
      var metadata = base ? metadataOnly(base) : newNoteMetadata();
      metadata.revision = base ? base.revision + 1 : 1;
      metadata.parent_semantic_digest = base ? base.semantic_digest : null;
      metadata.title = document.getElementById("arc-editor-title").value.trim() || null;
      metadata.role = document.getElementById("arc-editor-role").value;
      metadata.priority = Number(document.getElementById("arc-editor-priority").value);
      if (!Number.isInteger(metadata.priority) || metadata.priority < 1) {
        throw new Error("priority must be a positive integer");
      }
      metadata.citation_ids = citationIds(markdown);
      assertKnownCitations(metadata.citation_ids);
      metadata.provenance = Object.assign({}, metadata.provenance || {}, {
        last_editor: "arc-render-browser",
        edited_at: new Date().toISOString()
      });
      validateRevisionMetadata(metadata);
      var digest = await semanticDigest(metadata, markdown);
      var filename = "revision-" + String(metadata.revision).padStart(6, "0") +
        "-" + digest + ".md";
      var folder = await fragmentDirectory(metadata.fragment_id, true);
      try {
        await folder.getFileHandle(filename);
        throw new Error("revision file already exists; no file was overwritten");
      } catch (error) {
        if (error.name !== "NotFoundError") throw error;
      }
      var handle = await folder.getFileHandle(filename, {create: true});
      var writable = await handle.createWritable();
      var encoded = FRONT_BEGIN + "\n" + stableStringify(metadata) + "\n" +
        FRONT_END + "\n" + markdown;
      await writable.write(encoded);
      await writable.close();
      var revision = Object.assign({}, metadata, {
        markdown_body: markdown,
        semantic_digest: digest,
        _origin: "directory"
      });
      addRevision(revision);
      resolveAll();
      document.getElementById("arc-editor-dialog").close();
      setStatus(labels().saveSuccess);
      renderReader();
    } catch (error) {
      setStatus(String(error.message || error), "error");
    }
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

  function newNoteMetadata() {
    var publication = state.payload.publication;
    var profile = publication.reader_profile || {};
    return {
      schema_version: FRAGMENT_SCHEMA,
      source: state.payload.source_identity,
      fragment_id: "user-" + crypto.randomUUID().toLowerCase(),
      revision: 1,
      parent_semantic_digest: null,
      anchor: state.editorAnchor,
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

  async function fragmentDirectory(fragmentId, create) {
    var fragments = await state.directory.getDirectoryHandle(
      "fragments", {create: Boolean(create)}
    );
    return fragments.getDirectoryHandle(fragmentId, {create: Boolean(create)});
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
    validateSourceIdentity(metadata.source);
    if (
      stableStringify(metadata.source) !==
      stableStringify(state.payload.source_identity)
    ) {
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
    var documentValue = state.payload.publication.source_document;
    var blocks = new Map((documentValue.blocks || []).map(function (block) {
      return [block.block_id, block];
    }));
    var sections = new Set(
      (state.payload.publication.outline || []).map(function (section) {
        return section.section_id;
      })
    );
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
    status.textContent = value || "";
    status.dataset.kind = kind || "info";
    status.hidden = !value;
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
        state.directory = handle;
        await loadDirectoryRevisions();
        setStatus(labels().connected);
      }
    } catch (_error) {
      state.directory = null;
      /* Opaque file origins may not persist IndexedDB; reconnect still works. */
    }
  }

  async function initialize() {
    try {
      state.payload = readPayload();
      setupMarkdown();
      initialRevisions();
      renderReader();
      setupTooltip();
      await setupEditor();
      if (document.fonts && document.fonts.ready) await document.fonts.ready;
      await Promise.all(Array.prototype.slice.call(document.images).map(function (image) {
        if (image.complete) return Promise.resolve();
        if (typeof image.decode === "function") return image.decode().catch(function () {});
        return new Promise(function (resolve) {
          image.addEventListener("load", resolve, {once: true});
          image.addEventListener("error", resolve, {once: true});
        });
      }));
      document.body.dataset.arcRenderReady = "true";
    } catch (error) {
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
