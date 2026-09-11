"""Unit tests for `linestack.evaluation.harness`. No database, no network.

The harness's most important behaviour is what it REFUSES to score, and a
refusal nobody can exercise is a refusal nobody should trust. That is why
`pair_status` and `written_source_urls` are separate functions rather than
branches inside the run: this file is the only reason they can be checked
without Postgres.
"""

import pytest

from linestack.evaluation.harness import (
    NO_EVIDENCE_EXPECTED,
    NOT_INGESTED,
    SCORED,
    UNWRITTEN,
    PairResult,
    ProspectResult,
    RunRecord,
    pair_status,
    written_signals,
    written_source_urls,
)
from linestack.evaluation.metrics import recalls_for_question

TEAM = "https://ex.test/team"


def _written(**overrides) -> dict:
    pair = {
        "id": "q2_technical_capacity",
        "question": "What evidence is there of in-house technical capacity?",
        "reference": "They list 54 people with engineering titles.",
        "source_urls": [TEAM],
    }
    pair.update(overrides)
    return pair


# ---------------------------------------------------------------------------
# what counts as written
# ---------------------------------------------------------------------------
def test_a_fully_written_pair_is_scored() -> None:
    assert pair_status(_written()) == (SCORED, "")


def test_a_scaffolded_reference_is_not_scored() -> None:
    """The gate that lets the structural validator call a half-written file
    "in progress" instead of "broken". The validator says it is well-formed;
    the harness says it is not yet measurable. Different questions."""
    status, detail = pair_status(
        _written(reference="TODO 2-4 sentences, in the register a colleague would use")
    )

    assert status == UNWRITTEN
    assert "reference" in detail


def test_a_scaffolded_source_url_is_not_scored() -> None:
    """A placeholder URL matches nothing, so it would score 0 recall and look
    exactly like a ranking failure. That is the confusion this prevents."""
    status, detail = pair_status(
        _written(source_urls=["TODO https://... the pages you actually used"])
    )

    assert status == UNWRITTEN
    assert "source_urls" in detail


def test_an_empty_reference_is_not_scored_either() -> None:
    """A deleted TODO is not a written answer."""
    assert pair_status(_written(reference="   "))[0] == UNWRITTEN


@pytest.mark.parametrize("urls", [[], None, ["TODO https://..."], [TEAM, "TODO x"]])
def test_source_urls_are_unwritten_unless_every_entry_is_real(urls) -> None:
    """One placeholder among real URLs still makes the list unwritten. Scoring
    the partial list would quietly measure a different question from the one
    the author is answering."""
    assert written_source_urls(_written(source_urls=urls)) is None


def test_written_source_urls_are_returned_as_given() -> None:
    assert written_source_urls(_written()) == [TEAM]


def test_an_insufficient_evidence_pair_is_excluded_rather_than_failed() -> None:
    """It is a CORRECT answer, and the most valuable kind in the set
    (docs/ground-truth.md §3). Recall is undefined for it, not zero: scoring
    it 0 would drag the primary metric down for pairs that are right, and the
    error would grow as the set fills with them."""
    status, detail = pair_status(
        _written(expected_outcome="insufficient_evidence", source_urls=[])
    )

    assert status == NO_EVIDENCE_EXPECTED
    assert "undefined, not zero" in detail


def test_an_insufficient_evidence_pair_is_excluded_even_when_unwritten() -> None:
    """Order matters: the outcome is checked first, so a pair correctly marked
    as citing nothing is not reported as the author's unfinished work."""
    assert (
        pair_status(
            {"id": "q3", "reference": "TODO", "expected_outcome": NO_EVIDENCE_EXPECTED}
        )[0]
        == NO_EVIDENCE_EXPECTED
    )


# ---------------------------------------------------------------------------
# the run record
# ---------------------------------------------------------------------------
def _record(*pairs: PairResult) -> RunRecord:
    return RunRecord(
        started_at="2026-09-05T00:00:00",
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dimensions=384,
        retrieval_top_k=5,
        cutoffs=[1, 3, 5, 10],
        prospects=[ProspectResult(domain="ex.test", prospect_id=1, pairs=list(pairs))],
    )


def _scored(qid: str, retrieved: list[str]) -> PairResult:
    return PairResult(
        question_id=qid,
        status=SCORED,
        recalls=recalls_for_question(qid, retrieved, [TEAM]),
        retrieved_urls=retrieved,
    )


def test_recall_is_the_share_of_scored_pairs_whose_evidence_was_retrieved() -> None:
    record = _record(
        _scored("q1_what_and_to_whom", [TEAM]),
        _scored("q2_technical_capacity", ["https://ex.test/blog"] * 9 + [TEAM]),
    )

    assert record.recall_at(1) == 0.5
    assert record.recall_at(10) == 1.0


def test_unscored_pairs_are_not_in_the_denominator() -> None:
    """An unwritten pair must not make recall look worse. If it did, the
    metric would improve simply by someone writing more of the set, which is
    not a retrieval improvement."""
    record = _record(
        _scored("q1_what_and_to_whom", [TEAM]),
        PairResult("q2_technical_capacity", UNWRITTEN, "reference not written"),
        PairResult("q3_growth_signals", NO_EVIDENCE_EXPECTED, "cites nothing"),
        PairResult("q4_stated_need", NOT_INGESTED, "never crawled"),
    )

    assert record.recall_at(5) == 1.0


def test_a_run_that_scored_nothing_has_no_recall() -> None:
    """None, not 0.0. A set nobody has written has not scored badly; a 0.0 on
    a dashboard invites someone to try to improve it."""
    record = _record(PairResult("q1_what_and_to_whom", UNWRITTEN, "not written"))

    assert record.recall_at(5) is None
    assert any("no recall to report" in line for line in record.as_lines())


def test_the_run_record_states_the_configuration_that_produced_it() -> None:
    """A number without its configuration is not a measurement. Two embedding
    models' vectors are not comparable, so a recall figure without the model
    name cannot be compared with anything."""
    lines = "\n".join(_record(_scored("q1_what_and_to_whom", [TEAM])).as_lines())

    assert "BAAI/bge-small-en-v1.5" in lines
    assert "384 dimensions" in lines
    assert "top_k:     5" in lines


def test_the_report_says_where_the_first_hit_landed() -> None:
    """A boolean cannot tell a near miss from a rout, and the difference
    decides what to fix (docs/open-questions.md §2.5)."""
    record = _record(
        _scored("q2_technical_capacity", ["https://ex.test/b"] * 40 + [TEAM])
    )

    assert any("first hit at rank 41" in line for line in record.as_lines())


def test_the_report_names_an_unscored_pair_and_its_reason() -> None:
    """A5 applied to the dataset: "not scored" is one word covering several
    different facts, and only some of them are anyone's to fix."""
    lines = "\n".join(
        _record(
            PairResult(
                "q4_stated_need", NOT_INGESTED, "https://ex.test/x: never attempted"
            )
        ).as_lines()
    )

    assert "q4_stated_need: not_ingested" in lines
    assert "never attempted" in lines


# ---------------------------------------------------------------------------
# signals the author has not checked yet
# ---------------------------------------------------------------------------
def test_a_scaffolded_signal_is_unchecked_not_a_mismatch() -> None:
    """The scaffold writes `people_listed: TODO`. Compared as a value, that
    string disagrees with every number the crawler computed, and a file nobody
    has filled in would report the crawler as wrong about everything."""
    written = written_signals(
        {"people_listed": "TODO", "open_roles_seen": 0, "has_team_page": True}
    )

    assert written == {"open_roles_seen": 0, "has_team_page": True}


def test_no_signals_block_is_no_signals() -> None:
    assert written_signals(None) == {}


# ---------------------------------------------------------------------------
# declines, when the run generated answers
# ---------------------------------------------------------------------------
def _answered(qid: str, status: str, declined: bool, problems=()) -> PairResult:
    return PairResult(
        question_id=qid,
        status=status,
        answer="an answer",
        declined=declined,
        answer_problems=list(problems),
    )


def test_declines_are_counted_separately_for_the_two_kinds_of_pair() -> None:
    """Declining is right on an insufficient_evidence pair and wrong on an
    answerable one. Counted apart, because a model that declines everything
    would otherwise look perfect on the first count."""
    record = _record(
        _answered("q3_growth_signals", NO_EVIDENCE_EXPECTED, declined=True),
        _answered("q4_stated_need", NO_EVIDENCE_EXPECTED, declined=False),
        _answered("q1_what_and_to_whom", SCORED, declined=False),
        _answered("q2_technical_capacity", SCORED, declined=True),
    )

    assert record.declines_on_insufficient() == (1, 2)
    assert record.declines_on_answerable() == (1, 2)


def test_a_run_that_generated_nothing_has_no_decline_counts() -> None:
    """None, not (0, n): nothing was asked, so nothing declined or answered."""
    record = _record(PairResult("q3_growth_signals", NO_EVIDENCE_EXPECTED, "x"))

    assert record.declines_on_insufficient() is None
    assert not any("declined where" in line for line in record.as_lines())


def test_an_answer_that_could_not_be_produced_is_not_counted() -> None:
    """No chunks to answer from is a setup problem, not a decline and not an
    answer. Counting it either way would move the metric for the wrong reason."""
    unproduced = PairResult(
        "q3_growth_signals", NO_EVIDENCE_EXPECTED, "x", answer_detail="no chunks"
    )
    record = _record(
        unproduced, _answered("q4_stated_need", NO_EVIDENCE_EXPECTED, declined=True)
    )

    assert record.declines_on_insufficient() == (1, 1)
    assert any("not produced (no chunks)" in line for line in record.as_lines())


def test_the_report_says_which_way_each_answer_went() -> None:
    record = _record(
        _answered("q3_growth_signals", NO_EVIDENCE_EXPECTED, declined=False),
        _answered("q4_stated_need", NO_EVIDENCE_EXPECTED, declined=True),
        _answered("q2_technical_capacity", SCORED, declined=True),
    )
    lines = "\n".join(record.as_lines())

    assert "ANSWERED where it should have declined" in lines
    assert "declined, correctly" in lines
    assert "DECLINED where it should have answered" in lines
    assert "declined where it should:     1 of 2" in lines


def test_the_generation_setup_travels_with_the_counts() -> None:
    """A decline count from one model and prompt is not comparable with
    another's, so the report names both."""
    record = _record(_answered("q3_growth_signals", NO_EVIDENCE_EXPECTED, True))
    record.generation_model = "Qwen/Qwen3-1.7B"
    record.prompt_version = "answer-v1"

    lines = "\n".join(record.as_lines())

    assert "Qwen/Qwen3-1.7B" in lines and "answer-v1" in lines
