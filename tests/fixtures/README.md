# Frozen pages

Four roster pages, committed exactly as fetched on **2026-09-03**. They are the
evidence behind `docs/open-questions.md` §1.1b and
[ADR-0018](../../docs/decisions/0018-count-a-roster-by-its-portraits.md).

| File | Fetched from | Hand-counted people |
| --- | --- | --- |
| `fly_io_team.html` | `https://fly.io/team` | 57 |
| `thoughtbot_team.html` | `https://thoughtbot.com/team` | 54 |
| `buttondown_about.html` | `https://buttondown.com/about` | 14 |
| `basecamp_about.html` | `https://basecamp.com/about` | 0 (narrative page) |

## Why they are committed

The §1.1b table used to be a claim about a live fetch nobody could repeat. Two
of its numbers were the ones that caught real defects, and neither could be
re-checked without re-crawling four sites and hoping none had changed in the
meantime.

Frozen, the table is asserted on every test run. A site redesign now shows up
as a failing test naming the page, rather than as a table that quietly stopped
being true.

`basecamp_about.html` is the negative control and is the most important of the
four: it is what makes admitting `/about` as a team page safe. A
`has_team_page` that is true for every company is worth nothing to
qualification.

## How they were fetched

Through `ingest.PoliteClient`, so A6 applies unchanged — robots.txt was
fetched and honoured (`ok` on all four), the rate limit was respected, and
nothing login-gated was touched. Each is a single public page.

They are stored verbatim rather than reduced to the roster subtree, because
the strategies under test are DOM-shaped: trimming the page would change the
sibling structure that `count_people_structurally` and
`count_people_by_portrait` both read, and the fixture would stop being
evidence about the real page.
