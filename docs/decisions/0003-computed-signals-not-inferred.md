# ADR-0003 — Compute structured signals, never infer them

Status: Accepted · Date: 2026-09-02

## Decision

Facts that a function can establish from the fetched HTML are computed during
ingestion, stored as structured metadata on `prospects.signals`, and injected
into the model's context on every answer. The model is never asked to infer
them.

The current set (implemented in `ingest.py`): `has_team_page`, `team_page_url`,
`people_listed`, `technical_roles_named`, `has_careers_page`, `open_roles_seen`,
`technical_roles_open`, `blog_posts_seen`, `latest_post_date`, `pages_crawled`,
`total_words`.

## Alternatives

- **Ask the model.** "Based on these pages, how many people are on this
  company's team?" One prompt, no parsing code, no per-CMS breakage.
- **Ask the model and verify with code.** Two costs instead of one, and the
  verification code is the same code as computing it directly.

## Why

Question 2 of the four is "what evidence is there of in-house technical
capacity", and the answer usually reduces to: is there a team page, how many
people are on it, how many of them hold technical titles, and are there open
technical roles. Those are countable.

Asking a model to count is asking it to produce a plausible number. It will
produce one. It will not signal uncertainty, and the number will be wrong in a
way that reads exactly like a right answer — which is the failure mode this
project is built around.

Rule-based checks additionally cost no API call and cannot hallucinate (A2).

**The honest counter-evidence.** Computing is not free of error, it is free of
*confident* error — the mistakes are systematic and therefore findable. Both of
the following were found only by running against live sites, not by reasoning:

- **[verified]** Counting leaf-most elements matching person-ish class selectors
  reported **162** people on thoughtbot.com/team against a ground truth of
  **54**: the page renders each person as `{person-photo, person-info-name,
  person-info-title}`, three matching leaves per card. Fixed by grouping leaves
  by signature (tag plus the class tokens that matched) and taking the largest
  group, on the reasoning that a CMS emits one identical card per person. Now
  returns 54.
- **[verified]** That fix was necessary and not sufficient. Class-based
  counting reported **0** people on fly.io/about, a page listing **57** by
  name, because the site styles its roster entirely with Tailwind utility
  classes and the words `team`, `member`, `person` and `bio` appear nowhere in
  its markup. Counting repeated sibling elements whose text reads like a person
  entry returns 57 there and 54 on thoughtbot. See ADR-0014.
- **[verified]** Counting pages classified `job_posting` reported **3 roles / 2
  technical** for fly.io against a ground truth of **2 / 1**, and **4 / 3** for
  thoughtbot against a ground truth of **0 / 0**. thoughtbot's four
  `job_posting` documents were a jobs landing page, a compensation calculator,
  and two internal career-ladder playbook pages. Fixed by counting role
  *identity* rather than pages: a listing contributes the roles it links to and
  never itself, candidates are deduplicated by URL, and a candidate survives
  only if its name matches a job-title pattern. Both now match ground truth.
- **[verified]** `extract_role_headings()` returns zero headings on both
  fly.io/jobs and thoughtbot.com/jobs. Per-role links from the listing page are
  the reliable signal. Heading extraction is kept only as a fallback for
  listings that name roles inline without linking them; it has not been observed
  to fire on any real site.

A model asked to count would have produced a wrong number on all three of those
pages too, and there would have been no ground truth to compare it against and
no bug to fix.

## What would reverse it

Per signal, not globally. A signal moves to the model — or is dropped — when:

- Its computed value disagrees with hand-checked ground truth on more than 2 of
  10 prospects, and the disagreement is not a fixable pattern. Structural
  counting is a heuristic over other people's markup; a single client-rendered
  team grid defeats it entirely, and `selectolax` does not run JavaScript.
- The cost of maintaining the extractor exceeds the cost of a verification pass.
  Concretely: more than one fix per month per signal, sustained.

If a signal is moved to the model, it must be marked in the context as inferred,
not computed, so that a downstream reader can tell the difference (A4). A
computed signal that turns out to be unreliable and is *silently* left in place
is worse than either alternative.

Related known defect: the 30-word extraction threshold currently produces a
false `has_team_page: False` for fly.io. See `docs/open-questions.md`. That is a
bug in ingestion, not evidence against this decision, but it is evidence that a
computed signal needs its own failure reason code (A5).
