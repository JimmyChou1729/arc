# ARC Paper

`arc-paper` owns deterministic paper acquisition, content-addressed source
storage, parsing, reconciliation, and paper-specific workflows. `arc-jobs`
owns durable run state; `arc-llm` owns model execution.

The CLI always writes one `arc.command_result.v1` JSON document to stdout.
Warnings are part of that document and progress, when present, is written to
stderr. Do not add `--json`.

## Phase 1: Resolve Paper Identity And Metadata

### Step 1: Normalize identifiers

```bash
arc-paper extract-paper-ids "Compare arXiv:0911.3380 and hep-th/0601001."
arc-paper safe-dir-name arXiv:0911.3380 hep-th/0601001
```

### Step 2: Query INSPIRE

```bash
arc-paper get-metadata arXiv:0911.3380
arc-paper get-title arXiv:0911.3380
arc-paper get-abstract arXiv:0911.3380
arc-paper get-authors arXiv:0911.3380
arc-paper get-references arXiv:0911.3380 --enrich
arc-paper get-citers arXiv:0911.3380 --limit 1000 --sort mostrecent
arc-paper get-citer-count arXiv:0911.3380
arc-paper search-metadata "quasi-single-field inflation" --limit 20
```

Use `--refresh` only when the user explicitly needs a fresh remote response.

## Phase 2: Acquire Or Import A Source

### Step 1: Choose the primary

`fetch-arxiv-auto` fetches ar5iv HTML only. It never downloads a PDF.

```bash
arc-paper fetch-arxiv-auto arXiv:0911.3380
arc-paper fetch-arxiv-pdf arXiv:0911.3380
```

Import local HTML, Markdown, single-file TeX, or PDF through the same
content-addressed repository:

```bash
arc-paper import-source note.md
arc-paper import-source paper.tex --format tex
```

Local TeX must already be flattened; ARC does not expand `\input` or
`\include`. arXiv TeX archives, TeX projects, and arbitrary URL providers are
unsupported.

### Step 2: Query an arXiv document by ID

For a table of contents, one section, full-text hits, or equation hits from an
arXiv paper, use the cache-first document commands. They accept a bare ID, an
`arXiv:` ID, a versioned ID, or an arXiv `abs`/`pdf` URL; ARC normalizes all of
them to an unversioned canonical arXiv ID.

```bash
arc-paper get-arxiv-table-of-contents 0911.3380
arc-paper get-arxiv-section arXiv:0911.3380 "Introduction"
arc-paper search-arxiv-full-text https://arxiv.org/abs/0911.3380v2 "Hamiltonian constraint" --limit 20 --context-lines 1
arc-paper search-arxiv-equations https://arxiv.org/pdf/0911.3380.pdf "H^2" --limit 20
```

The corresponding registry operation IDs are:

```text
arc-paper.get-arxiv-table-of-contents.v1
arc-paper.get-arxiv-section.v1
arc-paper.search-arxiv-full-text.v1
arc-paper.search-arxiv-equations.v1
```

These commands own acquisition and parsing: callers provide an arXiv ID and
query intent, never a source path, `SourceArtifact`, or cache root. Results
include canonical arXiv provenance plus source and document digests; section
results include title, body, ordinal, and page range. `--refresh` refreshes the
ar5iv source mapping, but identical source bytes reuse the parsed document.

On a cache miss, ARC fetches ar5iv HTML into the global paper cache, then keeps
the derived parsed document under a content-identity and parser-contract key.
Both source and parsed entries are integrity checked. A corrupt derived parsed
entry is rebuilt from verified source and reported as a warning; corrupt source
data, missing ar5iv HTML, and parse failures are typed failures. These document
commands never fall back to PDF.

Use `fetch-arxiv-pdf`, `import-source`, `parse-local`, and `SourceRepository`
only for explicit local imports, HTML/PDF comparison, validation, visual review,
or other advanced source handling.

### Step 3: Parse and reconcile local or explicitly selected sources

```bash
arc-paper parse-local note.md
arc-paper parse-local note.tex --validator paper.pdf
arc-paper parse-local note.md --validator paper.pdf
arc-paper parse-local paper.pdf
```

A source bundle has exactly one authoritative primary. Validators are
independent evidence: conflicts are recorded and never overwrite primary text
or TeX.

Markdown+PDF defaults to full-page visual review inside a parent ARC run.
Standalone parsing has no parent run or model task service, so requested visual
pages are reported as `unreviewed` with warnings. The public in-run entry point
installs the renderer, reviewer, and `RunContext` through `arc-jobs`:

```python
from arc_jobs import RunRepository
from arc_llm import ModelSelection
from arc_paper import MarkdownPDFVisualParseRunner, SourceRepository

sources = SourceRepository(cache_root)
primary = sources.import_path("note.md")
validator = sources.import_path("paper.pdf")
runner = MarkdownPDFVisualParseRunner(RunRepository(jobs_root), sources)
snapshot = runner.execute(
    "paper-visual-review",
    primary,
    validator,
    model=ModelSelection(provider="auto"),
)
```

Each page result, including an `unreviewed` result caused by provider failure,
pause, or invalid structured output, is published as an immutable run artifact.
Re-executing an interrupted handler reuses that terminal page result without
another provider call. Use
`--policy deterministic_only` or `--policy none` when that is intentional.
TeX+PDF and ar5iv default to deterministic reconciliation.

### Step 4: Search an already parsed document in Python

Full-text and equation search are pure typed Python operations over one or more
`ParsedDocument` values:

```python
from arc_paper import (
    search_equations,
    search_full_text,
    select_section,
    table_of_contents,
)

toc = table_of_contents(parsed_document)
section = select_section(parsed_document, "Introduction")
text_hits = search_full_text(parsed_documents, "Hamiltonian constraint")
math_hits = search_equations(parsed_documents, r"\partial_\mu")
```

They do not accept paths, scan caches, fetch providers, or return the removed
legacy result envelope. Full-text results identify a section (or a page for a
sectionless document); equation results cover both inline and display
`MathSpan` values and carry their before/after context. `table_of_contents()`
is a typed projection of `ParsedDocument.sections`; `select_section()` selects
by ordinal, exact ID/title, or a unique title fragment.

## Phase 3: Durable Summary And Reference Workflows

Paper summaries and free-text reference inference are typed Python workflows,
not private worker commands:

```text
arc_paper.PaperSummaryService
arc_paper.SummaryBatchRunner
arc_paper.ReferenceInferenceService
arc_paper.ReferenceInferenceRunner
```

They execute child model tasks through
`LLMTaskService.execute_or_resume()` in the same parent `RunContext`. Summary
batch item state is the `arc-jobs` group unit result; failed-item retry creates
a new run.

Use `arc-jobs status`, `arc-jobs cancel`, and `arc-jobs validate` for generic
run control. The equivalent commands accepted by `arc-paper` delegate directly
to `arc-jobs`; `arc-paper` does not implement another status database.

## Resolver Policy

`arc_paper.registry_document()` exposes the default registry projection.
It excludes cache administration, destructive operations, arbitrary local-path
operations, and recursive LLM operations. Trusted local CLI commands may still
import and parse explicit local paths.

## Cache

Set `ARC_PAPER_CACHE` to override the paper cache root. Source objects and
remote request entries are integrity checked and published atomically. The
cache-first arXiv document commands also persist a separately verified parsed
document projection keyed by source content identity and
`arc.paper.parser.v1`; a parser-contract change naturally uses a new entry.
There is no cache list, delete, scan, or other administration command.
