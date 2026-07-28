"""XeLaTeX-safe escaping for reader-visible Unicode prose."""

from __future__ import annotations

import re
from typing import Any


_SPECIAL = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "#": r"\#",
    "$": r"\$",
    "%": r"\%",
    "&": r"\&",
    "_": r"\_",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}
_CJK_MATH_TEXT = re.compile(
    r"[\u2e80-\u303f\u3040-\u30ff\u3100-\u312f\u31a0-\u31ef"
    r"\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
    r"\uff01-\uff60\uffe0-\uffe6]+"
)
_ENCLOSED_MATH_TEXT = re.compile(r"[\u2460-\u24ff]+")


def escape_tex_text(value: Any) -> str:
    """Escape TeX syntax and select fonts for non-CJK symbol blocks.

    The Companion PDF uses a CJK body font, but no single CJK face covers
    mathematical operators, technical symbols, arrows, and enclosed Latin
    letters.  Keeping the original Unicode character inside an embedded
    fallback font preserves both its appearance and its searchable text.
    """

    values: list[str] = []
    for char in str(value):
        replacement = _SPECIAL.get(char)
        if replacement is not None:
            values.append(replacement)
            continue
        codepoint = ord(char)
        if 0x2460 <= codepoint <= 0x24FF:
            values.append(r"\ArcEnclosedSymbol{" + char + "}")
        elif (
            0x2190 <= codepoint <= 0x23FF
            or 0x27C0 <= codepoint <= 0x2BFF
        ):
            values.append(r"\ArcUnicodeSymbol{" + char + "}")
        else:
            values.append(char)
    return "".join(values)


def sanitize_tex_math(value: Any) -> str:
    """Keep CJK labels readable when they occur inside TeX math."""

    text = str(value).replace("\x00", "")
    text = _ENCLOSED_MATH_TEXT.sub(
        lambda match: r"\text{\ArcEnclosedSymbol{" + match.group(0) + "}}",
        text,
    )
    return _CJK_MATH_TEXT.sub(
        lambda match: r"\text{" + match.group(0) + "}",
        text,
    )


__all__ = ["escape_tex_text", "sanitize_tex_math"]
