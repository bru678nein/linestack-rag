"""Unit tests for `linestack.evaluation.dataset`. No database, no network.

Each test writes a deliberately broken file and asserts the validator names the
problem. A validator whose rules have never fired is not known to work -- the
same standard the isolation guards are held to.
"""

from pathlib import Path

import pytest
import yaml

from linestack.evaluation.dataset import (
    QUESTION_IDS,
    validate_directory,
)


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _valid(artifact: str = "corpus.json") -> dict:
    return {
        "prospect": {
            "company_name": "Example",
            "domain": "example.test",
            "corpus_artifact": artifact,
            "author": "someone@example.test",
        },
        "signals": {"has_team_page": True, "people_listed": 3},
        "questions": [
            {
                "id": qid,
                "question": "A question.",
                "reference": "A hand-written answer.",
                "source_urls": ["https://example.test/about"],
            }
            for qid in QUESTION_IDS
        ],
    }


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A directory with the frozen artifact a valid file must reference."""
    (tmp_path.parent / "corpus.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _run(directory: Path):
    return validate_directory(directory, repo_root=directory.parent)


def test_a_well_formed_set_passes(corpus: Path) -> None:
    _write(corpus, "example_test.yaml", _valid())

    report = _run(corpus)

    assert report.ok, [str(f) for f in report.findings]
    assert (report.files, report.pairs) == (1, 4)


def test_an_empty_directory_is_not_an_error(tmp_path: Path) -> None:
    """The set is written by hand over hours. A rule that fails the build on the
    first day teaches everyone to ignore red before the set even exists."""
    report = _run(tmp_path)

    assert report.ok
    assert report.files == 0


def test_a_missing_corpus_artifact_is_rejected_not_skipped(corpus: Path) -> None:
    """docs/ground-truth.md §1: a reference answer without its frozen corpus is
    unfalsifiable. Nobody can tell later whether the answer was wrong or the
    corpus had moved underneath it."""
    _write(corpus, "example_test.yaml", _valid(artifact="not_on_disk.json"))

    report = _run(corpus)

    assert not report.ok
    assert any("unfalsifiable" in str(f) for f in report.findings)


def test_an_unknown_question_id_is_rejected(corpus: Path) -> None:
    """The four ids are fixed. A typo would create a silent fifth column in
    every metric the harness reports."""
    data = _valid()
    data["questions"][0]["id"] = "q1_what_and_to_who"  # typo

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert not report.ok
    assert any("is not one of" in str(f) for f in report.findings)


def test_a_duplicated_question_id_is_rejected(corpus: Path) -> None:
    data = _valid()
    data["questions"][1]["id"] = data["questions"][0]["id"]

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert any("appears twice" in str(f) for f in report.findings)


@pytest.mark.parametrize("field", ["question", "reference", "source_urls"])
def test_every_required_question_field_is_checked(corpus: Path, field: str) -> None:
    data = _valid()
    del data["questions"][0][field]

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert any(field in str(f) and "required" in str(f) for f in report.findings)


def test_an_answerable_question_with_no_evidence_is_rejected(corpus: Path) -> None:
    """Recall is computed against source_urls. An answerable pair with none
    makes it unmeasurable, and the honest label already exists (§3)."""
    data = _valid()
    data["questions"][0]["source_urls"] = []

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert any("insufficient_evidence instead" in str(f) for f in report.findings)


def test_insufficient_evidence_may_legitimately_cite_nothing(corpus: Path) -> None:
    """It is a correct answer, not a gap. These pairs are the most valuable in
    the set (§3)."""
    data = _valid()
    data["questions"][0]["source_urls"] = []
    data["questions"][0]["expected_outcome"] = "insufficient_evidence"

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert report.ok, [str(f) for f in report.findings]
    assert report.insufficient == 1


def test_an_unknown_expected_outcome_is_rejected(corpus: Path) -> None:
    data = _valid()
    data["questions"][0]["expected_outcome"] = "maybe"

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert any("expected_outcome" in str(f) for f in report.findings)


def test_evidence_from_another_company_is_rejected(corpus: Path) -> None:
    """A1 reaching into the dataset. A reference answer citing someone else's
    page makes recall unmeasurable and crosses prospects quietly (§4)."""
    data = _valid()
    data["questions"][0]["source_urls"] = ["https://a-competitor.test/about"]

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert any("is not on example.test" in str(f) for f in report.findings)


def test_a_subdomain_of_the_prospect_is_accepted(corpus: Path) -> None:
    data = _valid()
    data["questions"][0]["source_urls"] = ["https://blog.example.test/a-post"]

    _write(corpus, "example_test.yaml", data)

    assert _run(corpus).ok


def test_a_relative_url_is_rejected(corpus: Path) -> None:
    """source_urls are compared against documents.source_url, which is
    absolute. A relative one would silently never match."""
    data = _valid()
    data["questions"][0]["source_urls"] = ["/about"]

    _write(corpus, "example_test.yaml", data)
    report = _run(corpus)

    assert any("absolute http(s) URL" in str(f) for f in report.findings)


def test_malformed_yaml_is_reported_as_such(corpus: Path) -> None:
    (corpus / "broken.yaml").write_text("prospect: [unclosed\n", encoding="utf-8")

    report = _run(corpus)

    assert any("not valid YAML" in str(f) for f in report.findings)


def test_a_set_with_too_few_insufficient_pairs_warns_but_does_not_fail(
    corpus: Path,
) -> None:
    """A warning, deliberately. The share is a property of the FINISHED set, and
    failing the build while it is half-written is how a rule gets deleted."""
    _write(corpus, "example_test.yaml", _valid())

    report = _run(corpus)

    assert report.ok
    assert any("rewards fluency" in str(w) for w in report.warnings)


def test_the_four_question_ids_match_the_specification(corpus: Path) -> None:
    """These strings appear in docs/ground-truth.md, docs/evaluation.md and
    every metric the harness will report. One source of truth."""
    spec = (
        Path(__file__).resolve().parent.parent / "docs" / "ground-truth.md"
    ).read_text()
    for qid in QUESTION_IDS:
        assert qid in spec, f"{qid} is not in docs/ground-truth.md"


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------
def test_a_scaffold_does_not_validate(tmp_path: Path) -> None:
    """Half the value of the validator is that "it passes" means someone did
    the work. A skeleton that validated would let an empty set report green."""
    import json

    from linestack.evaluation.dataset import scaffold

    artifact = tmp_path.parent / "corpus.json"
    artifact.write_text(
        json.dumps(
            {
                "company_name": "Example",
                "domain": "example.test",
                "crawled_at": "2026-09-02T00:00:00+00:00",
                "signals": {"people_listed": 57},
                "documents": [{"url": "https://example.test/about", "kind": "website"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "example_test.yaml").write_text(scaffold(artifact), encoding="utf-8")

    report = _run(tmp_path)

    # Asserts the INTENT, not the wording. This test pinned the word
    # "placeholder" and broke when the messages were rewritten to be readable
    # -- a test that fails on rephrasing tells you nothing about behaviour.
    assert not report.ok
    assert any("not written yet" in str(f) for f in report.findings)
    assert any("signals" in str(f) for f in report.findings)


def test_the_scaffold_does_not_fill_in_the_signals(tmp_path: Path) -> None:
    """docs/ground-truth.md §2 step 3: signals are hand-checked against the
    LIVE site. Copying the crawler's numbers would make signal accuracy 100% by
    construction, because those numbers are exactly what the pairs test.

    The crawler's claim appears as a comment, so it can be confirmed or
    refuted -- which is how the 162-vs-54 and 0-vs-57 defects were found.
    """
    import json

    from linestack.evaluation.dataset import scaffold

    artifact = tmp_path / "corpus.json"
    artifact.write_text(
        json.dumps(
            {
                "company_name": "Example",
                "domain": "example.test",
                "crawled_at": "2026-09-02T00:00:00+00:00",
                "signals": {"people_listed": 57},
                "documents": [],
            }
        ),
        encoding="utf-8",
    )

    text = scaffold(artifact)

    assert "people_listed: TODO" in text
    assert "the crawler says: 57" in text


def test_the_scaffold_lists_only_urls_that_were_actually_crawled(
    tmp_path: Path,
) -> None:
    """If the page you want is missing, that is a coverage finding worth more
    than the pair (§2 step 2) -- not something to cite from the live site."""
    import json

    from linestack.evaluation.dataset import scaffold

    artifact = tmp_path / "corpus.json"
    artifact.write_text(
        json.dumps(
            {
                "company_name": "Example",
                "domain": "example.test",
                "crawled_at": "2026-09-02T00:00:00+00:00",
                "signals": {},
                "documents": [
                    {"url": "https://example.test/about", "kind": "website"},
                    {"url": "https://example.test/jobs/x", "kind": "job_posting"},
                ],
            }
        ),
        encoding="utf-8",
    )

    text = scaffold(artifact)

    assert "https://example.test/about" in text
    assert "https://example.test/jobs/x" in text
    assert "coverage finding" in text
