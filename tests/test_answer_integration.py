"""`answer()` against a real database, with a stub embedder and a stub model.

The model is stubbed on purpose. What is under test here is everything around
it: that the prospect resolves, that the computed signals reach the context on
every answer (A2), that the passages come from this prospect and no other (A1),
and that the reply's citations are checked against what was actually sent.
None of that should need a 4 GB download to verify, and CI has no model.

Requires: make up && make migrate.
"""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from linestack.config import settings  # noqa: E402
from linestack.evaluation.dataset import QUESTIONS  # noqa: E402
from linestack.generation.answer import AnswerUnavailable, answer  # noqa: E402
from linestack.generation.prompts import SUGGESTION_ID  # noqa: E402

pytestmark = pytest.mark.integration

DOMAIN = "answer-probe.test"
OTHER = "someone-else.test"
DIM = settings.embedding_dimensions
NEAR = [1.0] + [0.0] * (DIM - 1)
FAR = [0.0, 1.0] + [0.0] * (DIM - 2)


def _embedder():
    """OpenAI-shaped, so it goes through embed_texts. Always returns NEAR."""

    class _E:
        class embeddings:  # noqa: N801
            @staticmethod
            async def create(model, input, **kwargs):
                return SimpleNamespace(
                    data=[SimpleNamespace(embedding=NEAR) for _ in input]
                )

    return _E()


class _Model:
    """Replies with a fixed string and keeps the messages it was sent."""

    name = "stub-model"
    load_seconds = 0.0

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[dict] = []

    async def complete(self, messages, *, max_tokens, temperature):
        self.messages = messages
        return self.reply


def _words(text_: str) -> int:
    return len(text_.split())


async def _prospect(db_session, domain: str, signals: dict, chunks: list) -> int:
    prospect_id = await db_session.scalar(
        text(
            "INSERT INTO prospects (company_name, domain, signals) "
            "VALUES ('Probe', :d, CAST(:s AS jsonb)) RETURNING id"
        ),
        {"d": domain, "s": json.dumps(signals)},
    )
    for index, (path, content, vector) in enumerate(chunks):
        document_id = await db_session.scalar(
            text(
                "INSERT INTO documents "
                "  (prospect_id, source_url, kind, content_hash, fetched_at) "
                "VALUES (:p, :u, 'website', :h, now()) RETURNING id"
            ),
            {"p": prospect_id, "u": f"https://{domain}{path}", "h": f"h{index}"},
        )
        await db_session.execute(
            text(
                "INSERT INTO chunks (document_id, prospect_id, kind, chunk_index, "
                "  content, token_count, embedding, embedding_model) "
                "VALUES (:d, :p, 'website', 0, :c, 1, CAST(:e AS halfvec), :m)"
            ),
            {
                "d": document_id,
                "p": prospect_id,
                "c": content,
                "e": str(vector),
                "m": settings.embedding_model,
            },
        )
    await db_session.flush()
    return prospect_id


async def _seed(db_session) -> None:
    await _prospect(
        db_session,
        DOMAIN,
        {"people_listed": 53, "has_team_page": True, "total_words": 18008},
        [
            ("/team", "Fifty-three people, most of them engineers.", NEAR),
            ("/pricing", "Plans start at twenty dollars.", FAR),
        ],
    )


async def test_the_answer_is_built_from_signals_and_this_prospects_passages(
    db_session,
) -> None:
    await _seed(db_session)
    model = _Model("They list 53 people [S], mostly engineers [1].")

    result = await answer(
        db_session,
        DOMAIN,
        "Who works there?",
        embedder=_embedder(),
        generator=model,
        count_tokens=_words,
    )

    sent = model.messages[-1]["content"]
    assert "people_listed = 53" in sent
    assert "not a headcount" in sent, "the meaning travels with the number (A4)"
    assert "total_words" not in sent, "crawl bookkeeping is not a company fact"
    assert f"[1] (website) https://{DOMAIN}/team" in sent, "most similar first"
    assert result.citations.passages == (1,)
    assert result.citations.signals is True
    assert result.problems == []
    assert (result.model, result.declined) == ("stub-model", False)
    await db_session.rollback()


async def test_signals_are_sent_even_when_retrieval_ranks_nothing_useful(
    db_session,
) -> None:
    """A2, at the level that matters: the context is assembled from what the
    database holds, and the facts are there regardless of ranking."""
    await _prospect(
        db_session, DOMAIN, {"open_roles_seen": 0}, [("/x", "unrelated", FAR)]
    )
    model = _Model("Insufficient evidence. Nothing here mentions hiring.")

    result = await answer(
        db_session,
        DOMAIN,
        "Are they hiring?",
        embedder=_embedder(),
        generator=model,
        count_tokens=_words,
    )

    assert "open_roles_seen = 0" in model.messages[-1]["content"]
    assert result.declined is True
    assert result.problems == [], "declining is a correct answer, not a failure"
    await db_session.rollback()


async def test_another_prospects_passages_never_reach_the_context(db_session) -> None:
    """A1, one layer up. The other company's chunk is a PERFECT match for the
    query vector and must still not appear, because retrieval goes through
    the prospect scope and nothing else."""
    await _seed(db_session)
    await _prospect(
        db_session, OTHER, {}, [("/secret", "Another company's page.", NEAR)]
    )
    model = _Model("Answer [1].")

    await answer(
        db_session,
        DOMAIN,
        "Who works there?",
        embedder=_embedder(),
        generator=model,
        count_tokens=_words,
    )

    assert OTHER not in model.messages[-1]["content"]
    await db_session.rollback()


async def test_a_citation_to_a_passage_that_was_never_sent_is_reported(
    db_session,
) -> None:
    await _seed(db_session)

    result = await answer(
        db_session,
        DOMAIN,
        "Are they growing?",
        embedder=_embedder(),
        generator=_Model("They raised a round [9]."),
        count_tokens=_words,
    )

    assert result.citations.invalid == ("9",)
    assert any("[9]" in p for p in result.problems)
    await db_session.rollback()


async def test_a_question_id_is_asked_in_the_ground_truth_wording(db_session) -> None:
    await _seed(db_session)
    model = _Model("Insufficient evidence. No page says.")

    result = await answer(
        db_session,
        DOMAIN,
        question_id="q2_technical_capacity",
        embedder=_embedder(),
        generator=model,
        count_tokens=_words,
    )

    assert result.question == QUESTIONS["q2_technical_capacity"]
    assert model.messages[-1]["content"].endswith(
        f"QUESTION: {QUESTIONS['q2_technical_capacity']}"
    )
    assert result.is_suggestion is False
    await db_session.rollback()


async def test_the_fifth_question_is_marked_as_a_suggestion(db_session) -> None:
    await _seed(db_session)
    model = _Model("Open with their engineering team [1].")

    result = await answer(
        db_session,
        DOMAIN,
        question_id=SUGGESTION_ID,
        embedder=_embedder(),
        generator=model,
        count_tokens=_words,
    )

    assert result.is_suggestion is True
    assert "suggestion, not a fact" in model.messages[0]["content"]
    await db_session.rollback()


async def test_an_unknown_prospect_says_how_to_add_one(db_session) -> None:
    with pytest.raises(AnswerUnavailable, match="make crawl"):
        await answer(
            db_session,
            "never-loaded.test",
            "Anything?",
            embedder=_embedder(),
            generator=_Model("x"),
            count_tokens=_words,
        )


async def test_a_prospect_with_nothing_embedded_says_how_to_embed(db_session) -> None:
    await db_session.execute(
        text("INSERT INTO prospects (company_name, domain) VALUES ('P', 'bare.test')")
    )
    await db_session.flush()

    with pytest.raises(AnswerUnavailable, match="make embed"):
        await answer(
            db_session,
            "bare.test",
            "Anything?",
            embedder=_embedder(),
            generator=_Model("x"),
            count_tokens=_words,
        )
    await db_session.rollback()
