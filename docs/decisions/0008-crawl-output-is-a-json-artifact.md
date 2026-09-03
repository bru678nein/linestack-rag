# ADR-0008 — Crawl output is a JSON artifact; the database load is a separate step

Status: Accepted · Date: 2026-09-02

## Decision

`ingest.py` writes `prospect_<domain>.json` and performs no database writes.
Loading that artifact into Postgres is a separate step
(`linestack/ingestion/loader.py`, `make load`), which is idempotent.

**Correction, 2026-09-02.** This decision originally said "idempotent on
`documents.content_hash`". That is wrong, and this ADR predates ADR-0013 which
explains why: some sites reshuffle repeated records on every request, so an
unchanged page yields a new `content_hash` on every crawl. fly.io/about is one
— four consecutive fetches, four hashes, one identical word multiset. Keying
skip-work on `content_hash` would re-chunk and re-embed that page forever, at
cost, for content that has not changed.

Idempotency keys on **`stable_hash`**. `content_hash` is still stored, exactly,
because it is the only thing keeping a reordering visible rather than hidden.
Crawl-run identity is separate again: a natural key on
`crawl_runs (prospect_id, started_at)` (migration 0003) makes a re-load
conflict rather than insert a second run.

## Alternatives

- **Crawl and write in one pass.** One command, no intermediate file, no risk of
  a stale artifact being loaded.
- **Crawl to the database as a staging table**, then transform. Keeps everything
  in one system at the cost of needing the database up before any crawling can
  happen.

## Why

A crawl is slow — 40 pages at 1.5 s of enforced delay is at least a minute of
wall clock, against someone else's server. Re-running one to fix a bug in the
loading code, the chunking code, or the signal computation is wasteful and
impolite (A6).

More importantly, an artifact can be **diffed**. Every crawler bug found so far
was found by comparing two runs: the FIFO mix against the quota mix, 162 people
against 54, a crawl that returned nothing against one that returned 37 pages.
That comparison is trivial on two JSON files and awkward on two database states.

It also keeps A7 checkable at the right layer. Idempotency is a property of the
content hashes, and the hashes are visible in the artifact without a query.
**[verified]** two consecutive crawls of fly.io produced identical content
hashes for all 40 documents.

## What would reverse it

- Artifacts get large enough that keeping them is impractical. **[verified]**
  the largest observed crawl is fly.io at 62,169 words, which is on the order of
  400 KB of JSON. Two orders of magnitude of headroom.
- The intermediate file becomes a source of confusion — someone loads a
  three-week-old artifact and the database silently reflects a stale site. The
  mitigation is that `crawled_at` is already recorded in the artifact and the
  loader should refuse an artifact older than a configured threshold. If that
  mitigation proves insufficient in practice, collapse the two steps.
- Continuous re-crawling at a cadence where the file is never inspected. At that
  point the artifact is overhead; keep it only for the manual path.
