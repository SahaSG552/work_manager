"""Shared test fixtures — in-memory DB, tmpdir vault, disabled LLM."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# ── Disable LLM for all tests (force regex fallback) ──────────────────
os.environ["LLM_API_KEY"] = ""


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _reset_obsidian_caches():
    """Reset obsidian index caches between tests."""
    from app.obsidian import refresh_index, _prefix_to_folder
    import app.obsidian as _obs

    refresh_index()
    _obs._prefix_to_folder = None
    yield
    refresh_index()
    _obs._prefix_to_folder = None


@pytest.fixture
def tmp_vault(tmp_path):
    """Create a temporary Obsidian vault with owner folders."""
    vault = tmp_path / "Vault"
    for folder in ("KK", "AM", "KH", "YG"):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    return vault


@pytest.fixture
def tmp_working(tmp_path):
    """Create a temporary YandexDisk working directory."""
    wd = tmp_path / "YandexDisk"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create an in-memory test database (overrides DB_PATH)."""
    import app.database as _db

    test_db_path = tmp_path / "test_orders.db"
    original = _db.DB_PATH
    _db.DB_PATH = test_db_path
    await _db.init_db()
    yield _db
    _db.DB_PATH = original


@pytest_asyncio.fixture
async def settings_db(tmp_path):
    """Create a test settings database."""
    import app.settings_db as _sdb

    test_db_path = tmp_path / "test_settings.db"
    original = _sdb._DB_PATH
    _sdb._DB_PATH = test_db_path
    yield _sdb
    _sdb._DB_PATH = original
