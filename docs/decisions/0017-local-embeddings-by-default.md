# ADR-0017 — Local embeddings by default; OpenAI as the alternative

Status: Accepted · Date: 2026-09-03

## Decision

`EMBEDDING_MODEL` selects between two implementations behind one interface:

- **`BAAI/bge-small-en-v1.5`** via `sentence-transformers`, running on the
  developer's machine. **384 dimensions.** The default.
- **`text-embedding-3-small`** via the OpenAI API. 1536 dimensions.

Migration `0004` narrows `chunks.embedding` to `halfvec(384)` to match.

A model name that is not a known OpenAI model is treated as a local
sentence-transformers model. `EmbeddingBatch` is unchanged, so neither
`ProspectScope` nor the loader nor `search.py` knows which produced a vector —
only `chunks.embedding_model` records it, and `ProspectScope.open` still refuses
a prospect whose stored vectors came from a different model.

## Why

The immediate reason is access, not cost. Cost is $0.0026 for this corpus and
was never the argument. The argument is that using OpenAI requires an account
and a key, and without one **nothing can be embedded at all** — no retrieval, no
ground truth written against real rankings, no harness. A pipeline that cannot
run is worse than one running on a model we have not yet A/B'd.

That reverses the recommendation given earlier in this project's history, which
was to wait for the evaluation harness before changing the embedder so the
change could be measured. That advice was right when the alternative was paying
a fraction of a cent. It is wrong when the alternative is zero vectors, because
zero vectors cannot be compared with anything.

**Measured on an M5 MacBook Air, 16 GB, 2026-09-03:**

| | |
| --- | --- |
| install | 7.5 s; the venv grows **784 MB** (torch is 529 MB of it) |
| model download | ~58 s, once |
| model load | 7.2 s per process |
| embed the 154-chunk corpus | **1.84 s** |
| CPU used | 0.33 s — **0.2 of 10 cores**; the work goes to the GPU via MPS |
| peak RSS | 459 MB |
| one question | **8 ms** |

It is a burst, not a service: nothing stays resident afterwards. Contrast
`ollama`, which leaves a daemon running.

## Alternatives

- **Stay on OpenAI.** Requires an account this project does not have. Blocks
  everything downstream.
- **A free API tier** (Cohere, Jina, HuggingFace). Still an account, plus rate
  limits, and it returns you to the same problem when they run out.
- **`ollama`.** Works, and leaves a service running. `sentence-transformers` is
  a library dependency instead of an operational one, which fits a project that
  already declares its pins exactly.
- **A larger local model** (`bge-base` 768, `bge-m3` 1024). Better retrieval,
  probably. Rejected for now under A3: there is no harness, so "probably" is all
  anyone can say. Changing model is a migration plus a re-embed, and the point
  of recording `embedding_model` per chunk is that this comparison becomes
  measurable later rather than argued now.

## Consequences

- **The embedding dimension is a schema commitment, not a setting.**
  `halfvec(N)` fixes N in the column. Switching back to OpenAI is migration
  `0005` and a full re-embed, not an environment variable. A unit test asserts
  the migration's dimension equals `settings.embedding_dimensions`, so the two
  cannot drift into a silent cast error.
- Migration `0004` is safe to apply now precisely because **no vectors exist
  yet**. It is the cheapest moment this change will ever have.
- `bge` wants a query-side instruction prefix and no document-side prefix. That
  asymmetry is in `embedding.py`; getting it wrong degrades ranking quietly
  rather than loudly.
- **Retrieval quality against `text-embedding-3-small` is unmeasured**, and
  stays unmeasured until the harness exists. This ADR claims the pipeline runs,
  not that it ranks as well. The one measurement taken so far
  (`docs/open-questions.md` §2.5) says ranking is the weak point regardless of
  model.
- torch on disk is ~784 MB. Anyone who wants it gone: `uv pip uninstall
  sentence-transformers torch` and switch `EMBEDDING_MODEL` back.
