from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

import pytest

from arc_render.standalone_html import (
    StandaloneHtmlError,
    main,
    standalone_html_bytes,
    write_standalone_html,
)


def _bundle(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    root = tmp_path / "reader"
    (root / "assets" / "fonts").mkdir(parents=True)
    image = b"\x89PNG\r\n\x1a\nstandalone-image"
    font = b"standalone-font"
    (root / "assets" / "image.png").write_bytes(image)
    (root / "assets" / "attachment.pdf").write_bytes(b"%PDF-1.4\nattachment")
    (root / "assets" / "fonts" / "reader.woff2").write_bytes(font)
    (root / "assets" / "base.css").write_text(
        '.card { background: url("image.png"); }', encoding="utf-8"
    )
    (root / "assets" / "reader.css").write_text(
        '@import "base.css"; @font-face { src: url("fonts/reader.woff2"); }',
        encoding="utf-8",
    )
    (root / "assets" / "reader.js").write_text(
        "window.readerReady = true;", encoding="utf-8"
    )
    index = root / "index.html"
    index.write_text(
        """<!doctype html><html><head><base href="ignored/"><link rel="stylesheet" href="assets/reader.css"><script defer src="assets/reader.js">ignored</script></head><body style="background-image:url('assets/image.png')"><img src="assets/image.png" srcset="assets/image.png 1x, assets/image.png 2x"><video src="assets/image.png" poster="assets/image.png"></video><object data="assets/attachment.pdf"></object><embed src="assets/attachment.pdf"><a href="assets/attachment.pdf">attachment</a><a href="https://example.test/source">source</a></body></html>""",
        encoding="utf-8",
    )
    return index, image, font


def test_standalone_html_recursively_embeds_local_resources_and_preserves_navigation(
    tmp_path: Path,
) -> None:
    index, image, font = _bundle(tmp_path)

    payload = standalone_html_bytes(index)
    text = payload.decode("utf-8")

    assert "<base" not in text.casefold()
    assert "assets/" not in text
    assert "window.readerReady = true;" in text
    assert "ignored</script>" not in text
    assert text.index("<script defer") > text.index("<body")
    assert "Content-Security-Policy" in text
    assert "default-src 'none'" in text
    assert 'href="https://example.test/source"' in text
    assert re.search(r'href="data:application/pdf;base64,', text)
    assert "url(\"data:image/png;base64," in text
    image_match = re.search(r'src="(data:image/png;base64,[^"]+)"', text)
    font_match = re.search(r'data:font/woff2;base64,([A-Za-z0-9+/=]+)', text)
    assert image_match is not None
    assert font_match is not None
    assert base64.b64decode(image_match.group(1).split(",", 1)[1]) == image
    assert base64.b64decode(font_match.group(1)) == font


def test_written_standalone_html_survives_copy_without_its_bundle(tmp_path: Path) -> None:
    index, _, _ = _bundle(tmp_path)
    delivered = tmp_path / "delivery" / "companion.html"

    write_standalone_html(index, delivered)
    copied = tmp_path / "copied" / "companion.html"
    copied.parent.mkdir()
    shutil.copyfile(delivered, copied)
    shutil.rmtree(index.parent)

    text = copied.read_text(encoding="utf-8")
    assert "assets/" not in text
    assert "data:image/png;base64," in text
    assert "window.readerReady = true;" in text


@pytest.mark.parametrize(
    "html, message",
    [
        ('<html><head></head><body><img src="https://example.test/a.png"></body></html>', "external automatic"),
        (
            '<html><head><link rel="stylesheet" href="https://example.test/reader.css"></head><body></body></html>',
            "external automatic",
        ),
        ('<html><head></head><body><img src="../outside.png"></body></html>', "unsafe local"),
        ('<html><head></head><body><img src="missing.png"></body></html>', "missing"),
    ],
)
def test_standalone_html_rejects_unavailable_automatic_resources(
    tmp_path: Path, html: str, message: str
) -> None:
    index = tmp_path / "index.html"
    index.write_text(html, encoding="utf-8")

    with pytest.raises(StandaloneHtmlError, match=message):
        standalone_html_bytes(index)


def test_standalone_html_cli_writes_atomic_output(tmp_path: Path) -> None:
    index, _, _ = _bundle(tmp_path)
    output = tmp_path / "output.html"

    assert main([str(index), str(output)]) == 0
    assert output.is_file()
    assert "Content-Security-Policy" in output.read_text(encoding="utf-8")


def test_standalone_html_replaces_bundle_csp_and_escapes_script_end(
    tmp_path: Path,
) -> None:
    (tmp_path / "reader.js").write_text(
        'window.marker = "</SCRIPT>";', encoding="utf-8"
    )
    index = tmp_path / "index.html"
    index.write_text(
        """<html><head><meta http-equiv="Content-Security-Policy" content="script-src 'none'"><script defer src="reader.js"></script></head><body></body></html>""",
        encoding="utf-8",
    )

    text = standalone_html_bytes(index).decode("utf-8")

    assert text.count("Content-Security-Policy") == 1
    assert "script-src 'unsafe-inline'" in text
    assert '<\\/script>' in text


def test_srcset_preserves_data_uri_commas_and_inlines_local_candidates(
    tmp_path: Path,
) -> None:
    image = b"local-image"
    (tmp_path / "image.png").write_bytes(image)
    index = tmp_path / "index.html"
    index.write_text(
        '<html><head></head><body><img srcset="'
        'data:image/png;base64,AAAA 1x, image.png 2x"></body></html>',
        encoding="utf-8",
    )

    text = standalone_html_bytes(index).decode("utf-8")

    assert "data:image/png;base64,AAAA 1x" in text
    encoded = base64.b64encode(image).decode("ascii")
    assert f"data:image/png;base64,{encoded} 2x" in text


def test_svg_image_and_use_resources_are_embedded(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"svg-image")
    (tmp_path / "icons.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<symbol id="mark"><circle r="1"/></symbol></svg>',
        encoding="utf-8",
    )
    index = tmp_path / "index.html"
    index.write_text(
        '<html><head></head><body><svg>'
        '<image href="image.png"></image>'
        '<use href="icons.svg#mark"></use>'
        '<use xlink:href="#local"></use>'
        '</svg></body></html>',
        encoding="utf-8",
    )

    text = standalone_html_bytes(index).decode("utf-8")

    assert 'href="data:image/png;base64,' in text
    assert 'href="data:image/svg+xml;base64,' in text
    assert '#mark"' in text
    assert 'xlink:href="#local"' in text
