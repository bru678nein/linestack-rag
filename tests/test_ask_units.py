"""Unit tests for `linestack.retrieval.ask`'s command line. No database.

What is under test is which question gets asked and which words get searched.
An evaluated id must search with its written query (ADR-0025), because that is
the ranking the model is given: a command that showed another ranking would
send someone to fix a retrieval failure that answer() never had.
"""

import pytest

from linestack.evaluation.dataset import QUESTION_IDS, QUESTIONS
from linestack.retrieval.ask import build_parser, question_and_query
from linestack.retrieval.queries import retrieval_query

PROSPECT = ["--prospect", "fly.io"]


def test_an_id_searches_the_way_answer_does() -> None:
    qid = "q2_technical_capacity"

    question, query = question_and_query(None, qid)

    assert question == QUESTIONS[qid], "asked in the ground-truth set's words"
    assert query == retrieval_query(QUESTIONS[qid], qid)


def test_free_text_searches_with_its_own_words() -> None:
    assert question_and_query("Who works here?") == (
        "Who works here?",
        "Who works here?",
    )


@pytest.mark.parametrize(
    "question, qid", [(None, None), ("", None), (None, "q9_made_up")]
)
def test_nothing_to_ask_is_refused(question, qid) -> None:
    with pytest.raises(ValueError):
        question_and_query(question, qid)


def test_every_evaluated_id_is_accepted() -> None:
    for qid in QUESTION_IDS:
        args = build_parser().parse_args([*PROSPECT, "--id", qid])

        assert (args.id, args.question, args.k, args.full) == (qid, None, None, False)


def test_a_depth_or_the_whole_prospect_is_read() -> None:
    parser = build_parser()

    assert parser.parse_args([*PROSPECT, "--question", "q?", "-k", "25"]).k == 25
    assert parser.parse_args([*PROSPECT, "--id", QUESTION_IDS[0], "--full"]).full


@pytest.mark.parametrize(
    "argv",
    [
        PROSPECT,  # neither a question nor an id
        [*PROSPECT, "--question", "q?", "--id", "q1_what_and_to_whom"],
        [*PROSPECT, "--id", "q9_made_up"],
        # The suggestion has no written query, so its ranking is its own
        # words, which Q= already shows; and it is not an evaluated question.
        [*PROSPECT, "--id", "q5_first_approach"],
        [*PROSPECT, "--question", "q?", "-k", "3", "--full"],
        [*PROSPECT, "--question", "q?", "-k", "0"],
        ["--question", "q?"],  # no prospect
    ],
)
def test_the_command_line_refuses_what_it_cannot_mean(argv) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)

    assert exc.value.code == 2
