from unittest.mock import AsyncMock

import asyncpg
import pytest

from tests import prepare_db


async def test_creates_test_database_and_closes_connection(monkeypatch):
    connection = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://catalog:secret@db/catalog_test")
    monkeypatch.setattr(prepare_db.asyncpg, "connect", connect)
    await prepare_db.main()
    connect.assert_awaited_once_with("postgresql://catalog:secret@db/postgres", timeout=5)
    connection.execute.assert_awaited_once_with("CREATE DATABASE catalog_test")
    connection.close.assert_awaited_once()


async def test_existing_test_database_is_allowed(monkeypatch):
    connection = AsyncMock()
    connection.execute.side_effect = asyncpg.DuplicateDatabaseError()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://catalog@db/catalog_test")
    monkeypatch.setattr(prepare_db.asyncpg, "connect", AsyncMock(return_value=connection))
    await prepare_db.main()
    connection.close.assert_awaited_once()


@pytest.mark.parametrize("database", ["catalog", "catalog_test_backup"])
async def test_refuses_other_database_names(monkeypatch, database):
    connect = AsyncMock()
    monkeypatch.setenv("TEST_DATABASE_URL", f"postgresql+asyncpg://catalog@db/{database}")
    monkeypatch.setattr(prepare_db.asyncpg, "connect", connect)
    with pytest.raises(ValueError, match="exact catalog_test"):
        await prepare_db.main()
    connect.assert_not_awaited()
