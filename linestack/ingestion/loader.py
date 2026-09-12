"""Responsibility: loading a crawl artifact (prospect_<domain>.json) into
Postgres, idempotently.

Owns: upsert of prospects, documents and chunks keyed on the natural keys the
schema declares; skipping re-chunking and re-embedding for documents whose
content_hash is unchanged (A7); removing documents the latest completed crawl
no longer contains; and writing crawl_runs and crawl_page_outcomes so that a
document that is absent has a recorded reason (A5).

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
    provenance_header,
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
    # Where `published` came from (ingest.py, ADR-0021). Defaulted, like
    # kind_conflicts, so artifacts frozen before the field still load. Not
    # persisted yet: `documents` has no column, and adding one before a
    # recency-weighting decision needs it is infrastructure ahead of
    # measurement (A9). The artifact carries it, which is where §1.6's
    # "record which source supplied it" is satisfied.
    published_source: str = "none"
    extract_reason: str = ""
    content_hash: str
    stable_hash: str = ""
    duplicate_urls: list[str] = Field(default_factory=list)
    # Kinds claimed by URLs that lost deduplication, when they disagree with
    # `kind` (ingest.py, ADR-0019). Defaulted rather than required: the three
    # frozen fixtures predate the field, and `extra="forbid"` would otherwise
    # reject every artifact written after it. Not yet persisted -- `documents`
    # has no column for it, and adding one before a single conflict has been
    # observed is infrastructure ahead of measurement (A9).
    kind_conflicts: list[str] = Field(default_factory=list)


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
    #: Same text, but the chunks carry a provenance header other than the one
    #: the document would get now: a changed title, kind or date, a changed
    #: header format, or chunks an earlier load left stale. Re-chunked.
    documents_relabelled: int = 0
    chunks_written: int = 0
    blocks_force_split: int = 0
    #: Stored documents this crawl no longer contains, deleted with their
    #: chunks. By URL, not a count: each one is a page that left the corpus.
    documents_removed: list[str] = field(default_factory=list)
    #: Why this load left the stored documents alone, when it did.
    documents_held: str = ""

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
            f"{self.documents_reordered} reordered, "
            f"{self.documents_relabelled} relabelled",
            f"  chunks:    {self.chunks_written} written"
            + (
                f", {self.blocks_force_split} blocks force-split"
                if self.blocks_force_split
                else ""
            ),
            f"  removed:   {len(self.documents_removed)} no longer in this crawl"
            + "".join(f"\n    {url}" for url in self.documents_removed),
            *([f"  held:      {self.documents_held}"] if self.documents_held else []),
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

    # The stored documents are the latest crawl's. An older artifact still
    # records its run and outcomes -- that is history -- but writing its
    # documents would put back pages a newer crawl already replaced or dropped.
    latest = await _latest_crawl_start(session, report.prospect_id)
    if artifact.crawled_at_utc < latest:
        report.documents_held = (
            f"a newer crawl ({latest.isoformat()}) is already loaded, so this "
            "one's documents were not written"
        )
        return report

    await _load_documents(session, artifact, report, count_tokens)

    # Removal only on a crawl that finished normally and stored something. A
    # host that was unreachable today, or a robots.txt answering 5xx, says
    # nothing about which pages still exist, and must not empty the corpus.
    if artifact.crawl_outcome != "completed":
        report.documents_held = (
            f"the crawl ended {artifact.crawl_outcome}, so stored documents it "
            "did not reach were kept"
        )
    elif not artifact.documents:
        report.documents_held = "the crawl stored no documents, so none were removed"
    else:
        report.documents_removed = await _remove_documents_not_in(
            session, report.prospect_id, artifact
        )
    return report


async def _latest_crawl_start(session: AsyncSession, prospect_id: int) -> dt.datetime:
    """The newest crawl loaded for a prospect. Called after this load's own
    run is inserted, so it is never earlier than this artifact's."""
    return await session.scalar(
        text("SELECT max(started_at) FROM crawl_runs WHERE prospect_id = :p"),
        {"p": prospect_id},
    )


async def _remove_documents_not_in(
    session: AsyncSession, prospect_id: int, artifact: Artifact
) -> list[str]:
    """Delete the prospect's documents this crawl does not contain. Returns
    their URLs.

    **[verified] 2026-09-11** (docs/open-questions.md §1.10): two fly.io pages
    from the 2026-09-02 crawl survived the 2026-09-09 load, and one ranked 3rd
    for q1, pushing the ground truth's best source page out of the top 10.

    Their chunks go with them through the chunks' ON DELETE CASCADE, so this
    module still writes no chunk SQL of its own (A1).
    """
    rows = await session.execute(
        text(
            "DELETE FROM documents "
            " WHERE prospect_id = :p AND source_url <> ALL(CAST(:urls AS text[])) "
            "RETURNING source_url"
        ),
        {"p": prospect_id, "urls": [doc.url for doc in artifact.documents]},
    )
    return sorted(row.source_url for row in rows)


# What to do with one document, given what is already stored for that URL.
#
#   stable_hash equal, content_hash equal      unchanged   touch timestamps
#   stable_hash equal, content_hash differs    reordered   store the new exact
#                                                          hash, keep chunks
#   stable_hash equal, chunk header stale      relabelled  re-chunk
#   stable_hash differs                        changed     re-chunk
#   stored stable_hash IS NULL                 unknown     re-chunk
#
# `relabelled` exists because a chunk is a function of FOUR inputs, not one:
# the text, and the title, kind and publication date that ADR-0005's
# provenance header writes into every chunk's content. The skip test used to
# look at the text alone. **[verified] 2026-09-10**: after ADR-0021 corrected
# `published` on most documents, a re-load updated `documents.published_at`
# (0 rows left at 2026-01-01) and skipped the chunks -- 30 of thoughtbot's 43
# still embedded the fabricated `2026-01-01` in their header, and 3 of fly.io's
# embedded `1998-01-01`. The database disagreed with itself, and the side the
# model reads was the wrong one.
#
# Checked against the chunks THEMSELVES, not the document row. The first
# version of this fix compared title, kind and date on the row -- and healed
# nothing, because the earlier load had already corrected the row: row and
# artifact agreed while the chunks still said 2026-01-01. The chunk text is the
# only witness to that staleness. Checking the header the chunks carry also
# catches a change to the header's FORMAT, which no comparison of inputs can.
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
    heads = await scope.first_chunk_contents()

    existing = {
        row.source_url: row
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
            same_text = bool(previous.stable_hash) and (
                previous.stable_hash == doc.stable_hash
            )
            # Do the stored chunks start with the header this document would
            # get now? The "\n\n" makes the match exact: a header that is a
            # prefix of the old one ("Title · website" against "Title ·
            # website · 2026-01-01") does not pass. A document with no stored
            # chunk fails too, and is re-chunked -- the safe direction.
            header = provenance_header(doc.title, doc.kind, doc.published)
            head = heads.get(previous.id)
            same_header = head is not None and head.startswith(
                f"{header}\n\n" if header else ""
            )
            if same_text and same_header:
                report.documents_updated += 1
                if previous.content_hash != doc.content_hash:
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
            if same_text:
                report.documents_relabelled += 1
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

    Stored exactly as given, never repaired: A4 forbids inventing a
    measurement. Until ADR-0021 that meant storing htmldate's coarse fallback
    -- 31 of 76 documents carried exactly `2026-01-01`. The crawler now records
    a date only when the page declares one, so a bad date here is a crawler
    defect to fix upstream, not something to patch in the loader.
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
