"""Test configuration.

`ingest.py` lives at the repository root rather than inside the package
(ADR-0010), so the root has to be importable before the ingestion tests can
reach it. This is the whole cost of that decision, and it disappears when the
module moves.

Also holds the one database session fixture the integration tests share.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DSN = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://linestack:linestack@localhost:5432/linestack",
)


def _unusable(reason: str) -> str:
    return (
        f"{DSN} is not a usable linestack database: {reason}.\n"
        f"Run `make up && make migrate`. If another Postgres already owns "
        f"port 5432 — that is common, and it happened on this machine on "
        f"2026-09-02 — set BOTH DATABASE_URL and DATABASE_URL_SYNC to the "
        f"port `docker port linestack-rag-postgres-1` reports. They are "
        f"separate variables and setting only one leaves the other pointing "
        f"at the default."
    )


@pytest.fixture
async def db_session():
    """A session on the linestack database, always rolled back.

    Two failure modes, both of which must produce a readable skip rather than
    a stack trace, because both are configuration rather than defects:

    - the DSN reaches no database, or one that rejects our credentials;
    - it reaches a database that is not ours, with no `chunks` table.

    The second is the dangerous one. Port 5432 is a popular default, and a
    test that finds someone else's Postgres there would write into their
    schema. Guarding only the second was not enough: the first is the more
    likely mistake and it used to surface as a raw asyncpg traceback.

    Nothing is committed. Every test rolls back, pass or fail.
    """
    pytest.importorskip("asyncpg")
    pytest.importorskip("sqlalchemy")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from linestack.db import create_engine

    engine = create_engine(DSN)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        try:
            async with factory() as probe:
                has_chunks = await probe.scalar(
                    text("SELECT to_regclass('public.chunks') IS NOT NULL")
                )
        except Exception as exc:  # connection refused, auth, wrong database
            pytest.skip(_unusable(f"{type(exc).__name__}: {exc}"[:200]))

        if not has_chunks:
            pytest.skip(_unusable("no `chunks` table — unmigrated, or not ours"))

        async with factory() as session:
            try:
                yield session
            finally:
                await session.rollback()
    finally:
        await engine.dispose()
