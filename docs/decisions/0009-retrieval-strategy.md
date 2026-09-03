# ADR-0009 — Retrieval: naive vector search first, hybrid only on evidence

Status: Accepted · Date: 2026-09-02

## Decision

The first working retrieval implementation is:

```sql
SELECT id, content, kind, 1 - (embedding <=> :q) AS score
  FROM chunks
 WHERE prospect_id = :prospect_id
 ORDER BY embedding <=> :q
 LIMIT :k
```

Cosine distance, exact search, single-stage, no reranking, no query rewriting,
no source weighting. `prospects.signals` is injected into the answer context
unconditionally, alongside whatever this returns.

Hybrid search (vector + `content_tsv`), reciprocal-rank fusion, source weighting
by `kind`, and a cross-encoder reranker are all **planned but not adopted**.
Each ships only with a recorded before-and-after on the evaluation set (A3).

## Alternatives

- **Hybrid from the start.** Lexical search catches exact terms — a framework
  name, a product name, a job title — that embeddings blur. It is very likely
  to help. It also has fusion weights to tune, and tuning without a baseline
  produces numbers nobody can interpret.
- **Reranking from the start.** A cross-encoder over the top 50 would likely
  improve precision. It adds a model call to every query, on the latency path,
  before there is a latency budget to spend.
- **Source weighting from the start.** `kind` is already denormalised onto
  `chunks` specifically to make this cheap (ADR-0004). Still a tuning parameter
  with no baseline.

## Why

A3 is explicit about the build order: naive vector search working end-to-end →
ground truth written → evaluation harness → *then* hybrid retrieval, reranking,
chunking changes. Each improvement records its delta, and a change with no
measured effect is reverted.

The reason to hold the line here specifically is that all three candidate
improvements are individually plausible and jointly unattributable. Ship hybrid
and reranking together and a 12-point recall gain cannot be assigned to either,
so neither can be tuned and neither can be safely removed.

There is also a diagnostic argument. A8 says that when an answer is wrong, the
first hypothesis is that the right chunk was never retrieved. A single-stage
retriever makes that hypothesis cheap to test: the retrieved chunks and their
scores are shown in the UI, and the score ordering is the whole of the retrieval
logic. Add fusion and a reranker first, and the same investigation requires
reconstructing three stages of scoring.

**[assumed]** naive vector search will be visibly insufficient — most likely on
question 4 ("what pain do they state explicitly"), where the wording in the
question and the wording on the page have little lexical overlap, and on exact
product or framework names. That expectation is what the harness is for.

**Partly measured, 2026-09-03.** The insufficiency is real and showed up before
the harness existed; the guess about *which* question was wrong. On fly.io's
111 chunks, question **2** — "What evidence is there of in-house technical
capacity?" — ranks the team roster, 57 named people with job titles, at
**110 of 111**. Reworded in the page's own vocabulary it ranks **4 of 111**.
Details and caveats in `docs/open-questions.md` §2.5. This is one model, one
phrasing, no ground truth: enough to know a problem exists, not enough to know
which fix is right.

## What would reverse it

In this order, one at a time, each with a recorded delta:

1. **Hybrid.** Add when retrieval recall@5 on the ground-truth set is below 0.8
   for any of the four questions **and** manual inspection of the misses shows
   an exact term present in the corpus that vector search ranked outside the top
   5. Requires the GIN index on `content_tsv`, and requires resolving the text
   search configuration question in `docs/open-questions.md` first — `simple`
   versus `english` changes what lexical search can match.
2. **Source weighting by `kind`.** Add when the misses concentrate in one page
   kind. Cheapest of the three; the column is already there.
3. **Reranking.** Add last, and only when recall is high but faithfulness is
   low — that combination means the right chunk is being retrieved and ranked
   too low to influence the answer. Reranking cannot fix a chunk that was never
   retrieved, so it must not be reached for before recall is measured.

Reversed in the other direction if recall is already high across all four
questions on a corpus of 12 prospects, in which case none of the above ships and
the effort goes to generation and to the signals instead.
