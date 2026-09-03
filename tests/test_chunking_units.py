"""Unit tests for `linestack.ingestion.chunking`. No database, no network.

Token counting is injected throughout. tiktoken downloads its BPE file on first
use, which would make "unit tests, no network" false on a cold cache; one test
exercises the real encoder and skips if it cannot load.
"""

import json
import statistics
from pathlib import Path

import pytest

from linestack.config import settings
from linestack.ingestion.chunking import (
    ChunkingReport,
    chunk_document,
    looks_like_heading,
    provenance_header,
    segment,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def words(text: str) -> int:
    """A hermetic stand-in for tiktoken: one token per word."""
    return len(text.split())


def prose(n: int, marker: str = "w") -> str:
    return " ".join(f"{marker}{i}" for i in range(n))


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "Meet the team",
        "Decision #1: No More Container Images",
        "## A markdown heading",
        "What We Do",
    ],
)
def test_a_heading_looks_like_a_heading(line: str) -> None:
    assert looks_like_heading(line)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "- a list item",
        "| a | table | row |",
        "This is an ordinary sentence that ends with a full stop.",
        "we ship on fridays",
        prose(40),
    ],
)
def test_prose_and_structure_are_not_headings(line: str) -> None:
    assert not looks_like_heading(line)


def test_an_index_page_of_titles_does_not_become_all_headings() -> None:
    """The regression that shaped the whole design.

    fly.io/blog is 11,175 words in which nearly every line is a post title.
    A naive "short line is a heading" rule matched 806 of its 1,156 lines,
    which would have produced 806 chunks of about 40 tokens. A heading is
    followed by prose; a list of titles is followed by another title.
    """
    titles = "\n".join(f"Some Blog Post Number {i}" for i in range(60))

    blocks = segment(titles, words)

    assert sum(1 for b in blocks if b.is_heading) == 0, (
        "consecutive short lines are a list of titles, not 60 sections"
    )


def test_a_heading_followed_by_prose_is_a_heading() -> None:
    text = f"Meet The Team\n{prose(30)}"
    blocks = segment(text, words)
    assert [b.is_heading for b in blocks] == [True, False]


# ---------------------------------------------------------------------------
# Block segmentation
# ---------------------------------------------------------------------------
def test_consecutive_list_lines_become_one_block() -> None:
    """A boundary inside a list produces a fragment that starts mid-item."""
    text = "- alpha\n- bravo\n- charlie"
    blocks = segment(text, words)
    assert len(blocks) == 1
    assert blocks[0].kind == "list"


def test_consecutive_table_rows_become_one_block() -> None:
    text = "| a | b |\n| 1 | 2 |\n| 3 | 4 |"
    blocks = segment(text, words)
    assert len(blocks) == 1
    assert blocks[0].kind == "table"


def test_a_blank_line_ends_a_paragraph() -> None:
    blocks = segment(f"{prose(20)}\n\n{prose(20, 'x')}", words)
    assert len(blocks) == 2


# ---------------------------------------------------------------------------
# ADR-0005 policy
# ---------------------------------------------------------------------------
def test_a_job_posting_is_never_split_however_long(_=None) -> None:
    """ADR-0005, unconditional. A posting split in half is two half-answers to
    question 3, and neither says what the role is."""
    report = ChunkingReport()
    drafts = chunk_document(
        text=prose(4000),
        kind="job_posting",
        title="Networking Engineer",
        count_tokens=words,
        report=report,
    )
    assert len(drafts) == 1
    assert drafts[0].token_count > settings.chunk_max_tokens


def test_a_short_document_is_one_short_chunk_not_padded() -> None:
    """ADR-0005 forbids merging a short section with an unrelated one, so a
    document under the band is simply a short chunk."""
    drafts = chunk_document(text=prose(50), kind="website", count_tokens=words)
    assert len(drafts) == 1
    assert drafts[0].token_count < settings.chunk_min_tokens


def test_a_long_document_is_split_near_the_band() -> None:
    sections = "\n\n".join(
        f"Section Number {i}\n{prose(400, f'p{i}')}" for i in range(6)
    )
    drafts = chunk_document(text=sections, kind="website", count_tokens=words)

    assert len(drafts) > 1
    assert all(d.token_count <= settings.chunk_hard_max_tokens for d in drafts)


def test_chunks_overlap_so_a_split_sentence_is_reachable_from_either_side() -> None:
    """Overlap is word-level, not block-level, and this test is why.

    The first implementation carried whole trailing BLOCKS back under the
    150-token budget. A 400-token paragraph never fits, so every boundary in
    ordinary prose got zero overlap -- consecutive chunks shared three words,
    all of them from the provenance header. It looked tidier and did nothing.
    """
    sections = "\n\n".join(
        f"Section Number {i}\n{prose(400, f'p{i}')}" for i in range(4)
    )
    drafts = chunk_document(text=sections, kind="website", count_tokens=words)

    assert len(drafts) >= 2
    tail = set(drafts[0].content.split())
    head = set(drafts[1].content.split())
    shared = {w for w in tail & head if w.startswith("p")}
    assert len(shared) > 50, (
        f"consecutive chunks share only {len(shared)} body words; the overlap "
        f"budget is {settings.chunk_overlap_tokens} tokens"
    )


def test_a_block_too_large_for_the_model_is_force_split_and_counted() -> None:
    """fly.io's pricing page is one table of roughly 13,000 tokens, past
    text-embedding-3-small's 8191 limit, with no heading or paragraph inside
    it. There is no good boundary -- only a necessary one, so it is counted
    rather than absorbed.
    """
    table = "\n".join(f"| row {i} | {prose(20)} |" for i in range(800))
    report = ChunkingReport()

    drafts = chunk_document(
        text=table, kind="website", count_tokens=words, report=report
    )

    assert report.force_split_blocks == 1
    assert all(d.token_count <= settings.chunk_hard_max_tokens + 50 for d in drafts), (
        "a chunk survived above the hard cap and would be rejected by the API"
    )


def test_an_empty_document_produces_no_chunks() -> None:
    assert chunk_document(text="   \n\n  ", kind="website", count_tokens=words) == []


# ---------------------------------------------------------------------------
# Provenance header (ADR-0005 asks for the date; chunks has no metadata column)
# ---------------------------------------------------------------------------
def test_the_header_carries_title_kind_and_date_into_the_chunk() -> None:
    header = provenance_header("About Us", "website", "2026-05-01")
    assert header == "About Us · website · 2026-05-01"


def test_the_header_omits_what_is_missing_rather_than_writing_none() -> None:
    assert provenance_header("", "blog_post", None) == "blog_post"


def test_every_chunk_carries_the_header() -> None:
    paragraphs = "\n\n".join(prose(300, f"p{i}") for i in range(8))
    drafts = chunk_document(
        text=paragraphs,
        kind="blog_post",
        title="A Post",
        published="2026-05-01",
        count_tokens=words,
    )
    assert len(drafts) > 1
    assert all(d.content.startswith("A Post · blog_post · 2026-05-01") for d in drafts)


def test_one_enormous_paragraph_is_not_split_and_that_is_recorded() -> None:
    """A known limitation, deliberately not fixed.

    The packer never breaks inside a block, so a single paragraph with no
    internal line breaks becomes one oversized chunk. Sentence-level splitting
    would fix it and is NOT built, because the corpus does not need it:
    **[verified]** the three single-line documents are 316, 53 and 40 words,
    all comfortably inside the band.

    The trigger to build it is the first single-line document above
    `chunk_max`. This test is that trigger's tripwire -- it documents current
    behaviour, so when the assumption stops holding someone reads this first.
    """
    drafts = chunk_document(text=prose(2000), kind="website", count_tokens=words)

    assert len(drafts) == 1
    assert drafts[0].token_count > settings.chunk_max_tokens


def test_the_hard_cap_holds_even_for_a_single_unbreakable_line() -> None:
    """Above the hard cap the API rejects the request outright, so a line with
    no boundary is cut at words. A poor boundary beats a rejected load."""
    drafts = chunk_document(
        text=prose(settings.chunk_hard_max_tokens * 3),
        kind="website",
        count_tokens=words,
    )

    assert len(drafts) > 1
    assert all(d.token_count <= settings.chunk_hard_max_tokens + 50 for d in drafts)


# ---------------------------------------------------------------------------
# The real corpus: the A3 before-and-after record
# ---------------------------------------------------------------------------
CORPUS = [("prospect_fly_io.json", 39), ("prospect_thoughtbot_com.json", 37)]


@pytest.mark.parametrize(("name", "documents"), CORPUS)
def test_no_chunk_from_the_real_corpus_exceeds_the_embedding_limit(
    name: str, documents: int
) -> None:
    """The one hard requirement. A chunk over 8191 tokens is rejected by the
    API, and a whole load fails on one pricing page."""
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is gitignored and not on disk; re-crawl to restore")
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert len(artifact["documents"]) == documents

    sizes = []
    for doc in artifact["documents"]:
        for draft in chunk_document(
            text=doc["text"],
            kind=doc["kind"],
            title=doc["title"],
            published=doc["published"],
            count_tokens=words,
        ):
            sizes.append(draft.token_count)

    assert sizes, "the corpus produced no chunks at all"
    assert max(sizes) <= settings.chunk_hard_max_tokens + 50, (
        f"largest chunk is {max(sizes)} tokens"
    )
    assert statistics.median(sizes) > 0


def test_real_tiktoken_agrees_that_the_largest_posting_fits() -> None:
    """Settles docs/open-questions.md section 2.3, previously [assumed].

    **[verified] 2026-09-02**: the largest job posting in the corpus,
    fly.io/jobs/networking-engineer, is 1,493 tiktoken tokens -- above
    ADR-0005's guessed "typically under 1500", and about five times under the
    8191-token embedding limit. The never-split rule holds with room.
    """
    tiktoken = pytest.importorskip("tiktoken")
    try:
        encoding = tiktoken.encoding_for_model(settings.embedding_model)
    except Exception:  # pragma: no cover - offline, no cached BPE file
        pytest.skip("tiktoken cannot load its encoding offline")

    path = REPO_ROOT / "prospect_fly_io.json"
    if not path.exists():
        pytest.skip("prospect_fly_io.json is not on disk")
    artifact = json.loads(path.read_text(encoding="utf-8"))

    postings = [d for d in artifact["documents"] if d["kind"] == "job_posting"]
    assert postings, "no job postings in the corpus; this test proves nothing"

    largest = 0
    for doc in postings:
        drafts = chunk_document(
            text=doc["text"],
            kind=doc["kind"],
            title=doc["title"],
            published=doc["published"],
            count_tokens=lambda t: len(encoding.encode(t)),
        )
        assert len(drafts) == 1, f"{doc['url']} was split"
        largest = max(largest, drafts[0].token_count)

    assert largest < 8191, f"largest posting is {largest} tokens"
    assert largest > 1000, "suspiciously small; is the corpus truncated?"
