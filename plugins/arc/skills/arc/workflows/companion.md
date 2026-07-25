# Companion Workflow

Use this workflow for an explicit request to build or refresh a textbook-style
reading companion. Read `manuals/arc-companion.md` before the first command.

## Phase 1: Prepare

### Step 1: Choose the project directory

Use a new directory for a new Companion build. Inside a Git worktree, keep
non-development output under the ignored `local/` tree and verify it before
running:

```bash
git check-ignore -q --no-index <project-dir>
```

Do not point the new CLI at a legacy Companion project. Old static outputs stay
readable, but state is not migrated.

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
  --workers <workers> \
  --json
```

Add `--pdf <path>` for a local validator or `--pdf fetch` for a remote paper.
Use `--refresh` only when fresh remote source bytes were requested.

Companion detects source language once. It skips translation only for a known
matching primary language; mixed or unknown source is translated. Chapters are
derived from headings and cover every source block exactly once. Textbook
notes are selective rather than one expansion per paragraph. When translation
is enabled, `arc-translate` builds the bilingual glossary after chapter
planning and evidence. The glossary is a barrier; translation and guide
generation then run in parallel for each chapter using only locally occurring
glossary entries.

### Step 2: Resolve a pause

Inspect the returned `resume` descriptor and request artifact. Resolve paper
evidence with `arc-paper`; resolve other Web or user evidence with the
appropriate host tool. Freeze the response and resume with the same opaque
resume key:

```bash
arc-companion resume --project-dir <project-dir> \
  --input '<resume-input-json>' --json
```

For an unsafe reviewer patch, the supervision request permits discarding that
review while retaining the locally validated draft. There is no review
arbitration or conflict graph.

## Phase 3: Deliver

### Step 1: Inspect and validate

```bash
arc-companion status --project-dir <project-dir> --json
arc-companion validate --project-dir <project-dir> --json
```

The current release changes only after both PDF and Web validate. Technical
diagnostics remain in command results and run events, never in reader content.

### Step 2: Render without model calls

Use render after a style, font, renderer, or validator change:

```bash
arc-companion render --project-dir <project-dir> \
  --format all --json
```

`--format pdf` and `--format web` limit returned delivery artifacts; publication
still validates one complete immutable release from the same `AcceptedBook`.
