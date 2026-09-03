"""Responsibility: ranking chunks for a question inside a single prospect scope.

The first implementation is deliberately naive: cosine distance, exact search
under the prospect filter, single stage, no reranking, no query rewriting, no
source weighting (ADR-0009). There is no HNSW index; the candidate set after
the prospect filter is small enough for exact search, which additionally has
perfect recall (ADR-0001).

Hybrid search, source weighting by kind, and reranking are planned in that
order, and each ships only with a recorded before-and-after on the evaluation
set (A3). Do not add two at once: their effects are not separable afterwards.

Every result carries its score outward, unchanged, all the way to the UI. A
retrieval failure that is not visible gets attributed to the model.

This module is deliberately thin, and that is the design rather than an
omission. The ranking SQL lives in scope.py because that is the one place
allowed to query chunks (A1), so what is left here is: order the call, carry
the scores out untouched, and be the obvious place a second stage would be
added -- where a static test will notice it.
"""

from __future__ import annotations

from linestack.config import settings
from linestack.retrieval.scope import ProspectScope, ScoredChunk


async def search(
    scope: ProspectScope,
    query_vector: list[float],
    k: int | None = None,
) -> list[ScoredChunk]:
    """Rank one prospect's chunks against an already-embedded question.

    Takes a `ProspectScope`, never a database handle. The scope carries the
    prospect filter, so there is no call site here at which it can be lost --
    which is the whole reason the scope exists rather than a helper that takes
    a prospect id alongside a connection.

    The vector is passed in rather than embedded here: embedding costs money
    and belongs in one place (`linestack/retrieval/embedding.py`), and a search
    function that silently spent money would be a poor neighbour to a project
    whose discipline is stating cost before spending it.
    """
    return await scope.top_chunks(
        query_vector, k=k if k is not None else settings.retrieval_top_k
    )


def format_hits(
    hits: list[ScoredChunk],
    urls: dict[int, str] | None = None,
    width: int = 96,
) -> list[str]:
    """Render results for a human, score first.

    Score first, always, and never rounded away. The scores ARE the retrieval
    signal: a wrong answer with a 0.9 top score and a wrong answer with a 0.2
    top score are different failures with different fixes, and hiding the
    number turns both into "the model got it wrong" (A8).
    """
    if not hits:
        return [
            "  no chunks returned.",
            "  Either this prospect has no embedded chunks yet "
            "(make embed PROSPECT=...), or the filter matched nothing.",
        ]

    urls = urls or {}
    lines = []
    for rank, hit in enumerate(hits, start=1):
        body = " ".join(hit.content.split())
        source = urls.get(hit.id, "(source url not resolved)")
        lines.append(f"  {rank}. score {hit.score:.4f}  [{hit.kind}]  {source}")
        lines.append(f"     {body[:width]}")
    return lines
