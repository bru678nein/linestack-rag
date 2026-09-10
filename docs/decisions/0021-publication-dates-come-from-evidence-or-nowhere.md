# ADR-0021 — A publication date comes from evidence, or from nowhere

Status: Accepted · Date: 2026-09-09

Closes [open-questions.md §1.6](../open-questions.md).

## Decision

`published` is now taken from the best available evidence, and
`Document.published_source` records which:

| source | what it means |
| --- | --- |
| `url_path` | the site filed the page under a dated path (`/blog/2026-08-19`) |
| `metadata` | the page declares it — `<time>`, JSON-LD, `article:published_time` |
| `byline` | a date in the visible text, **preceded by a publication cue** |
| `none` | no evidence. `published` is null, and that is the answer |

htmldate's fuzzy search is **no longer consulted**. `find_date` is called with
`extensive_search=False`; `trafilatura.extract_metadata` (which enables it by
default) is not used for dates at all.

## Why

**[verified] 2026-09-03.** Across the 76 documents of the two validation
crawls, **31 carried exactly `2026-01-01`** and 9 were null. A date shared by
31 documents across two unrelated sites is not 31 publication dates; it is
htmldate's coarse fallback reaching for a year boundary. One page,
`fly.io/docs/about/healthcare`, was dated **`1998-01-01`**.

Three things rested on that field and none could tell a declared date from a
guess: `latest_post_date`, the chunk provenance header ADR-0005 embeds in every
chunk, and any future recency weighting. A4 forbids substituting a plausible
guess for a bad measurement, because only one of the two is detectable
afterwards — and here the guess was winning, silently, on 41% of the corpus.

### Strict keeps the real dates

The risk was that turning off fuzzy search would lose genuine dates along with
the invented ones. Measured on four pages before writing anything:

| page | extensive | strict |
| --- | --- | --- |
| fly.io/blog/corrosion | 2025-10-22 | **2025-10-22** |
| fly.io/docs/about/healthcare | 1998-01-01 | **None** |
| fly.io/team | 2026-01-01 | **None** |
| thoughtbot.com/team | 2026-01-01 | **None** |

Strict keeps what the page declares and drops what nobody declared. That is
the whole trade.

### Before and after, on a full re-crawl

**[verified] 2026-09-09**, both validation sites re-crawled:

| `published` | before | after |
| --- | --- | --- |
| exactly `2026-01-01` | **31** | **0** |
| other dated | 36 | 30 |
| null | 9 | **46** |

Five non-fallback dates were also lost, and **every one was a guess**:

- `1998-01-01` on `docs/about/healthcare` — self-evidently not a date.
- `2026-08-14` on `docs/about/discontinued-plans` — extensive only.
- `contact`, `docs/about/cost-management`, `docs/about/free-trial` — all three
  return a date under extensive search and `None` under strict. Checked
  directly against the live pages; none declares a date.

**Zero real dates were lost.** The 30 that survive all come from `metadata`,
which is the page's own statement.

## The byline reader was wrong on its first run

Worth recording, because it is the exact defect this ADR exists to prevent,
committed by the fix for it.

§1.6 named "the visible byline" as a candidate source, so the first version
read any date near the top of the extracted text. Across 76 documents it fired
**once** — on `fly.io/docs/about/discontinued-plans`, reading

> "If you purchased a Launch or Scale plan before **October 7, 2024**, you can
> remain on those plans"

as a publication date of `2024-10-07`. One for one, and wrong.

A date in a sentence is a fact the page states, not a claim about when it was
written. The reader now requires a cue — *posted*, *published*, *last updated*,
*written*, *released* — within 24 characters before the date. After the gate,
the byline source contributes **0** dates to this corpus, which is the correct
number for two sites that both declare their dates in metadata.

It is kept rather than deleted because it costs nothing and covers the case
those two sites happen not to have. That is the same argument ADR-0014 makes
for keeping the class-based person count.

## Consequences

- **The frozen corpus moved, and the ground truth written against it is now
  stale.** In the seven days between crawls, thoughtbot's `people_listed` went
  54 → 53 and its `latest_post_date` 2026-09-02 → 2026-09-09; fly.io's
  `people_listed` went 57 → 53. None of that is this change — the frozen
  fixtures still count 57 / 54 / 14 / 0 under today's code — it is site drift.
  The validator now warns when a ground-truth file's `crawled_at` disagrees
  with its artifact's, because that file validates perfectly while describing
  a corpus that no longer exists.
- `latest_post_date` is now computed only from dates a page declared. It moved
  on both sites, in both cases to a *later* date, because the fallback dates it
  was competing against were January guesses.
- `published_source` is on the artifact and on `ArtifactDocument`, defaulted so
  older artifacts still load. It is **not** persisted: `documents` has no
  column, and adding one before a recency-weighting decision needs it is
  infrastructure ahead of measurement (A9).
- The chunk provenance header embeds `published`, and **for a day it
  embedded the old fabricated dates anyway.** The first re-load after this
  change updated `documents.published_at` but skipped re-chunking, because the
  skip was keyed on the text alone: 30 of thoughtbot's 43 chunks still carried
  `2026-01-01` in their header, and 6 of fly.io's carried `2026-01-01` or
  `1998-01-01`. This bullet originally said the header "now embeds a real date
  or nothing". For the loaded corpus that was false. Fixed 2026-09-10: the
  loader now re-chunks any document whose chunks carry a header other than the
  one it would get now (`relabelled`), and the reload brought both prospects
  to 0. Whether the corrected header changes retrieval is unmeasured.

## Alternatives

- **Keep htmldate's guess, labelled.** Rejected. The label would let a caller
  filter, but every existing caller would have to be taught to, and the
  default reading of `published` would stay wrong. A field whose honest use
  requires checking a second field is a field that gets misread.
- **Reject only suspicious values (Jan 1, Dec 31, far past).** A blocklist of
  shapes, which fails on the first site that genuinely publishes on 1 January.
  Asking the library for evidence rather than a guess is the same fix without
  the heuristic.
- **Set `min_date` on htmldate.** Would have caught `1998-01-01` and nothing
  else. It treats one symptom.
