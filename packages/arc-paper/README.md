# arc-paper

`arc-paper` owns deterministic paper identifiers, metadata, references,
citers, source acquisition, content-addressed caches, HTML/Markdown/TeX/PDF
parsing, full-text and equation search, approximate keyword inventory, and
paper-specific LLM workflows. Generic model execution and durable-run
mechanics remain in `arc-llm` and `arc-jobs`.

## Quick start

Fetch normalized metadata for one paper:

```bash
arc-paper get-metadata "<paper-id>"
```

Replace the quoted placeholder with an arXiv, INSPIRE, or DOI identifier.
Use `arc-paper --help` and `arc-paper get-metadata --help` for the current
paper, source, search, workflow, and cache commands.

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
