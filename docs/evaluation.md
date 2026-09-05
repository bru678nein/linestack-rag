# Evaluation

The harness does not exist. This document is its design, written first so that
the first measurement is not also the first time anyone thinks about what is
being measured.

Destination: `linestack/evaluation/` (`dataset.py`, `harness.py`, `metrics.py`).

---

## 1. What is being measured, and why in that order

A8: retrieval is the bottleneck, not the model. When an answer is wrong, the
first hypothesis is that the right chunk was never retrieved. **Retrieval
metrics are therefore reported separately from generation metrics, always** —
never averaged into one score, never shown as a single number.

| Priority | Metric | Measures | Noise |
| --- | --- | --- | --- |
| 1 | Retrieval recall@k | Did the right chunk get retrieved at all? | Low |
| 1 | Faithfulness | Is the answer supported by the retrieved chunks? | Low–medium |
| 2 | Answer correctness / relevancy | Does the answer match the reference? | **High** |
| Gate | Cross-prospect leakage | Did any chunk from another prospect appear? | None — binary |
| Diagnostic | Signal accuracy | Do the computed signals match ground truth? | None — exact |
| Diagnostic | Ingestion coverage | Was the evidence even crawled? | None — exact |

### Why correctness is secondary

"What does this company do, and who does it sell to?" has no single right
answer. Two accurate answers can share almost no vocabulary — one names the
product category, the other names the customer segment. Any similarity-based
correctness score will disagree with a human on those, in both directions, and
will do so inconsistently across runs.

Correctness is therefore recorded and tracked, but it does not gate anything and
a change is never accepted or rejected on correctness alone. Faithfulness and
recall are the metrics that decide.

### Why leakage is a gate, not a metric

A1 is a hard boundary. A run in which any retrieved chunk belongs to a prospect
other than the one asked about is a **failed run**, not a run with a lower
score. The harness asserts `chunk.prospect_id == prospect_id` for every
retrieved chunk and fails the whole evaluation if that assertion ever breaks.
There is no acceptable non-zero leakage rate.

---

## 2. Metric definitions

### 2.1 Retrieval recall@k — primary, IMPLEMENTED

For each ground-truth pair, the author records the URLs of the source pages that
contain the evidence. Recall@k is: of the retrieved chunks at cut-off `k`, did at
least one come from a document whose `source_url` is in that list?

Reported at k = 1, 3, 5, 10, **broken down per question**, because the four
questions fail differently. **[assumed]** question 4 ("what pain do they state
explicitly") will have the lowest recall, because the wording of the question and
the wording on the page share little vocabulary. That expectation is what the
harness is for.

Recall is computed at document granularity, not chunk granularity, so that the
metric does not change meaning when chunk sizes change (ADR-0005). A chunking
change that moves the boundary should show up as a change in the score, not as a
change in what the score measures.

### 2.2 Faithfulness — primary, NOT IMPLEMENTED

> **Status, 2026-09-05.** Specified here and not built. `ragas==0.4.3` does not
> install alongside `openai==3.7.0` (see `docs/open-questions.md` §3.1) — and
> more to the point, this metric is LLM-judged and there is no OpenAI key by
> design (ADR-0017), so it could not run even if the pin resolved. ADR-0020.

Is every claim in the answer supported by the retrieved context?

Measured with `ragas` faithfulness. This is an LLM-judged metric, so:

- The judge model and its version are pinned and recorded with every run. A
  judge change invalidates comparison with prior runs.
- Faithfulness is reported over **retrieved context only**. An answer that is
  true about the company but unsupported by what was retrieved scores low, and
  it should: an unsupported claim that happens to be right is the same mechanism
  as an unsupported claim that is wrong.
- The computed signals injected into the context count as retrieved context.
  They are cited facts, not model output.

### 2.3 Answer correctness — secondary, noisy, NOT IMPLEMENTED

> **Status, 2026-09-05.** Same reason as §2.2. ADR-0020.

`ragas` answer correctness and answer relevancy against the hand-written
reference answer. Recorded. Never used alone to accept or reject a change. When
correctness moves and neither recall nor faithfulness moves, the null hypothesis
is judge noise, and the check is to re-run the same configuration three times
and look at the spread before believing the delta.

### 2.4 Signal accuracy — diagnostic, exact, IMPLEMENTED

The computed signals (ADR-0003) are checked against hand-recorded ground truth
per prospect: `people_listed`, `open_roles_seen`, `technical_roles_open`,
`has_team_page`, `latest_post_date`.

This is an exact comparison with no judge and no noise, and it is the cheapest
signal in the whole harness. It has already caught real defects at the two
prospects that were hand-checked: 162 people against 54, and 4 open roles
against 0 (see ADR-0003).

### 2.5 Ingestion coverage — diagnostic, exact, IMPLEMENTED

Before attributing any failure to retrieval, check whether the evidence was
ingested at all. Per prospect, per question, the harness records:

- Are the ground-truth source URLs present in `documents`?
- If not, is there a `crawl_page_outcomes` row with a reason code explaining
  why (A5)?
- Page-kind mix against the quota caps (ADR-0007).

**[verified] 2026-09-02.** The crawler now produces these rows. Every URL it
touches gets one outcome in the `page_outcome` vocabulary — `stored`,
`skipped_robots`, `dns_failure`, `timeout`, `transport_error`, `http_error`,
`non_html`, `thin_extraction`, `duplicate_content`, `budget_exhausted` — on
`Prospect.page_outcomes` (ADR-0012), and **the load into `crawl_page_outcomes`
is now written** (`linestack/ingestion/loader.py`, `make load`).

**[verified] 2026-09-02** against a live database: fly.io loads 97 outcome rows
(39 stored, 36 budget_exhausted, 19 http_error, 2 non_html, 1
duplicate_content) and thoughtbot 68 (37 stored, 18 http_error, 9
budget_exhausted, 3 duplicate_content, 1 skipped_robots). Re-loading writes
nothing further, enforced by a natural key on
`crawl_runs (prospect_id, started_at)` (migration 0003) rather than by a check
in application code.

So this section's question — "the evaluation set expects this URL and it is not
in `documents`, why not?" — is answerable today for any URL a crawl touched.

**A missing source URL with no reason code is a harness-blocking bug, not a
retrieval miss.** Reporting recall over a corpus that silently lost pages is
reporting a number about the crawler while calling it a number about retrieval —
which is exactly the confusion ADR-0007 was written about.

---

## 3. What the harness runs against

- The ground-truth set: 12 prospects × 4 questions ≈ 48 pairs. Format and
  authoring procedure in `docs/ground-truth.md`.
- A **frozen corpus**. The evaluation crawls once and the resulting artifacts are
  committed. Re-crawling between runs means the corpus and the retrieval
  configuration both changed, and the delta is unattributable.
- **[assumed]** a frozen corpus stays valid for roughly one quarter before
  company sites drift enough to invalidate the reference answers. Not measured;
  re-check by re-crawling and diffing content hashes (A7 makes this a cheap
  check).

---

## 4. How a run is recorded

Every run writes one row per (prospect, question) and one summary, tagged with:

- Corpus version (hash of the committed artifacts).
- Retrieval configuration: `k`, chunk size and overlap, whether hybrid is on,
  fusion weights, reranker.
- Embedding model and dimension.
- Generation model, prompt version, temperature.
- Judge model, for the `ragas` metrics.
- Timings, split into embed / retrieve / generate (A8 — one aggregate latency
  number hides which stage is slow).

The summary is a table of the metrics in §1 with the delta against the previous
run of the same corpus. **A3: no retrieval improvement ships without a recorded
before-and-after, and a change with no measured effect is reverted.** The harness
exists to make that rule enforceable rather than aspirational.

Runs are traced to Langfuse so that an individual bad answer can be opened and
inspected — question, retrieved chunk ids, scores, assembled prompt, output.
Aggregate metrics say a run got worse; the trace says which answer and why.

---

## 5. Interpreting the two-by-two

The point of separating recall from faithfulness is that the four combinations
have four different causes and four different fixes.

| Recall | Faithfulness | Diagnosis | Fix |
| --- | --- | --- | --- |
| Low | Low | The right chunk was never retrieved. | Chunking, hybrid search, ingestion coverage. Not prompts. |
| Low | High | Answering honestly from the wrong context, or saying "I don't know". | Same as above. This is the *good* failure — it is visible. |
| High | Low | Retrieved but ignored, or the model is embellishing. | Prompt, context assembly, reranking (ADR-0009). |
| High | High | Working. | Look at correctness and at the remaining misses. |

The row that must never be reachable is the fifth one that is not in the table:
high faithfulness against chunks belonging to a different prospect. That is the
failure the whole project is built around, and it is why leakage is a gate in §1
rather than a metric here.

---

## 6. Running it

Not implemented. The intended interface:

```sh
make eval                     # full set, current configuration
make eval PROSPECT=fly.io     # one prospect, for iteration
make eval-report              # the delta table against the previous run
```

In CI the harness is **not** run on every push: it costs API calls, and the
judge introduces run-to-run variance that would produce flaky builds. The
GitHub Actions workflow runs unit tests on every push and has the evaluation job
present but disabled, to be enabled manually or on a schedule once the ground
truth exists. The dataset's *structural* validation — required fields, valid
prospect references, well-formed source URLs — does run on every push, because
it costs nothing and a malformed dataset should not reach the harness.

---

## 7. Open

- `ragas` is pinned to a specific version because its API changes across
  releases. The pin has not yet been installed or exercised; see
  `docs/open-questions.md`.
- Whether `ragas` faithfulness behaves sensibly with computed signals in the
  context is untested. **[assumed]** it treats them as ordinary context. If it
  does not, faithfulness may need to be scored over retrieved chunks only, with
  signal-derived claims excluded — which requires knowing which claims came from
  where.
- The number of prospects (12) is chosen for hand-authoring effort, not for
  statistical power. **[assumed]** 48 pairs is enough to detect a recall change
  of roughly 10 points and not enough to detect one of 2 points. Not computed.
