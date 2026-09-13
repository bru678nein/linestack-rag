# ADR-0025 — Retrieval searches with a query written for each question

Status: Accepted · Date: 2026-09-13

## Decision

The model is still asked each evaluated question in the ground-truth set's
exact words. Retrieval no longer searches with those words. Each of the four
evaluated questions has a search query written in the vocabulary of the kind
of page that answers it (`linestack/retrieval/queries.py`, `queries-v1`).
Free-text questions and q5 search with their own words, as before.

The harness and `answer()` both go through `retrieval_query()`, so what is
scored is what the model is given. `USE_RETRIEVAL_QUERIES=false` restores the
question as its own query, which is how the baseline below was reproduced in
the same code. The run record carries `queries: queries-v1` or
`queries: question`.

ADR-0009's ranking SQL is unchanged. Only the vector it is given changes.

## Why

q2 and q4 missed on fly.io and q2 on thoughtbot, with the evidence in the
corpus. The cause is vocabulary, not ranking (docs/open-questions.md §2.5): the
q2 question says "evidence", "in-house", "technical" and "capacity", and
neither roster contains one of those words. This is not in ADR-0009's list of
next steps, and ADR-0009's hybrid search would not have fixed it, because
lexical search needs the words to be on the page too.

## Two rules against tuning to the test

- A query is written from the question's definition (`QUESTION_SCOPE`,
  ADR-0024), naming a kind of page and its usual words. It never names a
  company, a product, or anything only one site says. A unit test rejects a
  query containing a ground-truth prospect's name.
- The queries were written once, before the first measurement, and not edited
  after seeing it. A changed query is a new version with its own
  before-and-after.

## Measured

**[verified] 2026-09-13**, bge-small-en-v1.5, both prospects, the six
answerable pairs. Rank of the first chunk from a source URL, over all of the
prospect's chunks:

| pair | question | queries-v1 |
| --- | --- | --- |
| fly.io q1 | 10 | **18** |
| fly.io q2 | 97 | **1** |
| fly.io q3 | 5 | **1** |
| fly.io q4 | 25 | **1** |
| thoughtbot q1 | 3 | **7** |
| thoughtbot q2 | 43 | **14** |

| | @1 | @3 | @5 | @10 |
| --- | --- | --- | --- | --- |
| question | 0.00 | 0.17 | 0.33 | 0.50 |
| queries-v1 | 0.50 | 0.50 | 0.50 | 0.67 |

Three pairs moved to rank 1. **q1 got worse on both prospects**: the question
already reads like the services pages that answer it, and the query ("what we
do: our products and services…") ranks them lower. thoughtbot's q2 improved
from last to 14th, not into the top 5; its top hit is now the playbook's page
about how teams work, which matches "our team" and is not a roster.

### What it did to the answers

`Qwen/Qwen3-1.7B`, `answer-v3`, full `make eval`, against the same run with the
question as its own query:

| | question | queries-v1 |
| --- | --- | --- |
| declined where it should | 1 of 2 | **0 of 2** |
| declined where it should not | 0 of 6 | 0 of 6 |

Reading the answers:

- **fly.io q2 and q4 improved.** q2 now names the roster's real titles instead
  of the mislabelled "38 technical job titles". q4 names the Infrastructure
  Operations Engineer role and what it is for, part of the reference, where
  it had turned an essay about coding agents into a need.
- **fly.io q3 traded one half for the other**: it now has the hiring and has
  lost the funding.
- **thoughtbot q3 still says the right thing in the wrong form.** "There are no
  signals of investment or growth… Insufficient evidence." It does not start
  with the phrase, so it counts as answered, and the ADR-0024 check reports it
  as answering and declining in one reply.
- **thoughtbot q4 is wrong in a new way.** The q4 query retrieved the DEI
  pages, and the answer calls the DEI work a stated priority, which is a
  `must_not_claim` on that pair word for word. It was wrong before too (it
  invented open roles); now it is wrong the way the pair predicted.

The mechanism is the one ADR-0024 found: a query for needs, on a company that
states none, returns the nearest thing to a need, and the model reports it.
Better retrieval makes that material more relevant-looking. The fix that
targets it is on the answering side (the quote-then-check prompt), not here.

## Consequences

- Accepted for what it was built to fix: retrieval of q2 to q4's evidence,
  measured, with the baseline one switch away.
- q1's regression is open. Reverting q1 to its own words because it measured
  worse is tuning on two data points; it needs a reason that holds for the
  next prospect, or more pairs.
- Decline accuracy fell from 1 of 2 to 0 of 2, and that is recorded, not
  explained away. It is the next change's baseline.
