"""Small, validated target-language UI vocabulary for Companion readers."""

from __future__ import annotations

from collections.abc import Mapping


READER_LABEL_KEYS = frozenset(
    {
        "author",
        "glossary",
        "references",
        "source_term",
        "source_equation_label",
        "translation",
        "definition",
        "notes",
        "figure_unfrozen",
        "figure_original",
        "unknown_reference",
        "untitled_document",
    }
)

_EN = {
    "author": "Author",
    "glossary": "Glossary",
    "references": "References",
    "source_term": "Source term",
    "source_equation_label": "Source equation label: {label}",
    "translation": "Translation",
    "definition": "Definition",
    "notes": "Notes",
    "figure_unfrozen": "The figure asset was not frozen with the source.",
    "figure_original": "Open original figure",
    "unknown_reference": "Unknown reference",
    "untitled_document": "Untitled document",
}
_ZH_HANS = {
    "author": "作者",
    "glossary": "术语表",
    "references": "参考文献",
    "source_term": "原文术语",
    "source_equation_label": "原文公式编号：{label}",
    "translation": "译文",
    "definition": "释义",
    "notes": "伴读",
    "figure_unfrozen": "该图片未随原文冻结保存。",
    "figure_original": "查看原始图片",
    "unknown_reference": "未知参考文献",
    "untitled_document": "无标题文档",
}
_ZH_HANT = {
    "author": "作者",
    "glossary": "術語表",
    "references": "參考文獻",
    "source_term": "原文術語",
    "source_equation_label": "原文公式編號：{label}",
    "translation": "譯文",
    "definition": "釋義",
    "notes": "伴讀",
    "figure_unfrozen": "該圖片未隨原文凍結保存。",
    "figure_original": "查看原始圖片",
    "unknown_reference": "未知參考文獻",
    "untitled_document": "無標題文件",
}


class ReaderLabelError(ValueError):
    """Reader UI labels are missing, malformed, or unsupported."""


def reader_labels(
    target_language: str,
    supplied: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a complete label package for a target language.

    A supplied package is deliberately all-or-nothing: a publication must not
    silently blend caller wording with a different locale.
    """

    if supplied is not None:
        result = dict(supplied)
        if set(result) != READER_LABEL_KEYS:
            missing = sorted(READER_LABEL_KEYS - set(result))
            extra = sorted(set(result) - READER_LABEL_KEYS)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("unknown " + ", ".join(extra))
            raise ReaderLabelError("reader label package is incomplete: " + "; ".join(detail))
        if any(not isinstance(value, str) or not value.strip() for value in result.values()):
            raise ReaderLabelError("reader labels must be non-empty strings")
        try:
            result["source_equation_label"].format(label="1")
        except (KeyError, ValueError) as exc:
            raise ReaderLabelError(
                "reader labels must accept source_equation_label {label}"
            ) from exc
        return result

    language = _canonical_language(target_language)
    if language == "en":
        return dict(_EN)
    if language == "zh-Hans":
        return dict(_ZH_HANS)
    if language == "zh-Hant":
        return dict(_ZH_HANT)
    raise ReaderLabelError(
        "no built-in reader labels for target language "
        f"{target_language!r}; supply a complete reader_labels package"
    )


def resolve_reader_labels(
    target_language: str,
    custom_labels: Mapping[str, str] | None = None,
) -> dict[str, str]:
    return reader_labels(target_language, custom_labels)


def _canonical_language(value: str) -> str:
    parts = value.strip().replace("_", "-").split("-")
    if not parts or not parts[0]:
        raise ReaderLabelError("target language is required for reader labels")
    primary = parts[0].casefold()
    if primary == "en":
        return "en"
    if primary != "zh":
        return primary
    lower = {part.casefold() for part in parts[1:]}
    if "hant" in lower or lower & {"tw", "hk", "mo"}:
        return "zh-Hant"
    return "zh-Hans"


__all__ = [
    "READER_LABEL_KEYS",
    "ReaderLabelError",
    "reader_labels",
    "resolve_reader_labels",
]
