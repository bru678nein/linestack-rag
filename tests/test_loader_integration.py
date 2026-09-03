"""The loader against a live database.

What this proves is narrow and deliberate: that a crawl's bookkeeping lands
correctly and that loading twice is not loading twice. Documents and chunks are
not written yet.

Requires: make up && make migrate.
"""

import datetime as dt
import itertools
from pathlib import Path

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from linestack.ingestion.loader import (  # noqa: E402
    ArtifactRefused,
    ArtifactTooOld,
    load_artifact,
    read_artifact,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 3, tzinfo=dt.UTC)


_SEQUENCE = itertools.count()


def _artifact(name: str, *, isolate: bool = True):
    """A frozen artifact, re-identified so it cannot collide with real data.

    The domain and crawled_at are rewritten per test. Without that these tests
    depend on nobody having run `make load` for the same prospect -- which is
    exactly what happened on 2026-09-02: a demonstration load committed fly.io
    and thoughtbot rows, and two tests asserting `crawl_run_existed is False`
    started failing on state rather than on a defect.

    (prospect_id, started_at) is the crawl_runs natural key from migration
    0003, so varying both is what makes a run unique.
    """
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is gitignored and not on disk; re-crawl to restore")
    artifact = read_artifact(path)
    if isolate:
        nth = next(_SEQUENCE)
        artifact.domain = f"test-{nth}-{artifact.domain}"
        artifact.crawled_at = (
            artifact.crawled_at_utc - dt.timedelta(seconds=nth + 1)
        ).isoformat()
    return artifact


async def test_a_full_crawl_loads_with_the_counts_the_artifact_reports(
    db_session,
) -> None:
    artifact = _artifact("prospect_fly_io.json")

    report = await load_artifact(db_session, artifact, now=NOW)

    assert report.crawl_run_existed is False
    assert report.outcomes_written == 97
    assert report.pages_fetched == 61
    assert report.documents_in_artifact == 39
    assert report.counts_by_outcome == {
        "stored": 39,
        "budget_exhausted": 36,
        "http_error": 19,
        "non_html": 2,
        "duplicate_content": 1,
    }

    rows = (
        await db_session.execute(
            text(
                "SELECT outcome, count(*) c FROM crawl_page_outcomes "
                " WHERE crawl_run_id = :r GROUP BY outcome"
            ),
            {"r": report.crawl_run_id},
        )
    ).all()
    assert {r.outcome: r.c for r in rows} == report.counts_by_outcome


async def test_loading_the_same_artifact_twice_does_not_duplicate_anything(
    db_session,
) -> None:
    """A7: re-running produces the same result or fails loudly.

    Enforced by the natural key added in migration 0003, not by a
    select-then-insert here. Every page outcome hangs off crawl_run_id, so a
    duplicate run would silently double the recorded outcomes and
    docs/evaluation.md section 2.5 would start answering twice.
    """
    artifact = _artifact("prospect_thoughtbot_com.json")

    first = await load_artifact(db_session, artifact, now=NOW)
    await db_session.flush()
    second = await load_artifact(db_session, artifact, now=NOW)

    assert second.crawl_run_id == first.crawl_run_id
    assert second.crawl_run_existed is True
    assert second.outcomes_written == 0
    assert second.outcomes_skipped == first.outcomes_written

    total = await db_session.scalar(
        text("SELECT count(*) FROM crawl_runs WHERE prospect_id = :p"),
        {"p": first.prospect_id},
    )
    assert total == 1


async def test_a_crawl_that_found_nothing_records_why_in_one_row(
    db_session,
) -> None:
    """The whole point of A5, in one assertion.

    Zero documents with a recorded cause is a fact about a dead domain. Zero
    documents with no cause is a fact about our crawler wearing the label of a
    fact about the company.
    """
    artifact = _artifact("prospect_this-domain-does-not-exist-9f3a2b1c_com.json")

    report = await load_artifact(db_session, artifact, now=NOW)
    await db_session.flush()

    run = (
        await db_session.execute(
            text(
                "SELECT outcome, robots_reason, documents_stored, pages_fetched "
                "  FROM crawl_runs WHERE id = :r"
            ),
            {"r": report.crawl_run_id},
        )
    ).one()
    assert run.outcome == "aborted_unreachable"
    assert run.robots_reason == "fetch_failed"
    assert run.documents_stored == 0

    outcomes = (
        await db_session.execute(
            text(
                "SELECT outcome, detail FROM crawl_page_outcomes "
                " WHERE crawl_run_id = :r"
            ),
            {"r": report.crawl_run_id},
        )
    ).all()
    assert [o.outcome for o in outcomes] == ["dns_failure"]


async def test_reloading_updates_the_signals_rather_than_accumulating(
    db_session,
) -> None:
    """Signals describe one crawl, not a growing history.

    A signal that was true last month and is not true now must go DOWN. Merging
    would make has_team_page a ratchet that can only ever become true.
    """
    artifact = _artifact("prospect_fly_io.json")
    report = await load_artifact(db_session, artifact, now=NOW)
    await db_session.flush()

    artifact.signals = {"has_team_page": False, "people_listed": 0}
    await load_artifact(db_session, artifact, now=NOW)
    await db_session.flush()

    stored = await db_session.scalar(
        text("SELECT signals FROM prospects WHERE id = :p"),
        {"p": report.prospect_id},
    )
    assert stored == {"has_team_page": False, "people_listed": 0}


async def test_a_stale_artifact_never_reaches_the_database(db_session) -> None:
    """Refused before the first INSERT, not rolled back after some of it."""
    artifact = _artifact("prospect_fly_io.json")
    much_later = artifact.crawled_at_utc + dt.timedelta(days=400)

    before = await db_session.scalar(text("SELECT count(*) FROM prospects"))
    with pytest.raises(ArtifactTooOld):
        await load_artifact(db_session, artifact, now=much_later)
    assert await db_session.scalar(text("SELECT count(*) FROM prospects")) == before


async def test_an_artifact_with_no_robots_reason_is_refused_readably(
    db_session,
) -> None:
    """crawl_runs.robots_reason is NOT NULL and 'we did not record it' is not
    one of the five codes. Better a named refusal than a constraint violation.
    """
    artifact = _artifact("prospect_fly_io.json")
    artifact.robots_reason = ""

    with pytest.raises(ArtifactRefused, match="predates ADR-0006"):
        await load_artifact(db_session, artifact, now=NOW)


async def test_the_derivation_of_uncertain_columns_is_recorded_on_the_run(
    db_session,
) -> None:
    """A4: a number nobody recorded must never later look like one that was.

    started_at is the crawl's END time, and max_pages and user_agent come from
    configuration because the artifact does not carry them.
    """
    artifact = _artifact("prospect_fly_io.json")
    report = await load_artifact(db_session, artifact, now=NOW)

    detail = await db_session.scalar(
        text("SELECT detail FROM crawl_runs WHERE id = :r"),
        {"r": report.crawl_run_id},
    )
    assert "end time" in detail
    assert "does not carry them" in detail
