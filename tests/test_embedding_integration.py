"""The embedding pass against a live database, with a fake client.

No API key and no spending. What needs proving here is the plumbing either side
of the request -- that vectors reach the right chunks with their model name,
that a dry run writes nothing, and that a second run costs nothing -- and none
of that requires a real model.

Requires: make up && make migrate.
"""

import datetime as dt
import itertools
from pathlib import Path

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from linestack.config import settings  # noqa: E402
from linestack.ingestion.loader import load_artifact, read_artifact  # noqa: E402
from linestack.retrieval.embedding import embed_prospect  # noqa: E402
from linestack.retrieval.scope import (  # noqa: E402
    EmbeddingModelMismatch,
    ProspectScope,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 3, tzinfo=dt.UTC)
DIM = settings.embedding_dimensions
_SEQUENCE = itertools.count(1000)


class _FakeClient:
    """Returns deterministic vectors and counts what it was asked for."""

    def __init__(self, dimensions: int = DIM) -> None:
        self.calls: list[list[str]] = []
        self._dimensions = dimensions
        self.embeddings = self

    async def create(self, *, model: str, input: list[str]):
        self.calls.append(list(input))
        return type(
            "Response",
            (),
            {
                "data": [
                    type(
                        "Item",
                        (),
                        {
                            "embedding": [float(len(t) % 7)]
                            + [0.0] * (self._dimensions - 1)
                        },
                    )()
                    for t in input
                ]
            },
        )()


async def _loaded(db_session, name: str = "prospect_thoughtbot_com.json"):
    """A small prospect, loaded with documents and chunks, uniquely named."""
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is gitignored and not on disk; re-crawl to restore")
    artifact = read_artifact(path)
    nth = next(_SEQUENCE)
    artifact.domain = f"embed-{nth}-{artifact.domain}"
    artifact.crawled_at = (
        artifact.crawled_at_utc - dt.timedelta(seconds=nth)
    ).isoformat()
    # Three documents is enough to exercise batching without minutes of setup.
    artifact.documents = artifact.documents[:3]
    report = await load_artifact(db_session, artifact, now=NOW)
    await db_session.flush()
    return report.prospect_id


async def test_a_dry_run_makes_no_request_and_writes_nothing(db_session) -> None:
    prospect_id = await _loaded(db_session)
    client = _FakeClient()

    report = await embed_prospect(db_session, prospect_id, client=client, dry_run=True)

    assert client.calls == [], "a dry run called the API"
    assert report.pending_chunks > 0
    assert report.pending_tokens > 0
    assert report.chunks_embedded == 0

    still_null = await db_session.scalar(
        text(
            "SELECT count(*) FROM chunks WHERE prospect_id = :p   AND embedding IS NULL"
        ),
        {"p": prospect_id},
    )
    assert still_null == report.pending_chunks


async def test_every_chunk_gets_a_vector_and_its_model_name(db_session) -> None:
    prospect_id = await _loaded(db_session)
    client = _FakeClient()

    report = await embed_prospect(db_session, prospect_id, client=client, commit=False)

    assert report.chunks_embedded == report.pending_chunks
    assert client.calls, "nothing was sent"

    rows = (
        await db_session.execute(
            text(
                "SELECT count(*) total, "
                "  count(*) FILTER (WHERE embedding IS NULL) unembedded, "
                "  count(DISTINCT embedding_model) models "
                " FROM chunks WHERE prospect_id = :p"
            ),
            {"p": prospect_id},
        )
    ).one()
    assert rows.unembedded == 0
    assert rows.models == 1


async def test_a_second_run_finds_nothing_pending_and_sends_nothing(
    db_session,
) -> None:
    """Resumable by construction: the work list is WHERE embedding IS NULL, so
    a crash re-embeds only what was never embedded and never pays twice."""
    prospect_id = await _loaded(db_session)
    await embed_prospect(db_session, prospect_id, client=_FakeClient(), commit=False)

    second_client = _FakeClient()
    report = await embed_prospect(
        db_session, prospect_id, client=second_client, commit=False
    )

    assert report.pending_chunks == 0
    assert second_client.calls == [], "a fully embedded prospect was re-sent"


async def test_a_partially_embedded_prospect_resumes_from_where_it_stopped(
    db_session,
) -> None:
    prospect_id = await _loaded(db_session)
    scope = ProspectScope(db_session, prospect_id)
    total = await scope.count_chunks()

    await embed_prospect(
        db_session, prospect_id, client=_FakeClient(), limit=2, commit=False
    )

    client = _FakeClient()
    report = await embed_prospect(db_session, prospect_id, client=client, commit=False)

    assert report.pending_chunks == total - 2
    sent = sum(len(call) for call in client.calls)
    assert sent == total - 2, "the resumed run re-sent chunks already embedded"


async def test_a_prospect_embedded_with_another_model_is_refused(
    db_session,
) -> None:
    """Mixing vector spaces produces plausible bad rankings, not an error.

    Refused when the scope opens, before any request is made, because a
    mismatch cannot be recovered from mid-pass and a wrong ranking looks
    exactly like a chunking problem.
    """
    prospect_id = await _loaded(db_session)
    scope = ProspectScope(db_session, prospect_id)
    pending = await scope.pending_embedding(1)
    from linestack.retrieval.scope import EmbeddingBatch

    await scope.write_embeddings(
        [pending[0].id],
        EmbeddingBatch("text-embedding-ada-002", DIM, [[0.1] + [0.0] * (DIM - 1)]),
    )
    await db_session.flush()

    client = _FakeClient()
    with pytest.raises(EmbeddingModelMismatch, match="not comparable"):
        await embed_prospect(db_session, prospect_id, client=client, commit=False)

    assert client.calls == [], "a request was sent before the mismatch was caught"


async def test_a_wrong_dimension_is_caught_before_the_database_sees_it(
    db_session,
) -> None:
    """Otherwise it surfaces as an opaque Postgres cast error instead of
    "config says 1536, the model returned 8"."""
    prospect_id = await _loaded(db_session)
    client = _FakeClient(dimensions=8)

    with pytest.raises(ValueError, match=f"{DIM}-dimension"):
        await embed_prospect(db_session, prospect_id, client=client, commit=False)


async def test_embedded_chunks_are_searchable_within_their_prospect(
    db_session,
) -> None:
    """The end of the slice, in one assertion: load, chunk, embed, retrieve."""
    prospect_id = await _loaded(db_session)
    await embed_prospect(db_session, prospect_id, client=_FakeClient(), commit=False)

    scope = await ProspectScope.open(db_session, prospect_id)
    hits = await scope.top_chunks([1.0] + [0.0] * (DIM - 1), k=3)

    assert hits, "nothing came back from a fully embedded prospect"
    assert all(isinstance(h.score, float) for h in hits)
    owners = await db_session.execute(
        text("SELECT DISTINCT prospect_id FROM chunks WHERE id = ANY(:ids)"),
        {"ids": [h.id for h in hits]},
    )
    assert {r[0] for r in owners} == {prospect_id}
