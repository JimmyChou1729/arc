from __future__ import annotations

import io

from PIL import Image

from arc_render import build_reader_icon, reader_initial


def _png(rgb: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), rgb).save(output, format="PNG")
    return output.getvalue()


def test_reader_icon_uses_cover_mean_complement_and_surname() -> None:
    first = build_reader_icon(
        _png((34, 37, 56)), authors=["Fred Hoyle"], title="Astronomy"
    )
    second = build_reader_icon(
        _png((34, 37, 56)), authors=["Fred Hoyle"], title="Astronomy"
    )

    assert first == second
    assert first.initial == "H"
    assert first.foreground_rgb == (34, 37, 56)
    assert first.background_rgb == (221, 218, 199)
    assert b'fill="#222538"' in first.payload
    assert b'fill="#dddac7"' in first.payload
    assert b">H</text>" in first.payload


def test_reader_initial_supports_comma_cjk_and_title_fallback() -> None:
    assert reader_initial(["Hoyle, Fred"], "Astronomy") == "H"
    assert reader_initial(["钱学森"], "工程控制论") == "钱"
    assert reader_initial([], "Astronomy") == "A"
