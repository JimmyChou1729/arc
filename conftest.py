"""Keep operator-selected Host and report profiles out of generic tests."""
import pytest


@pytest.fixture(autouse=True)
def isolated_operator_profiles(monkeypatch):
    # Host/report tests explicitly select their own profiles with monkeypatch.
    for name in ("AC_LLM_HOST_COORDINATOR", "ARC_REPORT_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
