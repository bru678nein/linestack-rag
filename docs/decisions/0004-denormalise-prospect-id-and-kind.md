# ADR-0004 — Denormalise `prospect_id` and `kind` onto `chunks`

Status: Accepted · Date: 2026-09-02

## Decision

`chunks` carries `prospect_id` and `kind` as its own columns, duplicated from
`documents`. `chunks (prospect_id)` gets a B-tree index.

The duplication is made safe by a composite foreign key rather than by
application discipline: `documents` carries a redundant `UNIQUE (id,
prospect_id)`, and `chunks` references it as

```sql
FOREIGN KEY (document_id, prospect_id) REFERENCES documents (id, prospect_id)
```

A chunk whose `prospect_id` disagrees with its document's `prospect_id` cannot
be inserted.

## Alternatives

- **Normalise, and join.** `SELECT … FROM chunks JOIN documents USING
  (document_id) WHERE documents.prospect_id = :id ORDER BY embedding <=> :q`.
  One join on every query, and the prospect filter becomes a property of the
  join rather than of the row.
- **Denormalise without the composite key.** Cheaper to write, and it makes the
  duplicated column a fact maintained by whoever wrote the last INSERT.
- **A separate table or schema per prospect.** Perfect isolation, unusable
  operationally past a few dozen prospects.

## Why

**`prospect_id`:** filtering by prospect is on the path of every single query,
and it is the mechanism by which A1 is enforced. Two reasons, in order of
importance:

1. **Correctness.** A1 says a chunk from prospect B must never be reachable when
   answering about prospect A. A `WHERE` clause on a column of the table being
   scanned is a smaller thing to get wrong than a join condition, and the
   composite foreign key means the denormalised column cannot drift from its
   source. The isolation is then a property the database enforces, not a
   property that code review enforces.
2. **Cost.** The filter is the most selective predicate in the query and it
   determines the size of the candidate set that exact vector search has to
   scan. ADR-0001 depends on that set staying small.

**`kind`:** a job posting is stronger evidence of technical capacity than an
About page. Weighting retrieval by source therefore needs `kind` available at
scoring time, and paying for a join to reach it would push weighting toward
being done in Python after retrieval, which is the wrong place for it.

**[assumed]** the join would be measurably slower. Not measured — there is no
data. The correctness argument is what carries this decision; the performance
argument is secondary and currently unverified.

## What would reverse it

- The composite foreign key turns out to be unmaintainable in practice — for
  example, a bulk re-ingestion path that must move documents between prospects.
  If the constraint has to be dropped, the denormalisation must be dropped with
  it; an unconstrained duplicated key is the worst of both designs.
- `kind` grows beyond a small closed enum, or becomes multi-valued (a page that
  is both a blog post and a job announcement). At that point it belongs in a
  join table and weighting has to be reconsidered.

## Known risk

`kind` is denormalised onto `chunks` for source weighting, and page
classification is path-only. It was also imprecise: `KIND_PATTERNS` matched
`careers?` anywhere in a path, so `/playbook/our-company/career-paths` was
classified `job_posting` and would have carried job-posting weight into
retrieval for a playbook article. **[verified]** on thoughtbot.com.

**Fixed 2026-09-02** by ADR-0015 — patterns are anchored to whole path
segments, and thoughtbot `job_posting` documents went 4 → 2. The residual risk
is smaller but real: classification is still path-only, so a job posting served
from a path that names neither jobs nor careers is classified `website`. The
page title was measured as an additional signal and rejected, three false
positives to zero true positives; see ADR-0015.
