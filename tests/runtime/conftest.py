"""Shared fixtures for runtime database/repository tests.

These tests deliberately avoid pytest-asyncio: each test drives async scenarios
through ``asyncio.run`` and treats the database as a synchronous black box.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh application-database path inside the test's tmp directory."""
    return tmp_path / "app.sqlite"
