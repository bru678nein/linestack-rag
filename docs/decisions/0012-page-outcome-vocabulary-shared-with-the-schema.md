# ADR-0012 — Every URL gets one classified outcome, in the schema's own vocabulary

Status: Accepted · Date: 2026-09-02

## Decision

`Prospect.page_outcomes` holds one `PageOutcome(url, outcome, http_status,
detail)` for every URL the crawl touched — stored and failed alike. The
`outcome` strings are not a parallel vocabulary: the `PAGE_*` constants in
`ingest.py` **are** the `page_outcome` enum in
`migrations/0001_initial_schema.sql`, and a unit test reads the migration and
asserts the two sets are equal, so they cannot drift apart quietly.

This replaces `skipped_by_robots` and `dropped_pages`, which were two lists
speaking two vocabularies about the same question.

Outcomes are kept in a `dict` keyed by URL, and a later outcome supersedes an
earlier one, because the destination table is `UNIQUE (crawl_run_id, url)`. A
page stored and then found to be a duplicate is a duplicate, not both.

`main()` exits non-zero when a prospect yields no documents, and prints
`explain_empty_crawl()`, which names the dominant transport failure **in
preference to** the robots.txt reason.

## Why

A5 requires failures to be classified, not counted. Before this, only
robots.txt had reason codes; every other failure was a bare `None`. A prospect
with zero documents read identically whether the domain did not resolve, the
site refused us, or the company genuinely has no website — and the run exited
0, so nothing downstream could tell.

That is not an abstract tidiness problem. Question 1 is about the company's web
presence. "We found nothing" is only an answer once the reason is known;
otherwise it is our own failure being reported as a fact about the prospect,
which is precisely the confidently-wrong answer this project exists to avoid.

**DNS failure is confirmed, not guessed.** httpx flattens `socket.gaierror`
into a plain `ConnectError` and drops `__cause__`, so a non-resolving host and
a refused connection arrive identical apart from an errno string. **[verified]**
Rather than match that string, `host_resolves()` asks the resolver directly,
and returns `True` when it cannot tell — claiming `dns_failure` without
evidence would be the invented measurement A4 forbids.

**Transport outranks robots.txt in the explanation.** The first version of
`explain_empty_crawl()` checked `robots_reason` first, and reported a dead
domain as "robots.txt could not be fetched at all" — a symptom presented as a
cause, sending the reader to check a robots policy on a host that does not
exist. Caught by running it, not by reading it.

## Verification

**[verified] 2026-09-02.** Eight of the ten enum values were observed in live
runs; the remaining two are covered by unit tests.

| Outcome | Evidence |
| --- | --- |
| `stored` | thoughtbot 38, fly.io 40 |
| `skipped_robots` | thoughtbot 1 (`/people`) |
| `http_error` | thoughtbot 18, fly.io 19 (seed paths that 404) |
| `non_html` | fly.io 2 (`/jobs/feed.xml`) |
| `duplicate_content` | thoughtbot 2 |
| `budget_exhausted` | thoughtbot 8, fly.io 36 |
| `dns_failure` | 26/26 against a non-resolving domain, exit 1 |
| `transport_error` | 26/26 against `127.0.0.1:9`, exit 1 |
| `timeout` | unit test (`httpx.ReadTimeout`) |
| `thin_extraction` | unit test; 0 on both live sites since ADR-0011 |

The `dns_failure` / `transport_error` split is the one worth noting: both are
`ConnectError` from httpx, and they came out correctly separated on live hosts.

Signals were unchanged across the change on both sites, as expected — this
records why pages are missing, it does not change which pages are fetched.

## Alternatives

- **Keep the two lists and add a third.** More vocabularies for one question.
- **An exception type per failure.** Moves classification to the call site,
  which is where it was already being lost.
- **Match the errno string for DNS.** Fragile, locale- and platform-dependent,
  and unnecessary when the resolver can simply be asked.
- **Log and move on.** Logs are not a queryable record; `crawl_page_outcomes`
  exists precisely so "the evaluation set expects this URL and it is not in
  documents — why not?" has an answer.

## Consequences

- The JSON artifact's shape changed: `skipped_by_robots` and `dropped_pages`
  are gone, replaced by `page_outcomes`. Nothing consumes the artifact yet
  (`linestack/ingestion/loader.py` is empty), so this is a free break — and
  cheaper now than after a loader exists.
- One extra DNS resolution per `ConnectError`, on the failure path only.
- `budget_exhausted` makes the crawl budget's cost visible for the first time:
  fly.io left 36 URLs unfetched at `max_pages=40`.
