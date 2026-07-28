# Движок, фабрика сессий и зависимость на запрос
"""Database engine, session factory and request-scoped dependency."""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.echo_sql,
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# выдать сессию БД и закрыть её после запроса
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session and guarantee it is closed afterwards."""
    async with async_session_maker() as session:
        yield session
