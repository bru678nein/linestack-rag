# ADR-0007 — Crawl budget: quota is a cap, ordering is by question value

Status: Accepted · Date: 2026-09-02

## Decision

The crawl queue is not FIFO. Each URL's page kind is derived from its path
(which costs no request), and the next URL fetched is chosen by:

1. **Cap.** Each kind has a share of the page budget — `website` 0.45,
   `job_posting` 0.30, `blog_post` 0.25, anything else 0.05. A kind that has
   reached its cap yields to every kind that has not. The quota is a **cap,
   never a floor**.
2. **Priority.** Within that, pages are ordered by how directly they answer the
   four questions: `website` and `job_posting` at priority 0, `blog_post` at 1,
   everything else at 2.
3. **Insertion order** as the final tie-break.

Leftover budget still goes to capped kinds rather than going unspent.

Implemented as `queue_rank()` in `ingest.py`.

## Alternatives

- **FIFO with a flat page cap.** What was there first.
- **Ratio balancing** — always serve whichever kind is furthest below its quota
  share. Implemented, measured, and reverted.
- **Per-kind hard budgets with separate queues.** Equivalent to caps, but leaves
  budget unspent when a kind runs out of URLs.
- **A larger budget.** Does not solve the allocation problem, it postpones it,
  and it costs someone else's server more requests.

## Why

A FIFO queue does not divide the budget between the four questions; it lets link
volume divide it. A blog index links to dozens of posts, an About page links to
none. The blog wins by structure, not by value.

**[verified]** page-kind mix at `max_pages=40`:

| Domain | Queue | website | job_posting | blog_post | total words |
| --- | --- | --- | --- | --- | --- |
| fly.io | FIFO | 3 | 3 | 34 | 61,649 |
| fly.io | ratio-balanced *(rejected)* | 7 | 3 | 30 | — |
| fly.io | quota-cap + priority | **16** | 3 | 21 | 62,169 |
| thoughtbot.com | FIFO | 22 | 3 | 12 | 17,368 |
| thoughtbot.com | ratio-balanced *(rejected)* | 18 | 4 | 17 | — |
| thoughtbot.com | quota-cap + priority | **24** | 4 | 10 | **21,174** |

Under FIFO, fly.io's crawl never reached a team page. Question 2 — evidence of
in-house technical capacity — was therefore unanswerable from a full-budget
crawl of a company that has both a team and open roles. During evaluation that
presents as a retrieval failure when it is an ingestion failure, which is the
most expensive kind of wrong diagnosis available (A8).

**Why ratio balancing was rejected.** It was the first design: always serve
whichever kind is furthest below its quota share. **[verified]** on thoughtbot it
made things worse — websites 22 → 18, blogs 12 → 17 — because being under quota
promoted low-value blog *tag-index* pages ahead of real content. A share of the
budget is a limit on how much a kind may take, not an entitlement it is owed.
Reverted under A3: a change with no measured improvement does not ship, and this
one had a measured regression.

Note that fly.io's total word count barely moved (61,649 → 62,169, +0.8%) while
its `website` page count went 3 → 16. The measurement that mattered was
composition, not volume. A budget policy evaluated on total words would have
concluded that FIFO was fine.

## What would reverse it

- The quota constants are **[assumed]**, not derived. 0.45 / 0.30 / 0.25
  reflects a belief that questions 1, 2 and 4 are answered from marketing pages
  and postings while question 3 (growth signals) is answered from blog recency.
  Measure it: once the evaluation harness exists, run the ground-truth set
  against corpora crawled at different quota splits and compare retrieval recall
  per question. If recall on question 3 is the weak one, `blog_post` needs a
  larger share.
- A prospect type where the assumption inverts — a developer-tools company whose
  entire positioning lives in engineering blog posts and whose About page is one
  sentence. If that appears, the quota should become a function of what the
  crawl finds rather than a constant.
- `max_pages` changes materially. The caps are fractions, so they scale, but the
  priority ordering was only observed at 40 pages.
