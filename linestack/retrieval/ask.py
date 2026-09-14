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

An evaluated question can be asked by id. It is then asked in the ground-truth
set's words and searched with the query written for it (ADR-0025), so the
ranking shown is the one answer() and the harness get. Asked as free text, the
same question searches with its own words, which is a different ranking -- and
until the id existed it was the only one this command could show. `full` ranks
every embedded chunk of the prospect, for the page at rank 14 that a cut at 10
hides.

Costs one embedding call per question -- roughly 20 tokens, a rounding error
against the corpus itself. Nothing is written to the database.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from linestack.config import settings
from linestack.evaluation.dataset import QUESTION_IDS, QUESTIONS
from linestack.retrieval.embedding import build_client, embed_question
from linestack.retrieval.queries import query_version, retrieval_query
from linestack.retrieval.scope import ProspectScope
from linestack.retrieval.search import format_hits, search


def question_and_query(
    question: str | None, question_id: str | None = None
) -> tuple[str, str]:
    """The words shown, and the words searched with.

    An evaluated question is asked in the ground-truth set's words and searched
    with its written query -- the same pair answer() uses. Free text searches
    with its own words, as it always has.
    """
    if question_id is not None:
        if question_id not in QUESTIONS:
            raise ValueError(
                f"unknown question id {question_id!r}; one of {list(QUESTIONS)}"
            )
        if question is None:
            question = QUESTIONS[question_id]
    if not question:
        raise ValueError("pass a question or a question_id")
    return question, retrieval_query(question, question_id)


async def ask(
    session,
    domain: str,
    question: str | None = None,
    *,
    question_id: str | None = None,
    k: int | None = None,
    full: bool = False,
    client=None,
) -> list[str]:
    """Answer nothing; show the evidence. Returns lines ready to print."""
    question, query = question_and_query(question, question_id)

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

    # The query-side instruction bge expects is applied inside embed_question,
    # the one place that rule now lives (it used to be copied here).
    query_vector = await embed_question(client, query)

    hits = await search(scope, query_vector, k=embedded if full else k)
    urls = await scope.source_urls([hit.id for hit in hits])

    lines = [
        f"  prospect:  {domain} (id {prospect_id}), {embedded} embedded chunks",
        f"  question:  {question}",
    ]
    if query != question:
        lines.append(f"  searched:  {query}  ({query_version()})")
    lines += [
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


def _at_least_one(value: str) -> int:
    k = int(value)
    if k < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {k}")
    return k


def build_parser() -> argparse.ArgumentParser:
    """The command line, apart from the run so its rules are testable without
    a database: one of a question or an evaluated id, and one of a depth or
    the whole prospect."""
    parser = argparse.ArgumentParser(prog="linestack.retrieval.ask")
    parser.add_argument("--prospect", required=True, help="domain, e.g. fly.io")
    asked = parser.add_mutually_exclusive_group(required=True)
    asked.add_argument("--question", help="free text, searched with its own words")
    asked.add_argument(
        "--id",
        choices=QUESTION_IDS,
        help="an evaluated question, searched with its written query (ADR-0025)",
    )
    depth = parser.add_mutually_exclusive_group()
    depth.add_argument("-k", type=_at_least_one, default=None, help="chunks to return")
    depth.add_argument(
        "--full",
        action="store_true",
        help="rank every embedded chunk of the prospect",
    )
    return parser


async def _main(argv: list[str]) -> int:
    from linestack.db import session_factory

    args = build_parser().parse_args(argv)
    async with session_factory() as session:
        lines = await ask(
            session,
            args.prospect,
            args.question,
            question_id=args.id,
            k=args.k,
            full=args.full,
        )
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
