from __future__ import annotations

from arc_translate.delivery import (
    publish_translation_html,
    validate_translation_html,
)
from arc_translate.project import TranslationProject
from arc_translate.workflow import BlocksResult, GlossaryResult, LanguageResult


def test_translation_runtime_is_hidden_and_can_share_an_arc_project(tmp_path) -> None:
    root = tmp_path / "project"
    (root / ".arc" / "companion").mkdir(parents=True)

    project = TranslationProject.open(root)

    assert project.runtime_root == root / ".arc" / "translate"
    assert project.marker == project.runtime_root / "project.json"
    assert not (root / "arc-translate-project.json").exists()
    assert not (project.runtime_root / "paper-cache").exists()


def test_successful_translation_delivery_is_visible_html_not_markdown(tmp_path) -> None:
    project = TranslationProject.open(tmp_path / "project")
    result = BlocksResult(
        document_digest="a" * 64,
        source_digest="b" * 64,
        source_language="en",
        target_language="zh-CN",
        mode="enabled",
        translations=(
            {"block_id": "section:intro", "text": "翻译后的段落。"},
        ),
    )

    delivery = publish_translation_html(
        project,
        run_id="translate-blocks-fixture",
        result=result,
    )

    assert delivery == project.root / "translation.html"
    text = delivery.read_text(encoding="utf-8")
    assert "<!doctype html>" in text.casefold()
    assert "翻译后的段落。" in text
    assert not list(project.root.glob("*.md"))
    validate_translation_html(project, run_id="translate-blocks-fixture")


def test_language_and_glossary_delivery_are_human_readable(tmp_path) -> None:
    project = TranslationProject.open(tmp_path / "project")
    language = LanguageResult(
        document_digest="a" * 64,
        source_digest="b" * 64,
        language_tag="en",
        classification="known",
        confidence=0.99,
        target_language="zh-CN",
        mode="enabled",
    )
    publish_translation_html(project, run_id="language", result=language)
    assert "Source language" in project.delivery_html.read_text(encoding="utf-8")

    glossary = GlossaryResult(
        document_digest="a" * 64,
        source_digest="b" * 64,
        target_language="zh-CN",
        approx_count=1,
        inventory_digest="c" * 64,
        entries=(
            {
                "term_id": "term-1",
                "term": "vacuum",
                "aliases": [],
                "occurrence_count": 1,
                "source_refs": ["section:intro"],
                "matched_sentences": [],
                "preferred_translation": "真空",
                "target_definition": "量子场的基态。",
            },
        ),
    )
    publish_translation_html(project, run_id="glossary", result=glossary)
    rendered = project.delivery_html.read_text(encoding="utf-8")
    assert "vacuum" in rendered
    assert "真空" in rendered
