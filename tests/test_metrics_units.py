"""Unit tests for `linestack.evaluation.metrics`. No database, no network, no key.

That the whole module is testable this way is the design, not a convenience:
the metrics decide whether a retrieval change ships (A3), and a metric nobody
can exercise is a number nobody should trust.
"""

import pytest

from linestack.evaluation.metrics import (
    RECALL_CUTOFFS,
    LeakageError,
    check_no_leakage,
    first_hit_rank,
    ingestion_coverage,
    recall_at_k,
    recalls_for_question,
    signal_accuracy,
)

TEAM = "https://ex.test/team"
ABOUT = "https://ex.test/about"
BLOG = "https://ex.test/blog/a-post"


# ---------------------------------------------------------------------------
# recall@k
# ---------------------------------------------------------------------------
def test_recall_is_true_when_an_expected_document_is_within_the_cut_off() -> None:
    retrieved = [BLOG, ABOUT, TEAM]

    assert recall_at_k(retrieved, [TEAM], k=3) is True
    assert recall_at_k(retrieved, [TEAM], k=2) is False


def test_recall_is_measured_at_document_granularity() -> None:
    """Three chunks of one page are three retrieved chunks, and they really do
    use three of the five slots -- so the duplication is NOT collapsed before
    the cut-off. What is compared is the document, so a chunking change moves
    the score without changing what the score means (docs/evaluation.md §2.1).
    """
    retrieved = [BLOG, BLOG, BLOG, TEAM]

    assert recall_at_k(retrieved, [TEAM], k=3) is False
    assert recall_at_k(retrieved, [TEAM], k=4) is True


def test_a_pair_with_no_expected_urls_is_refused_rather_than_scored_zero() -> None:
    """An insufficient_evidence pair has nothing to retrieve. Scoring it 0
    would silently drag the primary metric down for pairs that are CORRECT --
    and docs/ground-truth.md §3 wants roughly a quarter of the set to be
    those, so the error would grow with the set."""
    with pytest.raises(ValueError, match="excluded, not scored 0"):
        recall_at_k([BLOG], [], k=5)


def test_the_rank_of_the_first_hit_is_reported_alongside_the_boolean() -> None:
    """A near miss and a rout are both "missed at k=5" and need different
    fixes. **[verified]** docs/open-questions.md §2.5: 110 of 111 for one
    phrasing, 4 of 111 for another."""
    retrieved = [BLOG] * 109 + [TEAM] + [ABOUT]

    assert first_hit_rank(retrieved, [TEAM]) == 110
    assert first_hit_rank([TEAM] + [BLOG] * 3, [TEAM]) == 1


def test_a_document_that_never_appears_has_no_rank() -> None:
    assert first_hit_rank([BLOG, ABOUT], [TEAM]) is None


def test_recalls_are_reported_at_every_cut_off() -> None:
    results = recalls_for_question("q2_technical_capacity", [BLOG, ABOUT, TEAM], [TEAM])

    assert [r.k for r in results] == list(RECALL_CUTOFFS)
    assert [r.hit for r in results] == [False, True, True, True]
    assert {r.first_hit_rank for r in results} == {3}


@pytest.mark.parametrize(
    "expected, retrieved",
    [
        ("https://ex.test/team", "https://ex.test/team/"),
        ("https://ex.test/team/", "https://ex.test/team"),
        ("https://EX.test/team", "https://ex.test/team"),
        ("HTTPS://ex.test/team", "https://ex.test/team"),
    ],
)
def test_urls_that_mean_the_same_page_match(expected: str, retrieved: str) -> None:
    """The author writes source_urls by hand from a list of crawled URLs; the
    database returns what the crawler stored. A trailing slash between the two
    would report a miss the reader cannot see."""
    assert recall_at_k([retrieved], [expected], k=1) is True


@pytest.mark.parametrize(
    "expected, retrieved",
    [
        ("https://ex.test/blog?page=1", "https://ex.test/blog?page=2"),
        ("https://ex.test/team", "https://ex.test/teams"),
        ("https://ex.test/a/b", "https://ex.test/a"),
    ],
)
def test_urls_that_mean_different_pages_do_not_match(
    expected: str, retrieved: str
) -> None:
    """Normalisation is deliberately small. Stripping query strings would
    inflate recall in exactly the cases where the author was being precise."""
    assert recall_at_k([retrieved], [expected], k=1) is False


# ---------------------------------------------------------------------------
# signal accuracy
# ---------------------------------------------------------------------------
def test_matching_signals_score_one() -> None:
    truth = {"has_team_page": True, "people_listed": 54}
    report = signal_accuracy(truth, {"has_team_page": True, "people_listed": 54})

    assert report.accuracy == 1.0
    assert report.mismatches == []


def test_a_mismatch_reports_both_values() -> None:
    """The defect this metric exists to catch, in its real historical shape:
    the crawler counted 162 matching leaves for 54 people (ADR-0003)."""
    report = signal_accuracy({"people_listed": 54}, {"people_listed": 162})

    assert report.accuracy == 0.0
    assert len(report.mismatches) == 1
    assert "ground truth 54" in str(report.mismatches[0])
    assert "computed 162" in str(report.mismatches[0])


def test_an_unchecked_signal_is_not_a_mismatch() -> None:
    """Half-written ground-truth files are normal. Scoring an absent value as
    a disagreement reports the author's progress as the crawler's defect."""
    report = signal_accuracy(
        {"people_listed": 54, "open_roles_seen": None},
        {"people_listed": 54, "open_roles_seen": 3},
    )

    assert report.mismatches == []
    assert "open_roles_seen" in report.unchecked
    assert "technical_roles_open" in report.unchecked, "absent entirely"
    assert report.compared == 1


def test_a_file_with_no_checked_signals_has_no_accuracy() -> None:
    """None, not 1.0. It has not scored perfectly; it has not been scored."""
    assert signal_accuracy({}, {"people_listed": 3}).accuracy is None


def test_a_signal_the_crawler_did_not_compute_is_a_mismatch() -> None:
    """The author checked it and the crawler produced nothing. That is a
    disagreement, not an unchecked field."""
    report = signal_accuracy({"people_listed": 14}, {})

    assert [c.computed for c in report.mismatches] == [None]


# ---------------------------------------------------------------------------
# ingestion coverage
# ---------------------------------------------------------------------------
def test_coverage_is_complete_when_every_cited_page_was_crawled() -> None:
    report = ingestion_coverage([TEAM, ABOUT], {TEAM, ABOUT, BLOG})

    assert report.complete is True
    assert report.total == 2


def test_a_missing_page_carries_the_reason_it_is_missing() -> None:
    """A5. "We looked and it failed for this reason" and "we never looked" are
    different facts, and only one of them is about the site."""
    report = ingestion_coverage(
        [TEAM, ABOUT], {ABOUT}, outcomes={TEAM: "budget_exhausted"}
    )

    assert report.complete is False
    assert str(report.missing[0]) == f"{TEAM}: not stored, outcome budget_exhausted"
    assert report.unexplained == []


def test_a_page_missing_with_no_recorded_outcome_is_the_serious_case() -> None:
    """A page that vanished without a reason code is the defect ADR-0011 and
    ADR-0012 exist to prevent. If one shows up here, it came back."""
    report = ingestion_coverage([TEAM], set())

    assert len(report.unexplained) == 1
    assert "never attempted" in str(report.unexplained[0])


def test_coverage_matches_urls_the_same_way_recall_does() -> None:
    """One normalisation, or coverage and recall disagree about whether a page
    was crawled, and the disagreement looks like a retrieval failure."""
    assert ingestion_coverage(["https://ex.test/team/"], {TEAM}).complete is True


# ---------------------------------------------------------------------------
# the leakage gate
# ---------------------------------------------------------------------------
def test_a_run_whose_chunks_all_resolve_inside_the_scope_passes() -> None:
    check_no_leakage([1, 2, 3], {1, 2, 3, 4})


def test_a_chunk_the_scope_refuses_to_claim_voids_the_run() -> None:
    """A1 is a hard boundary, so this raises rather than scoring
    (docs/evaluation.md §1). There is no acceptable non-zero leakage rate."""
    with pytest.raises(LeakageError, match="void, not merely worse"):
        check_no_leakage([1, 99], {1})


def test_the_leakage_error_names_the_offending_chunks() -> None:
    """An assertion that fires without saying what fired it costs an hour."""
    with pytest.raises(LeakageError, match=r"\[99, 100\]"):
        check_no_leakage([1, 99, 100], {1})
