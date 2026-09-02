# Decision records

One file per decision. Numbered, immutable once merged: a decision that changes
gets a new record that supersedes the old one, and the old one stays with its
status updated. Reading a superseded record and understanding why it was wrong
is more useful than not being able to find it.

Every record states four things:

1. **Decision** — what was decided, in one sentence.
2. **Alternatives** — what else was considered, and what each would have cost.
3. **Why** — the reasoning, with each claim marked verified or assumed (A4).
4. **What would reverse it** — the specific observation or number that turns
   this decision back into an open question. A record without this section is
   an opinion, not a decision.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-no-hnsw-index-initially.md) | No HNSW index initially | Accepted |
| [0002](0002-no-langchain-in-core.md) | No LangChain in the core pipeline | Accepted |
| [0003](0003-computed-signals-not-inferred.md) | Compute structured signals, never infer them | Accepted |
| [0004](0004-denormalise-prospect-id-and-kind.md) | Denormalise `prospect_id` and `kind` onto `chunks` | Accepted |
| [0005](0005-chunk-sizing.md) | Chunk sizing: 800–1200 tokens, heading splits, postings unsplit | Accepted |
| [0006](0006-robots-txt-fetched-through-own-client.md) | Fetch robots.txt through the project's own HTTP client | Accepted |
| [0007](0007-crawl-budget-quota-as-cap.md) | Crawl budget: quota is a cap, ordering is by question value | Accepted |
| [0008](0008-crawl-output-is-a-json-artifact.md) | Crawl output is a JSON artifact; the database load is separate | Accepted |
| [0009](0009-retrieval-strategy.md) | Retrieval: naive vector search first, hybrid only on evidence | Accepted |
| [0010](0010-keep-ingest-py-despite-build-order-violation.md) | Keep `ingest.py` despite the build-order violation | Accepted |
| [0011](0011-extraction-escalates-with-reason-codes.md) | Extraction escalates through three passes and records which one won | Accepted |
| [0012](0012-page-outcome-vocabulary-shared-with-the-schema.md) | Every URL gets one classified outcome, in the schema's own vocabulary | Accepted |
| [0013](0013-order-insensitive-hash-for-reshuffled-content.md) | Order-insensitive hash for reshuffled content; keep duplicate URLs | Accepted |
| [0014](0014-count-people-structurally-first.md) | Count people by repeated structure first, class names second | Accepted |
