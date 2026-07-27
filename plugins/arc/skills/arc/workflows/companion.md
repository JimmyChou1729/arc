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
the new planning, drafting, and review calls as optional context.
The model may preserve, deepen, recombine, or discard prior ideas. Do not treat
the old form as a template or its bibliography as current evidence.

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

Model tasks receive a small cache-identity manifest, while each chapter task
receives that chapter's block locators in its task payload. With an exact
`arc-paper` cached document, agents should prefer cache-only reads of the
precise line ranges or search results they need. They may read a complete
current chapter when narrower excerpts are insufficient; discourage
whole-book reads when chapter-scoped access is enough, but do not prohibit
them in program logic. Companion does not duplicate the whole source into
every agent workspace. A verified text-only whole-source input exists only
when cache access could not be established. Do not open source image or media
assets for reading; deterministic assembly restores them later. Models author
semantic JSON, while Companion injects routing identities such as chapter IDs.

Do not run a document-wide literature survey before planning. Planning first
audits reader needs and supplies seed units; references are discovered on
demand while each chapter is proposed and reviewed. Both roles may inspect
the source, search cached references, suggest new references, and use currently
visible and authorized host research or download tools. Prefer an existing
`arc-paper` cache entry. When a DOI, arXiv record, ordinary URL, local file,
book, or other acquired resource is new, admit it to `arc-paper` so later roles
and projects can reuse it; use a child-workspace file only when cache admission
is unavailable.

Do not impose a reference target, minimum, or maximum, and do not criticize a
proposal merely for having few references. Keep only sources that improve a
specific anchored explanation. If the host exposes no suitable acquisition
tool or permission, continue with the resources that are available rather than
requiring installation, connection, or additional authority. Under
`restricted` or `unknown`, any model-requested host action uses the ordinary
`arc-llm` host-turn/broker contract; Companion has no separate evidence input
or resume contract.

Reference bodies remain in the cache or text-only workspace and are read
through tools; model requests and responses carry stable handles and semantic
reference metadata, not copies of whole works. English Wikipedia is an
optional ordinary source; reject other language editions. Reference prose and
translated excerpts use the target language while English page titles and URLs
retain source identity.

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
at least one anchored learning unit after review. A proposer may also add a
newly discovered anchored unit that was absent from the seed plan. There is no
minimum number of units, and zero is valid only when every block is genuinely
simple and self-contained for the resolved reader.

Prefer direct affirmative explanation. Use corrective contrasts only when the
source, user intent, or a cited reference establishes the misconception; never
invent a prior reader belief to create an explanatory effect. Review replaces
unsupported corrective framing and cannot remove the last unit serving a
required reader need.

The guide lane writes free-form CommonMark learning units rather than selecting
from a fixed menu of note types. Headings, short questions, worked reasoning,
comparisons, historical context, counterpoints, lists, equations, and other
forms are available when useful; none is a required repeated template. ARC
checks source anchoring, citation identity, coverage, and renderability while
leaving explanatory form and chapter composition to the model.

When translation is enabled, `arc-translate` builds the bilingual glossary and
completes translation before reviewed guide generation. Deterministic matching
supplies each chapter guide only the glossary entries that occur in that
chapter; translation windows likewise receive only entries matched to their
source window.

Guide improvement is owned by `arc-proposer-reviewer`, and every chapter enters
it even when the initial proposal is empty. The reviewer must not criticize
merely to fill a round: it stops immediately when the proposal satisfies the
reader needs and has no concrete improvement path. Otherwise it gives
constructive feedback, including good new anchored, well-supported ideas and
useful references, for up to two complete revisions. Both proposer and reviewer
may research during their own turns. The maximum sequence is
proposer-reviewer-proposer-reviewer-proposer; the last proposal is validated
directly, without an unused final review. Companion injects `chapter_id` into
the final candidate as caller-owned routing data.

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

For a failed build, inspect
`<project-dir>/.arc/companion/jobs/<run-id>/working/`. Candidate files are
written before semantic validation and errors report their exact paths. Edit a
candidate to adopt repaired content on a supported recovery path. A final guide
candidate is backed by the durable proposer-reviewer transcript; deleting it
re-materializes that frozen proposal rather than creating an extra model round.
Keep visual QA under
`.arc/companion/diagnostics/visual/<run-id>/`; do not create attempt-named
project roots or loose project-level QA directories.

## Phase 3: Deliver

### Step 1: Inspect and validate

```bash
arc-companion status --project-dir <project-dir>
arc-companion validate --project-dir <project-dir>
```

The current release changes only after its Web reader validates. Companion
also renders and validates PDF when the local toolchain permits it. A PDF
render or validation failure does not change the successful AcceptedBook or
build status: publication completes with the valid Web reader and a
`pdf_render_failed` command warning. The immutable release manifest and command
data list `available_formats`; CLI artifacts and delivery paths never advertise
a format that was not published.

Deliver `<project-dir>/companion.html` from every published release. It is a
managed root projection whose base points to
`releases/<release-id>/reader/index.html`, so canonical reader assets and links
remain valid. Deliver `<project-dir>/companion.pdf` only when `pdf` appears in
`available_formats`; it is then an exact physical copy of the canonical release
PDF. Publishing a Web-only release removes a stale managed root PDF rather than
leaving it associated with the new current release. A later successful render
publishes a new complete immutable release and restores the root PDF without
rewriting the earlier Web-only release.

If Web rendering itself fails after the AcceptedBook succeeds, the build
remains successful and the command reports `web_render_failed` without
publishing or advertising a new release. Repair the renderer or missing Web
asset and rerun `render`. Technical diagnostics remain in command results and
run events, never in reader content. Successful build and resume commands
attempt formal publication automatically.

### Step 2: Render without model calls

Use render after a style, font, renderer, or validator change:

```bash
arc-companion render --project-dir <project-dir> \
  --format all
```

`--format pdf` and `--format web` limit returned delivery artifacts, not the
formats ARC attempts. If PDF still fails, the command completes with the Web
release and a warning. `--format web` or `--format all` reports the Web
artifact; `--format pdf` returns no nonexistent delivery artifact. Repeating
the command after the PDF toolchain is repaired publishes the complete release
from the same `AcceptedBook` without model calls. This is the manual model-free
republication path, not an additional generation stage.
