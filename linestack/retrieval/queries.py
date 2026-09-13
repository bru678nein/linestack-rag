"""Responsibility: the text retrieval searches with, for each evaluated question.

The model is asked the question in the ground-truth set's exact words
(`linestack.generation.prompts`). Retrieval does not have to search with those
words, and measured on 2026-09-03 and again on 2026-09-11 it should not: the
q2 question says "evidence", "in-house", "technical" and "capacity", and
neither team page that answers it contains one of them. fly.io's roster ranked
97th of 110 chunks for the question and 4th for the page's own vocabulary
(docs/open-questions.md §2.5, ADR-0025).

So each evaluated question gets a search query written in the vocabulary of
the kind of page that answers it. Two rules keep this from becoming tuning to
the ground-truth set:

- A query is written from the question's definition (`QUESTION_SCOPE` in
  `linestack.generation.prompts`), never from a page known to answer it. It
  may name a kind of page and the words such pages use; it may not name a
  company, a product or anything only one site says.
- The queries were written once, on 2026-09-13, before the first measurement,
  and are not edited to move a result. A changed query is a new
  QUERY_VERSION with its own before-and-after (A3).

A question with no entry here -- a free-text question, or the q5 suggestion --
searches with its own words, as before.
"""

from __future__ import annotations

from linestack.config import settings

# Bump on ANY change to the text below. Recorded with every run, because a
# recall figure from one set of queries is not comparable with another's.
QUERY_VERSION = "queries-v1"

# What a run records when the switch is off and every question searches with
# its own words: the baseline this module is measured against.
QUESTION_AS_QUERY = "question"

RETRIEVAL_QUERIES = {
    "q1_what_and_to_whom": (
        "what we do: our products and services, what we build for customers, "
        "and the customers and industries we work with"
    ),
    "q2_technical_capacity": (
        "our team: the people who work here, developers, engineers and "
        "designers, and their job titles"
    ),
    "q3_growth_signals": (
        "company news: funding raised, investment, hiring and open positions, "
        "new offices, new products and teams launched"
    ),
    "q4_stated_need": (
        "we are looking for someone to join us; in this role you will; what "
        "we need to build, fix or rebuild next"
    ),
}


def query_version(enabled: bool | None = None) -> str:
    """What a run searched with, for the run record."""
    on = settings.use_retrieval_queries if enabled is None else enabled
    return QUERY_VERSION if on else QUESTION_AS_QUERY


def retrieval_query(
    question: str, question_id: str | None = None, *, enabled: bool | None = None
) -> str:
    """The text to embed and search with for one question.

    The written query for an evaluated question, or the question itself for
    anything else, or for everything when the switch is off.
    """
    on = settings.use_retrieval_queries if enabled is None else enabled
    if not on:
        return question
    return RETRIEVAL_QUERIES.get(question_id or "", question)
