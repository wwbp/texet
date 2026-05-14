"""Overrides for pure unit tests that need no database or network."""
import pytest


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db() -> None:  # type: ignore[override]
    pass


@pytest.fixture(scope="session", autouse=True)
def block_external_network() -> None:  # type: ignore[override]
    pass
