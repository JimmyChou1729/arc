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

If the user explicitly supplies an author, pass one `--author <name>` per
author. Otherwise leave author resolution to Companion: source metadata and
bylines are only candidates, and a model identity check publishes them only at
high confidence. Do not guess or force an uncertain author. Reader-facing
framework labels must use the target language; for an unsupported target
language, supply one complete label map with `--reader-labels <json>`.

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
It also copies the prior accepted Companion and supplies its guide content to
the new literature, planning, drafting, and review calls as optional context.
The model may preserve, deepen, recombine, or discard prior ideas. Do not treat
the old form as a template or its bibliography as current selected evidence.

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
Companion runs this research as a standard `arc-llm` task. Under
`unrestricted`, the model agent directly uses its available search, Web, and
paper tools; do not create or supply a Companion evidence response. Under
`restricted` or `unknown`, any required host action uses the ordinary
`arc-llm` host-turn/broker contract. This is an inspection requirement, not an
inclusion quota. Freeze the research log, select only evidence that directly
adds reader value, and allow only selected evidence into chapter planning and
the bibliography. English Wikipedia is an optional ordinary candidate; reject
other language editions. Evidence prose and translated excerpts use the target
language while English page titles and URLs retain source identity.

Treat paragraph-local notes and chapter-level or cross-paragraph notes as
equally legitimate. Choose placement from the explanatory need, with no quota
or default preference. Keep, replace, or remove proposed units based on whether
they add motivation, a genuinely different presentation, deeper implications,
omitted reasoning, substantive connections, reliable context, or materially
useful later developments. Paraphrase, same-meaning rewriting, repeated source
reasoning, and generic summary are not Companion value; remove such units
instead of forcing one expansion per paragraph or chapter.

Every chapter plan audits every source block in order for reader understanding
needs. An explicit user audience wins. Otherwise, popular, directional, or
weakly specialized writing assumes an adult with average general literacy and
no specialist background; a research paper assumes a professional student who
has completed the relevant foundational courses; a textbook assumes a student
who completed its standard prerequisites without presuming that difficult
prerequisite concepts are already firmly mastered. A required need must retain
at least one anchored learning unit after review. There is no minimum number of
units, and zero is valid only when every block is genuinely simple and
self-contained for the resolved reader.

Prefer direct affirmative explanation. Use corrective contrasts only when the
source, user intent, or selected evidence establishes the misconception; never
invent a prior reader belief to create an explanatory effect. Review replaces
unsupported corrective framing and cannot remove the last unit serving a
required reader need.

The guide lane writes free-form CommonMark learning units rather than selecting
from a fixed menu of note types. Headings, short questions, worked reasoning,
comparisons, historical context, counterpoints, lists, equations, and other
forms are available when useful; none is a required repeated template. ARC
checks source anchoring, citation identity, coverage, and renderability while
leaving explanatory form and chapter composition to the model.

When translation is enabled, `arc-translate` builds the bilingual glossary
after chapter planning and evidence. The glossary is a barrier; translation
and guide generation then run in parallel for each chapter using only locally
occurring glossary entries.

### Step 2: Resolve a pause

Inspect the returned `resume` descriptor and request artifact. A model-requested
host action is an `arc-llm` host turn and follows `manuals/arc-llm.md`; Companion
does not define a separate evidence-input or evidence-resume contract. Resume
with the same opaque resume key when input is required:

```bash
arc-companion resume --project-dir <project-dir> \
  --input '<resume-input-json>' \
  --host-authority <host-authority>
```

For an unsafe reviewer patch, the supervision request permits discarding that
review while retaining the locally validated draft. There is no review
arbitration or conflict graph.

For a failed build, inspect
`<project-dir>/.arc/companion/jobs/<run-id>/working/`. Candidate files are
written before semantic validation and errors report their exact paths. Edit a
candidate to adopt the repaired content on resume, or delete it to regenerate
that step. Keep visual QA under
`.arc/companion/diagnostics/visual/<run-id>/`; do not create attempt-named
project roots or loose project-level QA directories.

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
