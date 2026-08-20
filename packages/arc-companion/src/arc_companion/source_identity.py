"""Deterministic document-title and author-candidate resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from arc_document import RichBlockKind, RichDocument


_AUTHOR_BASES = {
    "metadata.authors",
    "metadata.author",
    "document.byline",
    "none",
}
_KNOWN_SOURCE_SUFFIXES = {".htm", ".html", ".markdown", ".md", ".tex"}
_DIGEST_NAME = re.compile(r"(?:sha256[-_:]?)?[0-9a-f]{40,}", re.IGNORECASE)
_AUTHOR_PREFIX = re.compile(r"^\s*authors?\s*:\s*(?P<names>.+)$", re.IGNORECASE)
_BY_PREFIX = re.compile(r"^\s*by\s+(?P<names>.+)$", re.IGNORECASE)
_BYLINE_BOUNDARY = re.compile(
    r"\s+(?="
    r"(?:english\s+)?translation\s*:"
    r"|translated\s+by\b"
    r"|translator\s*:"
    r"|edit(?:ed|or)\s*(?:by|:)"
    r"|source(?:\s+edition)?\s*:"
    r"|edition\s*:"
    r"|publication\s*:"
    r"|published\s+by\b"
    r"|copyright\s*:?"
    r")",
    re.IGNORECASE,
)
_AUTHOR_SEPARATOR = re.compile(r"\s*(?:;|\band\b|&)\s*", re.IGNORECASE)
_BYLINE_SCAN_BLOCKS = 5


@dataclass(frozen=True)
class DocumentIdentity:
    """Deterministic title plus unconfirmed author candidates."""

    title: str = ""
    candidate_authors: tuple[str, ...] = ()
    author_basis: str = "none"

    def __post_init__(self) -> None:
        title = _clean_text(self.title)
        authors = _normalize_authors(self.candidate_authors)
        if self.author_basis not in _AUTHOR_BASES:
            raise ValueError("unsupported document author basis")
        if (authors and self.author_basis == "none") or (
            not authors and self.author_basis != "none"
        ):
            raise ValueError("document author basis does not match candidates")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "candidate_authors", authors)


def resolve_document_identity(document: RichDocument) -> DocumentIdentity:
    """Resolve deterministic title and conservative, unconfirmed authors."""

    if not isinstance(document, RichDocument):
        raise TypeError("document must be a RichDocument")
    candidates, basis = _author_candidates(document)
    return DocumentIdentity(
        title=_document_title(document),
        candidate_authors=candidates,
        author_basis=basis,
    )


def _document_title(document: RichDocument) -> str:
    metadata_title = document.metadata.get("title")
    if isinstance(metadata_title, str) and _clean_text(metadata_title):
        return _clean_text(metadata_title)

    headings = [
        block
        for block in document.blocks
        if block.kind is RichBlockKind.HEADING
        and isinstance(block.payload.get("text"), str)
        and _clean_text(str(block.payload["text"]))
    ]
    if headings:
        heading = min(
            headings,
            key=lambda item: (
                int(item.payload["level"]),
                item.ordinal,
            ),
        )
        return _clean_text(str(heading.payload["text"]))

    origin = document.source.origin
    for key in ("filename", "file_name", "source_name", "name"):
        value = origin.metadata.get(key)
        if value:
            title = _source_name(value)
            if title:
                return title
    return _source_name(origin.locator)


def _author_candidates(
    document: RichDocument,
) -> tuple[tuple[str, ...], str]:
    for key in ("authors", "author"):
        candidates = _metadata_authors(document.metadata.get(key))
        if candidates:
            return candidates, f"metadata.{key}"

    for block in document.blocks[:_BYLINE_SCAN_BLOCKS]:
        text = block.payload.get("text")
        if not isinstance(text, str):
            continue
        candidates = _byline_authors(text)
        if candidates:
            return candidates, "document.byline"
    return (), "none"


def _metadata_authors(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return _split_authors(value)
    if isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, bytearray, Mapping)
    ):
        return _normalize_authors(
            item for item in value if isinstance(item, str)
        )
    return ()


def _byline_authors(value: str) -> tuple[str, ...]:
    text = _clean_text(value)
    match = _AUTHOR_PREFIX.match(text)
    explicit_label = match is not None
    if match is None:
        match = _BY_PREFIX.match(text)
    if match is None:
        return ()
    names = _BYLINE_BOUNDARY.split(match.group("names"), maxsplit=1)[0]
    if not explicit_label and not _plausible_by_names(names):
        return ()
    return _split_authors(names)


def _plausible_by_names(value: str) -> bool:
    if len(value) > 120 or any(mark in value for mark in ".!?"):
        return False
    words = value.split()
    if not 1 <= len(words) <= 8:
        return False
    alphabetic = next(
        (character for character in value if character.isalpha()),
        "",
    )
    return bool(alphabetic) and (
        not alphabetic.isascii() or alphabetic.isupper()
    )


def _split_authors(value: str) -> tuple[str, ...]:
    return _normalize_authors(_AUTHOR_SEPARATOR.split(value))


def _normalize_authors(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _clean_text(value).strip(" ,")
        identity = name.casefold()
        if not name or identity in seen:
            continue
        seen.add(identity)
        output.append(name)
    return tuple(output)


def _source_name(value: str) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = unquote(parsed.path or raw)
    name = PurePosixPath(path.replace("\\", "/").rstrip("/")).name
    if not name or _DIGEST_NAME.fullmatch(name):
        return ""
    lowered = name.casefold()
    suffix = next(
        (item for item in _KNOWN_SOURCE_SUFFIXES if lowered.endswith(item)),
        "",
    )
    if suffix:
        name = name[: -len(suffix)]
    return _clean_text(name.replace("_", " ").replace("-", " "))


def _clean_text(value: str) -> str:
    return " ".join(value.split())


__all__ = ["DocumentIdentity", "resolve_document_identity"]
