"""Async PostgreSQL engine and session factory for BFF-owned data."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_bff_engine(database_url: str) -> AsyncEngine:
    """Create an async engine without opening a connection during module import."""

    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create transaction-scoped sessions with explicit commit behavior."""

    return async_sessionmaker(engine, expire_on_commit=False)
