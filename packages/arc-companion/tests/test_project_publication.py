from __future__ import annotations

from pathlib import Path

from arc_companion.project import CompanionProjectPaths


def test_html_promotion_requires_the_run_to_remain_selected(
    tmp_path: Path,
) -> None:
    paths = CompanionProjectPaths.open(tmp_path / "project")
    paths.select_run("run-a")
    run_a = paths.publication_html("run-a")
    run_a.parent.mkdir(parents=True)
    run_a.write_bytes(b"run a")

    paths.select_run("run-b")

    assert not paths.promote_publication_html("run-a")
    assert not paths.delivery_html.exists()

    run_b = paths.publication_html("run-b")
    run_b.parent.mkdir(parents=True)
    run_b.write_bytes(b"run b")
    assert paths.promote_publication_html("run-b")
    assert paths.delivery_html.read_bytes() == b"run b"
