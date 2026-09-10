"""Unit tests for `linestack.generation`. No model, no database, no network.

Everything that decides whether an answer can be trusted is testable without a
model: what goes into the context, in what order, under what budget, and
whether the citations in the reply point at anything real. The model is the
one part these tests do not cover, and it is the part the harness exists for.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from linestack.evaluation.dataset import QUESTION_IDS, QUESTIONS
from linestack.generation.answer import (
    ANSWER_SIGNALS,
    Citations,
    Context,
    GeneratedAnswer,
    assemble_context,
    is_decline,
    parse_citations,
    render_context,
    signals_block,
)
from linestack.generation.client import (
    LocalGenerator,
    build_generator,
    strip_reasoning,
    uses_openai_chat,
)
from linestack.generation.prompts import (
    INSUFFICIENT,
    PROMPT_VERSION,
    SUGGESTION_ID,
    SYSTEM_PROMPT,
    build_messages,
    question_text,
)


@dataclass
class _Hit:
    id: int
    content: str
    kind: str = "website"
    score: float = 0.5


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _count(text: str) -> int:
    """A deterministic stand-in for tiktoken: one token per word."""
    return len(text.split())


# ---------------------------------------------------------------------------
# the questions
# ---------------------------------------------------------------------------
def test_every_question_is_asked_in_the_words_the_ground_truth_uses() -> None:
    """One definition. A question asked in different words from the one the
    reference answers is a different question, and the pair would then be
    measuring the rewording."""
    spec = (
        Path(__file__).resolve().parent.parent / "docs" / "ground-truth.md"
    ).read_text()

    assert tuple(QUESTIONS) == QUESTION_IDS
    for qid in QUESTION_IDS:
        assert question_text(qid) == QUESTIONS[qid]
        assert QUESTIONS[qid] in spec, f"{qid}'s wording is not in docs/ground-truth.md"


def test_an_unknown_question_id_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown question id"):
        question_text("q9_made_up")


def test_the_fifth_question_exists_and_is_not_one_of_the_evaluated_four() -> None:
    assert SUGGESTION_ID not in QUESTION_IDS
    assert question_text(SUGGESTION_ID)


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------
def test_the_prompt_forbids_outside_knowledge_and_requires_citations() -> None:
    assert "only the COMPUTED FACTS and the RETRIEVED PASSAGES" in SYSTEM_PROMPT
    assert "[S]" in SYSTEM_PROMPT and "[1]" in SYSTEM_PROMPT
    assert INSUFFICIENT in SYSTEM_PROMPT


def test_the_prompt_is_versioned() -> None:
    """Without a version in the run record, a delta cannot be attributed to
    the prompt rather than to retrieval or the model (A3)."""
    assert PROMPT_VERSION


def test_the_question_is_the_last_thing_the_model_reads() -> None:
    messages = build_messages("Who are they?", "CONTEXT HERE")

    assert messages[0]["role"] == "system"
    assert messages[-1]["content"].endswith("QUESTION: Who are they?")
    assert messages[-1]["content"].index("CONTEXT HERE") < messages[-1][
        "content"
    ].index("QUESTION")


def test_a_suggestion_is_told_it_is_a_suggestion() -> None:
    plain = build_messages("q", "c")[0]["content"]
    suggestion = build_messages("q", "c", is_suggestion=True)[0]["content"]

    assert "suggestion, not a fact" in suggestion
    assert "suggestion, not a fact" not in plain


# ---------------------------------------------------------------------------
# the signals block
# ---------------------------------------------------------------------------
def test_every_signal_carries_what_it_measures() -> None:
    """A bare `people_listed: 53` invites "53 employees", which the
    ground-truth notes say is not the same thing. The meaning rides with the
    value (A4)."""
    block = signals_block({"people_listed": 53})

    assert "people_listed = 53" in block
    assert "not a headcount" in block


def test_crawl_bookkeeping_is_not_offered_as_a_fact_about_the_company() -> None:
    """`total_words: 18008` describes the crawl. A model handed it will
    eventually report it as something about the business."""
    block = signals_block(
        {"total_words": 18008, "pages_crawled": 37, "people_listed": 3}
    )

    assert "total_words" not in block
    assert "pages_crawled" not in block
    assert "people_listed" in block


def test_no_signals_is_said_rather_than_left_blank() -> None:
    """Nothing computed is information. A missing block is indistinguishable
    from a bug."""
    assert "none were computed" in signals_block({})


def test_null_and_boolean_signals_are_written_as_values() -> None:
    block = signals_block({"has_team_page": True, "latest_post_date": None})

    assert "has_team_page = true" in block
    assert "latest_post_date = null" in block


def test_every_answer_signal_is_one_the_crawler_actually_computes() -> None:
    """A signal listed here that `ingest.Signals` does not produce would never
    appear, silently."""
    from dataclasses import fields

    import ingest

    computed = {f.name for f in fields(ingest.Signals)}
    assert set(ANSWER_SIGNALS) <= computed


# ---------------------------------------------------------------------------
# context assembly
# ---------------------------------------------------------------------------
def test_signals_are_in_the_context_even_when_retrieval_returned_nothing() -> None:
    """A2: a computed fact does not compete with vector similarity for a place
    in the context window."""
    context = assemble_context(
        {"people_listed": 3}, [], {}, budget_tokens=100, count_tokens=_count
    )

    assert "people_listed = 3" in render_context(context)
    assert context.passages == ()
    assert "(none were retrieved)" in render_context(context)


def test_passages_are_numbered_in_rank_order_with_their_source() -> None:
    hits = [_Hit(10, "first"), _Hit(20, "second")]
    urls = {10: "https://ex.test/a", 20: "https://ex.test/b"}

    context = assemble_context({}, hits, urls, budget_tokens=1000, count_tokens=_count)

    assert [(p.number, p.chunk_id) for p in context.passages] == [(1, 10), (2, 20)]
    rendered = render_context(context)
    assert "[1] (website) https://ex.test/a" in rendered
    assert rendered.index("[1]") < rendered.index("[2]")


def test_passages_that_do_not_fit_are_dropped_and_recorded() -> None:
    """Never silently. A miss on evidence retrieval DID find is a budget
    failure, not a retrieval one, and they need different fixes (A8)."""
    hits = [_Hit(1, _words(40)), _Hit(2, _words(40)), _Hit(3, _words(40))]

    context = assemble_context({}, hits, {}, budget_tokens=100, count_tokens=_count)

    assert [p.chunk_id for p in context.passages] == [1]
    assert context.dropped == (2, 3)


def test_admission_is_a_rank_prefix_not_a_best_fit() -> None:
    """Skipping a long passage for a shorter one further down would replace
    "top k by similarity" with "top k that happened to be short" -- a
    retrieval change made by the budget."""
    hits = [_Hit(1, _words(10)), _Hit(2, _words(500)), _Hit(3, _words(5))]

    context = assemble_context({}, hits, {}, budget_tokens=100, count_tokens=_count)

    assert [p.chunk_id for p in context.passages] == [1]
    assert context.dropped == (2, 3), "the short third passage is NOT slipped in"


def test_the_top_passage_is_admitted_even_when_it_alone_breaks_the_budget() -> None:
    """Sending none would force a decline for reasons that have nothing to do
    with the company. It is flagged instead."""
    context = assemble_context(
        {}, [_Hit(1, _words(500))], {}, budget_tokens=100, count_tokens=_count
    )

    assert [p.chunk_id for p in context.passages] == [1]
    assert context.over_budget is True


def test_an_unresolved_source_is_labelled_not_invented() -> None:
    context = assemble_context(
        {}, [_Hit(1, "x")], {}, budget_tokens=100, count_tokens=_count
    )

    assert context.passages[0].source_url == "(source not resolved)"


# ---------------------------------------------------------------------------
# citations: the faithfulness check that needs no judge
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reply, passages, signals",
    [
        ("They build Rails apps [1].", (1,), False),
        ("They list 53 people [S].", (), True),
        ("Both [1] and [3] say so, as does [S].", (1, 3), True),
        ("Several pages agree [1, 2].", (1, 2), False),
        ("Several pages agree [1-3].", (1, 2, 3), False),
        ("Adjacent [2][1].", (1, 2), False),
    ],
)
def test_citations_are_read_in_every_shape_a_model_writes_them(
    reply, passages, signals
) -> None:
    citations = parse_citations(reply, passage_count=5)

    assert citations.passages == passages
    assert citations.signals is signals
    assert citations.invalid == ()


def test_a_citation_to_a_passage_that_was_never_sent_is_caught() -> None:
    """[7] when five passages were in the context is a fabricated citation --
    the one faithfulness failure that can be detected mechanically."""
    citations = parse_citations(
        "They raised a round [7] and hired [2].", passage_count=5
    )

    assert citations.passages == (2,)
    assert citations.invalid == ("7",)


def test_bracketed_text_that_is_not_a_citation_is_ignored() -> None:
    assert parse_citations(
        "They said [sic] it was fine.", passage_count=5
    ) == Citations((), False, ())


def test_a_decline_is_recognised_by_its_fixed_phrase() -> None:
    assert is_decline("Insufficient evidence. No page mentions funding.")
    assert is_decline("  insufficient evidence -- nothing about hiring.")
    assert not is_decline("They have insufficient evidence of growth [1].")


def _answer(reply: str, passage_count: int = 3) -> GeneratedAnswer:
    context = Context(
        signals="",
        signals_tokens=0,
        passages=tuple(),
        dropped=(),
        budget_tokens=100,
    )
    return GeneratedAnswer(
        question="q",
        question_id="q1_what_and_to_whom",
        text=reply,
        declined=is_decline(reply),
        citations=parse_citations(reply, passage_count),
        context=context,
        model="stub",
        prompt_version=PROMPT_VERSION,
    )


def test_an_answer_that_cites_nothing_is_flagged() -> None:
    """Rule 2 of the prompt, checked. A confident uncited claim is the failure
    mode this project exists to prevent (docs/ground-truth.md §3)."""
    assert (
        "without citing anything"
        in _answer("They are a Rails consultancy.").problems[0]
    )


def test_a_decline_needs_no_citation() -> None:
    assert _answer("Insufficient evidence. Nothing mentions funding.").problems == []


def test_a_fabricated_citation_is_reported_as_a_problem() -> None:
    problems = _answer("They are growing [9].").problems

    assert any("not in the context" in p and "[9]" in p for p in problems)


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model, openai",
    [
        ("gpt-4o-mini", True),
        ("o3-mini", True),
        ("Qwen/Qwen3-1.7B", False),
        ("Qwen/Qwen3-4B", False),
    ],
)
def test_the_model_name_decides_local_or_openai(model: str, openai: bool) -> None:
    assert uses_openai_chat(model) is openai


def test_a_local_model_builds_without_loading_anything() -> None:
    """Lazy, like the embedder: constructing a generator must not download or
    load a model. A run that answers nothing loads nothing."""
    generator = build_generator("Qwen/Qwen3-1.7B")

    assert isinstance(generator, LocalGenerator)
    assert generator._model is None
    assert generator.load_seconds == 0.0


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("<think>let me reason</think>They build apps [1].", "They build apps [1]."),
        ("<think>\nmulti\nline\n</think>\n\nAnswer [S].", "Answer [S]."),
        # The token limit cut the model off mid-thought: no closing tag.
        ("Answer [1]. <think>and then", "Answer [1]."),
        ("No reasoning at all [2].", "No reasoning at all [2]."),
    ],
)
def test_reasoning_never_reaches_the_citation_checker(raw: str, clean: str) -> None:
    """A reasoning block left in the answer would be read as claims."""
    assert strip_reasoning(raw) == clean
