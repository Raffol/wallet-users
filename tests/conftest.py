# Общие фикстуры тестов
"""Shared test fixtures.

By default tests run against an in-memory SQLite database so they need zero
setup. Point ``TEST_DATABASE_URL`` at a Postgres DSN to run the same suite
(including the row-lock concurrency test) against the real engine::

    export TEST_DATABASE_URL=\
        postgresql+asyncpg://wallet:wallet@localhost:5432/wallet_test
    pytest
"""
import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.database import get_session
from app.main import app
from app.models import Base

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "sqlite+aiosqlite:///:memory:",
)
IS_SQLITE = TEST_DATABASE_URL.startswith("sqlite")


# Создать тестовый движок (SQLite или Postgres)
def _create_engine():
    if IS_SQLITE:
        # StaticPool keeps a single connection so the in-memory DB persists
        # across sessions within one test.
        return create_async_engine(
            TEST_DATABASE_URL,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    # NullPool: every checkout opens a fresh connection, so concurrent
    # requests genuinely contend for the wallet row lock.
    return create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


# Поднять и снести схему вокруг теста
@pytest_asyncio.fixture
async def engine():
    eng = _create_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


# HTTP-клиент с подменённой сессией БД
@pytest_asyncio.fixture
async def client(engine):
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def _override_get_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()
