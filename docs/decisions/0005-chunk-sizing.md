# ADR-0005 — Chunk sizing: 800–1200 tokens, split on headings, postings unsplit

Status: Accepted · Date: 2026-09-02

## Decision

- Target chunk size 800–1200 tokens, with roughly 150 tokens of overlap.
- Split on semantic structure — heading boundaries — not on fixed character
  counts. A section shorter than the target is not padded by merging it with an
  unrelated section; a section longer than the target is split at the next
  subheading, and only then at a paragraph boundary.
- **A job posting is never split.** One posting is one chunk, whatever its
  length.
- Publication date is carried in chunk metadata.

None of this is implemented. `linestack/ingestion/chunking.py` is its
destination.

## Alternatives

- **Small chunks, 200–400 tokens.** The common default. Higher precision for
  exact-lookup questions ("what is their pricing"), more chunks per prospect,
  and answers assembled from more fragments.
- **Whole documents as chunks.** No splitting logic at all. A 62,000-word crawl
  would produce a handful of enormous chunks, most of each one irrelevant to any
  given question, and embedding quality degrades as a vector is asked to
  represent more distinct topics.
- **Fixed character windows.** Simplest to implement and it cuts sentences,
  tables, and job requirements in half.

## Why

The four questions are synthesis questions, not lookup questions. "What does
this company do, and who does it sell to" is answered by a section, not by a
sentence. A 300-token chunk containing half of an About page's positioning
statement retrieves well and answers badly.

**[assumed]** larger chunks improve faithfulness on synthesis questions at this
corpus size, because the model sees complete arguments rather than fragments,
and because the prospect filter already keeps the candidate set small enough
that precision loss is affordable. Not measured. This is the single most
important unmeasured assumption in the retrieval design.

**Job postings are not split** for a concrete reason rather than a stylistic
one: a posting's value for question 2 is the *combination* of its title,
responsibilities, and required stack. Split across three chunks, retrieval can
return the responsibilities of one role and the stack of another, and the model
will merge them into a role that does not exist. That is the confidently-wrong
failure mode in miniature. Postings are also short — **[assumed]** typically
under 1500 tokens, not measured against the crawled corpus.

**Overlap of 150 tokens** is a hedge against a boundary falling in the middle of
the one paragraph that answers a question. **[assumed]**, taken from common
practice at roughly 10–15% of chunk size. Not measured.

## What would reverse it

This is a chunking change, so under A3 it does not ship before the evaluation
harness exists and it must record a before-and-after.

Measure, on the ground-truth set:

1. **Retrieval recall@k** for each of the four questions, per chunk
   configuration. The configurations worth testing: 400/50, 800/150 (this
   decision), 1200/150, and document-level.
2. **Faithfulness** on the same configurations, reported separately from recall
   (A8).
3. **Chunks per prospect**, because it feeds ADR-0001's candidate-set estimate.

Reverse to smaller chunks if recall@5 at 400 tokens beats 800–1200 by more than
5 points without a faithfulness loss. Reverse the no-split rule for postings if
any posting in the corpus exceeds the embedding model's context window — at
which point the split must be at a requirement boundary, with the title repeated
into every resulting chunk.

Before any of this is measured, the corpus must be inspected for the known
extraction defect described in `docs/open-questions.md`: chunk statistics
computed over documents that were silently dropped at 30 words are not
statistics about the sites.
