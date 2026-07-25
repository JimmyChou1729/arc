from __future__ import annotations

import re
from pathlib import Path


DOMAIN_TEST_ROOT = Path("packages/arc-domain/tests")
NETWORK_OPT_IN = "ARC_RUN_NET_TESTS"
EXPENSIVE_MARKER = re.compile(
    r"pytest\.mark\.(?:integration|network|slow|live)(?:_|\b)"
)


def test_expensive_domain_tests_are_opt_in() -> None:
    """Keep current and future domain live tests out of the default suite."""

    test_files = sorted(DOMAIN_TEST_ROOT.rglob("test_*.py"))
    assert test_files, "arc-domain test suite is unexpectedly empty"

    violations = []
    for path in test_files:
        source = path.read_text(encoding="utf-8")
        in_live_directory = "live" in path.relative_to(DOMAIN_TEST_ROOT).parts
        if (in_live_directory or EXPENSIVE_MARKER.search(source)) and not _has_opt_in(source):
            violations.append(str(path))

    assert not violations, (
        "live, network, slow, and integration arc-domain tests must be guarded "
        f"by pytest.mark.skipif(... {NETWORK_OPT_IN}=1 ...): "
        + ", ".join(violations)
    )


def _has_opt_in(source: str) -> bool:
    return "pytest.mark.skipif" in source and NETWORK_OPT_IN in source
