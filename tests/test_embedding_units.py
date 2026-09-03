"""Unit tests for `linestack.retrieval.embedding`. No database, no API calls.

Every test here uses a fake client. The point of the module is that it can be
reasoned about without spending money, and a test suite that needed a key to
check its retry logic would defeat that.
"""

import pytest

from linestack.config import settings
from linestack.retrieval.embedding import (
    MAX_ATTEMPTS,
    EmbeddingFailed,
    EmbedReport,
    embed_texts,
    is_retryable,
    plan_batches,
)
from linestack.retrieval.scope import PendingChunk


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Backoff is exponential and these tests exhaust it.

    Without this the unit suite spends 1+2+4+8 seconds asleep per exhausted
    retry test and goes from 0.5s to 13.8s. What is under test is the retry
    POLICY -- which failures are retried, how many times -- not the wall clock,
    and a slow unit suite is one people stop running.
    """
    import asyncio as _asyncio

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_asyncio, "sleep", _instant)


def chunk(id_: int, tokens: int) -> PendingChunk:
    return PendingChunk(id=id_, content=f"chunk {id_}", token_count=tokens)


class _Status(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeClient:
    """Records calls and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes=None, dimensions: int = 8) -> None:
        self.calls: list[list[str]] = []
        self._outcomes = list(outcomes or [])
        self._dimensions = dimensions
        self.embeddings = self

    async def create(self, *, model: str, input: list[str]):
        self.calls.append(list(input))
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return type(
            "Response",
            (),
            {
                "data": [
                    type("Item", (), {"embedding": [0.0] * self._dimensions})()
                    for _ in input
                ]
            },
        )()


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------
def test_batches_are_bounded_by_count() -> None:
    batches = plan_batches([chunk(i, 1) for i in range(150)], max_inputs=64)
    assert [len(b) for b in batches] == [64, 64, 22]


def test_batches_are_also_bounded_by_the_token_sum() -> None:
    """Either bound alone is wrong.

    64 chunks of 1,000 tokens is a 64,000-token request, which a count-only
    bound would happily send.
    """
    batches = plan_batches(
        [chunk(i, 1000) for i in range(10)], max_inputs=64, max_tokens=2500
    )
    assert [len(b) for b in batches] == [2, 2, 2, 2, 2]


def test_a_chunk_larger_than_a_whole_batch_still_gets_sent() -> None:
    """Alone, in its own request, so the API rejects it loudly.

    Dropping it would hide a chunk the chunker should never have produced;
    a rejection names the problem.
    """
    batches = plan_batches([chunk(1, 50_000)], max_tokens=1000)
    assert [len(b) for b in batches] == [1]


def test_no_pending_chunks_means_no_batches() -> None:
    assert plan_batches([]) == []


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_rate_limits_and_server_errors_are_retryable(status: int) -> None:
    assert is_retryable(_Status(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_error_is_never_retried(status: int) -> None:
    """A 400 means the input is malformed or too long.

    Retrying it five times burns money to fail five times identically, and
    hides the real cause behind a timeout-shaped delay.
    """
    assert not is_retryable(_Status(status))


def test_a_transport_failure_with_no_status_is_retryable() -> None:
    assert is_retryable(ConnectionError("connection reset"))
    assert is_retryable(TimeoutError())


async def test_a_retryable_failure_is_retried_and_then_succeeds() -> None:
    client = _FakeClient(outcomes=[_Status(429), _Status(503), None])
    report = EmbedReport()

    vectors = await embed_texts(client, ["a", "b"], report)

    assert len(vectors) == 2
    assert report.retries == 2
    assert report.requests == 1
    assert len(client.calls) == 3


async def test_a_400_is_not_retried_even_once() -> None:
    client = _FakeClient(outcomes=[_Status(400)])
    report = EmbedReport()

    with pytest.raises(EmbeddingFailed, match="HTTP 400"):
        await embed_texts(client, ["a"], report)

    assert report.retries == 0
    assert len(client.calls) == 1, "a 400 was retried; that is money burnt"


async def test_retries_are_bounded_and_then_fail_loudly() -> None:
    client = _FakeClient(outcomes=[_Status(429)] * 20)
    report = EmbedReport()

    with pytest.raises(EmbeddingFailed):
        await embed_texts(client, ["a"], report)

    assert len(client.calls) == MAX_ATTEMPTS
    assert report.chunks_embedded == 0


# ---------------------------------------------------------------------------
# Cost reporting
# ---------------------------------------------------------------------------
def test_the_report_states_the_cost_before_anything_is_spent() -> None:
    """A project whose discipline is measuring before and after should not have
    a step that cannot state its own cost in advance."""
    report = EmbedReport(
        prospect_id=1, pending_chunks=111, pending_tokens=105_318, dry_run=True
    )
    lines = "\n".join(report.as_lines())

    assert "111 chunks" in lines
    assert "105318 tokens" in lines
    assert "would embed" in lines
    # A cost line either quotes a price or says there is none. What it must
    # never do is stay silent about which, or quote a price for a model that
    # costs nothing (ADR-0017).
    assert ("$" in lines) or ("runs locally" in lines)


def test_a_completed_run_reports_what_it_did_not_what_it_would_do() -> None:
    report = EmbedReport(prospect_id=1, chunks_embedded=43, requests=2)
    assert "embedded" in "\n".join(report.as_lines())


# ---------------------------------------------------------------------------
# The model-name invariant
# ---------------------------------------------------------------------------
def test_the_batch_type_carries_the_model_so_it_cannot_be_forgotten() -> None:
    """The database has a CHECK enforcing the pairing. This makes the unpaired
    case unrepresentable in Python, so the CHECK never has to fire."""
    from dataclasses import fields

    from linestack.retrieval.scope import EmbeddingBatch

    names = {f.name for f in fields(EmbeddingBatch)}
    assert names == {"model", "dimensions", "vectors"}


def test_the_configured_dimension_is_plausible_for_an_embedding_model() -> None:
    """The exact value is asserted against the migration elsewhere in this file.

    This one only rules out a nonsense setting -- 0, or a typo of 3840 -- that
    would fail far from its cause. The dimension moved 1536 -> 384 with
    ADR-0017, so pinning a literal here would just be a second place to update.
    """
    assert 64 <= settings.embedding_dimensions <= 4096


def test_committing_per_batch_is_the_default() -> None:
    """Durability is the production behaviour; tests opt out, not the reverse.

    Embedding costs money. A failure on batch nine must not discard the eight
    batches already paid for, so `commit` defaults to True and only the
    integration tests -- which run inside a transaction they intend to roll
    back -- pass False. Flipping this default would make a crash mid-pass
    silently free to re-run and expensive to re-pay.
    """
    import inspect

    from linestack.retrieval.embedding import embed_prospect

    assert inspect.signature(embed_prospect).parameters["commit"].default is True


# ---------------------------------------------------------------------------
# Local embeddings (ADR-0017)
# ---------------------------------------------------------------------------
def test_the_schema_dimension_and_the_configured_dimension_agree() -> None:
    """A mismatch surfaces as an opaque Postgres cast error at the first
    INSERT, not as "config says 384, the model returned 1536".

    The dimension is a schema commitment: halfvec(N) fixes N in the column, so
    changing the embedding model to one of a different width is a migration and
    a full re-embed. This test is what stops the two drifting apart quietly.
    """
    import re
    from pathlib import Path

    migrations = sorted(
        (Path(__file__).resolve().parent.parent / "migrations").glob("*.sql")
    )
    declared = None
    for path in migrations:
        for match in re.finditer(
            r"halfvec\((\d+)\)", re.sub(r"--[^\n]*", "", path.read_text())
        ):
            declared = int(match.group(1))

    assert declared is not None, "no halfvec(N) found in any migration"
    assert declared == settings.embedding_dimensions, (
        f"the latest migration declares halfvec({declared}) but "
        f"settings.embedding_dimensions is {settings.embedding_dimensions}"
    )


def test_a_local_model_is_recognised_and_an_openai_one_is_not() -> None:
    from linestack.retrieval.embedding import uses_openai

    assert uses_openai("text-embedding-3-small")
    assert not uses_openai("BAAI/bge-small-en-v1.5")


def test_the_default_model_needs_no_api_key() -> None:
    """ADR-0017's whole point: the pipeline runs without an account."""
    from linestack.retrieval.embedding import LocalEmbedder, build_client, uses_openai

    assert not uses_openai(settings.embedding_model)
    assert isinstance(build_client(), LocalEmbedder)


def test_no_dollar_cost_is_quoted_for_a_model_that_costs_nothing() -> None:
    """A false number in a cost line is worse than no cost line: someone
    budgets against it. This printed "~$0.0021" for a local run until it did
    not."""
    report = EmbedReport(prospect_id=1, pending_chunks=111, pending_tokens=105_318)
    lines = "\n".join(report.as_lines())

    if settings.embedding_model.startswith("BAAI/"):
        assert "$" not in lines
        assert "runs locally" in lines


def test_a_bge_model_gets_the_query_prefix_and_documents_do_not() -> None:
    """bge asks for an instruction on the query side only. Getting the
    asymmetry wrong degrades ranking quietly rather than loudly."""
    from linestack.retrieval.embedding import BGE_QUERY_PREFIX, LocalEmbedder

    captured = {}

    class _Vector(list):
        """Stands in for a numpy row, including the .tolist() the code calls.

        numpy arrives with the optional `local` extra, not with `dev`, and a
        unit test that imports it fails on a CI runner that installed only the
        default set -- which is exactly how this test first failed.
        """

        def tolist(self):
            return list(self)

    class _Stub(LocalEmbedder):
        def _load(self):
            class _M:
                @staticmethod
                def encode(texts, **kw):
                    captured["texts"] = list(texts)
                    return [_Vector([0.0] * 4) for _ in texts]

            return _M()

    _Stub("BAAI/bge-small-en-v1.5").embed_query("who works here")
    assert captured["texts"] == [BGE_QUERY_PREFIX + "who works here"]

    _Stub("some/other-model").embed_query("who works here")
    assert captured["texts"] == ["who works here"]
