# ADR-0006 — Fetch robots.txt through the project's own HTTP client

Status: Accepted · Date: 2026-09-02

## Decision

`robots.txt` is fetched with the project's own `httpx` client and the project's
own `User-Agent`, then handed to `RobotFileParser.parse()`.
`RobotFileParser.read()` is not used.

The HTTP status of that fetch is classified into one of five reason codes and
recorded on the run:

| Code | Condition | Effect |
| --- | --- | --- |
| `ok` | 2xx | Rules parsed and applied. |
| `absent` | 4xx other than 401/403 | No policy exists; crawling permitted. |
| `unreadable` | 401 / 403 | Policy exists but was withheld; crawling permitted, recorded as unread. |
| `server_error` | 5xx | Full disallow; the site is unwell, stay off it. |
| `fetch_failed` | transport error, timeout, DNS | Recorded; crawling permitted. |

This follows RFC 9309 §2.3.1. Politeness itself is not up for discussion (A6):
a disallowed path is skipped and recorded in `skipped_by_robots`, never worked
around.

## Alternatives

- **`RobotFileParser.read()`**, the standard-library convenience method. This is
  what was there first, and it is the bug.
- **A third-party robots library** (`reppy`, `protego`). Better spec coverage
  than the standard library, particularly for wildcards and `crawl-delay`. An
  additional dependency for a component that currently works.
- **Ignore robots.txt.** Not an option (A6).

## Why

`RobotFileParser.read()` fetches robots.txt with urllib's own User-Agent
(`Python-urllib/x.y`). Bot protection commonly answers that with 403. `read()`
catches the `HTTPError` internally, sets `disallow_all = True`, and does not
raise. The caller is left holding a parser that looks healthy and denies every
URL. The crawl returns zero pages, and the run is recorded as "blocked by
robots.txt" for a block the site never declared.

**[verified]**, 2026-09-01, across 18 real software-company domains:

| Measurement | Before | After |
| --- | --- | --- |
| Domains reporting a total robots.txt block | **4 / 18** | **0 / 18** |
| thoughtbot.com pages crawled | **0** | **37** |

The four affected domains were thoughtbot.com, basecamp.com, sourcegraph.com and
planetscale.com. thoughtbot.com's actual robots.txt disallows exactly one path,
`/people`.

Two things follow, and both matter more than the fix itself:

1. **A politeness bug and a bot-blocked bug are indistinguishable from the
   outside** unless the outcome is classified. A single boolean "blocked by
   robots" would have hidden this indefinitely — the run reported a plausible
   reason for returning nothing, so nobody looked. This is A5 justifying itself
   with a concrete case.
2. **A 403 on robots.txt does not mean a site declined to be crawled.** RFC 9309
   §2.3.1.3 permits crawling when robots.txt is unreachable. Recording it as
   `unreadable` rather than as a denial keeps the distinction between "they said
   no" and "we could not ask".

## What would reverse it

- The standard library changes `read()` to accept a session or a User-Agent, and
  the workaround becomes unnecessary. Unlikely; check on Python upgrades.
- A domain is found whose robots.txt uses directives `RobotFileParser` parses
  incorrectly — wildcards in `Disallow`, or `Allow` precedence rules. That is an
  argument for `protego`, not against this decision, and it must be recorded as
  a measurement (which domain, which directive, which wrong answer) before the
  dependency is added.
- 5xx handling proves too strict in practice — a site with a flaky robots.txt
  endpoint that is never crawlable. Record how often `server_error` occurs
  before relaxing it; relaxing it means crawling a site that has not given us a
  policy, which is a politeness decision, not a performance one.
