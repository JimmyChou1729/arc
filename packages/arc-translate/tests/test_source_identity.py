from __future__ import annotations

import pytest

from arc_translate.source import (
    TranslationSourceError,
    formula_identity_diagnostics,
    prompt_block,
    source_identity,
    validate_translation_text,
)


def _paragraph_block() -> dict[str, object]:
    return {
        "block_id": "block-inline",
        "kind": "paragraph",
        "payload": {
            "text": r"Use $a$ before $\Psi^\dagger$ and read notes.",
            "inline_spans": [
                {
                    "kind": "text",
                    "start": 0,
                    "end": 4,
                    "text": "Use ",
                },
                {
                    "kind": "math",
                    "start": 4,
                    "end": 7,
                    "text": "$a$",
                    "tex": "a",
                    "source": "$a$",
                },
                {
                    "kind": "text",
                    "start": 7,
                    "end": 15,
                    "text": " before ",
                },
                {
                    "kind": "math",
                    "start": 15,
                    "end": 29,
                    "text": r"$\Psi^\dagger$",
                    "tex": r"\Psi^\dagger",
                    "source": r"$\Psi^\dagger$",
                },
                {
                    "kind": "text",
                    "start": 29,
                    "end": 39,
                    "text": " and read ",
                },
                {
                    "kind": "link",
                    "start": 39,
                    "end": 44,
                    "text": "notes",
                    "target": "https://example.test/notes",
                },
                {
                    "kind": "text",
                    "start": 44,
                    "end": 45,
                    "text": ".",
                },
            ],
        },
    }


def test_source_identity_uses_current_inline_spans() -> None:
    identity = source_identity(_paragraph_block())

    assert identity["equations"] == ["a", r"\Psi^\dagger"]
    assert identity["link_targets"] == ["https://example.test/notes"]


def test_formula_occurrences_and_links_can_be_reordered_exactly() -> None:
    validate_translation_text(
        r"先看 $\Psi^\dagger$，再看 $a$；"
        r"[注释](https://example.test/notes)。",
        _paragraph_block(),
    )


def test_undelimited_nested_formula_text_is_not_double_counted() -> None:
    block = {
        "block_id": "block-nested-html-math",
        "kind": "paragraph",
        "payload": {
            "text": r"H, {\cal O}(H), H",
            "inline_spans": [
                {"kind": "math", "tex": "H"},
                {"kind": "text", "text": ", "},
                {"kind": "math", "tex": r"{\cal O}(H)"},
                {"kind": "text", "text": ", "},
                {"kind": "math", "tex": "H"},
            ],
        },
    }

    validate_translation_text(r"质量为 H、远大于 {\cal O}(H)，或约为 H。", block)
    with pytest.raises(
        TranslationSourceError,
        match="changed formula occurrences",
    ):
        validate_translation_text(
            r"质量为 H、远大于 {\cal O}(H)，约为 H，另有 H。", block
        )


@pytest.mark.parametrize(
    "text",
    [
        r"先看 $\\Psi^\dagger$，再看 $a$；https://example.test/notes",
        r"先看 $\Phi^\dagger$，再看 $a$；https://example.test/notes",
        r"只看 $a$；https://example.test/notes",
        r"看 $a$、$\Psi^\dagger$ 和 $b$；https://example.test/notes",
        r"看 $a$、$a$、$\Psi^\dagger$；https://example.test/notes",
    ],
    ids=[
        "overescaped",
        "substituted",
        "missing",
        "extra-different",
        "extra-duplicate",
    ],
)
def test_changed_formula_multiset_is_rejected(text: str) -> None:
    with pytest.raises(
        TranslationSourceError,
        match="changed formula occurrences",
    ):
        validate_translation_text(text, _paragraph_block())


def test_formula_failure_exposes_missing_and_added_tex_diagnostics() -> None:
    with pytest.raises(TranslationSourceError) as raised:
        validate_translation_text(
            r"看 $b$；https://example.test/notes", _paragraph_block()
        )

    diagnostics = raised.value.details["formula_diagnostics"]
    assert {item["code"] for item in diagnostics} == {
        "formula_missing",
        "formula_added",
    }
    assert {item["tex"] for item in diagnostics} == {
        "a",
        r"\Psi^\dagger",
        "b",
    }
    assert all(item["source_block_id"] == "block-inline" for item in diagnostics)
    assert all(
        item["translation_block_id"] == "block-inline" for item in diagnostics
    )
    assert all(item["source_neighbor_block_ids"] == [] for item in diagnostics)
    assert all(item["translation_neighbor_block_ids"] == [] for item in diagnostics)


def test_formula_diagnostics_identify_cross_block_moves_with_neighbors() -> None:
    source_blocks = (
        _math_paragraph("first", "a"),
        _plain_paragraph("middle"),
        _plain_paragraph("last"),
    )
    diagnostics = formula_identity_diagnostics(
        source_blocks,
        (
            {"block_id": "first", "text": "翻译。"},
            {"block_id": "middle", "text": "翻译 $a$。"},
            {"block_id": "last", "text": "翻译。"},
        ),
    )

    assert diagnostics == (
        {
            "code": "formula_moved",
            "tex": "a",
            "occurrence_count": 1,
            "source_block_id": "first",
            "translation_block_id": "middle",
            "source_neighbor_block_ids": ["middle"],
            "translation_neighbor_block_ids": ["first", "last"],
        },
    )


def _math_paragraph(block_id: str, tex: str) -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "paragraph",
        "payload": {
            "text": f"${tex}$",
            "inline_spans": [
                {
                    "kind": "math",
                    "start": 0,
                    "end": len(tex) + 2,
                    "text": f"${tex}$",
                    "tex": tex,
                    "source": f"${tex}$",
                }
            ],
        },
    }


def _plain_paragraph(block_id: str) -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "paragraph",
        "payload": {"text": "plain", "inline_spans": []},
    }


@pytest.mark.parametrize(
    "text",
    [
        r"看 $a$ 和 $\Psi^\dagger$，但没有链接。",
        r"看 $a$ 和 $\Psi^\dagger$；"
        r"[注释](https://example.test/replaced)。",
        r"看 $a$ 和 $\Psi^\dagger$；"
        r"[注释](https://example.test/notes) "
        r"[额外](https://example.test/extra)。",
        r"看 $a$ 和 $\Psi^\dagger$；"
        r"https://example.test/notes https://example.test/notes",
    ],
    ids=["missing", "replaced", "extra-target", "extra-duplicate"],
)
def test_changed_link_multiset_is_rejected(text: str) -> None:
    with pytest.raises(
        TranslationSourceError,
        match="changed link occurrences",
    ):
        validate_translation_text(text, _paragraph_block())


def test_link_validation_ignores_markdown_shapes_inside_inline_math() -> None:
    tex = r"\left[p_\mu\right](p'-p)"
    block = {
        "block_id": "block-math-link-shape",
        "kind": "paragraph",
        "payload": {
            "text": f"${tex}$",
            "inline_spans": [
                {
                    "kind": "math",
                    "start": 0,
                    "end": len(tex) + 2,
                    "text": f"${tex}$",
                    "tex": tex,
                    "source": f"${tex}$",
                }
            ],
        },
    }

    assert source_identity(block)["link_targets"] == []
    validate_translation_text(f"${tex}$", block)
    with pytest.raises(
        TranslationSourceError,
        match="changed link occurrences",
    ):
        validate_translation_text(
            f"${tex}$ [extra](https://example.test/extra)",
            block,
        )


def test_list_identity_uses_each_items_inline_spans() -> None:
    block = {
        "block_id": "block-list",
        "kind": "list",
        "payload": {
            "items": [
                {
                    "text": "$x$",
                    "inline_spans": [
                        {
                            "kind": "math",
                            "start": 0,
                            "end": 3,
                            "text": "$x$",
                            "tex": "x",
                            "source": "$x$",
                        }
                    ],
                },
                {
                    "text": "reference",
                    "inline_spans": [
                        {
                            "kind": "link",
                            "start": 0,
                            "end": 9,
                            "text": "reference",
                            "target": "appendix.html",
                        }
                    ],
                },
            ]
        },
    }

    assert source_identity(block)["equations"] == ["x"]
    assert source_identity(block)["link_targets"] == ["appendix.html"]
    validate_translation_text("$x$ appendix.html", block)

    prompted = prompt_block(block)
    assert prompted["payload"] == {
        "ordered": False,
        "items": [{"text": "$x$"}, {"text": "reference"}],
    }
    assert "inline_spans" not in str(prompted["payload"])


def test_heading_identity_extracts_markdown_math_without_inline_spans() -> None:
    link_shaped_tex = r"\left[V\right](p)"
    block = {
        "block_id": "block-heading",
        "kind": "heading",
        "payload": {
            "text": (
                r"Representations of $G$, $\mathfrak g$, and "
                f"${link_shaped_tex}$"
            ),
            "level": 2,
        },
    }

    identity = source_identity(block)
    assert identity["equations"] == ["G", r"\mathfrak g", link_shaped_tex]
    assert identity["link_targets"] == []
    validate_translation_text(
        f"${link_shaped_tex}$、" r"$\mathfrak g$ 与 $G$ 的表示",
        block,
    )


def test_table_identity_extracts_markdown_math_and_links() -> None:
    block = {
        "block_id": "block-table",
        "kind": "table",
        "payload": {
            "headers": ["Operator", "[Reference](operators.html)"],
            "rows": [[r"$H$", "Hamiltonian"], [r"$P^\mu$", "momentum"]],
            "caption": "",
        },
    }

    identity = source_identity(block)
    assert identity["equations"] == ["H", r"P^\mu"]
    assert identity["link_targets"] == ["operators.html"]
    validate_translation_text(
        r"$P^\mu$ | 动量" "\n"
        r"$H$ | [哈密顿量](operators.html)",
        block,
    )

def test_figure_identity_uses_caption_without_exposing_asset_target() -> None:
    block = {
        "block_id": "block-figure",
        "ordinal": 4,
        "kind": "figure",
        "section_path": [],
        "payload": {
            "caption": r"Plot of $f(x)$ from [data](caption-data.html)",
            "alt_text": "private alt text",
            "asset_digest": "a" * 64,
            "target": "images/plot.png",
        },
    }

    prompted = prompt_block(block)
    assert prompted["source_identity"] == {
        "equations": ["f(x)"],
        "code_text": None,
        "link_targets": ["caption-data.html"],
    }
    assert "images/plot.png" not in str(prompted)
    validate_translation_text(
        r"[数据](caption-data.html)中的 $f(x)$ 图",
        block,
    )


def test_undelimited_inline_math_still_uses_exact_tex_identity() -> None:
    block = {
        "block_id": "block-html-math",
        "kind": "paragraph",
        "payload": {
            "text": r"\Psi",
            "inline_spans": [
                {
                    "kind": "math",
                    "start": 0,
                    "end": 4,
                    "text": r"\Psi",
                    "tex": r"\Psi",
                    "source": r"\Psi",
                }
            ],
        },
    }

    validate_translation_text(r"态 \Psi。", block)
    with pytest.raises(
        TranslationSourceError,
        match="changed formula occurrences",
    ):
        validate_translation_text(r"态 \\Psi。", block)


@pytest.mark.parametrize(
    ("source_tex", "text"),
    [
        (r"x^2 + y^2 = z^2", r"x^2 + y^2 = z^2"),
        (r"x^2 + y^2 = z^2", r"$$x^2 + y^2 = z^2$$"),
        (r"x^2 + y^2 = z^2", r"x^2  + y^2 = z^2"),
        (r"\left[p_\mu\right](p'-p)", r"\left[p_\mu\right](p'-p)"),
    ],
)
def test_equation_translation_must_equal_source_tex(
    source_tex: str,
    text: str,
) -> None:
    block = {
        "block_id": "block-equation",
        "kind": "equation",
        "payload": {
            "tex": source_tex,
            "display": True,
            "label": "",
        },
    }

    if text == block["payload"]["tex"]:
        validate_translation_text(text, block)
    else:
        with pytest.raises(
            TranslationSourceError,
            match="changed equation text",
        ):
            validate_translation_text(text, block)
