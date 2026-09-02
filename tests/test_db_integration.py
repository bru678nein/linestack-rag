"""Does a halfvec actually survive a round trip through this stack?

Everything else in the retrieval slice rests on this. `pgvector==0.5.0` was
pinned, resolved and imported, but importing `HALFVEC` is not evidence that a
1536-dimension vector comes back as the vector that went in.

It does not, and only running this found out why. The first version of
`linestack/db.py` registered `pgvector.asyncpg.register_vector` on every
connection, which is the obvious thing to write. It broke every insert:

    invalid input for query argument $7: '[0.0,0.0,...]' (expected list or
    ndarray)

The two adapters are alternatives, not layers. `pgvector.sqlalchemy.HALFVEC`
already serialises to pgvector's text form; the asyncpg codec then tries to
binary-encode a string. Removing the registration makes the round trip exact.

That is why these tests assert values and ordering rather than "it inserted".
A vector adapter can fail by returning something plausible: wrong similarity
scores look like a bad chunking strategy, not like a codec bug, and would be
chased for days in the wrong file.

Requires: make up && make migrate.
"""

import os

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from linestack.db import create_engine  # noqa: E402
from linestack.models import Chunk  # noqa: E402

pytestmark = pytest.mark.integration

DSN = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://linestack:linestack@localhost:5432/linestack",
)

DIM = 1536


@pytest.fixture
async def session():
    """A session on the linestack database, rolled back afterwards.

    The guard is not ceremony. Port 5432 is a popular default: if another
    project's Postgres owns it and the linestack container comes up without
    publishing a port, this DSN reaches a database that is not ours and the
    test would write into someone else's schema. **[verified]** that exact
    situation occurred on this machine on 2026-09-02.

    Nothing is committed. Every test rolls back, so the database is unchanged
    whether the test passes or fails.
    """
    engine = create_engine(DSN)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            present = await s.scalar(
                text("SELECT to_regclass('public.chunks') IS NOT NULL")
            )
            if not present:
                pytest.skip(
                    f"{DSN} has no `chunks` table, so it is either unmigrated "
                    f"or not the linestack database. Run `make up && make "
                    f"migrate`; if another Postgres owns port 5432, set "
                    f"DATABASE_URL to the port `docker port "
                    f"linestack-rag-postgres-1` reports."
                )
            try:
                yield s
            finally:
                await s.rollback()
    finally:
        await engine.dispose()


async def _seed(session) -> int:
    """A prospect and a document to hang chunks from. Returns document id."""
    prospect_id = await session.scalar(
        text(
            "INSERT INTO prospects (company_name, domain) "
            "VALUES ('Probe', 'halfvec-probe.test') RETURNING id"
        )
    )
    return await session.scalar(
        text(
            "INSERT INTO documents "
            "  (prospect_id, source_url, kind, content_hash, fetched_at) "
            "VALUES (:p, 'https://halfvec-probe.test/a', 'website', 'h', now()) "
            "RETURNING id"
        ),
        {"p": prospect_id},
    ), prospect_id


async def test_a_1536_dimension_halfvec_survives_a_round_trip(session) -> None:
    (document_id, prospect_id) = await _seed(session)

    # Values chosen to be exactly representable in float16 so that the
    # assertion tests the codec, not floating-point rounding.
    vector = [0.5, -0.25, 0.125] + [0.0] * (DIM - 3)

    chunk = Chunk(
        document_id=document_id,
        prospect_id=prospect_id,
        kind="website",
        chunk_index=0,
        content="probe",
        token_count=1,
        embedding=vector,
        embedding_model="text-embedding-3-small",
    )
    session.add(chunk)
    await session.flush()
    session.expunge_all()

    stored = await session.get(Chunk, chunk.id)

    assert stored is not None
    assert len(stored.embedding) == DIM, (
        "dimension changed in transit: the halfvec codec is not registered, "
        "or the column dimension disagrees with settings.embedding_dimensions"
    )
    assert list(stored.embedding[:3]) == pytest.approx([0.5, -0.25, 0.125])

    await session.rollback()


async def test_halfvec_is_half_precision_and_that_is_expected(session) -> None:
    """halfvec is float16. A value needing more precision comes back rounded.

    Recorded rather than worked around: 1536 float16s are half the storage of
    float32 and the precision loss is far below what cosine ranking can
    distinguish. If a future metric ever needs more, this test is where the
    trade-off is written down.
    """
    (document_id, prospect_id) = await _seed(session)

    original = 0.1234567
    chunk = Chunk(
        document_id=document_id,
        prospect_id=prospect_id,
        kind="website",
        chunk_index=0,
        content="probe",
        token_count=1,
        embedding=[original] + [0.0] * (DIM - 1),
        embedding_model="text-embedding-3-small",
    )
    session.add(chunk)
    await session.flush()
    session.expunge_all()

    stored = await session.get(Chunk, chunk.id)
    returned = float(stored.embedding[0])

    assert returned != original, "float16 cannot hold 7 significant digits"
    assert abs(returned - original) < 1e-3, (
        f"{returned} is further from {original} than half precision explains; "
        f"suspect the text codec rather than rounding"
    )

    await session.rollback()


async def test_cosine_distance_orders_by_similarity(session) -> None:
    """The operator ADR-0009's query depends on, exercised end to end.

    A registered-but-wrong codec can still store and return something; what it
    cannot do is produce a sensible distance ordering.
    """
    (document_id, prospect_id) = await _seed(session)

    near = [1.0, 0.0] + [0.0] * (DIM - 2)
    far = [0.0, 1.0] + [0.0] * (DIM - 2)
    for index, vector in enumerate((near, far)):
        session.add(
            Chunk(
                document_id=document_id,
                prospect_id=prospect_id,
                kind="website",
                chunk_index=index,
                content=f"chunk {index}",
                token_count=1,
                embedding=vector,
                embedding_model="text-embedding-3-small",
            )
        )
    await session.flush()

    rows = (
        await session.execute(
            text(
                "SELECT chunk_index, 1 - (embedding <=> :q) AS score "
                "FROM chunks WHERE prospect_id = :p "
                "ORDER BY embedding <=> :q"
            ),
            {"q": str(near), "p": prospect_id},
        )
    ).all()

    assert [r.chunk_index for r in rows] == [0, 1]
    assert rows[0].score == pytest.approx(1.0, abs=1e-3)
    assert rows[1].score == pytest.approx(0.0, abs=1e-3)

    await session.rollback()


async def test_the_database_refuses_a_vector_without_its_model_name(
    session,
) -> None:
    """chunks_embedding_model_paired. Two models' vectors are not comparable,
    and a vector whose model is unknown cannot be excluded from a mixed set."""
    from sqlalchemy.exc import IntegrityError

    (document_id, prospect_id) = await _seed(session)

    session.add(
        Chunk(
            document_id=document_id,
            prospect_id=prospect_id,
            kind="website",
            chunk_index=0,
            content="probe",
            token_count=1,
            embedding=[0.0] * DIM,
            embedding_model=None,
        )
    )

    with pytest.raises(IntegrityError, match="chunks_embedding_model_paired"):
        await session.flush()

    await session.rollback()
