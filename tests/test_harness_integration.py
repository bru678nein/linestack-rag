"""The harness against a real database.

The unit tests cover what it refuses to score. This covers the half they
cannot: that the SQL is right, that a prospect resolves, and that signal
accuracy actually compares the JSONB the loader wrote against the YAML the
author wrote. Those two have never been compared anywhere else, and a metric
that reads the wrong column reports a perfect score forever.

Requires: make up && make migrate.
"""

import pytest
import yaml

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from linestack.evaluation.harness import (  # noqa: E402
    NOT_INGESTED,
    SCORED,
    UNWRITTEN,
    evaluate_directory,
)

pytestmark = pytest.mark.integration

DOMAIN = "harness-probe.test"


def _write(directory, questions, signals=None):
    (directory / "harness_probe_test.yaml").write_text(
        yaml.safe_dump(
            {
                "prospect": {
                    "company_name": "Probe",
                    "domain": DOMAIN,
                    "corpus_artifact": "probe.json",
                },
                "signals": signals or {},
                "questions": questions,
            }
        ),
        encoding="utf-8",
    )
    return directory


async def _seed(db_session, signals: dict) -> int:
    prospect_id = await db_session.scalar(
        text(
            "INSERT INTO prospects (company_name, domain, signals) "
            "VALUES ('Probe', :d, CAST(:s AS jsonb)) RETURNING id"
        ),
        {"d": DOMAIN, "s": __import__("json").dumps(signals)},
    )
    await db_session.execute(
        text(
            "INSERT INTO documents "
            "  (prospect_id, source_url, kind, content_hash, fetched_at) "
            "VALUES (:p, :u, 'website', 'h', now())"
        ),
        {"p": prospect_id, "u": f"https://{DOMAIN}/team"},
    )
    await db_session.flush()
    return prospect_id


async def test_signal_accuracy_compares_the_yaml_against_the_loaded_jsonb(
    db_session, tmp_path
) -> None:
    """The comparison this metric exists for, across the boundary it has to
    cross: the author's YAML on one side, `prospects.signals` on the other."""
    await _seed(db_session, {"people_listed": 54, "has_team_page": True})
    _write(
        tmp_path,
        questions=[],
        signals={"people_listed": 54, "has_team_page": True},
    )

    record = await evaluate_directory(db_session, tmp_path)

    report = record.prospects[0].signals
    assert report.accuracy == 1.0
    assert report.compared == 2
    await db_session.rollback()


async def test_a_disagreeing_signal_is_reported_with_both_values(
    db_session, tmp_path
) -> None:
    """The historical defect, in its real shape: 162 counted for 54 people.
    A metric that has never disagreed with anything is not known to work."""
    await _seed(db_session, {"people_listed": 162})
    _write(tmp_path, questions=[], signals={"people_listed": 54})

    record = await evaluate_directory(db_session, tmp_path)

    mismatches = record.prospects[0].signals.mismatches
    assert [(m.expected, m.computed) for m in mismatches] == [(54, 162)]
    await db_session.rollback()


async def test_an_unwritten_pair_is_not_scored_against_a_real_corpus(
    db_session, tmp_path
) -> None:
    """No embedding, no retrieval, no cost. The refusal happens before any of
    that, which is what makes running the harness on a half-written set free
    rather than merely harmless."""
    await _seed(db_session, {})
    _write(
        tmp_path,
        questions=[
            {
                "id": "q1_what_and_to_whom",
                "question": "What does this company do?",
                "reference": "TODO 2-4 sentences",
                "source_urls": ["TODO https://..."],
            }
        ],
    )

    record = await evaluate_directory(db_session, tmp_path)

    assert [p.status for p in record.prospects[0].pairs] == [UNWRITTEN]
    assert record.recall_at(5) is None
    await db_session.rollback()


class _Explodes:
    """An embedder that fails if anything touches it.

    Asserting `model_load_seconds == 0` would only prove the timing, not that
    nothing loaded. This proves it.
    """

    class embeddings:  # noqa: N801
        @staticmethod
        async def create(*args, **kwargs):
            raise AssertionError("the embedder was used for a run that scored nothing")

    def embed_query(self, *args, **kwargs):
        raise AssertionError("the embedder was used for a run that scored nothing")


async def test_a_half_written_set_never_touches_the_embedder(
    db_session, tmp_path
) -> None:
    """The property `make eval` on a half-written set depends on, pinned.

    **[verified] 2026-09-05**, this regressed within an hour of being claimed.
    A warm-up call added to measure model-load time separately ran
    unconditionally at the top of the run, so the model loaded even when every
    pair was refused -- and five tests failed on CI, which does not install the
    784 MB `[local]` extra. The docstring below the fix said a half-written set
    costs nothing while the code above it loaded a model to find that out.

    Loading is lazy now, and this is what keeps it lazy. A property asserted
    only in prose is a property with nothing holding it.
    """
    await _seed(db_session, {"people_listed": 54})
    _write(
        tmp_path,
        questions=[
            {
                "id": "q1_what_and_to_whom",
                "question": "What does this company do?",
                "reference": "TODO 2-4 sentences",
                "source_urls": ["TODO https://..."],
            },
            {
                "id": "q3_growth_signals",
                "question": "Are they growing?",
                "reference": "A written answer.",
                "source_urls": [f"https://{DOMAIN}/never-crawled"],
            },
        ],
        signals={"people_listed": 54},
    )

    record = await evaluate_directory(db_session, tmp_path, client=_Explodes())

    assert [p.status for p in record.prospects[0].pairs] == [UNWRITTEN, NOT_INGESTED]
    assert record.prospects[0].signals.accuracy == 1.0, "diagnosis still runs"
    assert record.model_load_seconds == 0.0
    assert record.embed_seconds == 0.0
    await db_session.rollback()


async def test_a_pair_citing_an_uncrawled_page_is_diagnosed_not_scored(
    db_session, tmp_path
) -> None:
    """Reported as an ingestion finding rather than as 0 recall. Reading that
    0 as a ranking failure sends someone to tune retrieval over a corpus that
    does not contain the answer (docs/evaluation.md §2.5)."""
    await _seed(db_session, {})
    _write(
        tmp_path,
        questions=[
            {
                "id": "q1_what_and_to_whom",
                "question": "What does this company do?",
                "reference": "A written answer.",
                "source_urls": [f"https://{DOMAIN}/never-crawled"],
            }
        ],
    )

    record = await evaluate_directory(db_session, tmp_path)

    pair = record.prospects[0].pairs[0]
    assert pair.status == NOT_INGESTED
    assert "never attempted" in pair.detail
    await db_session.rollback()


async def test_a_ground_truth_file_for_an_unloaded_prospect_says_so(
    db_session, tmp_path
) -> None:
    """A file whose corpus was never loaded must not silently contribute
    nothing. It is a setup problem with a fix, and the fix is printed."""
    _write(tmp_path, questions=[])

    record = await evaluate_directory(db_session, tmp_path)

    assert record.prospects[0].prospect_id is None
    assert "not loaded" in record.prospects[0].detail
    assert "make load" in record.prospects[0].detail


async def test_recall_is_computed_against_the_real_ranking(
    db_session, tmp_path
) -> None:
    """The whole path: resolve, embed, rank, resolve URLs, score. Uses a stub
    embedder so the test needs no model download and no key -- the ranking and
    the URL join are what is under test here, not the model."""
    prospect_id = await _seed(db_session, {})
    from linestack.config import settings

    dim = settings.embedding_dimensions
    document_id = await db_session.scalar(
        text("SELECT id FROM documents WHERE prospect_id = :p"), {"p": prospect_id}
    )
    for index, vector in enumerate(([1.0] + [0.0] * (dim - 1), [0.0] * dim)):
        await db_session.execute(
            text(
                "INSERT INTO chunks (document_id, prospect_id, kind, chunk_index, "
                "  content, token_count, embedding, embedding_model) "
                "VALUES (:d, :p, 'website', :i, 'probe', 1, "
                "  CAST(:e AS halfvec), :m)"
            ),
            {
                "d": document_id,
                "p": prospect_id,
                "i": index,
                "e": str(vector),
                "m": settings.embedding_model,
            },
        )
    await db_session.flush()

    _write(
        tmp_path,
        questions=[
            {
                "id": "q1_what_and_to_whom",
                "question": "What does this company do?",
                "reference": "A written answer.",
                "source_urls": [f"https://{DOMAIN}/team"],
            }
        ],
    )

    class _Stub:
        """Returns a fixed vector. Not a LocalEmbedder, so the harness takes
        the OpenAI-shaped branch -- which is also worth exercising."""

        class embeddings:  # noqa: N801
            @staticmethod
            async def create(model, input, **kwargs):
                from types import SimpleNamespace

                return SimpleNamespace(
                    data=[SimpleNamespace(embedding=[1.0] + [0.0] * (dim - 1))]
                    * len(input)
                )

    record = await evaluate_directory(db_session, tmp_path, client=_Stub())

    pair = record.prospects[0].pairs[0]
    assert pair.status == SCORED
    assert pair.first_hit_rank == 1
    assert record.recall_at(1) == 1.0
    await db_session.rollback()


# ---------------------------------------------------------------------------
# answers and declines, with a stub model
# ---------------------------------------------------------------------------
class _VecStub:
    """OpenAI-shaped embedder returning one fixed vector."""

    class embeddings:  # noqa: N801
        @staticmethod
        async def create(model, input, **kwargs):
            from types import SimpleNamespace

            from linestack.config import settings

            dim = settings.embedding_dimensions
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[1.0] + [0.0] * (dim - 1)) for _ in input
                ]
            )


class _Reply:
    """A generator that always says the same thing."""

    name = "stub-model"
    load_seconds = 0.0

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, messages, *, max_tokens, temperature):
        return self.reply


def _words(text_: str) -> int:
    return len(text_.split())


_Q3 = {
    "id": "q3_growth_signals",
    "question": "Are they growing?",
    "reference": "Insufficient evidence. Nothing says so.",
    "source_urls": [],
    "expected_outcome": "insufficient_evidence",
}


async def _seed_answerable(db_session) -> int:
    """_seed plus one embedded chunk, so answer() has something to answer from."""
    from linestack.config import settings

    prospect_id = await _seed(db_session, {})
    document_id = await db_session.scalar(
        text("SELECT id FROM documents WHERE prospect_id = :p"), {"p": prospect_id}
    )
    dim = settings.embedding_dimensions
    await db_session.execute(
        text(
            "INSERT INTO chunks (document_id, prospect_id, kind, chunk_index, "
            "  content, token_count, embedding, embedding_model) "
            "VALUES (:d, :p, 'website', 0, 'We are not hiring right now.', 1, "
            "  CAST(:e AS halfvec), :m)"
        ),
        {
            "d": document_id,
            "p": prospect_id,
            "e": str([1.0] + [0.0] * (dim - 1)),
            "m": settings.embedding_model,
        },
    )
    await db_session.flush()
    return prospect_id


async def test_a_decline_on_an_insufficient_pair_is_counted_as_correct(
    db_session, tmp_path
) -> None:
    await _seed_answerable(db_session)
    _write(tmp_path, questions=[_Q3])

    record = await evaluate_directory(
        db_session,
        tmp_path,
        client=_VecStub(),
        generator=_Reply("Insufficient evidence. No page mentions growth."),
        count_tokens=_words,
    )

    assert record.prospects[0].pairs[0].declined is True
    assert record.declines_on_insufficient() == (1, 1)
    assert record.generation_model == "stub-model"
    await db_session.rollback()


async def test_an_invented_answer_on_an_insufficient_pair_is_counted_as_wrong(
    db_session, tmp_path
) -> None:
    """The failure the model showed on thoughtbot's q3 (ADR-0022), as a count."""
    await _seed_answerable(db_session)
    _write(tmp_path, questions=[_Q3])

    record = await evaluate_directory(
        db_session,
        tmp_path,
        client=_VecStub(),
        generator=_Reply("They are investing in strategic growth [1]."),
        count_tokens=_words,
    )

    assert record.declines_on_insufficient() == (0, 1)
    assert any(
        "ANSWERED where it should have declined" in line for line in record.as_lines()
    )
    await db_session.rollback()


async def test_nothing_to_answer_from_is_reported_not_counted(
    db_session, tmp_path
) -> None:
    """No embedded chunks is a setup problem. It must not become a decline, an
    answer, or a crash."""
    await _seed(db_session, {})
    _write(tmp_path, questions=[_Q3])

    record = await evaluate_directory(
        db_session,
        tmp_path,
        client=_VecStub(),
        generator=_Reply("never called"),
        count_tokens=_words,
    )

    pair = record.prospects[0].pairs[0]
    assert "no embedded chunks" in pair.answer_detail
    assert record.declines_on_insufficient() is None
    await db_session.rollback()
