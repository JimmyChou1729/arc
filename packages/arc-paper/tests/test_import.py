def test_package_imports():
    import arc_paper
    from pathlib import Path

    version = (Path(__file__).resolve().parents[3] / "VERSION").read_text().strip()
    assert arc_paper.__version__ == version
