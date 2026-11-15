from __future__ import annotations

import argus_iris


def test_version_is_exposed() -> None:
    assert isinstance(argus_iris.__version__, str)
    assert argus_iris.__version__
