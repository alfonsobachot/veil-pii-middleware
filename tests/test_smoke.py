"""Smoke test: the package imports and exposes its version."""

import veil_pii_middleware


def test_package_exposes_version() -> None:
    assert veil_pii_middleware.__version__ == "0.1.0"
