"""客户资料服务的异步 PostgreSQL 连接工厂。"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_business_engine(database_url: str) -> AsyncEngine:
    """Create an async engine without connecting during module import."""

    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return short-lived sessions with explicit transaction behavior."""

    return async_sessionmaker(engine, expire_on_commit=False)
