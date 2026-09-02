# ADR-0001 — No HNSW index initially

Status: Accepted · Date: 2026-09-02

## Decision

`chunks.embedding` gets no approximate-nearest-neighbour index. Vector search
runs exact (a sequential scan over the rows that survive the `prospect_id`
filter). The index that matters is the B-tree on `chunks (prospect_id)`.

## Alternatives

- **HNSW from the start.** Costs build time on every ingest, roughly a
  vector-sized amount of extra memory per index, and — the part that matters —
  gives up recall guarantees. With a filtered query, pgvector's HNSW scan can
  return fewer than `k` rows because it walks the index globally and only then
  applies the filter; `hnsw.iterative_scan` exists to work around exactly that,
  and adds its own tuning parameters.
- **IVFFlat.** Cheaper to build, worse recall, and needs re-training as data
  grows. Same filtering problem.
- **Partitioning `chunks` by prospect.** Would make the filter free, but
  Postgres partition counts in the thousands have their own planning costs, and
  we do not yet know how many prospects there will be.

## Why

Every vector query in this system is `WHERE prospect_id = :id ORDER BY embedding
<=> :q LIMIT :k`. The filter runs first and is highly selective.

**[assumed]** the candidate set after the prospect filter is in the hundreds of
chunks. The estimate: a 40-page crawl at roughly 20,000–62,000 words per
prospect (**[verified]** 21,174 words for thoughtbot and 62,169 for fly.io at
`max_pages=40`) at 800–1200 tokens per chunk gives on the order of 30–100 chunks
per prospect. Exact search over that is trivial, and it has perfect recall,
which an approximate index does not.

**[assumed]** exact search at that size costs single-digit milliseconds. Not
measured — no embeddings exist yet.

This is A9 applied literally: an approximate index is infrastructure, and there
is no measurement yet that justifies it.

## What would reverse it

Measure, once retrieval exists, on a realistic corpus:

1. `SELECT count(*) FROM chunks WHERE prospect_id = :id` — the p95 across
   prospects. The relevant threshold is roughly 10,000 chunks for a single
   prospect; below that, a sequential scan of `halfvec(1536)` rows is not the
   bottleneck.
2. `EXPLAIN (ANALYZE, BUFFERS)` on the real query, p95 latency of the vector
   search **isolated from embedding and generation latency** (A8).

Add HNSW when p95 vector-search latency exceeds 100 ms with the prospect filter
applied, and only then. If it is added:

- Set `hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8.0) so the filtered
  query cannot silently return fewer than `k` rows.
- Record recall@k against the exact search on the same corpus, before and after.
  A change with no measured effect is reverted (A3).

Also reversed if the query shape changes — a cross-prospect search would remove
the filter that this decision depends on. Note that such a query would violate
A1, so this is a warning sign, not a feature request.
