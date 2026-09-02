# ADR-0015 — Classify by whole path segments; no title; deterministic canonical URL

Status: Accepted · Date: 2026-09-02

Three decisions about how `kind` is decided, taken together because they are
one question: what determines a page's kind, and can it change between runs?

## 1. Patterns match whole path segments, never substrings

`KIND_PATTERNS` is anchored (`^…$`) and matched against each path segment.

**[verified]** on thoughtbot.com, the substring form classified two playbook
articles as job postings:

- `/playbook/our-company/career-paths` — `careers?` matched inside
  `career-paths`
- `/playbook/strategy/design-sprints/02-pre-product-validation/jobs-profile` —
  `jobs?` matched inside `jobs-profile`

`kind` is denormalised onto `chunks` for retrieval source weighting
(ADR-0004), so a playbook article carrying job-posting weight is a retrieval
defect, not a cosmetic one. **[verified]** thoughtbot `job_posting` documents
4 → 2; the two that remain are `/jobs` and `/jobs/compensation`, both genuinely
under the careers section. fly.io is unchanged at 3.

## 2. `classify()` no longer takes a title

The signature took a `title` it ignored. Before dropping the parameter, the
title was measured as a signal rather than assumed useless.

**[verified]** across both validation corpora, the only three pages whose
`<title>` matches career/job/hiring words are thoughtbot playbook **articles**:

| Title | Actually |
| --- | --- |
| `Career Paths \| thoughtbot's Playbook…` | playbook article |
| `Jobs Profile \| thoughtbot's Playbook…` | playbook article |
| `Hiring \| thoughtbot's Playbook…` | playbook article |

Three false positives, zero true positives. Wiring the title in would have
reintroduced the exact misclassification decision 1 removes. So the parameter
is deleted rather than implemented, and the reasoning lives in the function's
docstring so that nobody re-adds it on the reasonable-sounding theory that a
title saying "Careers" means a careers page.

This closes `docs/open-questions.md` §1.5, which offered both options — use it
or drop it. The measurement chose.

## 3. The canonical URL of a deduplicated page is deterministic

Deduplication kept whichever copy was crawled first, so both `source_url` and
`kind` were an accident of queue order: a page reachable at `/handbook/x` and
`/careers/x` carried whichever classification was fetched first, and that could
change between runs as a site's own links change. `canonical_document()` now
picks `min(group, key=url)`.

**This is a determinism fix, not a semantic one.** It makes no claim that the
alphabetically first URL is the *right* one, and it deliberately does **not**
prefer a more specific `kind`.

There is no measurement saying which URL should win. Neither validation site
has ever produced two URLs for one page that disagree about `kind` — after
decision 1, thoughtbot's two `career-paths` URLs both classify `website` and
agree. Inventing a winner would be an assumption dressed as a rule. Instead the
disagreement is recorded on the `duplicate_content` outcome detail
(`kind conflict X vs Y`), so that when one occurs there is a measurement to
decide the rule with (A3).

## Alternatives

- **Prefer the more specific kind among duplicate URLs.** Plausible and
  unmeasured. `/about` also served at `/blog/about-us` would become a blog
  post, which is worse than the status quo.
- **Prefer the shortest path.** Equally arbitrary, and it would have moved
  fly.io's canonical URL from `/about` to `/team` for no reason.
- **Use the title as a tiebreak only.** Still wrong on all three measured
  cases; the titles say "Careers" on pages that are not.
- **Keep the ignored `title` parameter.** A signature that promises something
  it does not do is discovered when someone changes the title handling and
  nothing happens.

## Consequences

- **[verified]** thoughtbot `job_posting` 4 → 2, `website` 24 → 26. Signals
  unchanged and still matching ground truth on both sites: thoughtbot 54
  people / 0 roles, fly.io 57 people / 2 roles / 1 technical.
- Changing `classify()` changes `queue_rank()`, so the crawl mix shifts
  slightly: thoughtbot 38 → 37 documents as two pages stop being fetched with
  job-posting priority. That is the budget working (ADR-0007).
- `kind` is now reproducible across runs for a deduplicated page.
- Still unresolved and now observable: which URL *should* win a genuine `kind`
  disagreement.
