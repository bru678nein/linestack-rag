# Architecture

Status of this document: the ingestion path describes code that exists and has
been run against live sites. The query path describes code that does not exist
yet; every statement about it is a design intention, marked as such.

Claims are labelled:

- **[verified]** — measured or observed, with the measurement stated.
- **[assumed]** — believed, with the reason, and not yet measured.
- **[planned]** — a design decision about code that has not been written.

---

## 1. Ingestion path

Implemented in `ingest.py`. One prospect per run. No database writes.

```
base_url
   │
   ▼
PoliteClient                      one client per prospect
   ├── fetch /robots.txt          through OUR httpx client, OUR User-Agent
   ├── classify the outcome       ok | absent | unreadable | server_error | fetch_failed
   └── rate limit                 1.5 s between requests to the same domain
   │
   ▼
crawl loop (budget: 40 pages)
   ├── seed queue: 24 known paths (/, /about, /team, /careers, /blog, …)
   ├── pick next URL by queue_rank(kind) — quota-capped, priority-ordered
   ├── robots check → skipped_by_robots, or fetch
   ├── trafilatura extract  → Document(url, kind, title, text, published, hash)
   └── discover same-domain links that look like content
   │
   ▼
deduplicate by content_hash       /about and /about-us often serve one page
   │
   ├──▶ documents[]               text for chunking and embedding
   └──▶ compute_signals()         deterministic facts
   │
   ▼
prospect_<domain>.json
```

### 1.1 robots.txt is fetched through our own client

`urllib.robotparser.RobotFileParser.read()` is not used. It fetches robots.txt
with urllib's own User-Agent (`Python-urllib/x.y`), which bot protection
commonly answers with 403. `read()` swallows that `HTTPError` internally, sets
`disallow_all = True`, and does not raise. The caller is handed a healthy-looking
parser that denies every URL. The crawl returns zero pages and the run is
recorded as "blocked by robots.txt" for a block the site never declared.

**[verified]** 4 of 18 real software-company domains were affected:
thoughtbot.com, basecamp.com, sourcegraph.com, planetscale.com. After fetching
robots.txt through the project's own `httpx` client and User-Agent: 0 of 18.
thoughtbot.com went from 0 pages to 37 pages; its real robots.txt disallows
exactly one path, `/people`.

Status handling follows RFC 9309 §2.3.1 — 2xx applies the rules, 4xx permits
crawling, 5xx is a full disallow — and the outcome is recorded as one of five
reason codes rather than a boolean (A5, A6). See ADR-0006.

### 1.2 The page budget is allocated, not consumed first-come

A FIFO queue with a flat page cap does not divide the budget between the four
questions; it lets link volume divide it. A blog index links to dozens of posts,
an About page links to none.

**[verified]** page-kind mix at `max_pages=40`:

| Domain | Queue | website | job_posting | blog_post | total words |
| --- | --- | --- | --- | --- | --- |
| fly.io | FIFO | 3 | 3 | 34 | 61,649 |
| fly.io | ratio-balanced *(rejected)* | 7 | 3 | 30 | — |
| fly.io | quota-cap + priority *(current)* | 16 | 3 | 21 | 62,169 |
| thoughtbot.com | FIFO | 22 | 3 | 12 | 17,368 |
| thoughtbot.com | ratio-balanced *(rejected)* | 18 | 4 | 17 | — |
| thoughtbot.com | quota-cap + priority *(current)* | 24 | 4 | 10 | 21,174 |

Under FIFO, fly.io's crawl never reached a team page, so question 2 — evidence
of in-house technical capacity — was unanswerable from a full-budget crawl. That
presents during evaluation as a retrieval failure when it is an ingestion
failure. A8 says the first hypothesis for a wrong answer is that the right chunk
was never retrieved; this is the case one step earlier, where the right chunk was
never *ingested*, and it is why ingestion coverage is reported per page kind.

The rejected intermediate design and the reason it was reverted are in ADR-0007.

### 1.3 Computed signals

`compute_signals()` produces the `Signals` dataclass: `has_team_page`,
`team_page_url`, `people_listed`, `technical_roles_named`, `has_careers_page`,
`open_roles_seen`, `technical_roles_open`, `blog_posts_seen`,
`latest_post_date`, `pages_crawled`, `total_words`.

These are facts a function can establish, so no model is asked for them (A2).
Two of them were wrong in ways that only running against live sites revealed:

- **People counting.** Counting leaf-most elements matching person-ish class
  selectors overcounts, because one card usually holds several matching leaves.
  **[verified]** thoughtbot.com/team renders 54 people as 54 × {person-photo,
  person-info-name, person-info-title} = 162 leaves; the count reported 162
  against a ground truth of 54. Fixed by grouping leaves by signature (tag plus
  the matched class tokens) and taking the largest group. Now returns 54.
- **Role counting.** Counting pages classified `job_posting` counts the careers
  listing as a vacancy alongside the postings it links to, and counts policy
  pages as vacancies. **[verified]** fly.io reported 3 roles / 2 technical
  against a ground truth of 2 / 1; thoughtbot reported 4 / 3 against a ground
  truth of 0 / 0, its four `job_posting` documents being a jobs landing page, a
  compensation calculator, and two internal career-ladder playbook pages. Fixed
  by counting role *identity*: a listing contributes the roles it links to and
  never itself, and a candidate survives only if its name reads like a job
  title. Both now match ground truth.

**[verified]** `extract_role_headings()` returns zero headings on both
fly.io/jobs and thoughtbot.com/jobs. Per-role links from the listing page are
the reliable signal; heading extraction is retained only as a fallback for
listings that name roles inline without linking them, and has never been
observed to fire on a real site.

See ADR-0003.

### 1.4 What ingestion does not do

The crawl writes JSON and stops. Loading into Postgres is a separate step
(ADR-0008), so a crawl can be re-run and diffed without touching the database.
That separate step does not exist yet; `linestack/ingestion/loader.py` is its
destination.

---

## 2. Query path — [planned]

None of this is implemented.

```
POST /prospects/{prospect_id}/ask   { question }
   │
   ▼
ProspectScope(prospect_id)          the only object that can build a chunk query
   │
   ▼
embed(question)                     text-embedding-3-small → halfvec(1536)
   │
   ▼
SELECT … FROM chunks
 WHERE prospect_id = :prospect_id   B-tree index; exact vector search inside it
 ORDER BY embedding <=> :q
 LIMIT :k
   │
   ├── retrieved chunks + scores
   └── prospects.signals            computed facts, always injected
   │
   ▼
answer with citations  ──▶ streamed to the client
   │                   └─▶ Langfuse trace: question, chunk ids, scores, tokens
   ▼
UI renders the answer AND the chunks with their scores
```

The chunks are rendered in the UI because a retrieval failure that is not
visible gets attributed to the model, and then someone spends a week tuning
prompts to fix a chunking bug (A8).

`prospects.signals` is injected into every answer's context regardless of what
retrieval returned. A computed fact does not compete with vector similarity for
a place in the context window.

---

## 3. Schema

The full DDL is `migrations/0001_initial_schema.sql`. This section explains the
shape, not the syntax.

```
prospects ─┬─< documents ─┬─< chunks
           │              │
           │              └── (id, prospect_id) ◀── composite FK from chunks
           └─< crawl_runs ──< crawl_page_outcomes
```

### 3.1 prospects

`(id, company_name, domain UNIQUE, signals JSONB, created_at, updated_at)`

`signals` is JSONB rather than columns because the signal set is still moving —
three of its fields changed definition during crawler debugging. **[assumed]**
It will stabilise; when it does, the fields that are actually queried should
become columns. The trigger to migrate: the first time a signal appears in a
`WHERE` clause rather than only in a prompt context block.

### 3.2 documents

`(id, prospect_id FK, source_url, kind, title, published_at, word_count,
fetched_at, content_hash, UNIQUE (prospect_id, source_url))`

`content_hash` is what makes re-ingestion idempotent (A7): an unchanged hash
means the document's chunks and embeddings are still valid and are not
recomputed. **[verified]** two consecutive crawls of fly.io produced identical
content hashes for all 40 documents.

`kind` is an enum (`website`, `job_posting`, `blog_post`) rather than free text,
because it is used for source weighting and a typo would silently change
retrieval behaviour.

### 3.3 chunks

`(id, document_id FK, prospect_id, kind, chunk_index, content, token_count,
embedding halfvec(1536), embedding_model, content_tsv GENERATED,
UNIQUE (document_id, chunk_index))`

`prospect_id` is denormalised from `documents` because filtering by prospect is
the hot path on every single query and must not require a join. `kind` is
denormalised because a job posting is stronger evidence of technical capacity
than an About page, and weighting by source has to be cheap. See ADR-0004.

`content_tsv` is a stored generated column, present now so the schema does not
have to change when lexical search ships. Its GIN index is *not* created yet:
there is no lexical search to serve, and an index with no query is cost without
benefit (A9). The generated column uses the `simple` text-search configuration,
not `english`, because the crawler's seed paths include Spanish ones
(`/nosotros`, `/equipo`, `/empleos`) and English stemming applied to Spanish text
is worse than no stemming. That choice is unresolved — see `open-questions.md`.

### 3.4 crawl_runs and crawl_page_outcomes

These two tables are an addition to the schema given in the scaffold prompt. The
reason: A5 requires that failures are classified rather than counted, and there
was nowhere to put a reason code. A run records its robots.txt outcome and its
overall result; `crawl_page_outcomes` records one row per URL that was not
turned into a document, with the reason code and the HTTP status where there was
one.

Without these tables, "this prospect has 0 documents" is one number that means
at least six different things (DNS failure, timeout, non-200, non-HTML, thin
extraction, or a genuinely empty site), and the evaluation set would silently
inherit ingestion bugs as though they were facts about the company. That is not
hypothetical — see the `fly.io/about` case in `open-questions.md`.

`ingest.py` does not populate these tables yet. It currently classifies
robots.txt outcomes only; every other failure still collapses into a bare
`None`. That gap is tracked as a known defect.

---

## 4. How A1 is enforced structurally

A1: a chunk belonging to prospect B must never be reachable when answering about
prospect A. Three mechanisms, in decreasing order of strength.

### 4.1 A composite foreign key makes a mismatched chunk unrepresentable

`documents` carries a redundant `UNIQUE (id, prospect_id)`. `chunks` then
references it with a composite foreign key:

```sql
FOREIGN KEY (document_id, prospect_id) REFERENCES documents (id, prospect_id)
```

A chunk whose `prospect_id` disagrees with its document's `prospect_id` cannot
be inserted. The denormalised column cannot drift from its source, so filtering
on the denormalised column is exactly as correct as joining would have been.
This is enforced by the database, not by application code, and it holds against
manual `INSERT`s, migrations, and future code written by someone who has not
read this document.

**[verified]** 2026-09-02, against PostgreSQL 17.11 with pgvector 0.8.6
(`pgvector/pgvector:pg17`): the migration applies cleanly, an honest chunk
insert succeeds, and an insert of a chunk claiming prospect B against a
document owned by prospect A is rejected with a foreign-key violation.
`tests/test_isolation_contract.py::test_database_rejects_a_chunk_from_the_wrong_prospect`,
run with `make up && make migrate && make test-integration`.

### 4.2 One chokepoint for chunk queries — [planned]

`linestack/retrieval/scope.py` will expose a single object whose constructor
requires a `prospect_id` and which is the only place in the codebase that builds
a query against `chunks`. Every retrieval function takes that object, not a raw
session. A query that does not go through it does not compile, because there is
no other function that returns chunk rows.

This is weaker than 4.1 — it is enforced by review and by a test that greps for
`chunks` outside the scope module, not by the database. It is listed second for
that reason.

### 4.3 Row-level security — [not adopted]

Postgres RLS with a session-local `app.prospect_id` GUC would make the isolation
unbypassable even from a raw psql session. It is not enabled, because a
misconfigured RLS policy fails *silently and permissively* on the pooled
connections SQLAlchemy hands out, and a security control that can silently stop
working is worse than a constraint that cannot. The trigger to revisit: the
first time chunk-returning SQL is written outside the scope module, or the first
multi-tenant deployment. Tracked in `open-questions.md`.

---

## 5. Indexing

| Index | Status | Reason |
| --- | --- | --- |
| `chunks (prospect_id)` B-tree | created | Every query filters on it. |
| `chunks (document_id, chunk_index)` unique | created | Idempotent chunk writes. |
| `documents (prospect_id, source_url)` unique | created | Idempotent document writes. |
| `chunks` HNSW on `embedding` | **not created** | ADR-0001. |
| `chunks` GIN on `content_tsv` | **not created** | No lexical search exists yet. |

Vector search always runs inside a prospect filter, which keeps the candidate
set to hundreds of chunks. **[assumed]** exact search over that candidate set is
fast enough and has perfect recall, which an approximate index does not. The
number that would change the decision, and how to measure it, is in ADR-0001.

---

## 6. Chunking — [planned]

The four questions are synthesis questions, not exact-lookup questions, so
chunks run larger than is typical for RAG: 800–1200 tokens with roughly 150
tokens of overlap, split on semantic structure (headings) rather than fixed
character counts. Job postings are never split — one posting is one chunk.
Publication date is carried in chunk metadata. Rationale and the evidence that
would reverse it: ADR-0005.

---

## 7. Module layout

```
ingest.py                       the crawler (implemented)
linestack/
  config.py                     settings from environment
  db.py                         async engine and session factory
  models.py                     SQLAlchemy models mirroring migrations/
  ingestion/
    crawler.py                  destination of ingest.py
    signals.py                  computed-signal definitions
    chunking.py                 document → chunks
    loader.py                   crawl JSON → Postgres, idempotent on hash
  retrieval/
    scope.py                    the A1 chokepoint
    embedding.py                text → halfvec(1536)
    search.py                   vector search within a scope
  generation/
    prompts.py                  prompt templates, versioned
    answer.py                   context assembly and streamed generation
  api/
    main.py                     FastAPI application
    routes.py                   HTTP endpoints
    schemas.py                  Pydantic request/response models
  evaluation/
    dataset.py                  load and validate the ground-truth set
    harness.py                  run the set, record deltas
    metrics.py                  retrieval metrics, reported separately (A8)
  observability/
    langfuse.py                 tracing setup
```

Every one of those modules is currently empty apart from a docstring stating
what it is responsible for. The frontend (React + TypeScript + Vite) is not
scaffolded in this pass; its minimum viable scope is recorded in the README.
