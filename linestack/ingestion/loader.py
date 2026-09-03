"""Responsibility: loading a crawl artifact (prospect_<domain>.json) into
Postgres, idempotently.

Owns: upsert of prospects, documents and chunks keyed on the natural keys the
schema declares; skipping re-chunking and re-embedding for documents whose
content_hash is unchanged (A7); and writing crawl_runs and crawl_page_outcomes
so that a document that is absent has a recorded reason (A5).

Does not own: crawling. The two steps are deliberately separate so a crawl can
be re-run and diffed without touching the database (ADR-0008).

Must fail loudly rather than duplicate. "Re-running produces the same result or
fails loudly" is the whole of A7; a loader that silently inserts a second copy
of a document is the failure this module exists to prevent.

CORRECTION to the docstring above and to ADR-0008: idempotency keys on
`stable_hash`, not `content_hash`. ADR-0008 predates ADR-0013. fly.io/about
reshuffles its team roster on every request, so its content_hash changes on
every crawl for a page that has not changed; keying on it would re-chunk and
re-embed that page forever, at cost. content_hash is still stored, exactly, and
a disagreement between the two is recorded as `documents_reordered`.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from linestack.config import settings
from linestack.ingestion.chunking import (
    ChunkingReport,
    chunk_document,
    default_token_counter,
)
from linestack.retrieval.scope import ProspectScope

# --------------------------------------------------------------------------- #
# The artifact, as ingest.py writes it
# --------------------------------------------------------------------------- #
# These are named for what they are -- the shape of a JSON file on disk -- and
# are deliberately distinct types from the SQLAlchemy models they become. A
# value read from a crawl and a row in the database are different things, and
# A4 says not to let one quietly stand in for the other.


class ArtifactPageOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    outcome: str
    http_status: int | None = None
    detail: str = ""


class ArtifactDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    kind: str
    title: str = ""
    text: str
    published: str | None = None
    extract_reason: str = ""
    content_hash: str
    stable_hash: str = ""
    duplicate_urls: list[str] = Field(default_factory=list)


class ArtifactSignals(BaseModel):
    model_config = ConfigDict(extra="allow")  # the signal set is still moving


class Artifact(BaseModel):
    """One `prospect_<domain>.json`, validated."""

    model_config = ConfigDict(extra="forbid")

    company_name: str
    domain: str
    base_url: str
    documents: list[ArtifactDocument] = Field(default_factory=list)
    signals: dict = Field(default_factory=dict)
    crawled_at: str
    robots_reason: str = ""
    crawl_outcome: str = "completed"
    page_outcomes: list[ArtifactPageOutcome] = Field(default_factory=list)

    @property
    def crawled_at_utc(self) -> dt.datetime:
        """`crawled_at` as a datetime.

        The artifact stores an ISO string; asyncpg wants a datetime for a
        timestamptz column and rejects the string outright. Parsed once, here,
        rather than at three call sites.
        """
        return dt.datetime.fromisoformat(self.crawled_at)


class ArtifactTooOld(RuntimeError):
    """Raised when an artifact is older than the configured threshold."""


class ArtifactRefused(RuntimeError):
    """Raised when an artifact cannot be loaded as given."""


@dataclass
class LoadReport:
    """What one load did. Computed facts, not model output (A2, A4)."""

    domain: str
    prospect_id: int = 0
    crawl_run_id: int = 0
    crawl_run_existed: bool = False
    outcomes_written: int = 0
    outcomes_skipped: int = 0
    pages_fetched: int = 0
    documents_in_artifact: int = 0
    counts_by_outcome: dict[str, int] = field(default_factory=dict)

    # Per-document work. `unchanged` and `reordered` are both skips; they are
    # counted apart because their causes differ and only one of them is a
    # standing claim about a site (ADR-0013).
    documents_inserted: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    documents_reordered: int = 0
    chunks_written: int = 0
    blocks_force_split: int = 0

    def as_lines(self) -> list[str]:
        tally = ", ".join(f"{k} {v}" for k, v in sorted(self.counts_by_outcome.items()))
        return [
            f"  prospect:  {self.domain} (id {self.prospect_id})",
            f"  crawl run: {self.crawl_run_id}"
            + (" (already loaded)" if self.crawl_run_existed else " (new)"),
            f"  outcomes:  {self.outcomes_written} written, "
            f"{self.outcomes_skipped} already present",
            f"  tally:     {tally}",
            f"  fetched:   {self.pages_fetched} pages, "
            f"{self.documents_in_artifact} documents in the artifact",
            f"  documents: {self.documents_inserted} inserted, "
            f"{self.documents_updated} updated, "
            f"{self.documents_unchanged} unchanged, "
            f"{self.documents_reordered} reordered",
            f"  chunks:    {self.chunks_written} written"
            + (
                f", {self.blocks_force_split} blocks force-split"
                if self.blocks_force_split
                else ""
            ),
        ]


# Outcomes that were never fetched. `pages_fetched` counts requests actually
# made, so these are subtracted: a URL skipped by robots.txt cost no request,
# and one left in the queue when the budget ran out was never attempted.
NOT_FETCHED = {"skipped_robots", "budget_exhausted"}


def read_artifact(path: str | Path) -> Artifact:
    """Parse and validate one artifact. Raises rather than guessing."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Artifact.model_validate(raw)


def refuse_if_stale(
    artifact: Artifact,
    now: dt.datetime,
    max_age_hours: int | None = None,
) -> None:
    """ADR-0008: refuse an artifact older than a configured threshold.

    `now` is a parameter, not a call to the clock. Tests pin real frozen
    artifacts with real timestamps, and a clock-reading version would turn
    those tests red on a date rather than on a defect.

    Loading a stale artifact silently produces a corpus that disagrees with the
    live site for reasons nobody can see -- and then a ground-truth pair
    written against it looks like a retrieval failure.
    """
    limit = max_age_hours or settings.artifact_max_age_hours
    age = now - artifact.crawled_at_utc
    if age > dt.timedelta(hours=limit):
        raise ArtifactTooOld(
            f"{artifact.domain} was crawled {age.days} days ago "
            f"({artifact.crawled_at}), over the {limit}-hour limit. "
            f"Re-crawl with `make crawl DOMAIN={artifact.domain}`, or raise "
            f"ARTIFACT_MAX_AGE_HOURS if this corpus is deliberately frozen."
        )


def count_pages_fetched(artifact: Artifact) -> int:
    """Requests actually made, derived from the outcomes.

    Derived, not recorded: the artifact does not carry a request count. Every
    outcome except the two in NOT_FETCHED represents one request that was sent.
    """
    return sum(1 for o in artifact.page_outcomes if o.outcome not in NOT_FETCHED)


def describe_derivation(artifact: Artifact) -> str:
    """What in this crawl_runs row was derived or taken from config.

    Written into crawl_runs.detail so that a number nobody recorded is never
    mistaken later for one that was measured (A4). max_pages and user_agent
    are config values, not artifact values, and started_at is the crawl's END
    time because that is the only timestamp ingest.py writes.
    """
    return (
        "started_at is the artifact's crawled_at, which ingest.py records "
        "after the crawl loop, so it is the end time; max_pages and "
        "user_agent come from configuration because the artifact does not "
        "carry them; pages_fetched is derived as outcomes minus "
        f"{sorted(NOT_FETCHED)}."
    )


async def load_artifact(
    session: AsyncSession,
    artifact: Artifact,
    *,
    now: dt.datetime | None = None,
    max_age_hours: int | None = None,
    count_tokens=None,
) -> LoadReport:
    """Load a crawl's bookkeeping: prospect, crawl run, page outcomes.

    Documents and chunks are deliberately not written here yet; that is the
    next step. What this establishes first is the record that explains an
    absent document (A5), because a corpus whose gaps cannot be explained
    makes every later recall number a statement about the crawler wearing the
    label of a statement about retrieval (docs/evaluation.md section 2.5).
    """
    refuse_if_stale(artifact, now or dt.datetime.now(dt.UTC), max_age_hours)

    report = LoadReport(
        domain=artifact.domain,
        documents_in_artifact=len(artifact.documents),
        pages_fetched=count_pages_fetched(artifact),
    )

    report.prospect_id = await _upsert_prospect(session, artifact)
    run_id, existed = await _insert_crawl_run(session, artifact, report)
    report.crawl_run_id, report.crawl_run_existed = run_id, existed

    written, skipped = await _insert_page_outcomes(session, run_id, artifact)
    report.outcomes_written, report.outcomes_skipped = written, skipped

    for outcome in artifact.page_outcomes:
        report.counts_by_outcome[outcome.outcome] = (
            report.counts_by_outcome.get(outcome.outcome, 0) + 1
        )

    await _load_documents(session, artifact, report, count_tokens)
    return report


# What to do with one document, given what is already stored for that URL.
#
#   stable_hash equal, content_hash equal      unchanged   touch timestamps
#   stable_hash equal, content_hash differs    reordered   store the new exact
#                                                          hash, keep chunks
#   stable_hash differs                        changed     re-chunk
#   stored stable_hash IS NULL                 unknown     re-chunk
#
# Keyed on stable_hash, NOT content_hash, and ADR-0008 is corrected accordingly
# in this module's docstring. fly.io/about reshuffles its roster on every
# request: four consecutive fetches gave four content hashes and one word
# multiset. Keying skip-work on the exact hash would re-chunk and re-embed that
# page forever, at cost, for content that has not changed.
#
# The NULL case fails safe in the direction of doing the work: a document
# stored before migration 0002 has no stable_hash, and re-chunking it costs
# time where skipping it would silently keep a stale corpus.


async def _load_documents(
    session: AsyncSession,
    artifact: Artifact,
    report: LoadReport,
    count_tokens=None,
) -> None:
    count = count_tokens or default_token_counter()
    scope = ProspectScope(session, report.prospect_id)
    fetched_at = artifact.crawled_at_utc

    existing = {
        row.source_url: (row.id, row.stable_hash, row.content_hash)
        for row in (
            await session.execute(
                text(
                    "SELECT id, source_url, stable_hash, content_hash "
                    "  FROM documents WHERE prospect_id = :p"
                ),
                {"p": report.prospect_id},
            )
        ).all()
    }

    for doc in artifact.documents:
        previous = existing.get(doc.url)
        document_id = await _upsert_document(
            session, report.prospect_id, doc, fetched_at
        )

        if previous is not None:
            _, stored_stable, stored_content = previous
            if stored_stable and stored_stable == doc.stable_hash:
                report.documents_updated += 1
                if stored_content != doc.content_hash:
                    # Same words, different order. Recorded rather than
                    # collapsed into "unchanged": it is the observable evidence
                    # that this source reshuffles, and re-checking it on every
                    # load keeps ADR-0013's claim honest instead of one-off.
                    report.documents_reordered += 1
                else:
                    report.documents_updated -= 1
                    report.documents_unchanged += 1
                continue
            report.documents_updated += 1
        else:
            report.documents_inserted += 1

        chunking = ChunkingReport()
        drafts = chunk_document(
            text=doc.text,
            kind=doc.kind,
            title=doc.title,
            published=doc.published,
            count_tokens=count,
            report=chunking,
        )
        report.chunks_written += await scope.replace_document_chunks(
            document_id, drafts
        )
        report.blocks_force_split += chunking.force_split_blocks


def _parse_published(value: str | None) -> dt.date | None:
    """The artifact's `published` as a date, or None.

    Stored exactly as given, never repaired. **[verified]** 31 of the corpus's
    76 documents carry exactly `2026-01-01` and 9 carry none -- htmldate's
    coarse fallback rather than real publication dates. A4 forbids inventing a
    measurement, so a bad date is stored as the bad date it is. Anything keying
    on recency -- `latest_post_date`, the chunk provenance header -- rests on
    that, and this comment is where to start when it misleads someone.
    """
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


async def _upsert_document(
    session: AsyncSession,
    prospect_id: int,
    doc: ArtifactDocument,
    fetched_at: dt.datetime,
) -> int:
    """One row per (prospect, source_url). Returns the document id."""
    return await session.scalar(
        text(
            "INSERT INTO documents "
            "  (prospect_id, source_url, kind, title, published_at, "
            "   word_count, content_hash, stable_hash, duplicate_urls, "
            "   fetched_at) "
            "VALUES (:p, :url, CAST(:kind AS document_kind), :title, "
            "        :published, :words, :content_hash, :stable_hash, "
            "        :duplicates, :fetched) "
            "ON CONFLICT (prospect_id, source_url) DO UPDATE SET "
            "  kind = EXCLUDED.kind, title = EXCLUDED.title, "
            "  published_at = EXCLUDED.published_at, "
            "  word_count = EXCLUDED.word_count, "
            "  content_hash = EXCLUDED.content_hash, "
            "  stable_hash = EXCLUDED.stable_hash, "
            "  duplicate_urls = EXCLUDED.duplicate_urls, "
            "  fetched_at = EXCLUDED.fetched_at, updated_at = now() "
            "RETURNING id"
        ),
        {
            "p": prospect_id,
            "url": doc.url,
            "kind": doc.kind,
            "title": doc.title[:500],
            "published": _parse_published(doc.published),
            "words": len(doc.text.split()),
            "content_hash": doc.content_hash,
            "stable_hash": doc.stable_hash or None,
            "duplicates": doc.duplicate_urls,
            "fetched": fetched_at,
        },
    )


async def _upsert_prospect(session: AsyncSession, artifact: Artifact) -> int:
    """One row per domain. Signals are replaced wholesale on every load.

    Replaced rather than merged because they are computed facts about one
    crawl, not an accumulating record: a signal that was true last month and is
    not true now must go down, not persist.
    """
    domain = artifact.domain.lower()
    return await session.scalar(
        text(
            "INSERT INTO prospects (company_name, domain, signals) "
            "VALUES (:name, :domain, CAST(:signals AS jsonb)) "
            "ON CONFLICT (domain) DO UPDATE SET "
            "  company_name = EXCLUDED.company_name, "
            "  signals = EXCLUDED.signals, "
            "  updated_at = now() "
            "RETURNING id"
        ),
        {
            "name": artifact.company_name,
            "domain": domain,
            "signals": json.dumps(artifact.signals),
        },
    )


async def _insert_crawl_run(
    session: AsyncSession, artifact: Artifact, report: LoadReport
) -> tuple[int, bool]:
    """One run per (prospect, crawl start). Re-loading finds the existing one.

    The natural key is enforced by migration 0003 rather than by a
    select-then-insert here, for the same reason A1's isolation is: a check
    that lives in application code is a check that eventually is not performed.
    """
    if not artifact.robots_reason:
        raise ArtifactRefused(
            f"{artifact.domain} has no robots_reason. crawl_runs.robots_reason "
            f"is NOT NULL, and 'we did not record it' is not one of the five "
            f"reason codes. This artifact predates ADR-0006 -- re-crawl it."
        )

    started = artifact.crawled_at_utc
    run_id = await session.scalar(
        text(
            "INSERT INTO crawl_runs "
            "  (prospect_id, started_at, finished_at, robots_reason, outcome, "
            "   max_pages, pages_fetched, documents_stored, user_agent, detail) "
            "VALUES (:p, :started, :started, CAST(:robots AS robots_outcome), "
            "        CAST(:outcome AS crawl_outcome), :max_pages, :fetched, "
            "        :stored, :ua, :detail) "
            "ON CONFLICT (prospect_id, started_at) DO NOTHING "
            "RETURNING id"
        ),
        {
            "p": report.prospect_id,
            "started": started,
            "robots": artifact.robots_reason,
            "outcome": artifact.crawl_outcome,
            "max_pages": settings.crawl_max_pages,
            "fetched": report.pages_fetched,
            "stored": report.documents_in_artifact,
            "ua": settings.crawl_user_agent,
            "detail": describe_derivation(artifact),
        },
    )
    if run_id is not None:
        return run_id, False

    existing = await session.scalar(
        text(
            "SELECT id FROM crawl_runs "
            " WHERE prospect_id = :p AND started_at = :started"
        ),
        {"p": report.prospect_id, "started": started},
    )
    return existing, True


async def _insert_page_outcomes(
    session: AsyncSession, crawl_run_id: int, artifact: Artifact
) -> tuple[int, int]:
    """One row per URL the crawl touched. Returns (written, already present).

    ON CONFLICT DO NOTHING rather than an update: an outcome is what happened
    during that run, and a run does not happen twice.
    """
    if not artifact.page_outcomes:
        return 0, 0

    # Counted with a SELECT rather than inferred from RETURNING. SQLAlchemy's
    # executemany closes the result for a text() statement -- "This result
    # object does not return rows" -- so RETURNING id here yields nothing to
    # count. Verified 2026-09-02.
    before = await _outcome_count(session, crawl_run_id)

    await session.execute(
        text(
            "INSERT INTO crawl_page_outcomes "
            "  (crawl_run_id, url, outcome, http_status, detail) "
            "VALUES (:run, :url, CAST(:outcome AS page_outcome), "
            "        :status, :detail) "
            "ON CONFLICT (crawl_run_id, url) DO NOTHING"
        ),
        [
            {
                "run": crawl_run_id,
                "url": o.url,
                "outcome": o.outcome,
                "status": o.http_status,
                "detail": o.detail or None,
            }
            for o in artifact.page_outcomes
        ],
    )

    written = await _outcome_count(session, crawl_run_id) - before
    return written, len(artifact.page_outcomes) - written


async def _outcome_count(session: AsyncSession, crawl_run_id: int) -> int:
    return int(
        await session.scalar(
            text("SELECT count(*) FROM crawl_page_outcomes  WHERE crawl_run_id = :run"),
            {"run": crawl_run_id},
        )
    )


async def _main(paths: list[str]) -> int:
    """`python -m linestack.ingestion.loader prospect_fly_io.json ...`"""
    from linestack.db import session_factory

    failures = 0
    async with session_factory() as session:
        for path in paths:
            print(f"\n=== {path}")
            try:
                report = await load_artifact(session, read_artifact(path))
            except (ArtifactTooOld, ArtifactRefused) as exc:
                print(f"  REFUSED: {exc}")
                failures += 1
                continue
            for line in report.as_lines():
                print(line)
            await session.commit()
    return 1 if failures else 0


if __name__ == "__main__":
    import asyncio
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m linestack.ingestion.loader <artifact.json> ...")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
