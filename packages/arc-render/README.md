# arc-render

`arc-render` owns ARC's immutable Markdown overlay contracts and standalone
reader delivery. A publication contains its frozen rich source document and
refers to immutable fragment revision and resource files, so it does not depend
on an unrecorded caller-supplied source.

The v1 fragment format uses strict JSON front matter between ARC-specific
delimiters. YAML is not accepted. Fragment priorities are positive integers;
block and section are the only anchor kinds. A publication with no overlay
layers is valid.

The initial public API includes:

- frozen source, anchor, fragment revision, layer, and publication value
  objects;
- exact JSON document codecs;
- canonical fragment semantic digests and JSON-front-matter Markdown codecs;
- revision resolution with diagnostics for malformed files, dangling
  revisions, and forks.

Standalone HTML and command-line delivery are built on these contracts. For
now, a PDF copy may be made manually with Chrome's Print / Save as PDF command.
Such a PDF is a user-side derivative, not an ARC release artifact, and ARC does
not validate, reproduce, or automatically publish it.
