# Linestack lead-gen RAG

## The problem

Linestack is a small software development company. Before anyone spends time on
a prospect — writing an approach, preparing a call, quoting work — someone has
to read that company's public web presence and decide whether the prospect is
worth contacting at all. That reading is slow, it is done inconsistently, and it
does not scale past a handful of companies a week.

## What a wrong answer costs

The failure that matters is not "the system said *I don't know*". It is a
confident, fluent, well-sourced answer about the **wrong company**. A sentence
lifted from a real job posting at company B, attributed to company A, reads
exactly like a correct answer. It survives review. It goes into an outreach
email, and it burns the prospect permanently — you cannot un-send a message that
proves you did not read their site.

That single failure mode drives most of the design in this repository:

- Retrieval is scoped to one prospect, structurally, not by convention (A1).
- Facts that a function can compute are never asked of a model (A2).
- Faithfulness and retrieval recall are the primary evaluation metrics; answer
  correctness is secondary and noisy (see `docs/evaluation.md`).
- Retrieved chunks and their scores are visible in the UI, because a retrieval
  failure that you cannot see is a retrieval failure you will attribute to the
  model.

## The four questions

For each prospect the system answers exactly four questions:

1. What does this company do, and who does it sell to?
2. What evidence is there of in-house technical capacity?
3. What signals are there that they are investing or growing?
4. What pain or problem do they state explicitly?

A fifth question — "what is a concrete angle for a first approach?" — is a
product output, not an extraction task. It has no ground truth, so it is
deliberately excluded from the evaluation set.

## Pipeline

```
                 ingestion                         query
  ┌────────────────────────────────┐   ┌───────────────────────────────┐
  domain ─▶ polite crawl ─▶ extract │   │ question + prospect_id
             (robots, rate limit)   │   │        │
                    │               │   │        ▼
                    ├─▶ documents ──┼──▶│  embed question
                    │   (text)      │   │        │
                    │               │   │        ▼
                    └─▶ signals ────┼──▶│  vector search  WHERE prospect_id = ?
                        (computed   │   │        │        (exact, no ANN index)
                         facts)     │   │        ▼
                                    │   │  chunks + computed signals
                                    │   │        │
                                    │   │        ▼
                                    │   │  answer + citations ─▶ Langfuse trace
  └────────────────────────────────┘   └───────────────────────────────┘
```

Ingestion produces two separate outputs on purpose:

- **documents** — readable text, destined for chunking and embedding. This is
  what retrieval answers from.
- **signals** — deterministic computed facts: whether a team page exists, how
  many people are listed on it, how many open roles there are, the date of the
  most recent post. These are computed, stored as structured metadata, and
  handed to the model as context. They are never inferred, because "does this
  company have an in-house team" is precisely the question a model answers
  confidently and wrongly (A2, ADR-0003).

## Current state

**Verified.** The crawler (`ingest.py`) is implemented and has been run against
live company domains. It honours robots.txt, rate limits per domain, classifies
pages, allocates the page budget across page kinds, and computes the signals
listed above. Re-crawling produces identical `stable_hash` values, which is
what A7 idempotency is checked on — **[verified]** across four fetches of a page
whose content the site reshuffles on every request, and across separate runs.

**Verified.** The schema applies. `migrations/0001_initial_schema.sql` was
applied against `pgvector/pgvector:pg17` (PostgreSQL 17.11, pgvector 0.8.6) on
2026-09-02, and the prospect-isolation constraint was exercised rather than
assumed: a chunk claiming one prospect against another prospect's document is
rejected by the database with a foreign-key violation.

**Not implemented.** Everything else. There is no database load, no chunking, no
embedding, no retrieval, no generation, no API, no frontend, and no evaluation
harness. The modules under `linestack/` are empty and carry only a docstring
stating what each is responsible for.

**Known defects.** All measured, all in `docs/open-questions.md`. Three are
**fixed**: the silent 30-word extraction threshold (ADR-0011); the missing
failure classification (ADR-0012) — every URL the crawl touches now carries an
outcome in the schema's own vocabulary, and a crawl that finds nothing exits
non-zero saying why instead of exiting 0; and content that is reshuffled on
every request (ADR-0013), which was breaking A7 idempotency and defeating
deduplication; and person counting that returned 0 on a page listing 57 people
by name (ADR-0014), because class-name selectors cannot see a roster built from
utility classes; and page-kind misclassification (ADR-0015), where `careers?`
matching inside a path segment labelled playbook articles as job postings and
deduplication let crawl order decide a page's `kind`.

**Every defect recorded in `docs/open-questions.md` §1 is now fixed.** Three
limitations remain, all documented under the entry they belong to and all
visible in the output rather than silent:

- No rule yet decides a genuine `kind` disagreement between two URLs for one
  page. The conflict is recorded on the document and printed by the crawl, and
  the first observed instance argues against the obvious rule (§1.1c,
  ADR-0019).
- Publication dates are largely `htmldate`'s coarse fallback — 31 of 76
  documents share one date — and `latest_post_date` rests on them (§1.6).
- A roster of single-word names is now counted by its portraits, at the cost
  of a shape a marketing grid of feature cards could also match. Bounded, not
  prevented (§1.1b, ADR-0018).

An unreachable host no longer costs a crawl 39.5 seconds of politeness extended
to a domain that does not exist — **[verified]** 26 attempts → 1, 0.25 s
(ADR-0016), still exiting non-zero with a reason.

**Ground truth is unblocked** (`docs/ground-truth.md` §6). Migration `0002` is
applied and exercised against `pgvector/pgvector:pg17` on 2026-09-02.

The build order (A3) is: naive vector search end-to-end → ground truth →
evaluation harness → *then* hybrid retrieval, reranking, chunking changes. That
order was already violated once: `ingest.py` was written before any
documentation, schema, or harness existed. See ADR-0010.

## Setup

Requirements: Docker, Python 3.13, [`uv`](https://docs.astral.sh/uv/).

If port 5432 is already taken on your machine — another project's Postgres is
the usual culprit — set `POSTGRES_PORT` in `.env` **and** point
`DATABASE_URL_SYNC` at the same port. Compose will otherwise fail to publish
the port, and `tests/test_isolation_contract.py`, which defaults to
`localhost:5432`, will happily connect to whatever else is listening there.
`make migrate` is unaffected: it runs `psql` inside the container.

Note that `DATABASE_URL` (async, used by the application) and
`DATABASE_URL_SYNC` (used by `psql` and the isolation test) are separate
variables. Setting only one leaves the other pointing at the default port, and
the tests that read it will connect somewhere else without saying so.

```sh
cp .env.example .env          # then fill in OPENAI_API_KEY
make install                  # create .venv and install pinned dependencies
make up                       # Postgres 17 + pgvector on localhost:5432
make migrate                  # apply migrations/*.sql in order
make test                     # unit tests
```

Optional, and off by default because it costs about 4 GB of RAM:

```sh
make observability-up         # Langfuse stack on localhost:3000
```

Crawl a prospect (this is the only feature code that exists today):

```sh
.venv/bin/python ingest.py fly.io
# writes prospect_fly_io.json
```

`make help` lists every target.

## Repository map

| Path | Contents |
| --- | --- |
| `ingest.py` | The crawler. Working, exercised against live sites. |
| `linestack/` | Empty modules with responsibility docstrings. |
| `migrations/` | Plain SQL migrations, applied in filename order. |
| `docs/architecture.md` | Ingestion and query paths, schema, how A1 is enforced. |
| `docs/decisions/` | One ADR per settled decision. |
| `docs/evaluation.md` | The harness design, written before the harness. |
| `docs/ground-truth.md` | Format for the evaluation set and how to write one. |
| `docs/open-questions.md` | Undecided, assumed, and known-broken. Read this first. |
| `eval/ground_truth/` | The evaluation set, once it is written. Empty today. |

## Conventions

Every claim in this repository's documentation is either **verified** — someone
ran it and recorded the number — or explicitly marked as an **assumption**. An
assumption presented as a finding is treated as a defect (A4). If you add a
claim, mark it.
