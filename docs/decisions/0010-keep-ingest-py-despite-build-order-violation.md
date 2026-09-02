# ADR-0010 — Keep `ingest.py` despite the build-order violation

Status: Accepted · Date: 2026-09-02

## Decision

`ingest.py` was written before any documentation, schema, ADR, or evaluation
harness existed. This is a violation of the build order A3 prescribes. The code
is kept as-is rather than rewritten, and this record exists so that the
violation is on the record rather than quietly normalised.

`ingest.py` stays at the repository root and is not modified in this pass. Its
eventual home is `linestack/ingestion/`, which currently holds empty modules
naming its destination.

## Alternatives

- **Delete it and rebuild in order.** Discards four measured bug fixes and the
  live-site measurements that justify ADR-0003, ADR-0006 and ADR-0007. Those
  measurements cannot be reproduced from documentation.
- **Move it into `linestack/ingestion/` now.** Touching working, debugged code
  during a documentation pass, with no tests in place at the time of the move,
  to gain a directory layout. Deferred until the module has tests around it.
- **Keep it and say nothing.** The option this record exists to close.

## Why

The code works, and it has been exercised against 18 live domains. Rewriting it
to satisfy an ordering rule would cost real evidence to buy process compliance.

The more useful observation is what the violation actually produced. Four
distinct bugs were found in `ingest.py`, and **every one of them was found by
running against live sites, not by reading the code**:

| Bug | Reported | Ground truth |
| --- | --- | --- |
| `RobotFileParser.read()` denies everything on a 403 | thoughtbot: 0 pages | 37 pages |
| Person-card counting counts leaves, not cards | thoughtbot: 162 people | 54 |
| Role counting counts pages, not roles | thoughtbot: 4 roles / 3 technical | 0 / 0 |
| FIFO budget lets link volume allocate the crawl | fly.io: 3 website pages / 40 | — |

That is A3's actual argument, demonstrated: not "documentation first" as
ceremony, but that a component's real behaviour is only knowable by measuring
it. It also shows the cost of the violation — those four bugs sat in the code
for an unknown period with no harness that would have caught any of them, and
they were found by hand-checking two companies. With 12 prospects in the
evaluation set, hand-checking does not scale.

**[verified]** all four bugs and their measurements. **[assumed]** that no fifth
bug of the same class remains; there is no harness, so this is an expectation,
and the five open defects in `docs/open-questions.md` argue against it.

## What would reverse it

- The move into `linestack/ingestion/` happens once the module has unit tests
  covering `classify`, `normalise`, `count_people`, `queue_rank`, and
  `_count_open_roles` against fixture HTML. Some of those tests exist already
  under `tests/`; the fixture-based ones do not.
- If the known defects in `docs/open-questions.md` — particularly the silent
  30-word extraction threshold and the absent failure reason codes — turn out to
  require restructuring rather than patching, rewriting becomes cheaper than
  repairing and this record is superseded.

## Consequence for the rest of the build

The order from here is the one A3 specifies and it is not negotiable again:
schema and loader → chunking and embedding → naive vector search end-to-end →
ground truth → evaluation harness → measured improvements. Nothing in the
retrieval path ships before the harness that can tell whether it helped.
