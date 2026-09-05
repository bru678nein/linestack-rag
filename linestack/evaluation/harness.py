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
    check_no_leakage,
    ingestion_coverage,
    recalls_for_question,
    signal_accuracy,
)
from linestack.retrieval.embedding import (
    EmbedReport,
    LocalEmbedder,
    build_client,
    embed_texts,
)
from linestack.retrieval.scope import ProspectScope
from linestack.retrieval.search import search

#: How a pair was treated. `scored` is the only one that contributes to recall,
#: and the other three are reasons rather than failures -- which is why they are
#: named rather than collapsed into "skipped" (A5, applied to the dataset).
SCORED = "scored"
UNWRITTEN = "unwritten"  # the author has not filled it in yet
NO_EVIDENCE_EXPECTED = "insufficient_evidence"  # correct, and cites nothing
NOT_INGESTED = "not_ingested"  # cited pages are not in the corpus


@dataclass
class PairResult:
    question_id: str
    status: str
    detail: str = ""
    recalls: list[Recall] = field(default_factory=list)
    coverage: CoverageReport | None = None
    retrieved_urls: list[str] = field(default_factory=list)

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
    #: One-time cost of getting the embedder ready, kept OUT of embed_seconds.
    #: ADR-0017 measured it at 7.2 s per process against 1.84 s to embed the
    #: whole 154-chunk corpus, so folding it in would report a run as dominated
    #: by embedding when it is dominated by starting up -- and would grow the
    #: apparent per-question cost as the set gets SMALLER.
    model_load_seconds: float = 0.0
    embed_seconds: float = 0.0
    retrieve_seconds: float = 0.0

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

    def as_lines(self) -> list[str]:
        lines = [
            f"  model:     {self.embedding_model} "
            f"({self.embedding_dimensions} dimensions)",
            f"  top_k:     {self.retrieval_top_k}",
        ]
        for prospect in self.prospects:
            lines.append(f"  {prospect.domain}:")
            if prospect.detail:
                lines.append(f"    {prospect.detail}")
            lines += [f"    {line}" for line in _signal_lines(prospect.signals)]
            for pair in prospect.pairs:
                lines.append(f"    {_pair_line(pair)}")

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
    where = f"first hit at rank {rank}" if rank else "evidence never retrieved"
    hits = " ".join(f"@{r.k}={'hit' if r.hit else 'miss'}" for r in pair.recalls)
    return f"{pair.question_id}: {hits}  ({where})"


# --------------------------------------------------------------------------- #
# what is scoreable, decided without touching a database
# --------------------------------------------------------------------------- #
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
    cutoffs: tuple[int, ...] = RECALL_CUTOFFS,
) -> RunRecord:
    """Run every ground-truth file in `directory` against the loaded corpus."""
    record = RunRecord(
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        retrieval_top_k=settings.retrieval_top_k,
        cutoffs=list(cutoffs),
    )
    paths = sorted(Path(directory).glob("*.yaml"))
    if not paths:
        return record

    # Built ONCE, here, and threaded through. The first version called
    # build_client() inside the per-question embed, which reloaded the
    # sentence-transformers model for every pair: "Loading weights" printed
    # twice for two questions and embed_seconds read 15.05s, nearly all of it
    # model loading. A timing that is mostly setup makes the embed/retrieve
    # split useless, which is the one thing the run record exists to give.
    client = client or build_client()
    # Warmed here so the one-time load is measured as itself. sentence-
    # transformers loads lazily on first use, so without this the first
    # question in the set silently absorbs 7 s of setup.
    started = time.perf_counter()
    await _embed_question("warm-up", client)
    record.model_load_seconds = time.perf_counter() - started

    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        record.prospects.append(
            await _evaluate_prospect(session, data, record, client, cutoffs)
        )
    return record


async def _evaluate_prospect(
    session,
    data: dict[str, Any],
    record: RunRecord,
    client,
    cutoffs: tuple[int, ...],
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
    result.signals = signal_accuracy(data.get("signals") or {}, computed)

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
        result.pairs.append(
            await _evaluate_pair(
                question, scope, crawled, outcomes, record, client, cutoffs
            )
        )
    return result


async def _evaluate_pair(
    question: dict[str, Any],
    scope: ProspectScope,
    crawled: set[str],
    outcomes: dict[str, str],
    record: RunRecord,
    client,
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

    started = time.perf_counter()
    query_vector = await _embed_question(str(question.get("question", "")), client)
    record.embed_seconds += time.perf_counter() - started

    started = time.perf_counter()
    hits = await search(scope, query_vector, k=max(cutoffs))
    urls = await scope.source_urls([hit.id for hit in hits])
    record.retrieve_seconds += time.perf_counter() - started

    # A1 is a hard boundary, so this raises and voids the run rather than
    # lowering a score (docs/evaluation.md §1).
    check_no_leakage([hit.id for hit in hits], set(urls))

    retrieved = [urls[hit.id] for hit in hits]
    return PairResult(
        question_id=qid,
        status=SCORED,
        recalls=recalls_for_question(qid, retrieved, expected, cutoffs),
        coverage=coverage,
        retrieved_urls=retrieved,
    )


async def _embed_question(question: str, client) -> list[float]:
    """Embed one question the same way the chunks were embedded.

    bge asks for a query prefix and the documents carry none; asking it the
    same way the chunks were embedded ranks worse. `LocalEmbedder.embed_query`
    is what applies that, so the branch matters and is not tidiness.

    Takes an already-built client. Building one here would reload the model per
    question; see evaluate_directory.
    """
    if isinstance(client, LocalEmbedder):
        return client.embed_query(question)
    return (await embed_texts(client, [question], EmbedReport()))[0]


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the ground-truth set.")
    parser.add_argument("--dir", default="eval/ground_truth")
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="also write the full run record here, for the A3 before-and-after",
    )
    args = parser.parse_args(argv)

    from linestack.db import session_factory

    async with session_factory() as session:
        record = await evaluate_directory(session, args.dir)

    print("\n".join(line for line in record.as_lines() if line))
    if args.json:
        Path(args.json).write_text(record.to_json(), encoding="utf-8")
        print(f"  -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(__import__("sys").argv[1:])))
