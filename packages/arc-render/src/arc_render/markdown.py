"""JSON-front-matter Markdown codec for immutable fragment revisions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from arc_document import RichBlock, RichBlockKind

from ._json import canonical_json_bytes
from .contracts import (
    FragmentRevision,
    FragmentRevisionRef,
    fragment_revision_from_document,
    fragment_revision_to_document,
)


FRONT_MATTER_BEGIN = "<!-- ARC:FRAGMENT-JSON:BEGIN -->"
FRONT_MATTER_END = "<!-- ARC:FRAGMENT-JSON:END -->"
_FILENAME_RE = re.compile(
    r"revision-(?P<revision>[0-9]{6,})-(?P<digest>[0-9a-f]{64})[.]md"
)
_CITATION_RE = re.compile(r"\[@([A-Za-z0-9][A-Za-z0-9._:-]*)\]")


def normalize_markdown(markdown: str) -> str:
    """Normalize Unicode and line endings without trimming authored content."""

    if not isinstance(markdown, str):
        raise ValueError("Markdown body must be a string")
    if "\x00" in markdown:
        raise ValueError("Markdown body cannot contain NUL")
    return unicodedata.normalize(
        "NFC", markdown.replace("\r\n", "\n").replace("\r", "\n")
    )


def extract_markdown_citation_ids(markdown: str) -> tuple[str, ...]:
    """Return ordered, unique ARC citation IDs declared in Markdown.

    ARC citations use the literal ``[@citation-id]`` form.  This deliberately
    extracts syntax rather than interpreting Markdown so producers, reviewers,
    and render validation share one stable citation contract without adding a
    Markdown parser dependency.
    """

    normalized = normalize_markdown(markdown)
    return tuple(dict.fromkeys(_CITATION_RE.findall(normalized)))


def block_text_to_markdown(block: RichBlock, text: str) -> str:
    """Encode accepted block text as canonical overlay Markdown.

    Models provide semantic text; the producer, not the model, supplies the
    deterministic Markdown structure inherited from the source block.
    """

    if not isinstance(block, RichBlock):
        raise ValueError("block must be a RichBlock")
    value = normalize_markdown(text)
    if not value.strip():
        raise ValueError("block text must be non-empty")
    if block.kind is RichBlockKind.HEADING:
        level = max(1, min(6, int(block.payload["level"])))
        lines = value.strip().splitlines()
        heading = f"{'#' * level} {lines[0].strip()}"
        remainder = "\n".join(lines[1:]).strip()
        return heading + (f"\n\n{remainder}" if remainder else "") + "\n"
    if block.kind is RichBlockKind.LIST:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        items = tuple(block.payload["items"])
        if len(lines) == len(items) and not any(
            re.match(r"(?:[-+*]|\d+[.)])\s+", line)
            for line in lines
        ):
            ordered = bool(block.payload["ordered"])
            lines = [
                (f"{index}. " if ordered else "- ") + line
                for index, line in enumerate(lines, 1)
            ]
        return "\n".join(lines) + "\n"
    if block.kind is RichBlockKind.CODE:
        longest = max(
            (len(match.group(0)) for match in re.finditer(r"`+", value)),
            default=0,
        )
        fence = "`" * max(3, longest + 1)
        body = value if value.endswith("\n") else value + "\n"
        return f"{fence}\n{body}{fence}\n"
    if block.kind is RichBlockKind.EQUATION:
        stripped = value.strip()
        if (
            stripped.startswith("$$")
            and stripped.endswith("$$")
            and len(stripped) > 4
        ) or (
            stripped.startswith(r"\[")
            and stripped.endswith(r"\]")
            and len(stripped) > 4
        ):
            return stripped + "\n"
        return f"$$\n{stripped}\n$$\n"
    return value.rstrip("\n") + "\n"


def fragment_semantic_digest(revision: FragmentRevision) -> str:
    material = {
        "metadata": fragment_revision_to_document(revision),
        "markdown_body": normalize_markdown(revision.markdown_body),
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def fragment_revision_filename(revision: FragmentRevision) -> str:
    return (
        f"revision-{revision.revision:06d}-"
        f"{revision.semantic_digest}.md"
    )


def parse_fragment_revision_filename(filename: str) -> tuple[int, str]:
    match = _FILENAME_RE.fullmatch(filename)
    if match is None:
        raise ValueError("invalid fragment revision filename")
    revision = int(match.group("revision"))
    if revision < 1:
        raise ValueError("fragment revision filename must be positive")
    return revision, match.group("digest")


def encode_fragment_revision(revision: FragmentRevision) -> str:
    metadata = canonical_json_bytes(
        fragment_revision_to_document(revision)
    ).decode("utf-8")
    return (
        f"{FRONT_MATTER_BEGIN}\n"
        f"{metadata}\n"
        f"{FRONT_MATTER_END}\n"
        f"{revision.markdown_body}"
    )


def decode_fragment_revision(
    value: str, *, filename: str | None = None
) -> FragmentRevision:
    if not isinstance(value, str):
        raise ValueError("fragment revision must be text")
    prefix = f"{FRONT_MATTER_BEGIN}\n"
    separator = f"\n{FRONT_MATTER_END}\n"
    if not value.startswith(prefix):
        raise ValueError("fragment revision is missing JSON front matter")
    payload = value[len(prefix) :]
    metadata_text, marker, markdown_body = payload.partition(separator)
    if not marker:
        raise ValueError("fragment revision has unterminated JSON front matter")
    try:
        metadata: Any = json.loads(
            metadata_text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("fragment revision front matter is not valid JSON") from exc
    revision = fragment_revision_from_document(metadata, markdown_body)
    if filename is not None:
        claimed_revision, claimed_digest = parse_fragment_revision_filename(
            Path(filename).name
        )
        if claimed_revision != revision.revision:
            raise ValueError("fragment revision filename has the wrong revision")
        if claimed_digest != revision.semantic_digest:
            raise ValueError("fragment revision filename digest does not match")
    return revision


def read_fragment_revision(path: str | Path) -> FragmentRevision:
    revision_path = Path(path)
    try:
        value = revision_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read fragment revision: {revision_path}") from exc
    return decode_fragment_revision(value, filename=revision_path.name)


def fragment_revision_ref(
    path: str, revision: FragmentRevision
) -> FragmentRevisionRef:
    return FragmentRevisionRef(
        path=path,
        fragment_id=revision.fragment_id,
        revision=revision.revision,
        semantic_digest=revision.semantic_digest,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"unsupported JSON number: {value}")


__all__ = [
    "FRONT_MATTER_BEGIN",
    "FRONT_MATTER_END",
    "decode_fragment_revision",
    "encode_fragment_revision",
    "extract_markdown_citation_ids",
    "block_text_to_markdown",
    "fragment_revision_filename",
    "fragment_revision_ref",
    "fragment_semantic_digest",
    "normalize_markdown",
    "parse_fragment_revision_filename",
    "read_fragment_revision",
]
