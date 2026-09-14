"""Responsibility: running the ground-truth set against a fixed configuration and
recording the result.

Owns: the run record -- corpus version, retrieval configuration, embedding
model, generation model and prompt version, judge model, and timings split into
embed / retrieve / generate -- and the delta table against the previous run of
the same corpus.

A3: no retrieval improvement ships without a recorded before-and-after, and a
change with no measured effect is reverted. This module is what makes that rule
enforceable rather than aspirational, so the run record is not optional output.

The corpus is frozen. If the harness re-crawls between runs, the corpus and the
configuration both changed and the delta means nothing.

## What it refuses to do

**It does not score an unwritten pair.** A pair whose `reference` or
`source_urls` still holds a `TODO` is reported as `unwritten` and excluded from
every metric, never scored 0. This is the gate that lets the structural
validator treat a half-written file as work in progress rather than as a broken
build: the validator says the file is well-formed, and the harness says it is
not yet measurable. Those are different questions and they belong in different
places.

**It does not score an `insufficient_evidence` pair for recall.** Those pairs
cite nothing by design, so there is nothing to retrieve. See ADR-0020.

**It does not average anything into one number.** Retrieval and diagnosis are
reported separately, always (docs/evaluation.md §1).

Faithfulness and answer correctness are not computed at all; ADR-0020 records
why, and `linestack/evaluation/metrics.py` carries the resolver output.

## What it does compute about answers

Given a generator, it also answers every pair it can -- the scored ones and the
insufficient_evidence ones -- through the same path a user gets, and records
whether each answer declined. That yields two counts no judge is needed for
(docs/evaluation.md §2.6): declines on insufficient_evidence pairs, which is
the correct answer there, and declines on answerable pairs, the opposite
failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from linestack.config import settings
from linestack.evaluation.dataset import TODO
from linestack.evaluation.metrics import (
    RECALL_CUTOFFS,
    CoverageReport,
    Recall,
    SignalReport,
    _normalise_url,
    check_no_leakage,
    ingestion_coverage,
    recalls_for_question,
    signal_accuracy,
)
from linestack.generation.answer import (
    DECLINE_INSIDE,
    DECLINE_WRAPPED,
    AnswerUnavailable,
    answer,
    decline_form,
)
from linestack.generation.prompts import PROMPT_VERSION
from linestack.retrieval.embedding import build_client, embed_question
from linestack.retrieval.queries import query_version, retrieval_query
from linestack.retrieval.scope import ProspectScope
from linestack.retrieval.search import search

#: How a pair was treated. `scored` is the only one that contributes to recall,
#: and the other three are reasons rather than failures -- which is why they are
#: named rather than collapsed into "skipped" (A5, applied to the dataset).
SCORED = "scored"
UNWRITTEN = "unwritten"  # the author has not filled it in yet
NO_EVIDENCE_EXPECTED = "insufficient_evidence"  # correct, and cites nothing
NOT_INGESTED = "not_ingested"  # cited pages are not in the corpus

#: Where the evidence stopped on an answered pair (roadmap 1.1). Named rather
#: than collapsed for the same reason as the statuses above (A5): a wrong answer
#: over a context that held an expected source is a generation failure, one
#: whose source the budget cut is a budget failure, and one whose source never
#: reached answer()'s top-k is a retrieval failure -- three different fixes
#: (A8). Mechanical: a label says what the model was shown, never whether its
#: answer was right.
ADMITTED = "admitted"
DROPPED_FOR_BUDGET = "dropped_for_budget"
NOT_RETRIEVED = "not_retrieved"
#: An insufficient_evidence pair cites nothing, so nothing on it can be
#: admitted. What the model was shown instead is the bait, and the recorded
#: passages -- their kinds and URLs -- are what it was tempted with.
BAIT_ADMITTED = "bait_admitted"
EMPTY_CONTEXT = "empty_context"


@dataclass
class AdmittedPassage:
    """One passage the model was given, as the run record keeps it.

    Everything except the text. The text stays in the database under
    `chunk_id`; a run record is written to be committed, and the text of a
    team page is a list of people's names.
    """

    number: int
    chunk_id: int
    source_url: str
    kind: str
    score: float
    tokens: int


@dataclass
class PairResult:
    question_id: str
    status: str
    detail: str = ""
    recalls: list[Recall] = field(default_factory=list)
    coverage: CoverageReport | None = None
    #: The top of the ranking, cut at the deepest recall cut-off.
    retrieved_urls: list[str] = field(default_factory=list)
    #: The generated answer, when the run was given a generator. `declined` is
    #: None when no answer was attempted, which is different from an answer
    #: that was attempted and could not be produced (`answer_detail`).
    answer: str | None = None
    declined: bool | None = None
    answer_problems: list[str] = field(default_factory=list)
    answer_detail: str = ""
    #: How many chunks `first_hit_rank` was measured over: every embedded chunk
    #: of the prospect, so a first hit at 14 of 43 reads as that rather than as
    #: "never retrieved". Recall@k is still cut at k.
    ranked: int | None = None
    #: What answer() put in front of the model, copied from its context (A8).
    #: Empty or None whenever no answer was produced.
    context_passages: list[AdmittedPassage] = field(default_factory=list)
    dropped_chunk_ids: list[int] = field(default_factory=list)
    over_budget: bool | None = None
    #: Scored pairs: distinct expected sources among the admitted passages, of
    #: the distinct expected sources.
    expected_admitted: int | None = None
    expected_total: int | None = None
    #: ADMITTED, DROPPED_FOR_BUDGET, NOT_RETRIEVED, BAIT_ADMITTED or
    #: EMPTY_CONTEXT, on answered pairs only.
    context_label: str | None = None
    #: answer.decline_form of the reply. Recorded beside `declined` and never
    #: counted: `declined` alone decides the decline counts.
    decline_form: str | None = None

    @property
    def first_hit_rank(self) -> int | None:
        return self.recalls[0].first_hit_rank if self.recalls else None


@dataclass
class ProspectResult:
    domain: str
    prospect_id: int | None
    signals: SignalReport | None = None
    pairs: list[PairResult] = field(default_factory=list)
    detail: str = ""

    @property
    def scored(self) -> list[PairResult]:
        return [p for p in self.pairs if p.status == SCORED]


@dataclass
class RunRecord:
    """Everything needed to say what produced a number, and to compare two runs.

    A number without its configuration is not a measurement. The embedding
    model is here because two models' vectors are not comparable, and the
    cut-off list is here because recall@5 from a run that only computed
    recall@1 does not exist.
    """

    started_at: str
    embedding_model: str
    embedding_dimensions: int
    retrieval_top_k: int
    cutoffs: list[int]
    prospects: list[ProspectResult] = field(default_factory=list)
    #: What retrieval searched with: a QUERY_VERSION, or "question" when every
    #: question searched with its own words (ADR-0025).
    retrieval_queries: str = ""
    #: One-time cost of getting the embedder ready, kept OUT of embed_seconds.
    #: ADR-0017 measured it at 7.2 s per process against 1.84 s to embed the
    #: whole 154-chunk corpus, so folding it in would report a run as dominated
    #: by embedding when it is dominated by starting up -- and would grow the
    #: apparent per-question cost as the set gets SMALLER.
    model_load_seconds: float = 0.0
    embed_seconds: float = 0.0
    retrieve_seconds: float = 0.0
    #: Set only when the run generated answers. A decline count from one model
    #: and prompt is not comparable with another's, so both travel with it.
    generation_model: str | None = None
    prompt_version: str | None = None
    generation_load_seconds: float = 0.0
    generate_seconds: float = 0.0

    def recall_at(self, k: int) -> float | None:
        """Share of scored pairs whose evidence was retrieved by rank `k`.

        None when nothing was scored. None rather than 0.0, for the same reason
        signal accuracy returns None: a set nobody has written has not scored
        badly, it has not been scored, and a 0.0 on a dashboard invites someone
        to try to improve it.
        """
        scored = [p for pr in self.prospects for p in pr.scored]
        if not scored:
            return None
        hits = sum(1 for p in scored for r in p.recalls if r.k == k and r.hit)
        return hits / len(scored)

    def _answered(self, status: str) -> list[PairResult]:
        return [
            p
            for pr in self.prospects
            for p in pr.pairs
            if p.status == status and p.declined is not None
        ]

    def declines_on_insufficient(self) -> tuple[int, int] | None:
        """(declined, answered) over insufficient_evidence pairs.

        Declining is the CORRECT answer on these (docs/ground-truth.md §3), so
        this is the count that catches a model inventing an answer the corpus
        does not contain. Counts, not a rate: at two pairs, "1 of 2" says what
        happened and "0.50" claims a precision it does not have.
        """
        answered = self._answered(NO_EVIDENCE_EXPECTED)
        if not answered:
            return None
        return sum(bool(p.declined) for p in answered), len(answered)

    def declines_on_answerable(self) -> tuple[int, int] | None:
        """(declined, answered) over scored pairs, where declining is WRONG.

        Kept beside the first count because a model that declines everything
        would otherwise score perfectly on it.
        """
        answered = self._answered(SCORED)
        if not answered:
            return None
        return sum(bool(p.declined) for p in answered), len(answered)

    def as_lines(self) -> list[str]:
        lines = [
            f"  model:     {self.embedding_model} "
            f"({self.embedding_dimensions} dimensions)",
            f"  top_k:     {self.retrieval_top_k}",
        ]
        if self.retrieval_queries:
            lines.append(f"  queries:   {self.retrieval_queries}")
        for prospect in self.prospects:
            lines.append(f"  {prospect.domain}:")
            if prospect.detail:
                lines.append(f"    {prospect.detail}")
            lines += [f"    {line}" for line in _signal_lines(prospect.signals)]
            for pair in prospect.pairs:
                lines.append(f"    {_pair_line(pair)}")
                for note in (_answer_note(pair), _context_note(pair)):
                    if note:
                        lines.append(f"      {note}")

        scored = sum(len(p.scored) for p in self.prospects)
        lines.append(f"  scored:    {scored} pair(s)")
        if not scored:
            lines += [
                "  no recall to report: no pair has both a reference and",
                "  source_urls written yet. docs/ground-truth.md §2 step 4.",
            ]
        else:
            for k in self.cutoffs:
                value = self.recall_at(k)
                lines.append(f"  recall@{k}:  {value:.2f}" if value is not None else "")
            lines.append(
                f"  timing:    {self.model_load_seconds:.2f}s model load, "
                f"{self.embed_seconds:.2f}s embed, "
                f"{self.retrieve_seconds:.2f}s retrieve"
            )
        insufficient = self.declines_on_insufficient()
        answerable = self.declines_on_answerable()
        if insufficient or answerable:
            lines.append(
                f"  answers:   {self.generation_model} "
                f"(prompt {self.prompt_version}), "
                f"{self.generation_load_seconds:.1f}s load, "
                f"{self.generate_seconds:.1f}s generate"
            )
        if insufficient:
            lines.append(
                f"  declined where it should:     {insufficient[0]} of "
                f"{insufficient[1]} insufficient_evidence pairs"
            )
        if answerable:
            lines.append(
                f"  declined where it should not: {answerable[0]} of "
                f"{answerable[1]} answerable pairs"
            )
        return lines

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


def _signal_lines(report: SignalReport | None) -> list[str]:
    if report is None:
        return ["signals:  not compared (no prospect loaded)"]
    if report.accuracy is None:
        return ["signals:  none checked in the ground-truth file yet"]
    lines = [
        f"signals:  {report.accuracy:.0%} "
        f"({report.compared - len(report.mismatches)}/{report.compared} agree"
        + (f", {len(report.unchecked)} unchecked)" if report.unchecked else ")")
    ]
    lines += [f"  {m}" for m in report.mismatches]
    return lines


def _pair_line(pair: PairResult) -> str:
    if pair.status != SCORED:
        return f"{pair.question_id}: {pair.status} ({pair.detail})"
    rank = pair.first_hit_rank
    depth = f" of {pair.ranked}" if pair.ranked else ""
    where = f"first hit at rank {rank}{depth}" if rank else "evidence never retrieved"
    hits = " ".join(f"@{r.k}={'hit' if r.hit else 'miss'}" for r in pair.recalls)
    return f"{pair.question_id}: {hits}  ({where})"


def _context_note(pair: PairResult) -> str:
    """What the model was shown, on answered pairs only (roadmap 1.1).

    The decline form is named only when it is why a reply that reads like a
    decline was not counted as one: "starts" is already in the decline count,
    and "absent" says nothing.
    """
    if pair.context_label is None:
        return ""
    parts = [f"context: {pair.context_label}"]
    if pair.expected_total:
        parts.append(
            f"{pair.expected_admitted} of {pair.expected_total} sources admitted"
        )
    if pair.decline_form in (DECLINE_WRAPPED, DECLINE_INSIDE):
        parts.append(f"decline phrase: {pair.decline_form}")
    return "; ".join(parts)


def _answer_note(pair: PairResult) -> str:
    """Which way the answer went, judged against what the pair expects."""
    if pair.answer_detail:
        return f"answer: not produced ({pair.answer_detail})"
    if pair.declined is None:
        return ""
    if pair.status == NO_EVIDENCE_EXPECTED:
        verdict = (
            "declined, correctly"
            if pair.declined
            else "ANSWERED where it should have declined"
        )
    else:
        verdict = (
            "DECLINED where it should have answered" if pair.declined else "answered"
        )
    extra = f"; {'; '.join(pair.answer_problems)}" if pair.answer_problems else ""
    return f"answer: {verdict}{extra}"


# --------------------------------------------------------------------------- #
# what reached the model, decided without touching a database (roadmap 1.1)
# --------------------------------------------------------------------------- #
def sources_admitted(expected: list[str], admitted: list[str]) -> tuple[int, int]:
    """(expected sources admitted, expected sources), as distinct pages.

    URLs compare the way recall compares them (`metrics._normalise_url`), so
    two chunks of one page, or a trailing slash, are one source.
    """
    wanted = {_normalise_url(u) for u in expected}
    shown = {_normalise_url(u) for u in admitted}
    return len(wanted & shown), len(wanted)


def label_context(
    status: str, expected: list[str], admitted: list[str], dropped: list[str]
) -> str:
    """Where the evidence stopped on an answered pair (A5).

    `admitted` and `dropped` are the source URLs of the passages answer() put
    in front of the model and of those its token budget cut. Admission is
    checked first: an expected page with one chunk admitted and another
    dropped was shown to the model, so a wrong answer over it is the model's.
    """
    if status == NO_EVIDENCE_EXPECTED:
        return BAIT_ADMITTED if admitted else EMPTY_CONTEXT
    if status != SCORED:
        raise ValueError(f"a {status} pair is never answered, so it has no context")
    wanted = {_normalise_url(u) for u in expected}
    if wanted & {_normalise_url(u) for u in admitted}:
        return ADMITTED
    if wanted & {_normalise_url(u) for u in dropped}:
        return DROPPED_FOR_BUDGET
    return NOT_RETRIEVED


# --------------------------------------------------------------------------- #
# what is scoreable, decided without touching a database
# --------------------------------------------------------------------------- #
def written_signals(signals: dict[str, Any] | None) -> dict[str, Any]:
    """The signals the author has actually checked.

    The scaffold writes `people_listed: TODO`. Compared as a value, that string
    would disagree with every number the crawler computed, and a file nobody
    has filled in yet would report the crawler as wrong about everything. A
    placeholder is unchecked, not a claim.
    """
    return {
        k: v
        for k, v in (signals or {}).items()
        if not (isinstance(v, str) and TODO in v)
    }


def written_source_urls(question: dict[str, Any]) -> list[str] | None:
    """The cited URLs, or None if the author has not written them.

    A scaffolded entry is the literal string `TODO https://... the pages you
    actually used`. Returning None rather than that string is what keeps a
    placeholder out of the metric: a URL that cannot match anything scores 0
    recall and looks exactly like a ranking failure.
    """
    urls = question.get("source_urls")
    if not isinstance(urls, list) or not urls:
        return None
    if any(not isinstance(u, str) or TODO in u for u in urls):
        return None
    return urls


def pair_status(question: dict[str, Any]) -> tuple[str, str]:
    """Whether a pair can be scored for recall, and why not when it cannot.

    Kept separate from the run so the rule is testable without a database. The
    harness's most important behaviour is what it REFUSES to score, and a
    refusal nobody can exercise is a refusal nobody should trust.
    """
    if question.get("expected_outcome") == NO_EVIDENCE_EXPECTED:
        return (
            NO_EVIDENCE_EXPECTED,
            "cites nothing by design; recall is undefined, not zero",
        )
    reference = question.get("reference")
    if not isinstance(reference, str) or TODO in reference or not reference.strip():
        return UNWRITTEN, "reference not written"
    if written_source_urls(question) is None:
        return UNWRITTEN, "source_urls not written"
    return SCORED, ""


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
async def evaluate_directory(
    session,
    directory: str | Path = "eval/ground_truth",
    *,
    client=None,
    generator=None,
    count_tokens=None,
    cutoffs: tuple[int, ...] = RECALL_CUTOFFS,
) -> RunRecord:
    """Run every ground-truth file in `directory` against the loaded corpus.

    With a `generator`, also answer every pair that can be answered and record
    whether it declined. Without one -- the default -- nothing is generated and
    no generation model is loaded, so tests and CI never pull one.
    """
    record = RunRecord(
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        retrieval_top_k=settings.retrieval_top_k,
        cutoffs=list(cutoffs),
        retrieval_queries=query_version(),
    )
    paths = sorted(Path(directory).glob("*.yaml"))
    if not paths:
        return record

    embedder = _Embedder(client, record)
    if generator is not None:
        record.generation_model = generator.name
        record.prompt_version = PROMPT_VERSION
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        record.prospects.append(
            await _evaluate_prospect(
                session, data, record, embedder, cutoffs, generator, count_tokens
            )
        )
    return record


class _Embedder:
    """Builds and warms the embedding client on FIRST ACTUAL USE.

    Three revisions, and the third exists because the second broke a property
    the first never had.

    v1 called `build_client()` inside the per-question embed, reloading the
    model for every pair: "Loading weights" printed twice for two questions and
    embed_seconds read 15.05s, nearly all of it setup.

    v2 built and warmed it once at the top of the run. That fixed the timing --
    ADR-0017 measures the load at 7.2s against 0.03s to embed two questions --
    and quietly broke something more important: the model loaded even when
    every pair was refused. **[verified] 2026-09-05**, five integration tests
    failed on CI, which does not install the 784 MB `[local]` extra, and one of
    them asserts in its own docstring that a half-written set costs nothing.
    The claim and the code disagreed, and the code was wrong.

    So: lazy, and warmed exactly once when the first scoreable, covered pair
    actually needs a vector. A run that scores nothing loads nothing.
    """

    def __init__(self, client, record: RunRecord) -> None:
        self._client = client
        self._record = record
        self._ready = False

    async def client(self):
        """The warmed client, for a caller that embeds through its own path.

        answer() does its own retrieval. Handing it this client is what stops
        it building a second embedder and loading the model a second time --
        the v1 defect above, reached by a different door.
        """
        await self._warm()
        return self._client

    async def embed(self, question: str) -> list[float]:
        await self._warm()
        started = time.perf_counter()
        vector = await embed_question(self._client, question)
        self._record.embed_seconds += time.perf_counter() - started
        return vector

    async def _warm(self) -> None:
        if not self._ready:
            # Timed as itself. sentence-transformers loads lazily on first use,
            # so without this the first question absorbs the whole load and the
            # embed/retrieve split -- the one thing the run record exists to
            # give -- reports setup as embedding.
            started = time.perf_counter()
            self._client = self._client or build_client()
            await embed_question(self._client, "warm-up")
            self._record.model_load_seconds = time.perf_counter() - started
            self._ready = True


async def _evaluate_prospect(
    session,
    data: dict[str, Any],
    record: RunRecord,
    embedder: _Embedder,
    cutoffs: tuple[int, ...],
    generator=None,
    count_tokens=None,
) -> ProspectResult:
    domain = str(data.get("prospect", {}).get("domain", "")).lower()
    prospect_id = await session.scalar(
        text("SELECT id FROM prospects WHERE domain = :d"), {"d": domain}
    )
    if prospect_id is None:
        return ProspectResult(
            domain=domain,
            prospect_id=None,
            detail=(
                f"not loaded: no prospect with domain {domain!r}. "
                f"make load ARTIFACTS={data.get('prospect', {}).get('corpus_artifact')}"
            ),
        )

    result = ProspectResult(domain=domain, prospect_id=prospect_id)

    # Diagnosis before retrieval, deliberately. A question whose evidence was
    # never crawled scores 0 recall, and reading that 0 as a retrieval failure
    # sends someone to tune ranking over a corpus without the answer.
    computed = (
        await session.scalar(
            text("SELECT signals FROM prospects WHERE id = :p"), {"p": prospect_id}
        )
        or {}
    )
    result.signals = signal_accuracy(written_signals(data.get("signals")), computed)

    crawled = set(
        (
            await session.execute(
                text("SELECT source_url FROM documents WHERE prospect_id = :p"),
                {"p": prospect_id},
            )
        )
        .scalars()
        .all()
    )
    outcomes = dict(
        (
            await session.execute(
                text(
                    "SELECT o.url, o.outcome FROM crawl_page_outcomes o "
                    "  JOIN crawl_runs r ON r.id = o.crawl_run_id "
                    " WHERE r.prospect_id = :p"
                ),
                {"p": prospect_id},
            )
        ).all()
    )

    scope = await ProspectScope.open(session, prospect_id)
    for question in data.get("questions") or []:
        pair = await _evaluate_pair(
            question, scope, crawled, outcomes, record, embedder, cutoffs
        )
        if generator is not None and pair.status in (SCORED, NO_EVIDENCE_EXPECTED):
            await _answer_pair(
                session,
                domain,
                scope,
                question,
                pair,
                record,
                embedder,
                generator,
                count_tokens,
            )
        result.pairs.append(pair)
    return result


async def _evaluate_pair(
    question: dict[str, Any],
    scope: ProspectScope,
    crawled: set[str],
    outcomes: dict[str, str],
    record: RunRecord,
    embedder: _Embedder,
    cutoffs: tuple[int, ...],
) -> PairResult:
    qid = str(question.get("id", "?"))
    status, detail = pair_status(question)
    if status != SCORED:
        return PairResult(question_id=qid, status=status, detail=detail)

    expected = written_source_urls(question) or []
    coverage = ingestion_coverage(expected, crawled, outcomes)
    if not coverage.present:
        # Every cited page is missing, so recall can only be 0 and that 0 would
        # be about ingestion, not ranking. Reported as its own status rather
        # than as a bad score (docs/evaluation.md §2.5).
        return PairResult(
            question_id=qid,
            status=NOT_INGESTED,
            detail="; ".join(str(m) for m in coverage.missing),
            coverage=coverage,
        )

    # The same text answer() searches with, so what is scored is what the
    # model is given (ADR-0025).
    query_vector = await embedder.embed(
        retrieval_query(str(question.get("question", "")), qid)
    )

    # The whole prospect is ranked, so the first hit's rank is known however
    # deep it is. "Missed at 10, found at 14" and "missed at 10, found at 97"
    # are different problems (A8), and cut at 10 both read "never retrieved".
    # Never shallower than the deepest cut-off, so a prospect with fewer
    # embedded chunks than that is ranked by exactly the query it always was.
    depth = max(await scope.count_embedded(), max(cutoffs))

    started = time.perf_counter()
    hits = await search(scope, query_vector, k=depth)
    urls = await scope.source_urls([hit.id for hit in hits])
    record.retrieve_seconds += time.perf_counter() - started

    # A1 is a hard boundary, so this raises and voids the run rather than
    # lowering a score (docs/evaluation.md §1). Every ranked hit is checked,
    # not only the top ten: a foreign chunk at rank 40 is as much a leak.
    check_no_leakage([hit.id for hit in hits], set(urls))

    ranking = [urls[hit.id] for hit in hits]
    return PairResult(
        question_id=qid,
        status=SCORED,
        # recall_at_k cuts the ranking at each k itself, so handing it the
        # whole ranking moves first_hit_rank and nothing else.
        recalls=recalls_for_question(qid, ranking, expected, cutoffs),
        coverage=coverage,
        retrieved_urls=ranking[: max(cutoffs)],
        ranked=len(ranking),
    )


async def _answer_pair(
    session,
    domain: str,
    scope: ProspectScope,
    question: dict[str, Any],
    pair: PairResult,
    record: RunRecord,
    embedder: _Embedder,
    generator,
    count_tokens=None,
) -> None:
    """Answer one pair through the path a user gets, and record whether it
    declined and what it was shown.

    answer() is called as a black box on purpose. A decline count measured on
    anything but the real answering path would measure something else. For
    the same reason, the context recorded is the one answer() returns, read
    back rather than rebuilt from the harness's own ranking.
    """
    load_before = generator.load_seconds
    try:
        result = await answer(
            session,
            domain,
            str(question.get("question") or "") or None,
            question_id=pair.question_id,
            embedder=await embedder.client(),
            generator=generator,
            count_tokens=count_tokens,
        )
    except AnswerUnavailable as exc:
        pair.answer_detail = str(exc).splitlines()[0]
        return
    record.generation_load_seconds += generator.load_seconds - load_before
    record.generate_seconds += result.generate_seconds
    pair.answer = result.text
    pair.declined = result.declined
    pair.answer_problems = result.problems
    pair.decline_form = decline_form(result.text)

    context = result.context
    pair.context_passages = [
        AdmittedPassage(p.number, p.chunk_id, p.source_url, p.kind, p.score, p.tokens)
        for p in context.passages
    ]
    pair.dropped_chunk_ids = list(context.dropped)
    pair.over_budget = context.over_budget
    admitted = [p.source_url for p in context.passages]
    dropped = (
        list((await scope.source_urls(list(context.dropped))).values())
        if context.dropped
        else []
    )
    expected = written_source_urls(question) or []
    if pair.status == SCORED:
        pair.expected_admitted, pair.expected_total = sources_admitted(
            expected, admitted
        )
    pair.context_label = label_context(pair.status, expected, admitted, dropped)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the ground-truth set.")
    parser.add_argument("--dir", default="eval/ground_truth")
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="also write the full run record here, for the A3 before-and-after",
    )
    parser.add_argument(
        "--no-answers",
        action="store_true",
        help="score retrieval only; load no model and generate nothing",
    )
    args = parser.parse_args(argv)

    from linestack.db import session_factory

    generator = None
    if not args.no_answers:
        from linestack.generation.client import build_generator

        generator = build_generator()

    async with session_factory() as session:
        record = await evaluate_directory(session, args.dir, generator=generator)

    print("\n".join(line for line in record.as_lines() if line))
    if args.json:
        Path(args.json).write_text(record.to_json(), encoding="utf-8")
        print(f"  -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(__import__("sys").argv[1:])))
