from __future__ import annotations

import pytest

from arc_companion.reader_labels import reader_labels


@pytest.mark.parametrize(
    ("language", "expected"),
    (("zh-CN", "译文"), ("zh-TW", "譯文"), ("en", "Translation")),
)
def test_builtin_translation_role_label(language: str, expected: str) -> None:
    assert reader_labels(language)["translation"] == expected
