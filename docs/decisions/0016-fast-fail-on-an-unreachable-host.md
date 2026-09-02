# ADR-0016 — Stop crawling a host that is not answering

Status: Accepted · Date: 2026-09-02

## Decision

A crawl now ends early, with a reason, in three cases:

1. **robots.txt failed at the transport level with `dns_failure` or
   `transport_error`** — abort immediately, before any seed path is tried.
   robots.txt is the first request of every crawl, so its failure is the
   earliest available evidence, and a host that does not resolve or refuses
   connections will not serve `/about` either.
2. **`UNREACHABLE_STREAK` (3) consecutive transport failures with nothing yet
   fetched** — abort. This covers the host that accepts a connection and then
   hangs.
3. **robots.txt returned 5xx** — abort as `aborted_robots`. RFC 9309 §2.3.1.4
   makes that a full disallow, so every URL would be skipped one at a time.

`Prospect.crawl_outcome` records which, using the `crawl_outcome` enum already
in `migrations/0001_initial_schema.sql` — `completed`, `aborted_robots`,
`aborted_unreachable`, `failed` — the same shared-vocabulary rule as ADR-0012,
with a unit test asserting the code and the schema hold the same set.
`crawl_runs.outcome` already exists to receive it; no migration is needed.

## Why

**[verified] 2026-09-02.** A non-resolving domain cost **26 attempts, 39.5
seconds**. The DNS lookup itself fails instantly; all 39 seconds were our own
`DELAY_SECONDS` politeness, extended 26 times to a host that does not exist.
Worse, the run printed nothing between the robots line and the summary, because
the loop only prints when it stores a document — so it read as a hang.

**[verified] after: 0.25 seconds, 1 attempt.** Refused connections
(`127.0.0.1:9`) likewise: 26 → 1, 0.24 s.

Rate limiting exists to be considerate to somebody's server (A6). There is
nobody there. Politeness toward a host that does not resolve is not politeness,
it is just delay.

The earlier estimate of this defect said roughly 8 minutes, from 24 seed paths
× `TIMEOUT = 20 s`. That was marked **[computed]**, and it was measuring a
different case: a host that *hangs*. A non-resolving host fails fast and cost
39.5 s. Both are now bounded — the hang by the streak, at 3 × `TIMEOUT`.

## Why DNS aborts on one attempt but a timeout does not

They are not equally certain. `host_resolves()` asks the resolver directly, so
a `dns_failure` is confirmed, not inferred (ADR-0012); one attempt settles it.
A single timeout may be transient — a slow response to one path says little
about the next — so that case is *bounded* rather than diagnosed. Three is a
compromise between reacting quickly and acting on one bad sample.

## Alternatives

- **Abort on the first transport failure of any kind.** Simpler, and wrong: a
  single timeout on `/robots.txt` would discard a site that is merely slow.
- **Lower `TIMEOUT`.** Trades one problem for another; slow sites are real
  sites, and this does nothing about the 26 wasted delays.
- **Probe the host with a HEAD request first.** An extra request per prospect
  to learn what the robots.txt fetch already reveals.
- **Skip the rate-limit delay on failed requests.** Would have cut 39.5 s to
  near zero without any of the reasoning above — but it removes the delay
  exactly when a host may be struggling, which is when backing off matters
  most (A6).

## Consequences

- **[verified]** dead domain 39.5 s → 0.25 s; refused connection → 0.24 s. Both
  still exit non-zero with a reason.
- **[verified]** healthy crawls are unchanged: thoughtbot 37 documents / 54
  people / 0 roles, fly.io 39 / 57 / 2 roles / 1 technical, both
  `crawl: completed`.
- A prospect with no documents now carries whether the crawl *finished* and
  found nothing or *stopped*. Those are different facts and only one of them is
  about the company.
- A site whose first three reachable pages all time out is now abandoned even
  if the fourth would have worked. Accepted: the alternative is minutes spent
  per prospect on a host that is not answering.
