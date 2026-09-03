"""`ProspectScope` against a live database.

The static guards in test_isolation_contract.py prove nobody else *can* query
chunks. These prove that when this object does, it returns one prospect's rows
and nobody else's -- with two prospects' chunks sitting in the same table at
the same time, which is the only arrangement where the question is real.

Requires: make up && make migrate.
"""

import os

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from linestack.config import settings  # noqa: E402
from linestack.retrieval.scope import (  # noqa: E402
    ChunkDraft,
    EmbeddingBatch,
    EmbeddingModelMismatch,
    ProspectScope,
)

pytestmark = pytest.mark.integration

DSN = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://linestack:linestack@localhost:5432/linestack",
)
DIM = settings.embedding_dimensions


async def _prospect(db_session, domain: str) -> tuple[int, int]:
    """A prospect with one document. Returns (prospect_id, document_id)."""
    prospect_id = await db_session.scalar(
        text(
            "INSERT INTO prospects (company_name, domain) VALUES (:d, :d) RETURNING id"
        ),
        {"d": domain},
    )
    document_id = await db_session.scalar(
        text(
            "INSERT INTO documents "
            "  (prospect_id, source_url, kind, content_hash, fetched_at) "
            "VALUES (:p, :u, 'website', 'h', now()) RETURNING id"
        ),
        {"p": prospect_id, "u": f"https://{domain}/about"},
    )
    return prospect_id, document_id


def _vector(first: float, second: float) -> list[float]:
    return [first, second] + [0.0] * (DIM - 2)


async def test_a_scope_returns_no_other_prospects_chunks_at_any_k(
    db_session,
) -> None:
    """A1, the whole point, with both prospects' rows in the table at once.

    The other prospect's chunk is deliberately the closer match to the query
    vector. If the filter were missing, it would rank FIRST -- so this fails
    loudly rather than passing by luck of ordering.
    """
    mine_p, mine_d = await _prospect(db_session, "mine.test")
    theirs_p, theirs_d = await _prospect(db_session, "theirs.test")

    mine = ProspectScope(db_session, mine_p)
    theirs = ProspectScope(db_session, theirs_p)

    await mine.replace_document_chunks(mine_d, [ChunkDraft(0, "mine", 5, "website")])
    await theirs.replace_document_chunks(
        theirs_d, [ChunkDraft(0, "theirs", 5, "website")]
    )
    await db_session.flush()

    mine_ids = [c.id for c in await mine.pending_embedding(10)]
    theirs_ids = [c.id for c in await theirs.pending_embedding(10)]

    # theirs is an exact match for the query; mine is orthogonal to it.
    await mine.write_embeddings(
        mine_ids,
        EmbeddingBatch(settings.embedding_model, DIM, [_vector(0.0, 1.0)]),
    )
    await theirs.write_embeddings(
        theirs_ids,
        EmbeddingBatch(settings.embedding_model, DIM, [_vector(1.0, 0.0)]),
    )
    await db_session.flush()

    query = _vector(1.0, 0.0)
    for k in (1, 3, 5, 10):
        results = await mine.top_chunks(query, k=k)
        assert [r.content for r in results] == ["mine"], (
            f"at k={k} the scope returned {[r.content for r in results]}; "
            f"'theirs' is the closer vector and must never appear"
        )


async def test_the_scope_refuses_to_touch_another_prospects_document(
    db_session,
) -> None:
    """The composite foreign key stops a bad INSERT. It does not stop a
    DELETE against another prospect's document, because a delete violates no
    constraint -- so the ownership check is not redundant with it."""
    mine_p, _ = await _prospect(db_session, "mine.test")
    _, theirs_d = await _prospect(db_session, "theirs.test")

    mine = ProspectScope(db_session, mine_p)

    with pytest.raises(PermissionError, match="does not belong to prospect"):
        await mine.replace_document_chunks(
            theirs_d, [ChunkDraft(0, "injected", 5, "website")]
        )


async def test_replacing_chunks_leaves_another_prospects_chunks_alone(
    db_session,
) -> None:
    mine_p, mine_d = await _prospect(db_session, "mine.test")
    theirs_p, theirs_d = await _prospect(db_session, "theirs.test")
    mine = ProspectScope(db_session, mine_p)
    theirs = ProspectScope(db_session, theirs_p)

    await mine.replace_document_chunks(
        mine_d, [ChunkDraft(i, f"m{i}", 5, "website") for i in range(3)]
    )
    await theirs.replace_document_chunks(
        theirs_d, [ChunkDraft(i, f"t{i}", 5, "website") for i in range(2)]
    )
    await db_session.flush()
    assert (await mine.count_chunks(), await theirs.count_chunks()) == (3, 2)

    await mine.replace_document_chunks(mine_d, [ChunkDraft(0, "m-new", 5, "website")])
    await db_session.flush()

    assert await mine.count_chunks() == 1
    assert await theirs.count_chunks() == 2, "a replace crossed prospects"


async def test_open_refuses_a_prospect_embedded_with_another_model(
    db_session,
) -> None:
    """Mixed vector spaces produce plausible bad rankings, not errors.

    Caught when the scope is opened rather than at query time, because a
    mismatch cannot be recovered from mid-query and a wrong ranking looks
    exactly like a chunking problem.
    """
    prospect_id, document_id = await _prospect(db_session, "stale.test")
    scope = ProspectScope(db_session, prospect_id)
    await scope.replace_document_chunks(
        document_id, [ChunkDraft(0, "old", 5, "website")]
    )
    await db_session.flush()

    ids = [c.id for c in await scope.pending_embedding(10)]
    await scope.write_embeddings(
        ids, EmbeddingBatch("text-embedding-ada-002", DIM, [_vector(1.0, 0.0)])
    )
    await db_session.flush()

    with pytest.raises(EmbeddingModelMismatch, match="not comparable"):
        await ProspectScope.open(db_session, prospect_id)


async def test_pending_embedding_reports_only_unembedded_chunks(
    db_session,
) -> None:
    """The embedding pass is resumable: it never pays twice for a chunk."""
    prospect_id, document_id = await _prospect(db_session, "resume.test")
    scope = ProspectScope(db_session, prospect_id)
    await scope.replace_document_chunks(
        document_id, [ChunkDraft(i, f"c{i}", 7, "website") for i in range(3)]
    )
    await db_session.flush()

    assert len(await scope.pending_embedding(10)) == 3
    assert await scope.pending_token_total() == 21

    ids = [c.id for c in await scope.pending_embedding(2)]
    await scope.write_embeddings(
        ids,
        EmbeddingBatch(settings.embedding_model, DIM, [_vector(1.0, 0.0)] * len(ids)),
    )
    await db_session.flush()

    assert len(await scope.pending_embedding(10)) == 1
    assert await scope.pending_token_total() == 7
