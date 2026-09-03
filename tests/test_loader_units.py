"""Unit tests for `linestack.ingestion.loader`. No database, no network.

These read the three frozen artifacts on disk. That is deliberate: the loader's
contract is with what `ingest.py` actually writes, and a hand-built fixture
would only assert that the loader agrees with my idea of the format.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from linestack.ingestion.loader import (
    NOT_FETCHED,
    Artifact,
    ArtifactTooOld,
    count_pages_fetched,
    read_artifact,
    refuse_if_stale,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# (filename, expected pages_fetched, expected documents)
FIXTURES = [
    ("prospect_fly_io.json", 61, 39),
    ("prospect_thoughtbot_com.json", 58, 37),
]


def _load(name: str) -> Artifact:
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(
            f"{name} is not on disk. It is gitignored; regenerate with "
            f"`make crawl DOMAIN=...`. These tests assert against a real "
            f"crawl rather than a hand-built fixture on purpose."
        )
    return read_artifact(path)


@pytest.mark.parametrize(("name", "fetched", "documents"), FIXTURES)
def test_the_real_artifacts_parse_with_no_unexpected_fields(
    name: str, fetched: int, documents: int
) -> None:
    """`extra="forbid"`: a new key in the artifact must fail here loudly.

    ingest.py has added three fields to the artifact during this project
    (extract_reason, stable_hash, duplicate_urls) and dropped two. A loader
    that ignores unknown keys would silently stop loading the next one.
    """
    artifact = _load(name)
    assert len(artifact.documents) == documents
    assert artifact.crawled_at_utc.tzinfo is not None


@pytest.mark.parametrize(("name", "fetched", "documents"), FIXTURES)
def test_pages_fetched_is_derived_the_way_crawl_runs_records_it(
    name: str, fetched: int, documents: int
) -> None:
    """The artifact carries no request count, so it is derived.

    Every outcome except skipped_robots and budget_exhausted cost one request:
    a disallowed path was never requested, and a URL left in the queue when the
    budget ran out was never attempted.
    """
    assert count_pages_fetched(_load(name)) == fetched


@pytest.mark.parametrize(("name", "fetched", "documents"), FIXTURES)
def test_every_stored_outcome_corresponds_to_a_document(
    name: str, fetched: int, documents: int
) -> None:
    """The two records must agree, or `documents` and `crawl_page_outcomes`
    tell different stories about the same crawl."""
    artifact = _load(name)
    stored = {o.url for o in artifact.page_outcomes if o.outcome == "stored"}
    assert stored == {d.url for d in artifact.documents}


@pytest.mark.parametrize(("name", "fetched", "documents"), FIXTURES)
def test_every_duplicate_url_already_has_its_own_outcome_row(
    name: str, fetched: int, documents: int
) -> None:
    """So the loader needs no special handling for duplicate_urls.

    A deduplicated URL is recorded twice, in two different senses: as an alias
    on the surviving document, and as its own duplicate_content outcome. The
    two are independent records and neither is derived from the other.
    """
    artifact = _load(name)
    duplicates = {
        o.url for o in artifact.page_outcomes if o.outcome == "duplicate_content"
    }
    aliases = {url for d in artifact.documents for url in d.duplicate_urls}
    assert aliases, f"{name} has no deduplicated URLs; this test proves nothing"
    assert aliases <= duplicates


@pytest.mark.parametrize(("name", "fetched", "documents"), FIXTURES)
def test_no_url_appears_twice_in_the_outcomes(
    name: str, fetched: int, documents: int
) -> None:
    """crawl_page_outcomes is UNIQUE (crawl_run_id, url). A duplicate in the
    artifact would be silently dropped by ON CONFLICT and the counts would
    disagree with no error."""
    artifact = _load(name)
    urls = [o.url for o in artifact.page_outcomes]
    assert len(urls) == len(set(urls))


def test_a_stale_artifact_is_refused_with_a_readable_reason() -> None:
    artifact = _load("prospect_fly_io.json")
    much_later = artifact.crawled_at_utc + dt.timedelta(days=400)

    with pytest.raises(ArtifactTooOld, match="Re-crawl with"):
        refuse_if_stale(artifact, now=much_later)


def test_staleness_takes_now_as_a_parameter_not_from_the_clock() -> None:
    """Otherwise these tests go red on a date rather than on a defect.

    The fixtures are frozen crawls with real timestamps. A loader that read
    the clock would start refusing them 30 days after they were made, and the
    failure would look like a loader bug.
    """
    artifact = _load("prospect_fly_io.json")
    just_after = artifact.crawled_at_utc + dt.timedelta(hours=1)

    refuse_if_stale(artifact, now=just_after)  # must not raise


def test_the_age_limit_is_configurable_per_call() -> None:
    artifact = _load("prospect_fly_io.json")
    later = artifact.crawled_at_utc + dt.timedelta(hours=10)

    refuse_if_stale(artifact, now=later, max_age_hours=24)
    with pytest.raises(ArtifactTooOld):
        refuse_if_stale(artifact, now=later, max_age_hours=5)


def test_an_unknown_field_in_an_artifact_is_a_loud_failure() -> None:
    with pytest.raises(Exception, match="extra_inputs_are_not_permitted|Extra"):
        Artifact.model_validate(
            {
                "company_name": "x",
                "domain": "x.test",
                "base_url": "https://x.test",
                "crawled_at": "2026-09-02T00:00:00+00:00",
                "a_field_ingest_started_writing": 1,
            }
        )


def test_not_fetched_matches_the_outcomes_that_cost_no_request() -> None:
    """Named rather than inlined, because getting this set wrong silently
    changes crawl_runs.pages_fetched for every prospect."""
    assert {"skipped_robots", "budget_exhausted"} == NOT_FETCHED


def test_a_crawl_that_found_nothing_still_carries_its_reason() -> None:
    """A5 in one artifact: zero documents, and a recorded cause.

    This is the case the whole outcome vocabulary exists for. Before ADR-0012
    this artifact and a successful crawl of an empty site were indistinguishable.
    """
    path = REPO_ROOT / "prospect_this-domain-does-not-exist-9f3a2b1c_com.json"
    if not path.exists():
        pytest.skip(
            "the dead-domain fixture is not on disk; regenerate with "
            "`.venv/bin/python ingest.py https://this-domain-does-not-exist"
            "-9f3a2b1c.com` (it exits 1, which is the point)"
        )
    artifact = read_artifact(path)

    assert artifact.documents == []
    assert artifact.crawl_outcome == "aborted_unreachable"
    assert [o.outcome for o in artifact.page_outcomes] == ["dns_failure"]


def test_crawled_at_is_parsed_because_asyncpg_rejects_the_string() -> None:
    """The artifact stores ISO text; timestamptz wants a datetime.

    Verified 2026-09-02: passing the raw string gives
    "invalid input for query argument $8 ... expected a datetime.date or
    datetime.datetime instance, got 'str'".
    """
    artifact = _load("prospect_fly_io.json")
    assert isinstance(artifact.crawled_at_utc, dt.datetime)
    assert isinstance(artifact.crawled_at, str)


def test_the_artifact_model_matches_what_ingest_actually_writes() -> None:
    """Guards the contract from the other side.

    If ingest.py grows a top-level key, this fails with the key named, rather
    than the loader failing later with a validation error nobody expects.
    """
    from dataclasses import fields

    import ingest

    written = {f.name for f in fields(ingest.Prospect)}
    modelled = set(Artifact.model_fields)
    assert written == modelled, (
        f"ingest.Prospect writes {written - modelled} that the loader does "
        f"not model, and the loader expects {modelled - written} that it does "
        f"not write"
    )


@pytest.mark.parametrize(("name", "fetched", "documents"), FIXTURES)
def test_the_document_model_matches_what_ingest_actually_writes(
    name: str, fetched: int, documents: int
) -> None:
    from linestack.ingestion.loader import ArtifactDocument

    # The same skip guard every other test in this file uses. Without it this
    # one read the file directly and failed on a clean checkout, where the
    # artifacts do not exist because they are gitignored. It passed locally
    # forever, which is exactly why CI on a fresh clone is worth having.
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is gitignored and not on disk; re-crawl to restore")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw["documents"][0]) == set(ArtifactDocument.model_fields)
