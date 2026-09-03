"""Responsibility: turning text into a halfvec(1536) with text-embedding-3-small,
and recording which model produced it.

Owns: batching, retry, and the invariant that an embedding is never stored
without its model name -- the schema enforces the pairing, this module supplies
it.

Does not own: the choice of model. That is configuration, because changing it
invalidates every embedding already stored.

This is the first module in the project that spends money, and it is built so
that it can always say how much BEFORE it does. `--dry-run` reports the pending
chunk count and the pending token total and makes zero API calls. A project
whose whole discipline is measuring before and after should not have a step
that cannot state its own cost in advance.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from linestack.config import settings
from linestack.retrieval.scope import EmbeddingBatch, ProspectScope

# text-embedding-3-small accepts 8191 tokens per input and a large but finite
# number of inputs per request. Both limits are respected; the token ceiling is
# the one that actually binds, because a batch of 64 chunks at 1,000 tokens is
# 64,000 tokens.
MAX_INPUTS_PER_REQUEST = 64
MAX_TOKENS_PER_REQUEST = 100_000

# Retry only what a retry can fix.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 1.0


class EmbeddingFailed(RuntimeError):
    """Raised when a batch could not be embedded after its retries."""


@dataclass
class EmbedReport:
    """What one embedding pass did, or would do. Counted, never estimated."""

    prospect_id: int = 0
    pending_chunks: int = 0
    pending_tokens: int = 0
    requests: int = 0
    retries: int = 0
    chunks_embedded: int = 0
    dry_run: bool = False
    batches: list[int] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        # 1M tokens at the text-embedding-3-small list price. Stated as an
        # order of magnitude, not a bill: prices change and this file will not
        # notice, so the token count above it is the number to trust.
        cost = self.pending_tokens / 1_000_000 * 0.02
        head = "would embed" if self.dry_run else "embedded"
        return [
            f"  prospect:  {self.prospect_id}",
            f"  pending:   {self.pending_chunks} chunks, {self.pending_tokens} tokens",
            f"  estimate:  ~${cost:.4f} at $0.02/1M tokens (list price, may be stale)",
            f"  {head}:  {self.chunks_embedded} chunks in "
            f"{self.requests} requests, {self.retries} retries",
        ]


def plan_batches(
    chunks: list,
    max_inputs: int = MAX_INPUTS_PER_REQUEST,
    max_tokens: int = MAX_TOKENS_PER_REQUEST,
) -> list[list]:
    """Split pending chunks into requests, bounded by count AND token sum.

    Bounded by both because either alone is wrong: 64 chunks of 1,000 tokens is
    a 64,000-token request, and one chunk of 6,000 tokens is nowhere near the
    input limit. A chunk larger than `max_tokens` still gets its own request
    rather than being dropped -- the API will reject it, loudly, which is the
    correct outcome for a chunk the chunker should never have produced.
    """
    batches: list[list] = []
    current: list = []
    current_tokens = 0
    for chunk in chunks:
        if current and (
            len(current) >= max_inputs
            or current_tokens + chunk.token_count > max_tokens
        ):
            batches.append(current)
            current, current_tokens = [], 0
        current.append(chunk)
        current_tokens += chunk.token_count
    if current:
        batches.append(current)
    return batches


def is_retryable(exc: Exception) -> bool:
    """Whether retrying this failure could plausibly succeed.

    A 400 is never retried. It means the input is malformed or too long, and
    retrying it burns money five times to fail identically. Rate limits, brief
    server errors and timeouts are the cases a retry exists for.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return status in RETRYABLE_STATUS
    # No status at all: a transport failure or timeout, which is retryable.
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))


async def embed_texts(
    client, texts: list[str], report: EmbedReport
) -> list[list[float]]:
    """One request, with bounded retries and jittered backoff.

    Jitter is not decoration. Without it, several batches that hit the same
    rate limit retry in lockstep and hit it again together.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await client.embeddings.create(
                model=settings.embedding_model, input=texts
            )
            report.requests += 1
            return [item.embedding for item in response.data]
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if not is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                break
            report.retries += 1
            delay = BASE_DELAY_SECONDS * (2**attempt)
            await asyncio.sleep(delay * (0.5 + random.random()))
    raise EmbeddingFailed(
        f"embedding failed after {report.retries} retries: "
        f"{type(last).__name__}: {last}"
    ) from last


async def embed_prospect(
    session,
    prospect_id: int,
    *,
    client=None,
    dry_run: bool = False,
    limit: int | None = None,
    commit: bool = True,
) -> EmbedReport:
    """Embed every chunk of one prospect that does not yet have a vector.

    Resumable by construction: the work list is `WHERE embedding IS NULL`, so a
    crash re-embeds only what was never embedded and never pays twice. That is
    also why chunks are written before they have vectors (ADR-0004 note on
    chunks.embedding being nullable).

    Opening the scope refuses a prospect whose stored vectors came from another
    model. Mixing two vector spaces produces plausible-looking bad rankings
    rather than an error, which is the hardest kind of defect to attribute.
    """
    scope = await ProspectScope.open(session, prospect_id)
    report = EmbedReport(prospect_id=prospect_id, dry_run=dry_run)

    pending = await scope.pending_embedding(limit or 10_000)
    report.pending_chunks = len(pending)
    report.pending_tokens = await scope.pending_token_total()
    report.batches = [len(b) for b in plan_batches(pending)]

    if dry_run or not pending:
        return report

    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.require_openai_key())

    for batch in plan_batches(pending):
        vectors = await embed_texts(client, [c.content for c in batch], report)
        report.chunks_embedded += await scope.write_embeddings(
            [c.id for c in batch],
            EmbeddingBatch(
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                vectors=vectors,
            ),
        )
        # Committed per batch, not at the end: a failure on batch nine must
        # not discard the eight batches already paid for. That is real money,
        # and it is why `commit` defaults to True.
        #
        # It is a parameter because a commit is not free of consequences
        # elsewhere: it ends the caller's transaction. The integration tests
        # run inside one they intend to roll back, and with an unconditional
        # commit here they left four prospects behind in the database --
        # exactly the non-hermetic state a previous step had to be fixed for.
        if commit:
            await session.commit()

    return report


async def _main(argv: list[str]) -> int:
    import argparse

    from sqlalchemy import text

    from linestack.db import session_factory

    parser = argparse.ArgumentParser(prog="linestack.retrieval.embedding")
    parser.add_argument("--prospect", required=True, help="domain, e.g. fly.io")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report pending chunks and tokens; make no API calls",
    )
    args = parser.parse_args(argv)

    async with session_factory() as session:
        prospect_id = await session.scalar(
            text("SELECT id FROM prospects WHERE domain = :d"),
            {"d": args.prospect.lower()},
        )
        if prospect_id is None:
            print(
                f"no prospect with domain {args.prospect!r}. Load one first: "
                f"make load ARTIFACTS=prospect_<domain>.json"
            )
            return 2

        report = await embed_prospect(session, prospect_id, dry_run=args.dry_run)
        print(f"\n=== {args.prospect}")
        for line in report.as_lines():
            print(line)
        if args.dry_run:
            print("  (dry run: no API calls were made and nothing was written)")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
