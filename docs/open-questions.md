# Open questions, assumptions, and known defects

Read this before building anything on top of the current code.

Three sections:

1. **Known defects** — measured, reproducible, broken. Fix these first.
2. **Assumptions that need verification** — stated in the docs as assumptions,
   listed here with how to check each one.
3. **Undecided** — genuinely open, including objections to the axioms.

---

## 1. Known defects

All **[verified]** against live sites on 2026-09-01/02. §1.1 is fixed;
§1.1a and §1.1b are new defects that fixing it exposed.

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
`Prospect.dropped_pages` rather than disappearing (A5).

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

### 1.1a fly.io serves a shuffled roster — breaks A7 idempotency [verified]

Surfacing `fly.io/about` exposed a new defect. The team roster is emitted in a
different order on every request. Three consecutive fetches, 316 words each,
three different content hashes:

```
fetch 1: hash=42ef59e449083838 words=316
fetch 2: hash=ec8e0c0528c27b35 words=316
fetch 3: hash=7c10735938aac23d words=316
```

Two consequences:

1. **A7 is violated for this page.** Re-crawling produces a "changed" document
   that has not changed. The earlier idempotency check passed only because this
   page was being dropped entirely.
2. **Near-duplicate dedup fails.** `/about` and `/team` are the same page
   (`/team` redirects), but their hashes differ, so `content_hash` dedup keeps
   both. Two budget slots and two near-identical chunk sets for one page.

Not fixed here — it needs a decision on whether `content_hash` should be
order-insensitive, or whether shuffled content should be normalised before
hashing, and both have consequences beyond this one page.

### 1.1b `people_listed` is 0 for a page that lists people [verified]

With `fly.io/about` now in the corpus, `has_team_page` is `True` but
`people_listed` is `0`, on a page whose text plainly reads "Vincent Charlebois
Developer … Michele Baroody Product Manager". `PERSON_SELECTORS` does not match
fly.io's markup. Previously invisible because the page never survived
extraction. Distinct from the thoughtbot overcount already fixed (§ADR-0003).

### 1.2 Failure classification is incomplete (violates A5)

Only robots.txt outcomes have reason codes (five of them, ADR-0006). Every other
failure collapses into a bare `None` from `PoliteClient.get()` or `extract()`:

| Failure | Current outcome | Needed |
| --- | --- | --- |
| DNS failure | `None` | reason code |
| Timeout | `None` | reason code |
| Non-200 status | `None` | reason code + status |
| Non-HTML content-type | `None` | reason code + content-type |
| Thin extraction (§1.1) | `None` | reason code + word count |

A total failure exits 0. A prospect with zero documents is reported the same way
whether the domain does not resolve, the site blocked us, or the company has no
website.

The schema already has somewhere to put these: `crawl_page_outcomes` in
`migrations/0001_initial_schema.sql`. The crawler does not populate it, and the
enum in that migration is the proposed vocabulary, not a validated one.

### 1.3 No fast-fail on an unreachable host

A domain that does not resolve costs roughly 24 seed paths × 20 s timeout ≈ **8
minutes** before the crawl gives up. **[computed]** from `SEED_PATHS` (24
entries) and `TIMEOUT = 20.0`; consistent with an observed run exceeding 300 s.
The per-path arithmetic is derived, not directly measured.

A single failed resolution or connection to the base URL should abort the run
with a reason code, rather than retrying the same dead host two dozen times.
This interacts with §1.2: without reason codes there is nothing to abort *with*.

### 1.4 Page-kind misclassification carries into retrieval weighting

`KIND_PATTERNS` matches `careers?` anywhere in a path, so
`/playbook/our-company/career-paths` is classified `job_posting`.
**[verified]** on thoughtbot.com.

It no longer inflates role counts — `_count_open_roles` counts role identity,
not pages (ADR-0003) — but `kind` is denormalised onto `chunks` specifically for
source weighting (ADR-0004). A playbook article would carry job-posting weight
into retrieval as soon as weighting ships. Fix before ADR-0009 step 2.

### 1.5 `classify(url, title)` ignores its `title` argument

Classification is path-only. The signature promises otherwise, which is the kind
of thing that gets discovered when someone changes the title-handling and
nothing happens. Either use the title — a `<title>` of "Careers at X" is a
useful signal where the path is not — or drop the parameter.

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

**[assumed]** everything else. The remaining pins were resolved from PyPI on
2026-09-02 as the then-current release and **have never been installed**. In
particular:

- **`ragas`** is pinned because its API changes across releases, exactly as the
  brief warns. The pinned version has not been imported, and the metric names
  and call signatures used in `docs/evaluation.md` are from the library's
  general shape, not from that version's documentation. **Verify before writing
  `linestack/evaluation/metrics.py`.**
- **`langfuse`** (Python SDK, 4.x) and the self-hosted Langfuse server images in
  `docker-compose.yml` are separate version lines. They are believed compatible;
  not tested.
- **`pgvector`** the Python package and the pgvector *extension* in the Postgres
  image are separate version lines. The extension is verified at 0.8.6 above;
  the Python package, which supplies the SQLAlchemy and asyncpg type adapters,
  is not installed. Without it the `halfvec` column round-trips as text.

Run `make install` and update this section with what the full tree resolves to.

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
