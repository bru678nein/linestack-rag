# ADR-0018 — Count a roster by its portraits, and choose the roster page by evidence

Status: Accepted · Date: 2026-09-03

Supersedes the "Known miss" section of [ADR-0014](0014-count-people-structurally-first.md).

## Decision

Two changes, both closing [open-questions.md §1.1b](../open-questions.md).

**1. A third counting strategy, `count_people_by_portrait()`, runs last.**
Among siblings of the same tag under one parent, count those holding exactly
one image and at most 20 words of text — provided every image source in the
group is distinct and there are at least three of them. No name pattern, no
capitalisation requirement.

`count_people()` is now structural, then class-based, then portraits.

**2. A page under `/about` can be the team page, but only on evidence.**
`TEAM_PATH_RE` still makes a page a team page unconditionally. The new
`ABOUT_PATH_RE` qualifies a page only if `MIN_ROSTER` people are actually found
on it. Where several pages qualify, `choose_roster_page()` takes the one with
the most people, breaking ties towards a `/team` path and then towards the
alphabetically first URL.

## Why

**[verified] 2026-09-03.** `buttondown.com/about` lists **14** people and both
existing strategies returned **0**, for two independent reasons stacked on top
of each other.

The names are one word each — "Anita", "Ben", "Justin" — so `PERSON_NAME_RE`,
which needs two capitalised words, matches none of them. There are no
person-ish class names either; the cards are Tailwind utilities, the same
shape that defeated the class-based count on fly.io. And `/about` does not
match `TEAM_PATH_RE`, so `count_people()` was never called on the page at all.
Fixing the count alone would have changed nothing.

### Why portraits, and not a looser name pattern

ADR-0014 recorded the objection correctly: accepting a single capitalised word
would count every navigation item. What it did not notice is that
capitalisation is not the discriminator it looks like — "Features", "Pricing"
and "Changelog" are capitalised too. Relaxing the pattern trades a miss for a
false positive and buys nothing.

What a navigation item does not have is a portrait of its own. So the rule
keys on the image, and requires the images in a group to be *distinct*: a menu
that repeats one chevron icon matches nothing, and a roster that gives every
person a different photograph matches exactly.

Dropping the name pattern entirely, rather than loosening it, is what makes
the count 14 and not 13. One of buttondown's fourteen is listed as `nickd` —
lowercase, one word. Any capitalisation rule loses that person.

### Measured, on frozen pages

The four pages are committed under `tests/fixtures/` exactly as fetched, and
the table below is asserted on every test run. It used to be a claim about a
live fetch nobody could repeat.

| Page | Truth | Structural | By class | By portrait | `count_people` |
| --- | --- | --- | --- | --- | --- |
| fly.io/team | 57 | **57** | 0 | 0 | **57** |
| thoughtbot.com/team | 54 | **54** | 54 | 54 | **54** |
| buttondown.com/about | 14 | 0 | 0 | **14** | **14** |
| basecamp.com/about | 0 | 0 | 0 | 0 | **0** |

thoughtbot is the corroboration: three independent strategies, one number.
basecamp is the negative control, and it is what makes admitting `/about`
safe — a narrative company story with nobody named on it stays
`has_team_page: False`. A signal that is true for every company is worth
nothing to qualification.

### Why the two path rules are asymmetric

A page at `/team` is a team page whether or not we can parse its roster.
Reporting `has_team_page: False` because the markup defeated us states an
ingestion artifact as a fact about the company — the same defect as §1.1.

A page at `/about` carries no such claim from the site, so it has to earn the
label by actually listing people.

### The tie-break was not decoration

**[verified] 2026-09-03**, on the first real crawl after this change:
buttondown publishes its newsletter archive under `/people/archive/…`, and
**21** of those pages match `TEAM_PATH_RE`. `/about` does not.

The previous rule took the first matching page in document order, so it would
have reported `has_team_page: True` with `team_page_url` pointing at a
newsletter archive and `people_listed: 0` — a confident, cited, wrong answer.
Preferring the page with the most people finds `/about` and 14 instead.

That case was found by running the fix, not by predicting it, which is the
argument for `choose_roster_page()` being one function with one deterministic
rule rather than a first-match loop.

## Known limitation

A marketing grid of feature cards — distinct illustration, two-word caption,
four of them — matches the portrait shape and would be counted as people.

Two things bound it, and neither is a claim that it cannot happen. The pass
runs only when both other strategies found nothing, and `count_people()` is
only ever called on a page already identified as a roster page. It is a
fallback on a narrow surface, not a detector turned loose on a whole site. If
a site is ever miscounted this way it will show up as a `people_listed` that
the ground-truth set disagrees with, which is the mechanism that is supposed
to catch it.

## Alternatives

- **Relax `PERSON_NAME_RE` to one capitalised word.** Rejected above: it
  misses `nickd`, and it counts navigation.
- **Match on the image path containing `/team/`.** Buttondown's does. That is
  the site's private vocabulary again, which is the exact thing ADR-0014
  moved away from.
- **Admit `/about` unconditionally.** Turns basecamp's correct `False` into a
  false positive and costs the negative control.
- **Run `count_people()` on every crawled page and call any page with a
  roster the team page.** The most evidence-driven version, and rejected on
  cost and blast radius: it parses every document instead of two or three,
  and it exposes the loosest strategy to marketing pages, where the known
  limitation above is most likely to fire.
