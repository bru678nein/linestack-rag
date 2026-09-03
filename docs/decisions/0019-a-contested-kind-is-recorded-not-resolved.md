# ADR-0019 — A contested `kind` is recorded on the document, not resolved

Status: Accepted · Date: 2026-09-03

Extends [ADR-0015](0015-classify-by-path-segment-not-substring.md).

## Decision

When deduplication collapses several URLs into one document and those URLs
disagree about `kind`, the losing kinds are recorded on
`Document.kind_conflicts`. The winner is still `canonical_document()`'s
alphabetically-first URL — **no rule is introduced for which kind should
win** — and the crawl prints the conflict in its run summary.

`deduplicate()` is extracted from `ingest()` so the branch can be tested.

## Why

[open-questions.md §1.1c](../open-questions.md) fixed the determinism half of
this in ADR-0015 and left one thing open: which URL should win a genuine
`kind` disagreement. It could not be decided, because no instance had ever
been observed.

The gap was not the missing rule. It was that a conflict, when it finally
happened, would leave almost no trace. It was written into a human-readable
`detail` string on a `duplicate_content` page outcome — and the surviving
document, the one that carries `kind` into the database and into retrieval
weighting (ADR-0004), looked exactly like a document whose classification was
never in question. "We chose this" and "we picked one of two" were the same
value, which is precisely what A4 exists to prevent.

So the uncertainty is now a field, and the crawl says so out loud on the run
that produces it. The open question stays open; it can no longer pass
unnoticed.

## What the first instance showed

**[verified] 2026-09-03.** The first crawl after this change found one, on
buttondown.com:

```
kind?:  https://buttondown.com/ is website, also classified job_posting
```

`https://buttondown.com/refer/jobs` is a **referral link**. It serves the
homepage, byte for byte, and `jobs` is a referral code — not a statement about
the content. The other two aliases are `/refer/people` and `/refer/Equipo`.

This is evidence against the rule that would otherwise have looked obvious.
Preferring the more specific `kind` would have classified buttondown's
homepage as a job posting and given it job-posting weight at retrieval time.
`min(url)` chose `https://buttondown.com/` and was right — by determinism, not
by knowing anything.

One instance is not a rule. But it is no longer zero, and it points the same
way as the measured failure behind ADR-0015, where substring matching
over-classified two thoughtbot playbook articles as job postings. Both errors
run in the direction of too much specificity, and neither runs the other way.

## Why not decide it now

Because the evidence is one case, and a rule written from one case is an
assumption with a citation attached. What ADR-0015 said still holds: inventing
a winner would be an assumption dressed as a rule.

What changed is that the next instance will be visible in the artifact and in
the run output, and `kind_conflicts` is the field an eventual rule will be
measured against.

## Not persisted

`documents` has no column for `kind_conflicts`, and none is added. Adding
schema before a single conflict has been *acted* on is infrastructure ahead of
measurement (A9). The artifact carries it, and the artifact is where this
measurement is being collected.

`ArtifactDocument` gains the field with a default rather than as a
requirement: the three frozen fixtures predate it, `extra="forbid"` would
otherwise reject every artifact written after it, and the frozen corpus is the
one the ground-truth answers cite (docs/ground-truth.md). The loader's
field-drift test was made asymmetric for the same reason — a key ingest writes
that the model does not know is still fatal.

## Why `deduplicate()` moved

The kind-conflict branch was reachable only through a live crawl of a site
that happened to have the defect. It had therefore never executed. A branch
that has never run is not known to work — the same standard the isolation
guards are held to — and it is now covered by two unit tests, one for a
conflict and one asserting that the field stays *empty* when the duplicates
agree. A field that is always populated says nothing.
