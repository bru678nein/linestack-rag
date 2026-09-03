# ADR-0014 — Count people by repeated structure first, class names second

Status: Accepted · Date: 2026-09-02

## Decision

`count_people()` runs two strategies and takes the first that finds anything:

1. `count_people_structurally()` — among siblings of the same tag under one
   parent, count those whose text reads like a person entry: 2–20 words
   beginning with two or more capitalised words. Three such siblings are the
   minimum to call it a roster.
2. `count_people_by_class()` — the previous behaviour, unchanged: leaf-most
   elements matching person-ish class selectors, grouped by signature
   (ADR-0003).

## Why

**[verified] 2026-09-02.** `fly.io/about` lists **57** people by name and role.
The class-based count returned **0**. fly.io styles its roster entirely with
Tailwind utility classes — `<figure class="w-full grid grid-cols-auto-span">`
inside `<div class="grid md:grid-cols-2 xl:grid-cols-3 ...">` — and the words
`team`, `member`, `staff`, `person`, `bio` and `profile` appear nowhere in the
markup. There was nothing for a class selector to match.

Class names are a site's private vocabulary and a site is free not to have one.
Repetition is not: a roster is a CMS emitting one element per person, and that
is visible whatever the elements are called.

Structure is tried first because where the two disagree it has the better
record. Hand-counted truth against both validation sites:

| Page | Truth | Structural | By class |
| --- | --- | --- | --- |
| fly.io/about | 57 | **57** | 0 |
| thoughtbot.com/team | 54 | **54** | 54 |
| basecamp.com/about | 0 | 0 | 0 |

basecamp is the negative control: `/about/team` redirects to a narrative page
with no roster, and 0 is the correct answer, not a miss.

The class-based path is kept rather than deleted. It needs no name-shaped text,
so it still covers rosters the structural pass cannot see — including the
lowercase or single-word names that `PERSON_NAME_RE` rejects.

## Known miss — superseded 2026-09-03 by [ADR-0018](0018-count-a-roster-by-its-portraits.md)

**[verified]** `buttondown.com/about` lists its team by first name only —
"Anita", "Ben", "Justin". Both strategies return 0. Relaxing the name pattern
to a single capitalised word would not fix this, it would count every
navigation item ("Features", "Pricing", "Changelog"), so it is recorded rather
than papered over. A single-name roster is currently invisible to both
strategies.

**The objection above was right, and the conclusion drawn from it was too
narrow.** Relaxing the name pattern is indeed the wrong fix — but the reason
is stronger than stated here: capitalisation does not separate a person from a
navigation item at all, because "Features" and "Pricing" are capitalised too.
ADR-0018 counts the page by a different property entirely, one distinct
portrait per repeated sibling, and reaches 14 of 14 — including the one member
listed as `nickd`, which no capitalisation rule can reach.

## Alternatives

- **Add fly.io's class names to `PERSON_SELECTORS`.** There are none to add,
  and chasing per-site utility classes does not generalise.
- **Replace the class-based count entirely.** Tempting — structural matched
  truth everywhere both were measured. Rejected on two sites' evidence: the
  class-based path costs one function and covers a shape structure cannot see.
- **Ask a model how many people are on the page.** Forbidden by A2, and this
  is a fact a function can establish. See ADR-0003.
- **Count names with a person-name gazetteer or NER.** A dependency and a
  model for something repetition already answers exactly.

## Consequences

- **[verified]** fly.io `people_listed` 0 → **57**; thoughtbot unchanged at 54;
  no other signal moved on either site.
- Ground truth for fly.io `people_listed` is now established by hand at 57 and
  can be written into the evaluation set.
- One extra DOM walk on team pages only. The full two-site crawl uses 7.3 s of
  CPU, so the cost is not measurable against the network time.
- A roster listing single-word names is still counted as 0, recorded above.
