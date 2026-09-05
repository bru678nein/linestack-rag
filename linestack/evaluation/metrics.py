"""Responsibility: computing the metrics, and keeping them separate.

Owns:
  - retrieval recall@k, per question, at document granularity so the metric
    does not change meaning when chunk size changes;
  - signal accuracy, an exact comparison against hand-checked ground truth;
  - ingestion coverage, which answers "was the evidence even crawled?" before
    any failure is attributed to retrieval;
  - the cross-prospect leakage assertion, which is a gate and not a metric: a
    run in which any retrieved chunk belongs to another prospect is a failed
    run, not a lower score.

Does not own: combining any of these into a single number. A change is accepted
or rejected on recall; the rest is diagnosis.

Does not own: any I/O. Every function here takes data that has already been
fetched and returns a value. That is what makes the whole module testable
without a database, a network, or a key -- and the reason the numbers below can
be trusted is that they are computed by code that is exercised on every push.

## Why there is no faithfulness or correctness here

`docs/evaluation.md` §2.2 and §2.3 specify both, measured with `ragas`. Neither
is implemented, and the reason is recorded rather than left as a gap.

**[verified] 2026-09-05.** `make install-eval` has never worked. `ragas==0.4.3`
cannot be installed alongside this project's `openai==3.7.0`: ragas depends
unconditionally on `instructor`, and the newest `instructor` (1.16.0) requires
`openai>=2.0.0,<3.0.0`. It is not fixable by choosing another ragas -- 0.4.3 is
the latest release, and ragas itself asks only for `openai>=1.0.0`. The ceiling
comes from instructor. The pin was marked RESOLVED, which meant "exists on
PyPI"; it does exist, and it has never been installable here.

Underneath that is the reason it stays unimplemented rather than being worked
around. Both metrics are LLM-judged, `settings.eval_judge_model` is an OpenAI
model, and this project runs with no OpenAI key by design (ADR-0017). Even
resolved, neither metric could execute.

So the harness computes the four metrics that need no judge, no key and no
network. One of them, recall@k, is the primary metric in `docs/evaluation.md`
§2.1 and the exact number ADR-0009 names as its trigger for hybrid search. See
ADR-0020.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# The signals compared against hand-checked truth (docs/evaluation.md §2.4).
# `notes` is prose written by the author to record HOW a value was checked, and
# is deliberately not compared.
SIGNAL_FIELDS = (
    "has_team_page",
    "people_listed",
    "open_roles_seen",
    "technical_roles_open",
    "latest_post_date",
)

# Cut-offs reported for recall. 5 is settings.retrieval_top_k, the one that
# decides anything; the others exist so that "the chunk was 6th" and "the chunk
# was 90th" are distinguishable failures. That distinction is not academic --
# docs/open-questions.md §2.5 turned on it.
RECALL_CUTOFFS = (1, 3, 5, 10)


class LeakageError(AssertionError):
    """A1 violated: a retrieved chunk did not belong to the prospect asked
    about. Raised, never returned as a score. See `check_no_leakage`."""


# --------------------------------------------------------------------------- #
# retrieval recall@k
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Recall:
    """Recall for one question at one cut-off."""

    question_id: str
    k: int
    hit: bool
    #: Where in the ranking the first correct document appeared, 1-based, or
    #: None if it never did within the retrieved list. Recorded because "missed
    #: at k=5" and "missed at k=5, found at 110" are different problems: the
    #: first is a cut-off, the second is a ranking failure (A8).
    first_hit_rank: int | None


def recall_at_k(
    retrieved_urls: Sequence[str],
    expected_urls: Collection[str],
    k: int,
) -> bool:
    """Did at least one of the top `k` retrieved chunks come from an expected
    document?

    Document granularity, not chunk granularity, and that is the whole point.
    A chunking change that moves a boundary should show up as a change in the
    SCORE, never as a change in what the score measures (docs/evaluation.md
    §2.1). Comparing chunk ids would silently redefine the metric on every
    ADR-0005 change and make two runs incomparable while looking comparable.

    `retrieved_urls` is one URL per retrieved chunk, in rank order, and may
    repeat: several chunks of one page are several retrieved chunks. The
    duplication is not collapsed before the cut-off, because k is a cut-off on
    what the system actually returns, and three chunks of the same page really
    do use three of the five slots.
    """
    if not expected_urls:
        raise ValueError(
            "recall is undefined with no expected URLs; an insufficient_evidence "
            "pair has nothing to retrieve and must be excluded, not scored 0"
        )
    wanted = {_normalise_url(u) for u in expected_urls}
    return any(_normalise_url(u) in wanted for u in retrieved_urls[:k])


def first_hit_rank(
    retrieved_urls: Sequence[str], expected_urls: Collection[str]
) -> int | None:
    """1-based rank of the first chunk from an expected document, or None.

    Reported alongside the pass/fail because a boolean cannot tell a near miss
    from a rout, and the difference decides what to fix. **[verified]**
    docs/open-questions.md §2.5: the right page ranked 110 of 111 for one
    phrasing and 4 of 111 for another. Both are "missed at k=5"; only one of
    them is a cut-off problem.
    """
    wanted = {_normalise_url(u) for u in expected_urls}
    for rank, url in enumerate(retrieved_urls, start=1):
        if _normalise_url(url) in wanted:
            return rank
    return None


def recalls_for_question(
    question_id: str,
    retrieved_urls: Sequence[str],
    expected_urls: Collection[str],
    cutoffs: Iterable[int] = RECALL_CUTOFFS,
) -> list[Recall]:
    """Recall at every cut-off for one question, plus where the first hit was."""
    rank = first_hit_rank(retrieved_urls, expected_urls)
    return [
        Recall(
            question_id=question_id,
            k=k,
            hit=recall_at_k(retrieved_urls, expected_urls, k),
            first_hit_rank=rank,
        )
        for k in cutoffs
    ]


def _normalise_url(url: str) -> str:
    """Compare URLs the way a reader means them.

    A trailing slash and a case-different scheme or host are the same page; a
    different path is not. Kept deliberately small: query strings and fragments
    are NOT stripped, because `?page=2` is a different page and silently
    treating it as the same one would inflate recall in exactly the cases where
    the author was being precise.
    """
    url = url.strip()
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/?#]+)(.*)$", url)
    if not match:
        return url.rstrip("/")
    origin, rest = match.groups()
    return origin.lower() + (rest.rstrip("/") or "")


# --------------------------------------------------------------------------- #
# signal accuracy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SignalComparison:
    field_name: str
    expected: object
    computed: object

    @property
    def agrees(self) -> bool:
        return self.expected == self.computed

    def __str__(self) -> str:
        verdict = "ok" if self.agrees else "MISMATCH"
        return (
            f"{self.field_name}: {verdict} "
            f"(ground truth {self.expected!r}, computed {self.computed!r})"
        )


@dataclass
class SignalReport:
    comparisons: list[SignalComparison] = field(default_factory=list)
    #: Fields the ground-truth author has not recorded a value for. Not
    #: failures: an unchecked field is unchecked, and counting it as a
    #: disagreement would make an incomplete file look like a broken crawler
    #: (A4).
    unchecked: list[str] = field(default_factory=list)

    @property
    def compared(self) -> int:
        return len(self.comparisons)

    @property
    def mismatches(self) -> list[SignalComparison]:
        return [c for c in self.comparisons if not c.agrees]

    @property
    def accuracy(self) -> float | None:
        """Share of compared fields that agree, or None if nothing was
        compared. None rather than 1.0: a file with no checked signals has not
        scored perfectly, it has not been scored."""
        if not self.comparisons:
            return None
        return 1 - len(self.mismatches) / len(self.comparisons)


def signal_accuracy(
    ground_truth: Mapping[str, object],
    computed: Mapping[str, object],
    fields: Iterable[str] = SIGNAL_FIELDS,
) -> SignalReport:
    """Compare hand-checked signals against what the crawler computed.

    Exact comparison, no judge, no noise, and the cheapest thing in the whole
    harness. It is also the one with the best record: 162 people against a
    hand-counted 54, and 4 open roles against 0, were both found this way
    (ADR-0003), as were 0 people against 57 (ADR-0014) and 0 against 14
    (ADR-0018).

    A field the author has not filled in is `unchecked`, never a mismatch. The
    ground-truth set is written by hand over hours and half-written files are
    normal; scoring an absent value as a disagreement would report the author's
    progress as the crawler's defect.
    """
    report = SignalReport()
    for name in fields:
        if name not in ground_truth or ground_truth[name] is None:
            report.unchecked.append(name)
            continue
        report.comparisons.append(
            SignalComparison(name, ground_truth[name], computed.get(name))
        )
    return report


# --------------------------------------------------------------------------- #
# ingestion coverage
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MissingSource:
    """An expected URL that is not in `documents`, and why -- if we know."""

    url: str
    #: The `crawl_page_outcomes` reason, or None when the URL was never even
    #: attempted. The distinction is the whole of A5: "we looked and it failed
    #: for this reason" and "we never looked" are different facts, and only one
    #: of them is about the site.
    outcome: str | None

    def __str__(self) -> str:
        if self.outcome is None:
            return f"{self.url}: never attempted (not in crawl_page_outcomes)"
        return f"{self.url}: not stored, outcome {self.outcome}"


@dataclass
class CoverageReport:
    present: list[str] = field(default_factory=list)
    missing: list[MissingSource] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.present) + len(self.missing)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def unexplained(self) -> list[MissingSource]:
        """Missing URLs with no recorded reason. These are the serious ones:
        a crawler that drops a page silently is the defect ADR-0011 and
        ADR-0012 exist to prevent, and one appearing here means it came back.
        """
        return [m for m in self.missing if m.outcome is None]


def ingestion_coverage(
    expected_urls: Iterable[str],
    crawled_urls: Collection[str],
    outcomes: Mapping[str, str] | None = None,
) -> CoverageReport:
    """Was the evidence a ground-truth answer cites actually ingested?

    This runs BEFORE recall is interpreted, and that order is the point. A
    question whose evidence was never crawled scores 0 recall, and reading that
    0 as a retrieval failure sends someone to tune ranking over a corpus that
    does not contain the answer (docs/evaluation.md §2.5).

    A missing URL is reported with its `crawl_page_outcomes` reason when there
    is one. Without that, "not retrieved" and "not crawled" and "never
    attempted" collapse into one number, and only the first is about retrieval.
    """
    outcomes = outcomes or {}
    crawled = {_normalise_url(u) for u in crawled_urls}
    by_normalised_outcome = {_normalise_url(u): o for u, o in outcomes.items()}

    report = CoverageReport()
    for url in expected_urls:
        key = _normalise_url(url)
        if key in crawled:
            report.present.append(url)
        else:
            report.missing.append(MissingSource(url, by_normalised_outcome.get(key)))
    return report


# --------------------------------------------------------------------------- #
# the leakage gate
# --------------------------------------------------------------------------- #
def check_no_leakage(
    retrieved_ids: Sequence[int], resolved_ids: Collection[int]
) -> None:
    """Fail the run if a retrieved chunk did not resolve inside the prospect.

    A1 is a hard boundary, so this raises rather than scoring
    (docs/evaluation.md §1). There is no acceptable non-zero leakage rate, and
    a run that leaked is a failed run, not a run with a worse number.

    **What this can and cannot prove, stated plainly.** ADR-0009 freezes the
    ranking SELECT at `id, content, kind, score`, so a retrieved chunk does not
    carry its `prospect_id` and cannot be checked directly -- and widening that
    SELECT to add one would edit a decision this project treats as frozen. What
    is available is `ProspectScope.source_urls`, whose own WHERE clause is
    scoped to the prospect: a chunk id that comes back from ranking but does
    NOT resolve through that lookup is a chunk the scope refuses to claim.

    So this detects a foreign chunk arriving from ranking. It does not detect
    a scope built around the wrong prospect id in the first place -- both
    queries would agree, consistently and wrongly. That case is covered where
    it belongs, by the composite foreign key in migrations/0001 and by
    tests/test_isolation_contract.py, not by a metric.

    An unresolved id can also mean the row was deleted between the two
    statements. Inside one transaction that cannot happen, and the harness runs
    them in one; outside it, this raises on something that is not leakage,
    which is the right direction for a gate to be wrong in.
    """
    foreign = [i for i in retrieved_ids if i not in resolved_ids]
    if foreign:
        raise LeakageError(
            f"{len(foreign)} retrieved chunk(s) did not resolve within the "
            f"prospect scope: {foreign}. A1 is a hard boundary; this run is "
            f"void, not merely worse. Check the scope's prospect_id and the "
            f"composite foreign key before re-running."
        )
