"""Responsibility: asking one prospect a question from the command line, and
showing what retrieval returned.

Owns: the smallest useful end-to-end path -- resolve a prospect, embed the
question, rank its chunks, print the hits with their scores and source URLs.

Does not own: generation. There is no model writing prose here and that is the
point at this stage of the build. A3 puts a working naive retriever, then
ground truth, then the harness, ahead of any generation work; and A8 says the
first hypothesis for a wrong answer is that the right chunk was never
retrieved. This command is how that hypothesis gets tested by eye, cheaply,
before anyone spends a week tuning a prompt to fix a chunking bug.

Costs one embedding call per question -- roughly 20 tokens, a rounding error
against the corpus itself. Nothing is written to the database.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from linestack.config import settings
from linestack.retrieval.embedding import (
    EmbedReport,
    LocalEmbedder,
    build_client,
    embed_texts,
)
from linestack.retrieval.scope import ProspectScope
from linestack.retrieval.search import format_hits, search


async def ask(
    session,
    domain: str,
    question: str,
    *,
    k: int | None = None,
    client=None,
) -> list[str]:
    """Answer nothing; show the evidence. Returns lines ready to print."""
    prospect_id = await session.scalar(
        text("SELECT id FROM prospects WHERE domain = :d"),
        {"d": domain.lower()},
    )
    if prospect_id is None:
        return [
            f"  no prospect with domain {domain!r}.",
            "  Crawl and load one first:",
            f"    make crawl DOMAIN={domain}",
            f"    make load ARTIFACTS=prospect_{domain.replace('.', '_')}.json",
        ]

    scope = await ProspectScope.open(session, prospect_id)

    embedded = await scope.count_embedded()
    if not embedded:
        total = await scope.count_chunks()
        return [
            f"  {domain} has {total} chunks and none of them are embedded.",
            "  Retrieval cannot rank what has no vector. Run:",
            f"    make embed PROSPECT={domain} DRY=1   # states the cost first",
            f"    make embed PROSPECT={domain}",
        ]

    if client is None:
        client = build_client()

    # A local bge model wants an instruction on the query side and none on the
    # documents. Asking it the same way the chunks were embedded ranks worse,
    # and does so silently.
    if isinstance(client, LocalEmbedder):
        query_vector = client.embed_query(question)
    else:
        query_vector = (await embed_texts(client, [question], EmbedReport()))[0]

    hits = await search(scope, query_vector, k=k)
    urls = await scope.source_urls([hit.id for hit in hits])

    lines = [
        f"  prospect:  {domain} (id {prospect_id}), {embedded} embedded chunks",
        f"  question:  {question}",
        f"  model:     {settings.embedding_model}",
        "",
        *format_hits(hits, urls),
    ]
    if hits:
        lines += [
            "",
            "  These are retrieved chunks, not an answer. Nothing here was "
            "written by a model.",
        ]
    return lines


async def _main(argv: list[str]) -> int:
    import argparse

    from linestack.db import session_factory

    parser = argparse.ArgumentParser(prog="linestack.retrieval.ask")
    parser.add_argument("--prospect", required=True, help="domain, e.g. fly.io")
    parser.add_argument("--question", required=True)
    parser.add_argument("-k", type=int, default=None, help="chunks to return")
    args = parser.parse_args(argv)

    async with session_factory() as session:
        for line in await ask(session, args.prospect, args.question, k=args.k):
            print(line)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
