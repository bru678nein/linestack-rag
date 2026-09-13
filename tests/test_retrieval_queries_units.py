"""Unit tests for `linestack.retrieval.queries`. No model, no database.

What matters about the queries is what they must NOT be: tuned to the
ground-truth set. A query that names a prospect would score well on that
prospect for a reason nobody could reproduce on the next one.
"""

from pathlib import Path

import yaml

from linestack.evaluation.dataset import QUESTION_IDS
from linestack.generation.prompts import SUGGESTION_ID
from linestack.retrieval.queries import (
    QUERY_VERSION,
    QUESTION_AS_QUERY,
    RETRIEVAL_QUERIES,
    query_version,
    retrieval_query,
)

GROUND_TRUTH = Path(__file__).resolve().parent.parent / "eval" / "ground_truth"


def test_every_evaluated_question_has_a_query_and_nothing_else_does() -> None:
    assert tuple(RETRIEVAL_QUERIES) == QUESTION_IDS


def test_an_evaluated_question_searches_with_its_query() -> None:
    query = retrieval_query("What evidence ...?", "q2_technical_capacity", enabled=True)

    assert query == RETRIEVAL_QUERIES["q2_technical_capacity"]


def test_anything_else_searches_with_its_own_words() -> None:
    """A free-text question from `make answer`, and the q5 suggestion."""
    assert retrieval_query("Who are they?", enabled=True) == "Who are they?"
    assert retrieval_query("q5?", SUGGESTION_ID, enabled=True) == "q5?"


def test_switched_off_every_question_searches_with_its_own_words() -> None:
    """The baseline, reproducible in the same code (A3)."""
    question = "What evidence ...?"

    assert retrieval_query(question, "q2_technical_capacity", enabled=False) == question
    assert query_version(enabled=False) == QUESTION_AS_QUERY
    assert query_version(enabled=True) == QUERY_VERSION


def test_no_query_names_a_prospect_in_the_ground_truth_set() -> None:
    names = set()
    for path in GROUND_TRUTH.glob("*.yaml"):
        prospect = (yaml.safe_load(path.read_text()) or {}).get("prospect", {})
        domain = str(prospect.get("domain", "")).lower()
        names |= {domain, domain.split(".")[0]}
    names.discard("")

    for qid, query in RETRIEVAL_QUERIES.items():
        for name in names:
            assert name not in query.lower(), f"{qid}'s query names {name}"
