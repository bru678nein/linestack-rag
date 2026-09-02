# ADR-0013 — Compare content with an order-insensitive hash; keep duplicate URLs

Status: Accepted · Date: 2026-09-02

## Decision

`Document` carries two digests and a list of aliases:

- `content_hash` — sha256 of the extracted text, exact. Unchanged meaning.
- `stable_hash` — sha256 of the same text with word order removed
  (`stable_digest`: sort the words, join, hash). Two texts that are
  permutations of each other share it.
- `duplicate_urls` — every other URL observed serving the same content.

Deduplication keys on `stable_hash`. The dropped page's URL is appended to the
survivor's `duplicate_urls`, and `compute_signals` reads the URL path of the
survivor **and** its aliases. `stable_hash` is the column to compare when
deciding whether a re-crawled document changed (A7); `content_hash` says
whether it was merely reordered. Migration `0002` adds both columns.

## Why

**[verified] 2026-09-02.** `fly.io/about` returns its team roster in a
different order on every request. Four consecutive fetches: 316 words each,
**four different `content_hash` values, one identical word multiset**. `/team`
redirects to `/about` and shares that multiset.

Two failures followed from hashing exactly:

1. **A7 was violated.** Every crawl reported a change on a page that had not
   changed, which would re-chunk and re-embed it forever.
2. **Deduplication failed.** `/about` and `/team` are one page; both were kept,
   costing a budget slot and putting two near-identical chunk sets in the
   corpus for retrieval to choose between.

**Word level is not a preference — it is what survives.** A shuffle destroys
adjacency at every record boundary, so bigrams and shingles do not survive it.
There was no coarser structure available either: trafilatura returns all 316
words of this page on a single line, and the DOM has one top-level block, so
there are no lines or blocks to sort instead.

**The collision mode is real and is accepted knowingly.** "5 engineers and 2
designers" and "2 engineers and 5 designers" share a word multiset.
**[verified]** across the 78 documents of the two validation crawls,
`stable_hash` produced exactly one collision group, and it was the genuine
duplicate — zero false positives. That is evidence, not proof, which is why
`content_hash` stays exact: the reordering remains visible rather than hidden,
and a future disagreement can be investigated rather than guessed at.

**Deduplication must not destroy URL evidence.** Keying dedup on `stable_hash`
initially flipped fly.io's `has_team_page` from `True` to **`False`**:
`has_team_page` matches the URL *path*, and `/team` — the URL carrying the
evidence — was the one dropped. The text survived; the proof did not. Hence
`duplicate_urls`, and hence signals reading aliases. Which of two URLs wins
deduplication is an accident of crawl order and must not decide a signal.
Caught by running the crawl, not by reading the diff.

## Alternatives

- **Normalise the page before hashing** (sort the roster in the HTML). Requires
  knowing which elements are the repeated records — site-specific, and wrong
  the moment a site changes markup.
- **Sort lines or blocks instead of words.** Strictly better where structure
  exists. This page has none: one line, one block. Kept in mind for a future
  extractor that preserves block boundaries, which would make this safer.
- **Hash only a prefix of the text.** Cheaper, and wrong here — the shuffle
  reaches the first record.
- **Fetch twice and compare.** Doubles every request for a property that only
  a minority of pages have. Contrary to A6.
- **Accept the duplicate.** Leaves A7 broken and pays a budget slot and a
  duplicated chunk set per shuffled page, forever.

## Consequences

- **[verified]** fly.io drops from 40 documents to 39, `duplicate_content 1`,
  and `has_team_page` stays `True`. `/about` and `/team` are one document with
  one alias. The freed budget slot goes to another page.
- **[verified]** `stable_hash` for `fly.io/about` was identical across four
  fetches and across separate crawl runs on the same day: `6cc2140dbf3a5a13`.
- thoughtbot's `technical_roles_named` moved 23 → 27, because a deduplicated
  team-ish alias URL now correctly counts toward the team signal.
- The `duplicate_content` outcome detail now distinguishes `reordered` from
  `identical` — the only place a shuffled source is observable without
  fetching the same URL twice.
- **Not addressed:** `Document.kind` still comes from the surviving URL alone.
  A page served at both `/handbook/x` and `/careers/x` would keep whichever
  kind won deduplication. No instance of this was observed; recorded in
  `docs/open-questions.md` rather than fixed speculatively.
- Migration `0002` is **written but not applied** — no database was available
  in this session. It must be applied before the columns are trusted.
