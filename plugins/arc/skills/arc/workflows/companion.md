# Companion Workflow

Use this workflow for an explicit request to build or refresh a textbook-style
reading companion. Read `manuals/arc-companion.md` before the first command.

## Phase 1: Prepare

### Step 1: Choose the project directory

Use the directory explicitly supplied by the user as `<project-dir>` itself.
Do not append another `companion`, `build-v2`, `fresh`, or attempt-specific
directory. If choosing the location inside this checkout, use one stable,
descriptive directory under the ignored `local/` tree and verify it before
running:

```bash
git check-ignore -q --no-index <project-dir>
```

The `local/` rule applies only inside this checkout; respect an external
project directory supplied by the user. A new Companion project root may
already contain source material, notes, or other unrelated user files.
Companion leaves them untouched and claims only `.arc/companion/`,
`releases/`, `companion.pdf`, and `companion.html`. Before the first build,
choose another root or resolve the conflict if any of those managed paths
already exists. An existing recognized Companion project may contain
additional unrelated files.

Ensure the installed environment contains the complete Companion runtime,
including the public `arc-translate` facade. Build checks this before creating
the project and returns `runtime_dependency_missing` for an incomplete
installation.

### Step 2: Resolve source and intent

Choose a rich Markdown, HTML, or flattened single-file TeX source, or a paper
identifier that `arc-paper` can resolve to rich source. A PDF is optional and
is used only for validation and page mapping. Preserve the user's exact
`user_intent`; when absent, Companion uses its neutral textbook intent.

## Phase 2: Build

### Step 1: Start the durable build

```bash
arc-companion build <source-path-or-paper-id> \
  --project-dir <project-dir> \
  --target-language <language-tag> \
  --approx-term-count <estimate> \
  --user-intent '<intent>' \
  --provider <provider> \
  --host-authority <host-authority> \
  --workers <workers>
```

To preserve an already accepted translation and glossary while rebuilding the
guide under a new intent or prompt contract, add:

```bash
  --reuse-translation-from <existing-project-dir>
```

Reuse is exact and target-owned: Companion verifies the successful source
book, language result, glossary, chapter coverage, and translated bytes, then
copies those bytes into the new project. The new run may regenerate guide
content without calling a translation provider.

Choose `<host-authority>` once: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value when resuming this run. Under `restricted` or `unknown`, follow
`manuals/arc-llm.md`: without an explicitly supplied broker, a model host
request becomes a durable manual pause.

Add `--pdf <path>` for a local validator or `--pdf fetch` for a remote paper.
Use `--refresh` only when fresh remote source bytes were requested.
Command JSON may be redirected to an unrelated path inside `<project-dir>`;
Companion does not treat unrelated files as project-state conflicts. Do not
redirect to a managed Companion path.

Companion detects source language once. It skips translation only for a known
matching primary language; mixed or unknown source is translated. Chapters are
derived from headings and cover every source block exactly once.

Guide planning is evidence-first. Before chapter planning, inspect at least 20
distinct candidate works or substantive discussions across sources named by
the document, important prior history, and later work central to its debates.
This is an inspection requirement, not an inclusion quota. Freeze the research
log, select only evidence that directly adds reader value, and allow only
selected evidence into chapter planning and the bibliography. Do not retain a
source merely to make the survey look broad.

Treat paragraph-local notes and chapter-level or cross-paragraph notes as
equally legitimate. Choose placement from the explanatory need, with no quota
or default preference. Keep, replace, or remove proposed units based on whether
they add motivation, a genuinely different presentation, deeper implications,
omitted reasoning, substantive connections, reliable context, or materially
useful later developments. Paraphrase, same-meaning rewriting, repeated source
reasoning, and generic summary are not Companion value; remove such units
instead of forcing one expansion per paragraph or chapter.

When translation is enabled, `arc-translate` builds the bilingual glossary
after chapter planning and evidence. The glossary is a barrier; translation
and guide generation then run in parallel for each chapter using only locally
occurring glossary entries.

### Step 2: Resolve a pause

Inspect the returned `resume` descriptor and request artifact. Resolve paper
evidence with `arc-paper`; resolve other Web or user evidence with the
appropriate host tool. Freeze the response and resume with the same opaque
resume key:

```bash
arc-companion resume --project-dir <project-dir> \
  --input '<resume-input-json>' \
  --host-authority <host-authority>
```

For an unsafe reviewer patch, the supervision request permits discarding that
review while retaining the locally validated draft. There is no review
arbitration or conflict graph.

## Phase 3: Deliver

### Step 1: Inspect and validate

```bash
arc-companion status --project-dir <project-dir>
arc-companion validate --project-dir <project-dir>
```

The current release changes only after both PDF and Web validate. Technical
diagnostics remain in command results and run events, never in reader content.
Successful build and resume commands perform this formal publication
automatically.

Deliver `<project-dir>/companion.pdf` and
`<project-dir>/companion.html` to the user. They are managed root projections
of the active immutable release. The PDF is an exact physical copy; the HTML
uses a base pointing to
`releases/<release-id>/reader/index.html` so canonical reader assets and links
remain valid. CLI artifacts and the release manifest continue to identify the
canonical files under `releases/`.

### Step 2: Render without model calls

Use render after a style, font, renderer, or validator change:

```bash
arc-companion render --project-dir <project-dir> \
  --format all
```

`--format pdf` and `--format web` limit returned delivery artifacts; publication
still validates one complete immutable release from the same `AcceptedBook`.
This is the manual model-free republication path, not an additional generation
stage.
