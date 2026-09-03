"""Responsibility: being the only place in the codebase that builds a query
against the `chunks` table.

This is the A1 chokepoint (docs/architecture.md section 4.2). The object here
takes a prospect_id in its constructor, and every retrieval function takes that
object rather than a raw session. A query that does not go through it does not
exist, because there is no other function that returns chunk rows.

This is the weaker of the two isolation mechanisms and it is deliberately
second. The stronger one is in the database: chunks(document_id, prospect_id)
is a composite foreign key onto documents(id, prospect_id), so a chunk whose
prospect_id disagrees with its document's cannot be inserted at all.

If you are about to write SQL against `chunks` in another module, that is the
trigger recorded in docs/open-questions.md section 3.3 for adopting row-level
security. Read it before proceeding.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from linestack.config import settings

# --------------------------------------------------------------------------- #
# The frozen ranking query (ADR-0009)
# --------------------------------------------------------------------------- #
# Cosine distance, exact search, single stage. No reranking, no hybrid, no
# source weighting -- each of those ships separately, one at a time, with a
# recorded before-and-after on the evaluation set (A3), because their effects
# are not separable afterwards.
#
# A unit test asserts this string, whitespace-normalised, against the text in
# ADR-0009. That test is what stops a filter or a second stage being added here
# without the ADR being changed to match.
RANKING_SQL = """
SELECT id, content, kind, 1 - (embedding <=> :q) AS score
  FROM chunks
 WHERE prospect_id = :prospect_id
 ORDER BY embedding <=> :q
 LIMIT :k
"""


@dataclass(frozen=True)
class ScoredChunk:
    """One retrieved chunk and the score that retrieved it.

    The score travels outward unchanged, all the way to the UI. A retrieval
    failure that is not visible gets attributed to the model, and then someone
    spends a week tuning prompts to fix a chunking bug (A8).
    """

    id: int
    content: str
    kind: str
    score: float


@dataclass(frozen=True)
class PendingChunk:
    """A chunk written but not yet embedded. `chunks.embedding` is nullable by
    design, so the embedding pass is separate and resumable."""

    id: int
    content: str
    token_count: int


@dataclass(frozen=True)
class ChunkDraft:
    """A chunk before it has an id or a vector. Produced by chunking."""

    chunk_index: int
    content: str
    token_count: int
    kind: str


@dataclass(frozen=True)
class EmbeddingBatch:
    """Vectors and the model that produced them, as one indivisible value.

    Never a bare list of vectors. Two models' vectors are not comparable, and
    mixing them silently degrades retrieval in a way that looks like a chunking
    problem. The database enforces the pairing with a CHECK constraint; this
    type makes the unpaired case unrepresentable in Python, so the constraint
    never has to fire.
    """

    model: str
    dimensions: int
    vectors: list[list[float]]


class EmbeddingModelMismatch(RuntimeError):
    """Raised when a prospect's stored vectors came from another model."""


class ProspectScope:
    """Every chunk query in the system, bound to one prospect.

    `prospect_id` is taken in the constructor and never appears as a method
    argument. That is the point: there is no call site where the wrong id can
    be passed, because there is no parameter to pass it to.
    """

    def __init__(self, session: AsyncSession, prospect_id: int) -> None:
        self._session = session
        self._prospect_id = prospect_id

    @property
    def prospect_id(self) -> int:
        return self._prospect_id

    @classmethod
    async def open(cls, session: AsyncSession, prospect_id: int) -> ProspectScope:
        """Bind a scope, refusing one whose vectors are from another model.

        Checked here rather than at query time because a mismatch cannot be
        recovered from mid-query, and because a silently mixed vector space
        produces plausible-looking bad rankings rather than an error.
        """
        scope = cls(session, prospect_id)
        models = await scope._distinct_embedding_models()
        unexpected = models - {settings.embedding_model}
        if unexpected:
            raise EmbeddingModelMismatch(
                f"prospect {prospect_id} has chunks embedded with "
                f"{sorted(unexpected)}, but settings.embedding_model is "
                f"{settings.embedding_model!r}. Vectors from two models are "
                f"not comparable. Re-embed this prospect, or change the "
                f"setting back."
            )
        return scope

    # -- read ------------------------------------------------------------

    async def top_chunks(
        self, query_vector: list[float], k: int | None = None
    ) -> list[ScoredChunk]:
        """The ADR-0009 query, verbatim. Nothing is added to it here."""
        rows = (
            await self._session.execute(
                text(RANKING_SQL),
                {
                    "q": _as_vector_literal(query_vector),
                    "prospect_id": self._prospect_id,
                    "k": k if k is not None else settings.retrieval_top_k,
                },
            )
        ).all()
        return [
            ScoredChunk(id=r.id, content=r.content, kind=r.kind, score=float(r.score))
            for r in rows
        ]

    async def source_urls(self, chunk_ids: list[int]) -> dict[int, str]:
        """Map chunk ids to the document URLs they came from.

        A SEPARATE statement, deliberately. ADR-0009 freezes the ranking query
        at `id, content, kind, score`, so a search result cannot carry a
        citation on its own -- and widening that SELECT to add one would edit a
        decision this project treats as frozen. Resolving the URLs in a second
        lookup keeps the ranking query exactly what the ADR says while still
        letting a caller show where a chunk came from.

        Scoped like everything else here: a chunk belonging to another prospect
        cannot be resolved through this object.
        """
        if not chunk_ids:
            return {}
        rows = (
            await self._session.execute(
                text(
                    "SELECT c.id, d.source_url FROM chunks c "
                    "  JOIN documents d ON d.id = c.document_id "
                    " WHERE c.prospect_id = :p AND c.id = ANY(:ids)"
                ),
                {"p": self._prospect_id, "ids": chunk_ids},
            )
        ).all()
        return {row.id: row.source_url for row in rows}

    async def count_embedded(self) -> int:
        """How many of this prospect's chunks already have a vector.

        Lives here rather than at the call site because it is a chunk query,
        and the chokepoint is not a style preference. The first version of
        `ask.py` ran this SELECT inline and the static guard failed the build
        -- correctly. The fix for that failure is always to move the query
        here, never to add the module to an allowlist.
        """
        return int(
            await self._session.scalar(
                text(
                    "SELECT count(*) FROM chunks "
                    " WHERE prospect_id = :p AND embedding IS NOT NULL"
                ),
                {"p": self._prospect_id},
            )
        )

    async def count_chunks(self) -> int:
        return int(
            await self._session.scalar(
                text("SELECT count(*) FROM chunks WHERE prospect_id = :p"),
                {"p": self._prospect_id},
            )
        )

    async def pending_embedding(self, limit: int) -> list[PendingChunk]:
        """Chunks written but not yet embedded, oldest first.

        The embedding pass resumes from here after a crash: it re-embeds only
        what was never embedded, and never pays twice for the same chunk.
        """
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, content, token_count FROM chunks "
                    " WHERE prospect_id = :p AND embedding IS NULL "
                    " ORDER BY id LIMIT :limit"
                ),
                {"p": self._prospect_id, "limit": limit},
            )
        ).all()
        return [
            PendingChunk(id=r.id, content=r.content, token_count=r.token_count)
            for r in rows
        ]

    async def pending_token_total(self) -> int:
        """Total tokens still to embed. What --dry-run reports before spending."""
        return int(
            await self._session.scalar(
                text(
                    "SELECT coalesce(sum(token_count), 0) FROM chunks "
                    " WHERE prospect_id = :p AND embedding IS NULL"
                ),
                {"p": self._prospect_id},
            )
        )

    async def _distinct_embedding_models(self) -> set[str]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT DISTINCT embedding_model FROM chunks "
                    " WHERE prospect_id = :p AND embedding_model IS NOT NULL"
                ),
                {"p": self._prospect_id},
            )
        ).all()
        return {r.embedding_model for r in rows}

    # -- write -----------------------------------------------------------
    # The loader owns the DECISION of what to write and when to skip; this
    # object owns the SQL. Splitting it the other way would put chunk queries
    # in two modules, which is the thing the chokepoint exists to prevent.

    async def replace_document_chunks(
        self, document_id: int, drafts: list[ChunkDraft]
    ) -> int:
        """Replace one document's chunks. Returns the number written.

        The ownership assertion is not redundant with the composite foreign
        key. The key stops a bad INSERT; it does not stop a DELETE against
        another prospect's document, because a delete violates no constraint.
        """
        owned = await self._session.scalar(
            text("SELECT 1 FROM documents  WHERE id = :d AND prospect_id = :p"),
            {"d": document_id, "p": self._prospect_id},
        )
        if not owned:
            raise PermissionError(
                f"document {document_id} does not belong to prospect "
                f"{self._prospect_id}. Refusing to touch its chunks."
            )

        await self._session.execute(
            text("DELETE FROM chunks WHERE document_id = :d AND prospect_id = :p"),
            {"d": document_id, "p": self._prospect_id},
        )
        if not drafts:
            return 0

        await self._session.execute(
            text(
                "INSERT INTO chunks "
                "  (document_id, prospect_id, kind, chunk_index, content, "
                "   token_count) "
                "VALUES (:d, :p, CAST(:kind AS document_kind), :i, :content, "
                "        :tokens)"
            ),
            [
                {
                    "d": document_id,
                    "p": self._prospect_id,
                    "kind": draft.kind,
                    "i": draft.chunk_index,
                    "content": draft.content,
                    "tokens": draft.token_count,
                }
                for draft in drafts
            ],
        )
        return len(drafts)

    async def write_embeddings(
        self, chunk_ids: list[int], batch: EmbeddingBatch
    ) -> int:
        """Attach vectors to chunks. Takes an EmbeddingBatch, never a list.

        There is deliberately no overload accepting bare vectors: the model
        name travels with them or they do not travel.
        """
        if len(chunk_ids) != len(batch.vectors):
            raise ValueError(
                f"{len(chunk_ids)} chunks but {len(batch.vectors)} vectors; "
                f"refusing to guess which vector belongs to which chunk"
            )
        for vector in batch.vectors:
            if len(vector) != batch.dimensions:
                raise ValueError(
                    f"expected {batch.dimensions}-dimension vectors, got "
                    f"{len(vector)}. Check settings.embedding_dimensions "
                    f"against the model that produced these."
                )
        if not chunk_ids:
            return 0

        await self._session.execute(
            text(
                "UPDATE chunks SET embedding = CAST(:v AS halfvec), "
                "       embedding_model = :model "
                " WHERE id = :id AND prospect_id = :p"
            ),
            [
                {
                    "id": chunk_id,
                    "p": self._prospect_id,
                    "v": _as_vector_literal(vector),
                    "model": batch.model,
                }
                # strict=True: the lengths were checked above, and a
                # silent truncation here would leave chunks unembedded
                # while reporting success.
                for chunk_id, vector in zip(chunk_ids, batch.vectors, strict=True)
            ],
        )
        return len(chunk_ids)


def _as_vector_literal(vector: list[float]) -> str:
    """pgvector's text representation.

    Deliberately text, not a bound list. `linestack/db.py` explains why the
    asyncpg binary codec is not registered: HALFVEC and the asyncpg codec are
    alternatives, not layers, and registering both breaks every insert.
    """
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"
