"""Deterministic cover-colored reader icons."""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass
from typing import Sequence

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError


READER_ICON_MEDIA_TYPE = "image/svg+xml"
READER_ICON_LOGICAL_NAME = "arc-reader-icon.svg"
_NEUTRAL_RGB = (96, 104, 112)


@dataclass(frozen=True)
class ReaderIcon:
    payload: bytes
    initial: str
    foreground_rgb: tuple[int, int, int]
    background_rgb: tuple[int, int, int]


def reader_initial(authors: Sequence[str], title: str) -> str:
    """Return first verified author's surname initial, then title initial."""

    author = next((item.strip() for item in authors if item.strip()), "")
    if author:
        surname = author.split(",", 1)[0] if "," in author else author
        words = re.findall(r"[^\W\d_]+", surname, flags=re.UNICODE)
        if words:
            return words[-1][0].upper()
        return author[0].upper()
    title_letters = re.findall(r"[^\W\d_]+", title, flags=re.UNICODE)
    return title_letters[0][0].upper() if title_letters else "A"


def build_reader_icon(
    cover_bytes: bytes | None,
    *,
    authors: Sequence[str],
    title: str,
) -> ReaderIcon:
    """Build canonical SVG using cover mean as text and complement as field."""

    foreground = _average_rgb(cover_bytes) if cover_bytes else _NEUTRAL_RGB
    background = tuple(255 - value for value in foreground)
    initial = reader_initial(authors, title)
    stroke = _contrast_stroke(foreground, background)
    foreground_hex = _hex(foreground)
    background_hex = _hex(background)
    payload = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="12" fill="{background_hex}"/>'
        '<text x="32" y="46" text-anchor="middle" '
        'font-family="system-ui,sans-serif" font-size="42" font-weight="700" '
        f'fill="{foreground_hex}" stroke="{stroke}" stroke-width="1.5" '
        f'paint-order="stroke">{html.escape(initial)}</text></svg>\n'
    ).encode("utf-8")
    return ReaderIcon(payload, initial, foreground, background)


def _average_rgb(payload: bytes) -> tuple[int, int, int]:
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image).convert("RGB")
            image = image.resize((64, 64), Image.Resampling.BOX)
            return tuple(round(value) for value in ImageStat.Stat(image).mean)
    except (OSError, UnidentifiedImageError, ValueError):
        return _NEUTRAL_RGB


def _contrast_stroke(
    foreground: tuple[int, int, int], background: tuple[int, int, int]
) -> str:
    if _contrast_ratio(foreground, background) >= 3:
        return "none"
    black = _contrast_ratio(foreground, (0, 0, 0))
    white = _contrast_ratio(foreground, (255, 255, 255))
    return "#000000" if black >= white else "#ffffff"


def _contrast_ratio(
    left: tuple[int, int, int], right: tuple[int, int, int]
) -> float:
    values = sorted((_luminance(left), _luminance(right)))
    return (values[1] + 0.05) / (values[0] + 0.05)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        normalized = value / 255
        return (
            normalized / 12.92
            if normalized <= 0.04045
            else ((normalized + 0.055) / 1.055) ** 2.4
        )

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{value:02x}" for value in rgb)


__all__ = [
    "READER_ICON_LOGICAL_NAME",
    "READER_ICON_MEDIA_TYPE",
    "ReaderIcon",
    "build_reader_icon",
    "reader_initial",
]
