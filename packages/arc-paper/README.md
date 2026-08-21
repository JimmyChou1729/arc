# arc-paper

`arc-paper` owns deterministic paper identifiers, arXiv/INSPIRE acquisition,
metadata, references, citers, paper-cache administration, and paper-specific
LLM workflows. Provider-neutral source storage, HTML/Markdown/TeX/PDF parsing,
rich-document contracts, document structure, full-text and equation search,
and approximate keyword inventory come from `ac-document` and remain exposed
through the compatible `arc-paper` API. Generic model execution and durable-run
mechanics remain in `ac-llm` and `ac-jobs`.

## Running the CLI

An installed package provides the `arc-paper` console script. The ARC Skill
runtime is the portable fallback; inside an ARC source checkout, the package
virtual environment is a direct development fallback:

```bash
arc-paper --help
<skill-dir>/scripts/arc-runtime arc-paper --help
packages/arc-paper/.venv/bin/arc-paper --help
```

Use the first available launcher consistently in later commands.

## Quick start

Fetch normalized metadata for one paper:

```bash
arc-paper get-metadata "<paper-id>"
```

Replace the quoted placeholder with an arXiv, INSPIRE, or DOI identifier.
Use `arc-paper --help` and `arc-paper get-metadata --help` for the current
paper, source, search, workflow, and cache commands.

Provider-neutral export belongs to `ac-document`; use its output for direct
`alc-render` composition:

```bash
ac-document export-rich-document ./note.md \
  --output-dir ./render-input \
  --cache-root ./.ac/cache/ac-document

alc-render compose \
  --source ./render-input/rich-source.json \
  --metadata ./render-input/metadata.json \
  --output ./render-input/publication.json
alc-render render \
  --publication ./render-input/publication.json \
  --html ./render-input/reader.html
```

`export-rich-document` accepts Markdown, HTML, or flattened TeX and an optional
PDF `--validator`. It refuses a nonempty output directory, copies verified
assets below `resources/`, and returns the source and metadata paths at
`data.source` and `data.metadata`.

## Cache portability

Cache entries are logical folders with stable IDs returned by `cache list`.
Export any exact selection, or the entire cache, to a verified tar.gz archive:

```bash
arc-paper cache list
arc-paper cache export <entry-id> [<entry-id> ...] --output selected.tar.gz
arc-paper cache export --all --output complete-cache.tar.gz
```

An export never overwrites an existing archive. Import validates and stages
the complete archive before changing the destination:

```bash
arc-paper cache import selected.tar.gz
arc-paper cache import selected.tar.gz --replace-conflicts
```

Identical files are reused and destination-only files are preserved. A
differing destination file rejects the whole import by default, with all
conflict paths reported and no writes performed. `--replace-conflicts`
explicitly replaces only those differing files after the same preflight.

Cached Markdown can retain its exact source identity while adopting hierarchy
from an independently cached PDF outline:

```bash
arc-paper reconstruct-cached-structure \
  --document-ref '<Markdown CachedDocumentRef JSON>' \
  --outline-document-ref '<PDF CachedDocumentRef JSON>'
arc-paper get-section \
  --document-ref '<Markdown CachedDocumentRef JSON>' \
  --structure-ref '<CachedDocumentStructureRef JSON>' \
  '<section id or exact title>'
arc-paper extract-keywords ./book.md \
  --project-dir ./project \
  --structure-ref '<CachedDocumentStructureRef JSON>' \
  --section-id '<content section id>' \
  --section-id '<another content section id>' \
  --host-authority unrestricted
```

With `--structure-ref`, keyword extraction groups text by the reconstructed
content hierarchy instead of unreliable native heading levels. Each model
request receives only its selected section text; the complete parsed document
and its assets are not attached.

References use exact identities and verified cache handles:

```bash
arc-paper lookup-reference --doi '10.1234/example'
arc-paper acquire-reference --url 'https://example.org/paper'
arc-paper admit-reference ./paper.pdf --doi '10.1234/example'
arc-paper materialize-reference \
  --resource-ref '<CachedResourceRef JSON>' \
  --output ./agent-workspace/paper.pdf
```

`arc-paper extract-keywords` accepts `--host-authority` for its LLM work. Use
`unrestricted` only when the host explicitly reports unrestricted authority;
otherwise use `unknown`. Reuse the identical value if the durable keyword run
is resumed. Authority is runtime-only rather than part of the keyword
inventory identity.

LLM workflow schemas bind request-local identities such as the requested
summary section and explicit-term source IDs. Cross-field source-coverage
errors that cannot be expressed in JSON Schema receive one deterministic fresh
retry with bounded validation feedback. If an explicit-term review remains
machine-invalid, that non-essential explicit field is discarded with a durable
warning and chapter extraction continues. A valid scientific or document
quality result such as `status="unusable"` is not a machine-output failure and
does not trigger this retry; its existing supervision path is preserved.

## Python API

The deterministic service exposes the same operation:

```python
from arc_paper import ArcPaperService

paper_id = input("Paper ID: ")
metadata: dict[str, object] = ArcPaperService().get_metadata(paper_id)
print(metadata.get("title", ""))
```

Repository-backed parsing and LLM-backed workflows are also available through
their public package facades.

### Reference material cache

Reference acquisition is cache-first and independent of any agent host:

```python
from arc_paper import ReferenceAcquisitionService, ReferenceIdentity

references = ReferenceAcquisitionService()
cached = references.lookup_cached_reference(doi="10.1000/example")
if cached is None:
    cached = references.acquire_reference("doi:10.1000/example")

# Files already obtained through an available, authorized channel can be
# admitted without coupling that channel to arc-paper.
local = references.admit_reference_file(
    "paper.epub",
    ReferenceIdentity(dois=("10.1000/example",)),
)
readable = references.cache.read_resource(local.readable_resource)
```

`ReferenceIdentity` retains every DOI while its `doi` property projects the
first DOI for compatibility. Exact cache-only lookup accepts a DOI, arXiv ID,
HTTP(S) URL, or normalized title. Titles are never fuzzy-matched. Arbitrary
media are stored through verified `CachedResourceRef` handles; EPUB originals
remain intact and receive a deterministic readable HTML derivative assembled
in publication spine order.

The built-in network paths cover official arXiv representations, DOI metadata
from INSPIRE and Crossref, Crossref full-text or landing links, and one ordinary
HTTP(S) resource. `ReferenceAcquisitionBackend` is the small extension contract
for caller-owned authorized backends. Package code does not inspect plugins,
agent hosts, or workflow files.

## Tests

The unit suite is offline; network integration is opt-in:

```bash
python -m pytest packages/arc-paper/tests
```
