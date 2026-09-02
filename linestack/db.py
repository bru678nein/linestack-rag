"""Responsibility: the async SQLAlchemy engine and session factory, and the
pgvector type registration that makes halfvec round-trip correctly.

Owns: engine construction from config, session lifecycle, and the FastAPI
dependency that yields a session.

Does not own: any query. Queries against `chunks` live in
linestack/retrieval/scope.py and nowhere else, because that is one of the
mechanisms enforcing A1 (docs/architecture.md section 4.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from linestack.config import settings

# --------------------------------------------------------------------------- #
# On NOT registering the asyncpg vector codec
# --------------------------------------------------------------------------- #
# The obvious thing to write here is a `connect` event that calls
# pgvector.asyncpg.register_vector on every raw connection. It was written,
# and it BROKE the round trip. **[verified]** 2026-09-02:
#
#   invalid input for query argument $7: '[0.0,0.0,...]' (expected list or
#   ndarray)
#
# The two adapters are alternatives, not layers. `pgvector.sqlalchemy.HALFVEC`
# already serialises a Python list to pgvector's text representation on the way
# out and parses it on the way back. Registering the asyncpg codec as well
# tells asyncpg to binary-encode a value SQLAlchemy has already turned into a
# string, and the codec rejects it.
#
# Removing the registration makes the round trip exact: 1536 floats in, 1536
# floats out, cosine similarity 1.0 against itself. Verified by
# tests/test_db_integration.py against pgvector 0.8.6 on PostgreSQL 17.
#
# The asyncpg codec is for code that talks to asyncpg directly, without
# SQLAlchemy. Nothing in this project does. If something ever must, register it
# on that connection only -- never on this engine.
# --------------------------------------------------------------------------- #


def create_engine(url: str | None = None) -> AsyncEngine:
    """Build the async engine. Vector handling comes from the column type."""
    return create_async_engine(
        url or settings.database_url,
        pool_pre_ping=True,
        future=True,
    )


engine: AsyncEngine = create_engine()

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Declared now, unused until the API slice."""
    async with session_factory() as session:
        yield session
