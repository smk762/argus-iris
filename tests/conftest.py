"""Suite-wide test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_suite_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's suite file (~/.config/argus/suite.env) is never read by a test."""
    monkeypatch.setenv("ARGUS_ENV_FILE", "off")
