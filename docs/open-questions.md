# Open questions, assumptions, and known defects

Read this before building anything on top of the current code.

Three sections:

1. **Known defects** — measured, reproducible, broken. Fix these first.
2. **Assumptions that need verification** — stated in the docs as assumptions,
   listed here with how to check each one.
3. **Undecided** — genuinely open, including objections to the axioms.

---

## 1. Known defects

All **[verified]** against live sites on 2026-09-01/02. Every defect listed
here is now fixed. What remains is recorded inline under the entry it belongs
to: §1.1c does not yet know which URL should win a genuine `kind`
disagreement, and §1.6's publication dates are still largely htmldate's
fallback.

### 1.1 Silent thin-extraction threshold — FIXED 2026-09-02

`extract()` discarded any page yielding fewer than 30 words, and said nothing
about it. `fly.io/about` (249,893 bytes, the entire team roster) extracted to
**29 words** and vanished, so `has_team_page: False` for fly.io was an
ingestion artifact rather than a fact about the company.

**Correction to the earlier diagnosis.** This document previously guessed the
content was "probably client-rendered". It is not. **[verified]** The roster is
plain HTML; `favor_precision=True` was throwing it away. Same page, same bytes,
recall mode: **316 words**. The defect was ours, not the site's.

Fixed by escalating instead of thresholding once (ADR-0011): precision, then
recall, then a `selectolax` DOM fallback, keeping the first result that clears
`MIN_WORDS` and recording which pass produced it on `Document.extract_reason`.
Every outcome now has a reason code, including the two failures
(`thin` vs `empty`), and dropped pages are recorded on
`Prospect.page_outcomes` as `thin_extraction` rather than disappearing (A5).

Measured before/after, both crawls run within minutes of each other on
2026-09-02 so that live-site drift is controlled for:

| | fly.io before | fly.io after | thoughtbot before | thoughtbot after |
| --- | --- | --- | --- | --- |
| `has_team_page` | `False` | **`True`** | `True` | `True` |
| pages recovered | — | 2 | — | 1 |
| pages dropped silently | unknown | **0, all recorded** | unknown | **0, all recorded** |

The two recovered fly.io pages displaced two blog posts from the fixed 40-page
budget (`/blog/kamal-in-production`, `/blog/youre-all-nuts`); on thoughtbot one
playbook page displaced another. That is the crawl budget working as designed —
recovered pages compete for slots like any other — and the fly.io trade, a team
roster for two blog posts, is the right way round for the four questions.

One consequence worth naming: `technical_roles_named` for thoughtbot moved
33 → 23. That is **not** a signal defect. It is the displaced page
(`/playbook/.../apprenticeship`) taking its role mentions with it, confirmed by
diffing the document sets of the two same-moment crawls.

### 1.1a fly.io serves a shuffled roster — FIXED 2026-09-02

`fly.io/about` emits its team roster in a different order on every request.
Four consecutive fetches: 316 words each, **four different `content_hash`
values, one identical word multiset**. `/team` redirects to `/about` and shares
that multiset. Exact hashing therefore reported a change on every crawl of an
unchanged page (breaking A7) and kept both URLs as separate documents.

Fixed by ADR-0013. `Document` gains `stable_hash` — sha256 over the text's
words sorted — and deduplication keys on it. `content_hash` stays exact, so a
reordering is still visible rather than hidden.

**[verified]** after the fix, four fetches of `fly.io/about` gave four
different `content_hash` values and one `stable_hash`, `6cc2140dbf3a5a13`,
identical across separate crawl runs. fly.io drops 40 → 39 documents with
`duplicate_content 1`, and the freed budget slot goes to another page.

Two things this cost, both recorded honestly:

- **A collision mode.** "5 engineers and 2 designers" and "2 engineers and 5
  designers" share a word multiset. **[verified]** across the 78 documents of
  the two validation crawls, `stable_hash` produced exactly one collision
  group and it was the genuine duplicate — zero false positives. Evidence, not
  proof. Word level is what survives a shuffle: bigrams and shingles do not,
  and this page has no lines or blocks to sort instead (trafilatura returns all
  316 words on one line; the DOM has one top-level block).
- **A regression, caught by running it.** Keying dedup on `stable_hash` flipped
  fly.io's `has_team_page` `True` → `False`, because `has_team_page` matches
  the URL *path* and `/team` was the URL dropped. The text survived, the proof
  did not. Fixed with `Document.duplicate_urls`: dedup keeps the dropped URLs
  as aliases and signals read them. Which URL wins dedup is an accident of
  crawl order and must never decide a signal.

### 1.1b `people_listed` is 0 for a page that lists people — FIXED 2026-09-02

`fly.io/about` lists **57** people by name and role, and `people_listed`
reported **0**. fly.io styles its roster entirely with Tailwind utility classes
— `<figure>` inside a grid `<div>` — and the words `team`, `member`, `staff`,
`person`, `bio` and `profile` appear nowhere in the markup, so the class-based
count had nothing to match.

Fixed by ADR-0014: count repeated sibling elements whose text reads like a
person entry (2–20 words starting with two or more capitalised words, at least
three siblings), and fall back to the class-based count when that finds
nothing. Class names are a site's private vocabulary; repetition is not.

**[verified]** against hand-counted truth:

| Page | Truth | Structural | By class |
| --- | --- | --- | --- |
| fly.io/about | 57 | **57** | 0 |
| thoughtbot.com/team | 54 | **54** | 54 |
| basecamp.com/about | 0 | 0 | 0 |

basecamp is the negative control — `/about/team` redirects to a narrative page
with no roster, so 0 is correct rather than a miss.

**The single-name roster is now counted too — FIXED 2026-09-03.**
`buttondown.com/about` lists **14** people by first name only, and returned 0
from both strategies for two stacked reasons: the names are one word each, so
`PERSON_NAME_RE` matches none of them; and `/about` never matched
`TEAM_PATH_RE`, so `count_people` was not called on the page at all. Fixing
the count alone would have changed nothing.

Fixed by ADR-0018, and *not* by loosening the name pattern. The objection
recorded here was right that accepting one capitalised word counts every
navigation item — and capitalisation turns out not to be the discriminator it
looks like, because "Features" and "Pricing" are capitalised too.
`count_people_by_portrait` keys on something a navigation item does not have:
one **distinct** image per repeated sibling. A menu repeating one icon matches
nothing; a roster giving every person a different photograph matches exactly.
Dropping the name pattern rather than relaxing it is what makes the count 14
and not 13 — one of the fourteen is listed as `nickd`, lowercase.

`ABOUT_PATH_RE` admits an `/about` page as the team page, but only when
`MIN_ROSTER` people are actually found on it. basecamp stays `False`.

**[verified]** against hand-counted truth. All four pages are committed under
`tests/fixtures/` exactly as fetched, and this table is now asserted on every
test run rather than being a claim about a live fetch nobody could repeat:

| Page | Truth | Structural | By class | By portrait | Reported |
| --- | --- | --- | --- | --- | --- |
| fly.io/team | 57 | **57** | 0 | 0 | **57** |
| thoughtbot.com/team | 54 | **54** | 54 | 54 | **54** |
| buttondown.com/about | 14 | 0 | 0 | **14** | **14** |
| basecamp.com/about | 0 | 0 | 0 | 0 | **0** |

**A third defect, found by running the fix.** **[verified] 2026-09-03:**
buttondown publishes its newsletter archive under `/people/archive/…`, and
**21** of those pages match `TEAM_PATH_RE` while `/about` does not. The old
rule took the first matching page in document order, so it reported
`has_team_page: True` with `team_page_url` pointing at a newsletter archive
and `people_listed: 0` — a confident, cited, wrong answer, on a site nobody
had crawled yet. `choose_roster_page()` now ranks candidates (most people
wins, `/team` breaks a tie, then the URL) instead of taking the first.

**Known limitation, deliberate:** a marketing grid of feature cards — distinct
illustration, short caption, four of them — matches the portrait shape. It is
bounded rather than prevented: the pass runs only where the other two found
nothing, and only on a page already identified as a roster page. See ADR-0018.

### 1.1c Deduplication picked `kind` by crawl order — FIXED 2026-09-02

`Document.kind` comes from the URL path, and deduplication kept whichever copy
was crawled first, so both `source_url` and `kind` were an accident of queue
order. A page reachable at `/handbook/x` and `/careers/x` carried whichever
classification happened to be fetched first, and that could change between runs
as a site's own links change.

Fixed by ADR-0015: `canonical_document()` picks `min(group, key=url)`, so the
same page yields the same canonical URL on every crawl.

**This is a determinism fix, not a semantic one.** It deliberately does *not*
prefer a more specific `kind`, because there is still no measurement saying
which URL should win — and after the segment-matching fix, thoughtbot's two
`career-paths` URLs both classify `website` and agree. Inventing a winner would
be an assumption dressed as a rule.

**The conflict is no longer invisible — FIXED 2026-09-03.** A disagreement used
to be recorded in exactly one place: a human-readable `detail` string on a
`duplicate_content` page outcome. The surviving document — the one that carries
`kind` into the database and into retrieval weighting (ADR-0004) — looked
exactly like a document whose classification was never in question. "We chose
this" and "we picked one of two" were the same value, which is what A4 exists
to prevent.

Fixed by ADR-0019: the losing kinds ride on `Document.kind_conflicts`, and the
crawl prints the conflict in its run summary on the run that produces it.
`deduplicate()` was extracted from `ingest()` so the branch could be tested at
all — it was reachable only through a live crawl of a site that happened to
have the defect, so it had never once executed.

**The first instance, [verified] 2026-09-03.** The very next crawl found one:

```
kind?:  https://buttondown.com/ is website, also classified job_posting
```

`https://buttondown.com/refer/jobs` is a **referral link**. It serves the
homepage byte for byte, and `jobs` is a referral code, not a statement about
the content — the sibling aliases are `/refer/people` and `/refer/Equipo`.

This is evidence against the rule that looked obvious. Preferring the more
specific `kind` would have classified buttondown's homepage as a job posting
and given it job-posting weight at retrieval time. `min(url)` was right, by
determinism rather than by knowing anything.

**Still open:** which URL should win. One instance is not a rule, and a rule
written from one case is an assumption with a citation attached. But it points
the same way as the measured failure behind ADR-0015, where substring matching
over-classified two thoughtbot playbook articles as job postings: both errors
run towards too much specificity, and neither runs the other way. The next
instance will announce itself instead of having to be excavated.

### 1.6 Publication dates are largely htmldate's fallback, not real dates

**[verified] 2026-09-03.** Across the 76 documents of the two validation
crawls:

| `published` value | documents |
| --- | --- |
| exactly `2026-01-01` | **31** |
| absent (`null`) | 9 |
| everything else | 36 |

A date shared by 31 documents across two unrelated sites is not 31 publication
dates; it is htmldate's coarse fallback reaching for a year boundary. The
crawler stores what the extractor returned, and the loader stores that exactly
without repair, because A4 forbids substituting a plausible guess for a bad
measurement — only one of the two is detectable afterwards.

Three things currently rest on it, and none should be trusted without checking:

- `latest_post_date`, a computed signal, which is `max(published)`.
- The chunk provenance header (`title · kind · published`, ADR-0005), which
  embeds the date into every chunk.
- Any future recency weighting.

Not fixed here. The candidate fix is to prefer a date parsed from the URL path
or the visible byline over htmldate's guess, and to record which source
supplied it — but that is a change to `ingest.py` and it invalidates the frozen
fixtures, so it belongs in its own change with a re-crawl.

### 1.2 Failure classification — FIXED 2026-09-02

Only robots.txt outcomes had reason codes. Every other failure collapsed into a
bare `None`, so a prospect with zero documents read identically whether the
domain did not resolve, the site blocked us, or the company has no website —
and the run exited 0.

Fixed by ADR-0012. Every URL the crawl touches gets one `PageOutcome(url,
outcome, http_status, detail)` on `Prospect.page_outcomes`. The outcome strings
**are** the `page_outcome` enum in the migration, and a unit test reads the
migration and asserts the two sets are equal, so they cannot drift. `main()`
now exits 1 on a prospect with no documents and prints why.

The enum in that migration was described here as "proposed, not validated". It
is now validated: eight of its ten values were observed in live runs, the other
two are covered by unit tests, and none of the ten turned out to be
unreachable or redundant.

**[verified] 2026-09-02:**

| Outcome | Evidence |
| --- | --- |
| `stored` | thoughtbot 38, fly.io 40 |
| `skipped_robots` | thoughtbot 1 (`/people`) |
| `http_error` | thoughtbot 18, fly.io 19 |
| `non_html` | fly.io 2 (`/jobs/feed.xml`) |
| `duplicate_content` | thoughtbot 2 |
| `budget_exhausted` | thoughtbot 8, fly.io 36 |
| `dns_failure` | 26/26 on a non-resolving domain, exit 1 |
| `transport_error` | 26/26 on `127.0.0.1:9`, exit 1 |
| `timeout` | unit test |
| `thin_extraction` | unit test; 0 on both live sites since ADR-0011 |

Two things worth carrying forward:

- **`dns_failure` is confirmed, not guessed.** httpx flattens `socket.gaierror`
  into a plain `ConnectError` and drops the cause, so a dead name and a refused
  connection differ only by an errno string. `host_resolves()` asks the resolver
  instead, and returns `True` when it cannot tell — claiming a DNS failure
  without evidence is the invented measurement A4 forbids.
- **The first version of the explanation blamed the wrong thing.** It checked
  `robots_reason` first and reported a non-resolving domain as "robots.txt could
  not be fetched at all" — a symptom presented as a cause. Transport failures now
  outrank robots.txt. Found by running it, not by reading it.

### 1.3 No fast-fail on an unreachable host — FIXED 2026-09-02

A domain that does not resolve cost **26 attempts and 39.5 seconds**
**[verified]**, and printed nothing between the robots line and the summary,
because the crawl loop only prints when it stores a document. It read as a
hang. The DNS lookup itself fails instantly; all 39 seconds were our own
`DELAY_SECONDS` politeness, extended 26 times to a host that does not exist.

Fixed by ADR-0016. A transport failure on robots.txt — the first request of
every crawl — aborts before any seed path is tried when the host does not
resolve or refuses the connection. A host that *hangs* is bounded instead by
`UNREACHABLE_STREAK` (3) consecutive transport failures with nothing fetched. A
5xx robots.txt aborts once as `aborted_robots` rather than skipping 26 URLs one
at a time.

**[verified]** before and after:

| Case | Before | After |
| --- | --- | --- |
| non-resolving domain | 26 attempts, 39.5 s | **1 attempt, 0.25 s** |
| refused connection | 26 attempts | **1 attempt, 0.24 s** |
| host that hangs | ~24 × `TIMEOUT` **[computed]** | 3 × `TIMEOUT` |

Both still exit non-zero with a reason. Healthy crawls are unchanged.

**Correction to the earlier estimate.** This entry previously gave the cost as
"roughly 24 seed paths × 20 s ≈ 8 minutes", marked **[computed]**. That figure
describes the *hanging* host, not the non-resolving one — a dead name fails
fast, and its real cost was 39.5 s of self-inflicted delay. Both cases are now
bounded, but they were never the same case.

`Prospect.crawl_outcome` records how the crawl ended, in the `crawl_outcome`
vocabulary the schema already had. `crawl_runs.outcome` exists to receive it,
so no migration was needed.

**Note on what was deliberately not done:** the delay is still applied to
failed requests. Skipping it would have cut the 39.5 s on its own, but it
removes backoff exactly when a host may be struggling, which is when it matters
most (A6).

### 1.4 Page-kind misclassification — FIXED 2026-09-02

`KIND_PATTERNS` matched `careers?` anywhere in a path, so
`/playbook/our-company/career-paths` was classified `job_posting`, as was
`/playbook/strategy/design-sprints/02-pre-product-validation/jobs-profile`.
**[verified]** on thoughtbot.com.

It never inflated role counts — `_count_open_roles` counts role identity, not
pages (ADR-0003) — but `kind` is denormalised onto `chunks` specifically for
source weighting (ADR-0004), so a playbook article would have carried
job-posting weight into retrieval as soon as weighting shipped.

Fixed by ADR-0015: patterns are anchored and matched against whole path
segments. **[verified]** thoughtbot `job_posting` documents 4 → 2, the two
remaining being `/jobs` and `/jobs/compensation`, both genuinely under the
careers section. fly.io unchanged at 3. Signals still match ground truth on
both sites.

### 1.5 `classify(url, title)` ignored its `title` argument — FIXED 2026-09-02

The parameter is **dropped**, not wired up, and the measurement is why.

**[verified]** across both validation corpora, the only three pages whose
`<title>` matches career/job/hiring words are thoughtbot playbook *articles* —
"Career Paths | thoughtbot's Playbook", "Jobs Profile | ...", "Hiring | ...".
Three false positives, zero true positives. Using the title would have
reintroduced the very misclassification §1.4 just removed.

The reasoning is in `classify()`'s docstring as well as here, so that nobody
re-adds it on the reasonable-sounding theory that a title saying "Careers"
means a careers page.

---

## 2. Assumptions that need verification

Each of these appears in the documentation marked as an assumption. This is the
list of how to check them.

| # | Assumption | Where | How to verify |
| --- | --- | --- | --- |
| ~~2.1~~ | ~~Exact vector search is fast enough~~ | ADR-0001 | **[verified] 2026-09-03** — median **0.70 ms**, p95 **0.76 ms** over 30 runs on 111 chunks. See below; the candidate set is smaller than assumed. |
| 2.2 | 800–1200-token chunks beat 400 on synthesis questions | ADR-0005 | Recall@k and faithfulness per configuration, on the ground-truth set. The most important unverified assumption in the design |
| ~~2.3~~ | ~~Job postings fit in one chunk~~ | ADR-0005 | **[verified] 2026-09-02** — the largest in the corpus, `fly.io/jobs/networking-engineer`, is **1,493 tiktoken tokens**. Above ADR-0005's guessed "typically under 1500", and about 5× under the 8191-token embedding limit, so the never-split rule holds with room. |
| 2.4 | Quota split 0.45 / 0.30 / 0.25 matches where the four questions are answered | ADR-0007 | Recall per question across corpora crawled at different splits |
| 2.5 | Naive vector search is insufficient, worst on question 4 | ADR-0009 | **Partly measured 2026-09-03 — see below.** Insufficiency confirmed; "worst on question 4" not confirmed, it was worst on question 2. |
| 2.6 | A frozen corpus stays valid for about a quarter | evaluation.md §3 | Re-crawl and diff content hashes |
| 2.7 | 48 pairs can detect a 10-point recall change | evaluation.md §7 | Not computed. Do the power calculation, or accept it as a rough set and say so |
| 2.8 | `ragas` faithfulness handles computed signals in the context sensibly | evaluation.md §7 | Run one pair with and without signals injected and compare |
| 2.9 | Pinned dependency versions install and work together | pyproject.toml | See §3.1 — several are resolved from PyPI but never installed |
| 2.10 | The signal set will stabilise, at which point queried fields leave JSONB | architecture.md §3.1 | The first time a signal appears in a `WHERE` clause |
| 2.11 | No fifth bug of the same class remains in `ingest.py` | ADR-0010 | There is no harness. The five defects above argue against this one |

---

### 2.1 (detail) Exact search is fast, and the candidate set is smaller than assumed

**[verified] 2026-09-03**, running ADR-0009's frozen query against a live
database with all 154 chunks carrying 1536-dimension vectors.

| | |
| --- | --- |
| chunks per prospect | fly.io **111**, thoughtbot **43** |
| median latency, 30 runs | **0.70 ms** |
| p95 | **0.76 ms** |
| max | 0.83 ms |
| `EXPLAIN` execution time | 0.263 ms |

ADR-0001 holds comfortably. Not adding an HNSW index was the right call, and it
is now a measurement rather than a judgement.

Two things the measurement corrected, both of them expectations of mine:

**The candidate set is smaller than `docs/architecture.md` assumed.** It said
"hundreds of chunks" per prospect. The real numbers are 111 and 43. That makes
ADR-0001 *stronger* than it claimed — the case for exact search is better at
111 rows than at several hundred — but the document was stating a guess in the
voice of a fact, and it is corrected.

**Postgres chooses a sequential scan, not the `prospect_id` index.** The plan
is `Seq Scan on chunks, Filter: prospect_id = 417, Rows Removed by Filter: 43`.
I had written into the plan for this slice that the verification should "expect
an index scan, no Seq Scan". That expectation was wrong: at 154 rows a
sequential scan is genuinely cheaper, and the planner is right. The index
earns its place when the table is large enough for it to, and asserting on the
plan shape at this size would be pinning an accident.

**What this does not establish.** 154 rows is a toy. `docs/ground-truth.md`
specifies twelve prospects; at the observed rate that is roughly 1,000–1,300
chunks total, still far inside exact search's comfort. The number to watch is
chunks *per prospect*, because that is what the filter leaves behind, and it is
around 100. ADR-0001's trigger for revisiting — a filtered candidate set large
enough that exact search shows up in p95 — is nowhere near.

---

### 2.5 (detail) The first real evidence that ranking, not ingestion, is the problem

**[verified] 2026-09-03**, on fly.io's 111 chunks, embedded locally with
`BAAI/bge-small-en-v1.5` (384 dimensions) because no OpenAI key was configured.

The team roster — `fly.io/about`, 57 named people with job titles, one chunk —
is what question 2 asks for. Where it ranks:

| question as put to the system | roster rank | score spread |
| --- | --- | --- |
| `"What evidence is there of in-house technical capacity?"` | **110 of 111** | 0.146 |
| `"who works here, employees and their roles"` | **4 of 111** | 0.235 |

The top hit for the first phrasing is a blog post about calibrating trust in AI
software. The right chunk is in the corpus — it is the same page ADR-0011 was
written to recover — and the ranking puts it second from last.

This is A8 stated as a measurement rather than a principle: the first hypothesis
for a wrong answer is that the right chunk was never retrieved, and here it was
retrieved 110th. It is also precisely the trigger ADR-0009 names for adopting
hybrid search — recall@5 below 0.8 on a question, with manual inspection showing
matter present in the corpus that vector search ranked outside the top 5.

**The likely cause is vocabulary.** The page says names and titles — "Developer",
"Support Engineer". It never says "capacity", "in-house" or "evidence". The
score spread collapses accordingly: 0.146 across all 111 chunks for the
project's phrasing against 0.235 for the page's own, so with the question as
written almost nothing is distinguishable from anything else.

**A hypothesis this measurement killed.** The provenance header added in
ADR-0005 (`title · kind · published`) looked like the culprit, especially with
htmldate's junk dates visible inside it — one chunk carries `1998-01-01`.
Re-embedding every chunk with the header stripped leaves the roster at **110**.
It widens the spread slightly (0.146 → 0.191) and moves nothing. The header is
not the problem.

**What this does NOT establish**, and the distinction matters before anyone
ships a fix:

- It is **one embedding model**, and not the configured one. `bge-small` is 384
  dimensions; the schema and ADR-0009 assume `text-embedding-3-small` at 1536.
  A different model could rank differently.
- It is **one phrasing of one question on one prospect**. Four questions across
  twelve prospects is what `docs/ground-truth.md` specifies, and none of it is
  written.
- It says nothing about **which** fix works. Hybrid search is the natural
  candidate and ADR-0009 puts it first, but "a lexical term was missed" is a
  diagnosis reached by eye, not a measured comparison.

So this is evidence that the harness is worth building, not a licence to skip
it. Under A3 the fix ships with a recorded before-and-after, and the "before"
does not exist yet.

**Method note.** A first pass at this measurement reported "27 roster chunks" and
was wrong: it matched `/about` as a substring, catching `/docs/about/pricing`
and friends. The roster is a single chunk. The numbers above are from the
corrected run — recorded because a plausible-looking measurement that is quietly
wrong is the failure mode this document exists to prevent.

---

## 3. Undecided

### 3.1 Dependency pins are resolved but unexercised

`pyproject.toml` pins exact versions with mixed evidence behind them.

**[verified]** installed in `.venv` and exercised:

| Package | Version | Exercised by |
| --- | --- | --- |
| httpx | 0.28.1 | live crawls of 18 domains |
| trafilatura | 2.2.0 | live crawls |
| selectolax | 0.4.11 | live crawls, signal counting |
| pytest | 9.1.1 | `make test` — 31 passed, 1 xfailed |
| pytest-asyncio | 1.4.0 | the integration test |
| ruff | 0.16.5 | `make lint` — clean |
| asyncpg | 0.31.0 | the A1 isolation test against a live database |

**[verified]** the schema applies: `migrations/0001_initial_schema.sql` was
applied on 2026-09-02 against `pgvector/pgvector:pg17`, which resolved to
PostgreSQL 17.11 with pgvector extension **0.8.6** — above the 0.7.0 the
`halfvec` column needs and above the 0.8.0 `hnsw.iterative_scan` would need if
ADR-0001 is ever reversed.

**[verified] 2026-09-02.** `make install` resolved the full tree in one pass —
every pin is satisfiable together, and each was the current release on PyPI at
the time. Now installed and importable:

> **One pin is not in that tree, and does not resolve at all.**
> **[verified] 2026-09-05**, `make install-eval`: `ragas==0.4.3` cannot be
> installed alongside `openai==3.7.0`. ragas depends unconditionally on
> `instructor`, and the newest `instructor` (1.16.0) requires
> `openai>=2.0.0,<3.0.0`. Not fixable by choosing another ragas — **0.4.3 is
> the latest release**, and ragas itself asks only for `openai>=1.0.0`; the
> ceiling comes from instructor.
>
> This is the fourth finding of the same shape on this page, and the clearest
> one. `RESOLVED` here has always meant "exists on PyPI", and that is exactly
> as much as it was worth: the `[eval]` extra was never installed, so nobody
> found out. See ADR-0020, and the extra is now marked BROKEN in
> `pyproject.toml` rather than left looking optional.

| Package | Version | First checked |
| --- | --- | --- |
| sqlalchemy | 2.0.52 | imports |
| pgvector | 0.5.0 | `HALFVEC`, `register_vector` |
| openai | 3.7.0 | `AsyncOpenAI` |
| tiktoken | 0.14.0 | `encoding_for_model("text-embedding-3-small")` → `cl100k_base` |
| pydantic | 2.13.5 | imports |
| pydantic-settings | 2.15.0 | `BaseSettings` |
| fastapi | 0.141.1 | imports |
| langfuse | 4.15.1 | imports |

**`pgvector.asyncpg.register_vector` does register `halfvec`** — read the
function, do not grep the module; a first check that searched the module source
for the string reported a false negative. It registers `vector` unconditionally,
then `halfvec` and `sparsevec` **inside a `try/except ValueError` that swallows
`unknown type:`**. So against a server whose pgvector extension is too old,
halfvec registration is skipped *silently* and every vector round-trips wrong
with no error raised. The extension here is 0.8.6, well above the 0.7.0 halfvec
needs, but the silent-skip shape is why the round-trip test is a requirement and
not a formality.

**[verified] 2026-09-02, by exercising rather than importing.** Two defects
surfaced the moment the halfvec round-trip test ran, neither of which an import
check could have found:

- **`sqlalchemy==2.0.52` was the wrong pin.** The async engine needs `greenlet`
  at runtime, and the bare package does not depend on it. `import sqlalchemy`
  succeeds; the first `await` raises *"the greenlet library is required"*. The
  pin is now `sqlalchemy[asyncio]==2.0.52`.
- **Registering the asyncpg vector codec breaks the SQLAlchemy path.** The
  obvious `connect`-event call to `pgvector.asyncpg.register_vector` produced
  `invalid input for query argument $7: '[0.0,...]' (expected list or ndarray)`
  on every insert. `pgvector.sqlalchemy.HALFVEC` already serialises to
  pgvector's text form; the asyncpg codec then tries to binary-encode a string.
  They are alternatives, not layers. `linestack/db.py` registers nothing and
  carries the reasoning, because "register the codec" is what anyone would
  reach for next.

With both fixed, a 1536-dimension halfvec round-trips exactly and cosine
ordering is correct against a live PostgreSQL 17 / pgvector 0.8.6.
`sqlalchemy`, `pgvector`, `greenlet` and `asyncpg` are therefore **exercised**,
not merely installed. Also measured: halfvec is float16, so a value like
`0.1234567` returns rounded — far below what cosine ranking distinguishes, and
now written down in `tests/test_db_integration.py` rather than discovered later.

**Still [assumed]:**

- **`ragas`** is in the optional `eval` extra and was NOT installed by
  `make install`. Its API changes across releases, exactly as the brief warns,
  and the metric names in `docs/evaluation.md` come from the library's general
  shape rather than that version's documentation. **Verify before writing
  `linestack/evaluation/metrics.py`.**
- **`langfuse`** the Python SDK and the self-hosted server images in
  `docker-compose.yml` are separate version lines. Imported, never connected.
- Every package above is **imported, not exercised**. Importing `AsyncOpenAI` is
  not evidence that `client.embeddings.create` has the signature this project
  assumes, and importing `HALFVEC` is not evidence that a 1536-dimension vector
  survives a round trip. Those become **[verified]** when the round-trip and
  embedding tests pass, not before.

### 3.2 Text-search configuration: `simple` or `english`

`chunks.content_tsv` is generated with the `simple` configuration. The crawler's
seed paths include `/nosotros`, `/equipo`, `/empleos`, so a Spanish-language
prospect is expected, and `english` stemming applied to Spanish is worse than no
stemming. `simple` gives up stemming for everyone.

The alternatives, none chosen:

- Detect the document language during ingestion, store it, and generate the
  tsvector per language. Correct, and it means the generated column becomes a
  function of a column that ingestion has to get right.
- Two tsvector columns, one per configuration.
- Accept `simple`, and rely on the vector side of hybrid search for semantic
  matching.

This must be resolved **before** hybrid search ships (ADR-0009 step 1), because
it determines what lexical search can match. It does not block anything before
that.

### 3.3 Row-level security for A1

Postgres RLS with a session-local `app.prospect_id` GUC would make prospect
isolation unbypassable even from psql. Not adopted (architecture.md §4.3): a
misconfigured RLS policy fails silently and permissively on pooled connections,
and a security control that can silently stop working is worse than a constraint
that cannot.

Adopt it if chunk-returning SQL is ever written outside
`linestack/retrieval/scope.py`, or on the first multi-tenant deployment. If it is
adopted, it must come with a test that asserts a query without the GUC set
returns zero rows — an RLS policy with no negative test is decoration.

### 3.4 Where `ingest.py` lives

It is at the repository root, outside the package, because it predates the
package (ADR-0010) and because moving working, debugged code during a
documentation pass buys a directory layout at the cost of risk. The move is
gated on fixture-based unit tests for `count_people`, `_count_open_roles`, and
the crawl-budget ordering — some of which now exist in
`tests/test_ingestion_units.py`.

`ingest.py` is also in `tool.ruff.extend-exclude`, for the same reason:
formatting it would rewrite the one module in the repository with measured
behaviour, in exchange for nothing. That exclusion is a deferral, not a
judgement that the file should stay unformatted, and it is removed when the
module moves. It does mean the file is currently unlinted, so a real defect
there will not be caught by CI.

### 3.5 Re-crawl cadence and change detection

`documents.content_hash` makes it cheap to detect that a prospect's site changed
(A7). Nothing decides *when* to look. Undecided: on demand only, scheduled, or
triggered by the age of `crawled_at` at query time. This affects §3.1 of
`docs/evaluation.md`, since the frozen evaluation corpus and the live corpus
would then diverge on purpose.

---

## 4. Objections to the axioms

Recorded as the brief requires. None of these changes what was built; all nine
axioms were followed as specified.

### 4.1 A2 — "if it is computable, do not infer it" understates the cost of computing

A2 is right about the failure mode: a model asked to count produces a plausible
number with no uncertainty signal. But the record so far is that computing the
same fact produced **three wrong answers on the first two companies tried** —
162 people against 54, 4 open roles against 0, 3 open roles against 2 — and each
took a debugging session against live HTML to fix.

The difference that justifies A2 is not that computing is more accurate; it is
that computing is wrong *systematically*, and a systematic error is findable,
fixable, and stays fixed. A model's error is different every time and there is
nothing to fix. That argument is stronger than "do not infer what you can
compute", and ADR-0003 is written on it.

The practical consequence: **a computed signal needs a confidence or reason code
as much as an inferred one does**. `has_team_page: False` currently means both
"there is no team page" and "the extractor dropped the page at 29 words" (§1.1).
A2 as written does not require distinguishing those, and A5 does. Where they meet
is where the current code is weakest.

### 4.2 A9 versus A1 on row-level security

A9 says no infrastructure without a measurement that justifies it. A1 says
prospect isolation outranks latency, cost, and code elegance. RLS is
infrastructure that only serves A1, and there is no measurement that could
justify it in advance — the measurement would be a leak that has already
happened, which A1 says is unacceptable.

Resolved in favour of A9 for now, on the grounds that the composite foreign key
(architecture.md §4.1) provides a database-enforced guarantee at no operational
cost, so RLS would be a second belt on the same trousers. Recorded because the
tension is real and the resolution depends on the composite key actually holding.

### 4.3 A3's build order was already violated, and the violation was productive

`ingest.py` was written before any documentation, schema, or harness existed.
Four measured bugs were found in it, all four by running it against live sites,
none by reading it. That is evidence *for* A3's underlying claim — behaviour is
only knowable by measuring — while also being a case where writing the code
first is what produced the measurements.

The honest reading: A3's build order is right about *retrieval improvements*,
where a change with no baseline cannot be evaluated. It is weaker as a rule about
ingestion, where the fastest way to learn what a component does is to run it.
Recorded in ADR-0010; the order is not being re-opened for the retrieval path.
