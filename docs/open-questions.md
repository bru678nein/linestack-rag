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
to: §1.1b cannot see a roster of single-word names, and §1.1c does not yet know
which URL should win a genuine `kind` disagreement.

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

**Still missed, [verified]:** `buttondown.com/about` lists its team by first
name only ("Anita", "Ben", "Justin"). Both strategies return 0. Relaxing the
pattern to a single capitalised word would count every navigation item
("Features", "Pricing"), so this is recorded rather than papered over. A
single-name roster is invisible to both strategies today.

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
be an assumption dressed as a rule. A genuine disagreement is now recorded on
the `duplicate_content` outcome as `kind conflict X vs Y`, so the measurement
will exist when one occurs (A3).

**Still open:** which URL should win a real `kind` disagreement. No instance
observed on either validation site.

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
| 2.1 | The prospect-filtered candidate set is in the hundreds of chunks, so exact vector search is fast enough | ADR-0001 | Count chunks per prospect after the first real load; `EXPLAIN (ANALYZE)` the filtered query at p95 |
| 2.2 | 800–1200-token chunks beat 400 on synthesis questions | ADR-0005 | Recall@k and faithfulness per configuration, on the ground-truth set. The most important unverified assumption in the design |
| 2.3 | Job postings fit in one chunk | ADR-0005 | Token-count the postings in the frozen corpus |
| 2.4 | Quota split 0.45 / 0.30 / 0.25 matches where the four questions are answered | ADR-0007 | Recall per question across corpora crawled at different splits |
| 2.5 | Naive vector search is insufficient, worst on question 4 | ADR-0009 | The first harness run |
| 2.6 | A frozen corpus stays valid for about a quarter | evaluation.md §3 | Re-crawl and diff content hashes |
| 2.7 | 48 pairs can detect a 10-point recall change | evaluation.md §7 | Not computed. Do the power calculation, or accept it as a rough set and say so |
| 2.8 | `ragas` faithfulness handles computed signals in the context sensibly | evaluation.md §7 | Run one pair with and without signals injected and compare |
| 2.9 | Pinned dependency versions install and work together | pyproject.toml | See §3.1 — several are resolved from PyPI but never installed |
| 2.10 | The signal set will stabilise, at which point queried fields leave JSONB | architecture.md §3.1 | The first time a signal appears in a `WHERE` clause |
| 2.11 | No fifth bug of the same class remains in `ingest.py` | ADR-0010 | There is no harness. The five defects above argue against this one |

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
