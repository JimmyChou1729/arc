"""Deterministically turn a local HTML bundle into one offline HTML file.

The public API deliberately uses only the Python standard library so it is
also useful to callers outside the Companion renderer.
"""

from __future__ import annotations

import argparse
import base64
from html import escape
from html.parser import HTMLParser
import mimetypes
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit


_CSP = (
    "default-src 'none'; base-uri 'none'; connect-src 'none'; "
    "font-src data:; form-action 'none'; frame-src 'none'; img-src data:; "
    "media-src data:; object-src data:; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'"
)
_URL_PATTERN = re.compile(
    r"url\(\s*(?P<value>(?:'[^']*'|\"[^\"]*\"|[^)]*)?)\s*\)",
    re.IGNORECASE,
)
_IMPORT_PATTERN = re.compile(
    r"@import\s+(?:url\(\s*)?(?P<value>'[^']*'|\"[^\"]*\"|[^;\s)]+)\s*\)?\s*;",
    re.IGNORECASE,
)
class StandaloneHtmlError(ValueError):
    """The source bundle cannot be represented safely as one HTML file."""


def standalone_html_bytes(index: Path) -> bytes:
    """Return a deterministic standalone representation of local ``index``.

    All automatically fetched local files are embedded as data URIs. External
    navigation anchors remain links; external automatic resources are errors.
    """

    index = index.resolve()
    try:
        source = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StandaloneHtmlError("HTML input is unreadable") from exc
    if not index.is_file():
        raise StandaloneHtmlError("HTML input is not a file")
    return _HtmlInliner(index.parent).inline(source).encode("utf-8")


def write_standalone_html(index: Path, output: Path) -> None:
    """Atomically write :func:`standalone_html_bytes` to ``output``."""

    payload = standalone_html_bytes(index)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class _HtmlInliner(HTMLParser):
    def __init__(self, root: Path) -> None:
        super().__init__(convert_charrefs=False)
        self.root = root.resolve()
        self.output: list[str] = []
        self._style_attrs: list[tuple[str, str | None]] | None = None
        self._style_parts: list[str] = []
        self._suppressed_endtags: list[str] = []
        self._deferred_scripts: list[str] = []
        self._has_head = False
        self._inserted_csp = False

    def inline(self, value: str) -> str:
        self.feed(value)
        self.close()
        if self._style_attrs is not None:
            raise StandaloneHtmlError("HTML contains an unclosed style element")
        if self._deferred_scripts:
            raise StandaloneHtmlError(
                "deferred scripts require a closing body element"
            )
        result = "".join(self.output)
        if not self._has_head:
            html = re.search(r"<html(?:\s[^>]*)?>", result, re.IGNORECASE)
            if html is None:
                raise StandaloneHtmlError("HTML has no head or html element")
            result = (
                result[: html.end()]
                + "<head>"
                + _csp_meta()
                + "</head>"
                + result[html.end() :]
            )
        return result

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lower = tag.casefold()
        if self._style_attrs is not None:
            self._style_parts.append(self.get_starttag_text())
            return
        if lower == "base":
            return
        if (
            lower == "meta"
            and (_attribute(attrs, "http-equiv") or "").casefold() == "refresh"
        ):
            raise StandaloneHtmlError("meta refresh is forbidden in standalone HTML")
        if (
            lower == "meta"
            and (_attribute(attrs, "http-equiv") or "").casefold()
            == "content-security-policy"
        ):
            # Replace bundle-relative policy with the stricter offline policy
            # injected above. Multiple CSP declarations intersect and could
            # otherwise disable the newly inlined reader assets.
            return
        if lower in {"frame", "iframe"}:
            raise StandaloneHtmlError("frames are forbidden in standalone HTML")
        if lower == "head":
            self._has_head = True
            self.output.append(_tag(tag, attrs))
            if not self._inserted_csp:
                self.output.append(_csp_meta())
                self._inserted_csp = True
            return
        if lower == "style":
            self._style_attrs = attrs
            self._style_parts = []
            return
        if lower == "script" and _attribute(attrs, "src") is not None:
            source = _attribute(attrs, "src")
            assert source is not None
            path = self._local_path(source, self.root)
            try:
                script = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise StandaloneHtmlError(
                    f"script resource is unreadable: {source}"
                ) from exc
            retained = [
                (key, val) for key, val in attrs if key.casefold() != "src"
            ]
            script = re.sub(r"</script", r"<\\/script", script, flags=re.IGNORECASE)
            rendered = _tag("script", retained) + script + "</script>"
            if _has_attribute(attrs, "defer"):
                self._deferred_scripts.append(rendered)
            else:
                self.output.append(rendered)
            self._suppressed_endtags.append("script")
            return
        if lower == "link" and _rel_contains(attrs, "stylesheet"):
            href = _attribute(attrs, "href")
            if not href:
                raise StandaloneHtmlError("stylesheet link has no href")
            css = self._read_css(href, self.root)
            retained = [
                (key, val)
                for key, val in attrs
                if key.casefold() not in {"href", "rel"}
            ]
            self.output.append(_tag("style", retained) + css + "</style>")
            return
        rewritten = self._rewrite_attributes(lower, attrs)
        self.output.append(_tag(tag, rewritten))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lower = tag.casefold()
        if lower == "base":
            return
        if lower == "link" and _rel_contains(attrs, "stylesheet"):
            self.handle_starttag(tag, attrs)
            return
        if (
            lower == "meta"
            and (_attribute(attrs, "http-equiv") or "").casefold() == "refresh"
        ):
            raise StandaloneHtmlError("meta refresh is forbidden in standalone HTML")
        if (
            lower == "meta"
            and (_attribute(attrs, "http-equiv") or "").casefold()
            == "content-security-policy"
        ):
            return
        if lower in {"frame", "iframe"}:
            raise StandaloneHtmlError("frames are forbidden in standalone HTML")
        rewritten = self._rewrite_attributes(lower, attrs)
        self.output.append(_tag(tag, rewritten, closing=True))

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if self._suppressed_endtags and self._suppressed_endtags[-1] == lower:
            self._suppressed_endtags.pop()
            return
        if self._style_attrs is not None:
            if lower == "style":
                css = self._rewrite_css(
                    "".join(self._style_parts), self.root, set()
                )
                self.output.append(
                    _tag("style", self._style_attrs) + css + "</style>"
                )
                self._style_attrs = None
                self._style_parts = []
            else:
                self._style_parts.append(f"</{tag}>")
            return
        if lower == "body" and self._deferred_scripts:
            self.output.extend(self._deferred_scripts)
            self._deferred_scripts = []
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._suppressed_endtags:
            return
        if self._style_attrs is not None:
            self._style_parts.append(data)
        else:
            self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.output.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.output.append(f"<?{data}>")

    def _rewrite_attributes(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> list[tuple[str, str | None]]:
        output: list[tuple[str, str | None]] = []
        for key, value in attrs:
            lower = key.casefold()
            if value is None:
                output.append((key, value))
                continue
            if lower in {"src", "poster"} or (tag == "object" and lower == "data"):
                output.append((key, self._resource_uri(value, self.root)))
            elif lower == "srcset":
                output.append((key, self._srcset(value)))
            elif lower == "style":
                output.append((key, self._rewrite_css(value, self.root, set())))
            elif lower in {"href", "xlink:href"}:
                output.append((key, self._href(tag, value)))
            else:
                output.append((key, value))
        return output

    def _href(self, tag: str, value: str) -> str:
        if tag in {"image", "feimage"}:
            return self._resource_uri(value, self.root)
        if tag == "use":
            if value.startswith("#"):
                return value
            parsed = urlsplit(value)
            fragment = parsed.fragment
            if parsed.query:
                raise StandaloneHtmlError(
                    f"local SVG resource must not contain a query: {value}"
                )
            resource = self._resource_uri(
                parsed._replace(fragment="").geturl(),
                self.root,
            )
            return resource + (f"#{fragment}" if fragment else "")
        if tag == "a":
            if _is_navigation(value):
                return value
            return self._resource_uri(value, self.root)
        if tag == "link":
            return self._resource_uri(value, self.root)
        return value

    def _srcset(self, value: str) -> str:
        values: list[str] = []
        for reference, descriptor in _srcset_candidates(value):
            uri = self._resource_uri(reference, self.root)
            values.append(uri + (f" {descriptor}" if descriptor else ""))
        if not values:
            raise StandaloneHtmlError("srcset has no usable candidates")
        return ", ".join(values)

    def _read_css(
        self,
        reference: str,
        base: Path,
        seen: set[Path] | None = None,
    ) -> str:
        path = self._local_path(reference, base)
        seen = set() if seen is None else seen
        if path in seen:
            raise StandaloneHtmlError(
                f"CSS import cycle: {path.relative_to(self.root)}"
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StandaloneHtmlError(
                f"CSS resource is unreadable: {reference}"
            ) from exc
        return self._rewrite_css(text, path.parent, seen | {path})

    def _rewrite_css(self, css: str, base: Path, seen: set[Path]) -> str:
        def import_replacement(match: re.Match[str]) -> str:
            raw = _unquote_url(match.group("value"))
            return self._read_css(raw, base, seen)

        css = _IMPORT_PATTERN.sub(import_replacement, css)
        if re.search(r"@import\b", css, re.IGNORECASE):
            raise StandaloneHtmlError("unsupported CSS import")

        def url_replacement(match: re.Match[str]) -> str:
            raw = _unquote_url(match.group("value"))
            if raw.startswith("#") or raw.startswith("data:"):
                return match.group(0)
            return f'url("{self._resource_uri(raw, base)}")'

        return _URL_PATTERN.sub(url_replacement, css)

    def _resource_uri(self, reference: str, base: Path) -> str:
        if reference.startswith("data:"):
            return reference
        path = self._local_path(reference, base)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise StandaloneHtmlError(
                f"local resource is unreadable: {reference}"
            ) from exc
        media_type = _media_type(path)
        return (
            f"data:{media_type};base64,"
            f"{base64.b64encode(payload).decode('ascii')}"
        )

    def _local_path(self, reference: str, base: Path) -> Path:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc or reference.startswith("//"):
            raise StandaloneHtmlError(
                f"external automatic resource is forbidden: {reference}"
            )
        if parsed.query or parsed.fragment:
            raise StandaloneHtmlError(
                f"local resource must not contain a query or fragment: {reference}"
            )
        encoded_path = unquote(parsed.path)
        candidate = Path(encoded_path)
        if not encoded_path or candidate.is_absolute() or ".." in candidate.parts:
            raise StandaloneHtmlError(f"unsafe local resource path: {reference}")
        resolved = (base / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise StandaloneHtmlError(
                f"resource escapes HTML bundle: {reference}"
            ) from exc
        if not resolved.is_file():
            raise StandaloneHtmlError(f"local resource is missing: {reference}")
        return resolved


def _srcset_candidates(value: str) -> list[tuple[str, str]]:
    """Parse the URL and descriptor portions needed for deterministic inlining.

    The HTML algorithm reads a URL through ASCII whitespace, not through
    commas. That distinction preserves the comma inside a data URI.
    """

    candidates: list[tuple[str, str]] = []
    position = 0
    length = len(value)
    whitespace = " \t\n\f\r"
    while position < length:
        while position < length and (
            value[position] in whitespace or value[position] == ","
        ):
            position += 1
        if position >= length:
            break
        start = position
        while position < length and value[position] not in whitespace:
            position += 1
        reference = value[start:position]
        if reference.endswith(","):
            reference = reference.rstrip(",")
            descriptor = ""
        else:
            while position < length and value[position] in whitespace:
                position += 1
            start = position
            parentheses = 0
            while position < length:
                character = value[position]
                if character == "(":
                    parentheses += 1
                elif character == ")" and parentheses:
                    parentheses -= 1
                elif character == "," and parentheses == 0:
                    break
                position += 1
            descriptor = value[start:position].strip()
            if position < length and value[position] == ",":
                position += 1
        if not reference:
            raise StandaloneHtmlError("srcset has an empty candidate")
        candidates.append((reference, descriptor))
    return candidates


def _tag(
    tag: str,
    attrs: list[tuple[str, str | None]],
    *,
    closing: bool = False,
) -> str:
    values = ["<", tag]
    for key, value in attrs:
        values.extend((" ", key))
        if value is not None:
            values.extend(('="', escape(value, quote=True), '"'))
    values.append("/>" if closing else ">")
    return "".join(values)


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key.casefold() == name:
            return value
    return None


def _has_attribute(attrs: list[tuple[str, str | None]], name: str) -> bool:
    return any(key.casefold() == name for key, _ in attrs)


def _rel_contains(attrs: list[tuple[str, str | None]], value: str) -> bool:
    rel = _attribute(attrs, "rel")
    return rel is not None and value in rel.casefold().split()


def _unquote_url(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _is_navigation(value: str) -> bool:
    parsed = urlsplit(value)
    if value.startswith("#") or not parsed.path and parsed.fragment:
        return True
    if parsed.scheme.casefold() == "javascript":
        raise StandaloneHtmlError(f"unsupported anchor scheme: {value}")
    if parsed.scheme or parsed.netloc:
        return True
    return False


def _media_type(path: Path) -> str:
    return {
        ".css": "text/css",
        ".eot": "application/vnd.ms-fontobject",
        ".eps": "application/postscript",
        ".gif": "image/gif",
        ".htm": "text/html",
        ".html": "text/html",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".pdf": "application/pdf",
        ".svg": "image/svg+xml",
        ".ttf": "font/ttf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }.get(
        path.suffix.casefold(),
        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def _csp_meta() -> str:
    return '<meta http-equiv="Content-Security-Policy" content="' + _CSP + '">'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arc-standalone-html",
        description="Inline a local HTML bundle into one offline HTML file.",
    )
    parser.add_argument("input", type=Path, help="bundle HTML entry point")
    parser.add_argument("output", type=Path, help="standalone HTML output path")
    args = parser.parse_args(argv)
    try:
        write_standalone_html(args.input, args.output)
    except StandaloneHtmlError as exc:
        parser.error(str(exc))
    return 0


__all__ = [
    "StandaloneHtmlError",
    "standalone_html_bytes",
    "write_standalone_html",
]
