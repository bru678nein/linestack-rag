"""Responsibility: splitting a document into embeddable chunks.

Owns: the policy in ADR-0005 -- 800-1200 tokens, roughly 150 tokens of overlap,
split on heading structure rather than character counts, job postings never
split, publication date carried into chunk metadata -- and token counting via
tiktoken so that "800-1200 tokens" is a measured quantity rather than an
estimate.

Does not own: embedding. A chunk is written to the database before it has a
vector; chunks.embedding is nullable for that reason.

Every parameter here is a measured variable, not a constant. Chunk size and
overlap are recorded with every evaluation run, and a change to either ships
only with a recorded before-and-after (A3).

WHAT THE CORPUS ACTUALLY LOOKS LIKE, measured 2026-09-02 over all 76 documents
of the two validation crawls, because ADR-0005 says "split on heading
structure" and it turns out this corpus barely has any:

  documents with markdown headings (`# `)      1 of 76
  documents over 900 words, needing any split  24 of 76
  documents that are a single line             3 of 76

`ingest.py` calls `trafilatura.extract` with no `output_format`, so the default
is plain text: headings survive only as short lines, indistinguishable from
short paragraphs. Splitting on headings alone therefore puts almost nothing in
the 800-1200 band.

Worse, a naive "short line is a heading" rule catastrophically over-segments.
fly.io/blog is an index page of 11,175 words in which nearly every line is a
post title: 806 of its 1,156 lines match a naive rule, which would produce 806
chunks of ~40 tokens each.

So headings are a PREFERRED BOUNDARY inside a size-driven packer, not the
splitter. The failure mode becomes "a chunk boundary fell at a paragraph
instead of a heading", not "806 chunks of nothing".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from linestack.config import settings
from linestack.retrieval.scope import ChunkDraft

# A heading, as it survives plain-text extraction: a short line that does not
# end like a sentence and is not part of a list or table. The run-suppression
# rule below is what stops an index page of post titles reading as 806
# headings -- a real heading is followed by prose, not by another heading.
_HEADING_MAX_WORDS = 10
_HEADING_MAX_CHARS = 70
_PROSE_MIN_WORDS = 12

_LIST_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+")
_TABLE_LINE = re.compile(r"^\s*\|")
_SENTENCE_END = re.compile(r"[.!?:;,]\s*$")

# Markdown headings do exist, rarely. When present they are unambiguous and
# outrank the heuristic.
_ATX_HEADING = re.compile(r"^\s*#{1,6}\s+\S")


@dataclass(frozen=True)
class Block:
    """One atomic unit of text. Never split, except by the hard cap.

    A list or a table read as a single block because breaking one mid-row
    produces a chunk that begins with half a row and means nothing on its own.
    """

    text: str
    tokens: int
    is_heading: bool
    kind: str  # "paragraph" | "list" | "table"


@dataclass
class ChunkingReport:
    """Measurements from one document. Counted, never inferred."""

    blocks: int = 0
    headings: int = 0
    chunks: int = 0
    force_split_blocks: int = 0
    over_max: int = 0


# One fixed encoding, deliberately NOT the embedding model's own tokenizer.
#
# ADR-0005 sizes chunks in tokens as a stand-in for content volume, and A3
# compares chunk-size distributions before and after a change. Both of those
# break if the counter changes when the embedding model changes: the same
# document would produce different chunks for reasons having nothing to do with
# chunking, and no measurement would be comparable across the switch.
#
# This was not hypothetical. Switching the default model to a local one
# (ADR-0017) made `encoding_for_model` raise, the old code swallowed it, and the
# counter silently became a WORD count. fly.io went from 111 chunks to 83 with
# no error anywhere. Words are fewer than tokens, so every chunk quietly grew.
TOKEN_ENCODING = "cl100k_base"


class TokenCountingUnavailable(RuntimeError):
    """Raised when tokens cannot be counted, rather than guessed at."""


def default_token_counter() -> Callable[[str], int]:
    """The fixed encoding above. Fails loudly rather than degrading quietly.

    tiktoken downloads its BPE file on first use, so a cold cache makes "unit
    tests, no network" false. Every function here takes `count_tokens` as a
    parameter for that reason; this is only the default for real use.

    There is no word-count fallback. A silent fallback here does not fail, it
    produces a differently-chunked corpus and calls the result a token count.
    """
    try:
        import tiktoken

        encoding = tiktoken.get_encoding(TOKEN_ENCODING)
    except Exception as exc:  # pragma: no cover - exercised only without tiktoken
        raise TokenCountingUnavailable(
            f"cannot load the {TOKEN_ENCODING} encoding, and chunk sizes are "
            f"specified in tokens (ADR-0005). Counting words instead would "
            f"silently produce a different corpus. Install tiktoken and allow "
            f"it to fetch its BPE file once, or pass count_tokens explicitly."
        ) from exc
    return lambda text: len(encoding.encode(text))


def provenance_header(title: str, kind: str, published: str | None) -> str:
    """The line prepended to every chunk.

    ADR-0005 requires the publication date in chunk metadata. `chunks` has no
    metadata column, and ADR-0009's frozen SELECT returns only id, content,
    kind and score -- so a header inside `content` is the only place the date
    can reach generation without editing either decision.

    The cost is real and recorded: the header is embedded, enters
    `content_tsv`, and counts toward `token_count`. It is a chunking parameter
    like any other and belongs in the A3 before-and-after.

    Note the date is often not a real publication date. **[verified]** 31 of
    the corpus's 76 documents carry exactly `2026-01-01` and 9 carry none --
    htmldate's coarse fallback. Stored as given (A4 forbids inventing a
    measurement); anything keying on recency rests on that.
    """
    parts = [p for p in (title.strip(), kind, published) if p]
    return " · ".join(parts)


def looks_like_heading(line: str) -> bool:
    """Whether one line, on its own, reads like a heading.

    Deliberately not the whole rule: see `segment`, which additionally requires
    that the next line be prose. Without that, an index page of post titles is
    all headings and nothing else.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _ATX_HEADING.match(stripped):
        return True
    if _LIST_LINE.match(stripped) or _TABLE_LINE.match(stripped):
        return False
    if len(stripped) > _HEADING_MAX_CHARS:
        return False
    words = stripped.split()
    if not 1 < len(words) <= _HEADING_MAX_WORDS:
        return False
    if _SENTENCE_END.search(stripped):
        return False
    # Mostly capitalised: "Meet the team" yes, "we ship on fridays" no.
    capitalised = sum(1 for w in words if w[:1].isupper())
    return capitalised >= max(1, len(words) // 2)


def segment(text: str, count_tokens: Callable[[str], int]) -> list[Block]:
    """Split text into atomic blocks, marking which begin a section.

    Consecutive list lines become one list block and consecutive table rows one
    table block, because a chunk boundary inside either produces a fragment
    that means nothing alone.
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    buffer: list[str] = []
    buffer_kind = "paragraph"

    def flush(is_heading: bool = False) -> None:
        nonlocal buffer, buffer_kind
        if not buffer:
            return
        joined = "\n".join(buffer).strip()
        if joined:
            blocks.append(Block(joined, count_tokens(joined), is_heading, buffer_kind))
        buffer, buffer_kind = [], "paragraph"

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            flush()
            index += 1
            continue

        kind = (
            "list"
            if _LIST_LINE.match(line)
            else "table"
            if _TABLE_LINE.match(line)
            else "paragraph"
        )

        if kind in ("list", "table"):
            if buffer_kind != kind:
                flush()
                buffer_kind = kind
            buffer.append(line)
            index += 1
            continue

        # A heading only counts as one when prose follows it. This is the rule
        # that stops fly.io/blog's 806 post titles from each opening a section.
        following = next((raw for raw in lines[index + 1 :] if raw.strip()), "")
        is_heading = looks_like_heading(line) and (
            len(following.split()) >= _PROSE_MIN_WORDS
            or _ATX_HEADING.match(line.strip()) is not None
        )

        if is_heading:
            flush()
            blocks.append(Block(line.strip(), count_tokens(line), True, "paragraph"))
            index += 1
            continue

        if buffer_kind != "paragraph":
            flush()
        buffer.append(line)
        index += 1

    flush()
    return blocks


def _force_split(
    block: Block, hard_max: int, count_tokens: Callable[[str], int]
) -> list[Block]:
    """Break a block too large for the embedding model, at line boundaries.

    Not hypothetical: fly.io/docs/about/pricing is one table of roughly 13,000
    tokens, well past text-embedding-3-small's 8191-token input limit. Nothing
    inside it is a heading or a paragraph break, so there is no good boundary
    -- only a necessary one, which is why every force-split is counted rather
    than absorbed.
    """
    if block.tokens <= hard_max:
        return [block]

    # A block with no internal line breaks cannot be cut at one. Three
    # documents in the corpus are a single line (trafilatura returns
    # fly.io/about's 316 words that way), and while none is currently large
    # enough to need this, a single line over the hard cap would otherwise be
    # sent to the API whole and rejected. Words are a poor boundary; being
    # rejected is worse.
    units = block.text.splitlines()
    if len(units) == 1:
        words = block.text.split()
        stride = max(1, len(words) * hard_max // max(1, block.tokens))
        units = [" ".join(words[i : i + stride]) for i in range(0, len(words), stride)]

    pieces: list[Block] = []
    current: list[str] = []
    current_tokens = 0
    for line in units:
        line_tokens = count_tokens(line)
        if current and current_tokens + line_tokens > hard_max:
            joined = "\n".join(current)
            pieces.append(Block(joined, current_tokens, False, block.kind))
            current, current_tokens = [], 0
        current.append(line)
        current_tokens += line_tokens
    if current:
        joined = "\n".join(current)
        pieces.append(Block(joined, current_tokens, False, block.kind))
    return pieces


def chunk_document(
    *,
    text: str,
    kind: str,
    title: str = "",
    published: str | None = None,
    count_tokens: Callable[[str], int] | None = None,
    report: ChunkingReport | None = None,
) -> list[ChunkDraft]:
    """Split one document into chunks, per ADR-0005.

    A job posting is never split, whatever its length. **[verified]** the
    largest in the corpus is fly.io/jobs/networking-engineer at 1,167 words,
    roughly 1,550 tokens -- above ADR-0005's guessed "typically under 1500" but
    about five times under the embedding limit, so the rule holds with room.
    That number was previously an assumption (open-questions section 2.3).
    """
    count = count_tokens or default_token_counter()
    report = report if report is not None else ChunkingReport()
    header = provenance_header(title, kind, published)

    body = text.strip()
    if not body:
        return []

    if kind == "job_posting":
        content = f"{header}\n\n{body}" if header else body
        report.chunks = 1
        report.blocks = 1
        return [ChunkDraft(0, content, max(1, count(content)), kind)]

    # The cap has to leave room for what the packer adds afterwards: the
    # provenance header on every chunk, and the overlap tail carried in from
    # the previous one. Splitting to the raw cap produced 6,153-token chunks
    # against a 6,000 cap -- a cap that is not a cap, and the API rejects at
    # 8,191 with no partial credit.
    room = settings.chunk_hard_max_tokens - settings.chunk_overlap_tokens
    room -= count(header) if header else 0

    blocks: list[Block] = []
    for block in segment(body, count):
        pieces = _force_split(block, max(1, room), count)
        if len(pieces) > 1:
            report.force_split_blocks += 1
        blocks.extend(pieces)

    report.blocks = len(blocks)
    report.headings = sum(1 for b in blocks if b.is_heading)

    drafts = _pack(blocks, header, kind, count, report)
    report.chunks = len(drafts)
    report.over_max = sum(
        1 for d in drafts if d.token_count > settings.chunk_max_tokens
    )
    return drafts


def _pack(
    blocks: list[Block],
    header: str,
    kind: str,
    count: Callable[[str], int],
    report: ChunkingReport,
) -> list[ChunkDraft]:
    """Greedy packing into the ADR-0005 band, preferring heading boundaries.

    Close the current chunk when it has reached the minimum AND the next block
    opens a section, or when adding the next block would exceed the maximum.
    Short sections are NOT padded by merging with an unrelated one: ADR-0005
    forbids it, so a document shorter than the band is simply one short chunk.
    """
    drafts: list[ChunkDraft] = []
    current: list[Block] = []
    current_tokens = 0

    def emit() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        body = "\n\n".join(b.text for b in current)
        content = f"{header}\n\n{body}" if header else body
        drafts.append(ChunkDraft(len(drafts), content, max(1, count(content)), kind))
        # Overlap: carry back the TAIL TEXT of what was just emitted, so a
        # sentence split across a boundary is retrievable from either side.
        #
        # Word-level, not block-level. Carrying whole trailing blocks looks
        # tidier and delivers nothing: a 400-token paragraph never fits inside
        # a 150-token budget, so every chunk boundary in ordinary prose got
        # zero overlap. Caught by a test asserting consecutive chunks share
        # text -- they shared three words, all of them from the header.
        tail_words = body.split()[-settings.chunk_overlap_tokens :]
        tail = " ".join(tail_words)
        current = [Block(tail, count(tail), False, "paragraph")] if tail else []
        current_tokens = current[0].tokens if current else 0

    for block in blocks:
        would_exceed = (
            current and current_tokens + block.tokens > settings.chunk_max_tokens
        )
        opens_section = block.is_heading and current_tokens >= settings.chunk_min_tokens
        if would_exceed or opens_section:
            emit()
            # An overlap tail that is itself a heading run adds nothing; drop
            # it rather than repeating a bare heading at the top of the chunk.
            if current and all(b.is_heading for b in current):
                current, current_tokens = [], 0

        # A block already at or over the band gets no overlap tail in front of
        # it. Carrying 150 tokens into a 5,850-token force-split table piece
        # pushed it past the hard cap -- the cap has to hold, because above
        # 8,191 the API rejects the request and the whole load fails.
        if block.tokens >= settings.chunk_max_tokens and current:
            emit()
            current, current_tokens = [], 0

        current.append(block)
        current_tokens += block.tokens

    # The final emit must not leave an overlap tail behind as a chunk of its
    # own: the tail is a copy of text already emitted.
    if current:
        body = "\n\n".join(b.text for b in current)
        content = f"{header}\n\n{body}" if header else body
        drafts.append(ChunkDraft(len(drafts), content, max(1, count(content)), kind))
    return drafts
