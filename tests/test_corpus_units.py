"""Unit tests for `linestack.evaluation.corpus`. No database, no network.

Small module, and the tests are small. The one that matters is the last: a
substring that matches several documents must show all of them, because the
alternative is silently showing one and letting someone write a reference
answer from a page they did not mean.
"""

import json
from pathlib import Path

import pytest

from linestack.evaluation.corpus import (
    _main,
    document_lines,
    find,
    load,
    text_lines,
)


def _artifact(tmp_path: Path, documents: list[dict]) -> Path:
    path = tmp_path / "prospect_ex_test.json"
    path.write_text(json.dumps({"documents": documents}), encoding="utf-8")
    return path


def _doc(url: str, text: str = "one two three", **extra) -> dict:
    return {"url": url, "kind": "website", "text": text, **extra}


def test_a_missing_artifact_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="make crawl"):
        load(tmp_path / "nope.json")


def test_documents_are_listed_longest_first(tmp_path: Path) -> None:
    """The question being answered is "where is there enough text to answer
    anything". A 3-word navigation stub sorted next to a long article hides
    that."""
    data = json.loads(
        _artifact(
            tmp_path,
            [_doc("https://ex.test/short"), _doc("https://ex.test/long", "a " * 50)],
        ).read_text()
    )

    lines = document_lines(data)

    assert "2 documents, 53 words total" in lines[0]
    assert "/long" in lines[1] and "/short" in lines[2]


def test_the_text_shown_is_the_crawled_text(tmp_path: Path) -> None:
    """Not the live page. This is the whole point of the module
    (docs/ground-truth.md §2 step 2)."""
    doc = _doc("https://ex.test/about", "what the crawler actually stored")

    body = "\n".join(text_lines(doc))

    assert "what the crawler actually stored" in body
    assert "https://ex.test/about" in body


def test_the_provenance_a_citation_needs_is_shown(tmp_path: Path) -> None:
    """An alias is why a page can be cited under a URL the author did not
    expect (ADR-0013), and a contested kind is worth seeing (ADR-0019)."""
    doc = _doc(
        "https://ex.test/",
        duplicate_urls=["https://ex.test/refer/jobs"],
        kind_conflicts=["job_posting"],
        published="2026-01-01",
        extract_reason="recovered_recall",
    )

    body = "\n".join(text_lines(doc))

    assert "also at:   https://ex.test/refer/jobs" in body
    assert "kind also: job_posting" in body
    assert "recovered_recall" in body


def test_every_matching_document_is_returned_not_just_the_first(
    tmp_path: Path,
) -> None:
    """`/blog` matches the index and every post. Showing one of them silently
    is how someone writes a reference answer from a page they did not mean."""
    data = json.loads(
        _artifact(
            tmp_path,
            [
                _doc("https://ex.test/blog"),
                _doc("https://ex.test/blog/a-post"),
                _doc("https://ex.test/about"),
            ],
        ).read_text()
    )

    assert len(find(data, "/blog")) == 2
    assert len(find(data, "/about")) == 1


def test_a_url_that_was_never_crawled_is_a_coverage_finding(
    tmp_path: Path, capsys
) -> None:
    """Not "not found". §2 step 2 says a page the crawler never fetched is
    worth more than the pair, and the exit code has to be non-zero or a script
    reading this would carry on."""
    path = _artifact(tmp_path, [_doc("https://ex.test/about")])

    code = _main([str(path), "--url", "/pricing"])

    assert code == 1
    assert "coverage finding" in capsys.readouterr().out
