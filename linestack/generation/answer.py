"""Responsibility: assembling context and producing a cited answer.

Owns: context assembly order, the token budget, the rule that
prospects.signals is injected on every answer regardless of what retrieval
returned -- a computed fact does not compete with vector similarity for a place
in the context window (A2) -- and the checks that can be made on an answer
WITHOUT a judge: whether every citation points at something that was actually
in the context, and whether the answer declined.

Does not own: deciding what is true. An answer that the retrieved context does
not support must say so. "Insufficient evidence" is a correct answer and is
graded as one (docs/ground-truth.md section 3).

The fifth question -- a concrete angle for a first approach -- is generated
here and is deliberately excluded from evaluation, because it has no ground
truth. Anything generated for it is marked as such in the output (A4).

## Not streamed, deliberately

The design called for a streamed answer. The two callers that exist -- this
module's CLI and the evaluation harness -- both need the whole text before they
can check a single citation, so streaming would buy them nothing and cost a
thread and a queue per answer. Streaming is a property of the transport, and it
belongs with the API route, which has a client on the other end to stream to.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text

from linestack.config import settings
from linestack.generation.client import build_generator
from linestack.generation.prompts import (
    INSUFFICIENT,
    PROMPT_VERSION,
    SIGNALS_CITATION,
    SUGGESTION_ID,
    build_messages,
    question_text,
)
from linestack.retrieval.embedding import build_client as build_embedder
from linestack.retrieval.embedding import embed_question
from linestack.retrieval.scope import ProspectScope
from linestack.retrieval.search import search

# The computed signals that describe the COMPANY, each with the sentence that
# says what it measures. Two things are deliberate.
#
# Crawl bookkeeping is left out. `pages_crawled` and `total_words` describe the
# crawl, and a model handed "total_words: 18008" will sooner or later report it
# as a fact about the business.
#
# Every value carries its meaning, because the bare name invites the canonical
# misreading. `people_listed: 53` is how many people the PAGE names, and a model
# given only the number writes "53 employees" -- which is exactly what the
# ground-truth notes warn is not the same thing (A4).
ANSWER_SIGNALS: dict[str, str] = {
    "has_team_page": "whether the site has a page listing its people",
    "people_listed": "people named on that page -- a page count, not a headcount",
    "technical_roles_named": "technical job titles mentioned on team pages",
    "has_careers_page": "whether the site has a careers or jobs page",
    "open_roles_seen": "vacancies listed on the careers pages when crawled",
    "technical_roles_open": "of those vacancies, how many have a technical title",
    "blog_posts_seen": "blog posts crawled -- a crawl count, not the blog's size",
    "latest_post_date": "newest publication date the crawled blog posts declare",
}


class AnswerUnavailable(RuntimeError):
    """There is nothing to answer from yet. The message says what to run."""


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Passage:
    """One retrieved chunk as the model saw it, under the number it cites."""

    number: int
    chunk_id: int
    kind: str
    source_url: str
    score: float
    tokens: int
    content: str


@dataclass(frozen=True)
class Context:
    signals: str
    signals_tokens: int
    passages: tuple[Passage, ...]
    #: Chunk ids that retrieval returned but the budget did not admit. Recorded,
    #: never silently discarded: an answer that misses evidence retrieval DID
    #: find is a context-budget failure, not a retrieval one, and the two need
    #: different fixes (A8).
    dropped: tuple[int, ...]
    budget_tokens: int

    @property
    def tokens(self) -> int:
        return self.signals_tokens + sum(p.tokens for p in self.passages)

    @property
    def over_budget(self) -> bool:
        return self.tokens > self.budget_tokens


def signals_block(signals: Mapping[str, object]) -> str:
    """The computed facts, labelled as what they are.

    Always present, even when empty: "nothing was computed" is itself
    information, and a missing block would be indistinguishable from a bug.
    """
    header = (
        f"COMPUTED FACTS [{SIGNALS_CITATION}] -- counted by the crawler from the "
        f"company's own pages, not inferred:"
    )
    lines = [
        f"- {name} = {json.dumps(signals[name])}: {meaning}"
        for name, meaning in ANSWER_SIGNALS.items()
        if name in signals
    ]
    return "\n".join([header, *(lines or ["- none were computed for this company"])])


def assemble_context(
    signals: Mapping[str, object],
    hits: Sequence,
    urls: Mapping[int, str],
    *,
    budget_tokens: int,
    count_tokens: Callable[[str], int],
) -> Context:
    """Signals first and always; then passages in rank order while they fit.

    The signals are admitted before the budget is consulted (A2): a computed
    fact does not compete with similarity for a place in the context.

    Passages are admitted as a rank PREFIX. The first that does not fit ends
    admission, rather than being skipped for a smaller one further down,
    because skipping would quietly replace "the top k by similarity" with "the
    top k that happened to be short" -- a retrieval change made by the budget.

    The top passage is admitted even if it alone breaks the budget. Sending
    none would force a decline for reasons that have nothing to do with the
    company, and `over_budget` records that it happened.
    """
    block = signals_block(signals)
    used = count_tokens(block)
    passages: list[Passage] = []
    dropped: list[int] = []
    for hit in hits:
        if dropped:
            dropped.append(hit.id)
            continue
        body = " ".join(hit.content.split())
        cost = count_tokens(body)
        if passages and used + cost > budget_tokens:
            dropped.append(hit.id)
            continue
        passages.append(
            Passage(
                number=len(passages) + 1,
                chunk_id=hit.id,
                kind=hit.kind,
                source_url=urls.get(hit.id, "(source not resolved)"),
                score=hit.score,
                tokens=cost,
                content=body,
            )
        )
        used += cost
    return Context(
        signals=block,
        signals_tokens=count_tokens(block),
        passages=tuple(passages),
        dropped=tuple(dropped),
        budget_tokens=budget_tokens,
    )


def render_context(context: Context) -> str:
    parts = [context.signals, "", "RETRIEVED PASSAGES, most similar first:"]
    if not context.passages:
        parts.append("(none were retrieved)")
    for p in context.passages:
        parts += ["", f"[{p.number}] ({p.kind}) {p.source_url}", p.content]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# checks that need no judge
# --------------------------------------------------------------------------- #
_BRACKET_RE = re.compile(r"\[([^\[\]]{1,24})\]")
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


@dataclass(frozen=True)
class Citations:
    #: Passage numbers cited that exist in the context, sorted, unique.
    passages: tuple[int, ...]
    #: Whether the computed facts were cited.
    signals: bool
    #: Citations to passages that were never in the context -- e.g. [7] when
    #: five passages were sent. A fabricated citation, and the one faithfulness
    #: failure that can be caught mechanically, with no judge at all.
    invalid: tuple[str, ...]

    @property
    def cited_anything(self) -> bool:
        return bool(self.passages or self.signals)


def parse_citations(answer_text: str, passage_count: int) -> Citations:
    """Every [n], [S], [1, 2] and [1-3] in the text, checked against the
    context. Bracketed text that is not a citation -- "[sic]" -- is ignored."""
    cited: set[int] = set()
    invalid: list[str] = []
    signals = False
    for group in _BRACKET_RE.findall(answer_text):
        for part in (x.strip() for x in group.split(",")):
            if part.upper() == SIGNALS_CITATION:
                signals = True
            elif part.isdigit():
                n = int(part)
                (cited.add(n) if 1 <= n <= passage_count else invalid.append(part))
            elif m := _RANGE_RE.match(part):
                for n in range(int(m.group(1)), int(m.group(2)) + 1):
                    (
                        cited.add(n)
                        if 1 <= n <= passage_count
                        else invalid.append(str(n))
                    )
    return Citations(tuple(sorted(cited)), signals, tuple(dict.fromkeys(invalid)))


def is_decline(answer_text: str) -> bool:
    """Whether the answer declined, by the fixed phrase the prompt requires."""
    phrase = INSUFFICIENT.rstrip(".").lower()
    return answer_text.strip().lower().startswith(phrase)


# --------------------------------------------------------------------------- #
# the answer
# --------------------------------------------------------------------------- #
@dataclass
class GeneratedAnswer:
    """Model output. A different type from the computed signals, named
    differently, because "the crawler counted X" and "the model says X" are
    different claims (A4)."""

    question: str
    question_id: str | None
    text: str
    declined: bool
    citations: Citations
    context: Context
    model: str
    prompt_version: str
    #: The fifth question: generated, has no ground truth, never evaluated.
    is_suggestion: bool = False
    load_seconds: float = 0.0
    generate_seconds: float = 0.0
    retrieve_seconds: float = 0.0

    @property
    def problems(self) -> list[str]:
        """What can be said to be wrong without a judge. Empty is not "correct"
        -- it is "not detectably wrong", which is all a judge-free check can
        promise (ADR-0020)."""
        found = []
        if self.citations.invalid:
            found.append(
                "cites passages that were not in the context: "
                + ", ".join(f"[{c}]" for c in self.citations.invalid)
            )
        if not self.declined and not self.citations.cited_anything:
            found.append("makes claims without citing anything")
        return found


async def answer(
    session,
    domain: str,
    question: str | None = None,
    *,
    question_id: str | None = None,
    k: int | None = None,
    embedder=None,
    generator=None,
    count_tokens: Callable[[str], int] | None = None,
) -> GeneratedAnswer:
    """Answer one question about one prospect, with citations, or decline."""
    if question is None:
        if question_id is None:
            raise ValueError("pass a question or a question_id")
        question = question_text(question_id)

    prospect_id = await session.scalar(
        text("SELECT id FROM prospects WHERE domain = :d"), {"d": domain.lower()}
    )
    if prospect_id is None:
        raise AnswerUnavailable(
            f"no prospect with domain {domain!r}. Crawl and load one first:\n"
            f"  make crawl DOMAIN={domain}\n"
            f"  make load ARTIFACTS=prospect_{domain.replace('.', '_')}.json"
        )
    scope = await ProspectScope.open(session, prospect_id)
    if not await scope.count_embedded():
        raise AnswerUnavailable(
            f"{domain} has no embedded chunks, so there is nothing to answer "
            f"from.\n  make embed PROSPECT={domain}"
        )

    signals = (
        await session.scalar(
            text("SELECT signals FROM prospects WHERE id = :p"), {"p": prospect_id}
        )
        or {}
    )

    started = time.perf_counter()
    query_vector = await embed_question(embedder or build_embedder(), question)
    hits = await search(scope, query_vector, k=k)
    urls = await scope.source_urls([hit.id for hit in hits])
    retrieve_seconds = time.perf_counter() - started

    if count_tokens is None:
        from linestack.ingestion.chunking import default_token_counter

        count_tokens = default_token_counter()
    context = assemble_context(
        signals,
        hits,
        urls,
        budget_tokens=settings.generation_context_tokens,
        count_tokens=count_tokens,
    )

    is_suggestion = question_id == SUGGESTION_ID
    generator = generator or build_generator()
    load_before = generator.load_seconds
    started = time.perf_counter()
    reply = await generator.complete(
        build_messages(question, render_context(context), is_suggestion=is_suggestion),
        max_tokens=settings.generation_max_tokens,
        temperature=settings.generation_temperature,
    )
    elapsed = time.perf_counter() - started
    loaded = generator.load_seconds - load_before

    return GeneratedAnswer(
        question=question,
        question_id=question_id,
        text=reply,
        declined=is_decline(reply),
        citations=parse_citations(reply, len(context.passages)),
        context=context,
        model=generator.name,
        prompt_version=PROMPT_VERSION,
        is_suggestion=is_suggestion,
        load_seconds=loaded,
        generate_seconds=elapsed - loaded,
        retrieve_seconds=retrieve_seconds,
    )


def format_answer(result: GeneratedAnswer, domain: str) -> list[str]:
    """For a human. The passages and their scores are printed with the answer
    because they are how a bad answer gets diagnosed: a wrong answer over a
    context that never contained the evidence is a retrieval failure, and it
    must not be blamed on the model (A8)."""
    import textwrap

    ctx = result.context
    lines = [
        f"  prospect:  {domain}",
        f"  model:     {result.model}  (prompt {result.prompt_version})",
        f"  question:  {result.question}",
    ]
    if result.is_suggestion:
        lines.append("  [a SUGGESTION: generated, has no ground truth, not evaluated]")
    lines.append("")
    lines += ["  " + line for line in textwrap.wrap(result.text, width=86)] or [
        "  (empty)"
    ]
    lines.append("")
    cited = [
        f"[{n}] {ctx.passages[n - 1].source_url}" for n in result.citations.passages
    ]
    if result.citations.signals:
        cited.append(f"[{SIGNALS_CITATION}] computed facts")
    lines.append("  cites:     " + ("; ".join(cited) if cited else "nothing"))
    status = "declined" if result.declined else "answered"
    problems = result.problems
    lines.append(
        f"  checks:    {status}; "
        + ("; ".join(problems) if problems else "no detectable problem")
    )
    lines.append(
        f"  context:   {len(ctx.passages)} passage(s), {ctx.tokens:,} of "
        f"{ctx.budget_tokens:,} tokens"
        + (f", {len(ctx.dropped)} dropped for budget" if ctx.dropped else "")
        + (" -- OVER BUDGET, top passage admitted anyway" if ctx.over_budget else "")
    )
    for p in ctx.passages:
        lines.append(f"    [{p.number}] {p.score:.4f}  [{p.kind}]  {p.source_url}")
    lines.append(
        f"  timing:    {result.load_seconds:.1f}s model load, "
        f"{result.generate_seconds:.1f}s generate, "
        f"{result.retrieve_seconds:.2f}s retrieve"
    )
    return lines


async def _main(argv: list[str]) -> int:
    import argparse

    from linestack.db import session_factory

    parser = argparse.ArgumentParser(prog="linestack.generation.answer")
    parser.add_argument("--prospect", required=True, help="domain, e.g. fly.io")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--question")
    group.add_argument("--id", help="q1_what_and_to_whom ... q4_stated_need, or q5")
    parser.add_argument("-k", type=int, default=None, help="passages to retrieve")
    args = parser.parse_args(argv)

    async with session_factory() as session:
        try:
            result = await answer(
                session,
                args.prospect,
                args.question,
                question_id=args.id,
                k=args.k,
            )
        except AnswerUnavailable as exc:
            print(f"  {exc}")
            return 1
    print("\n".join(format_answer(result, args.prospect)))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
