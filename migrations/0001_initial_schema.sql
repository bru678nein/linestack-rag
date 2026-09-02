-- 0001_initial_schema.sql
--
-- Linestack lead-gen RAG: prospects, crawled documents, embedded chunks, and
-- the crawl bookkeeping that makes a failed ingestion explainable.
--
-- Applied by `make migrate`, which runs migrations/*.sql in filename order
-- inside a single transaction each and records them in schema_migrations.
--
-- Rationale for every non-obvious choice is in docs/architecture.md section 3
-- and in docs/decisions/. Comments here state the reason, not the alternative.

BEGIN;

-- --------------------------------------------------------------------------
-- extensions
-- --------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- halfvec requires pgvector >= 0.7.0. Assert it here so a too-old extension
-- fails at migration time with a readable message, rather than at the first
-- cast with a type error. (hnsw.iterative_scan, if HNSW is ever adopted under
-- ADR-0001, additionally requires >= 0.8.0.)
DO $$
DECLARE
    v text;
BEGIN
    SELECT extversion INTO v FROM pg_extension WHERE extname = 'vector';
    IF string_to_array(v, '.')::int[] < ARRAY[0, 7, 0] THEN
        RAISE EXCEPTION
            'pgvector % is too old: halfvec requires >= 0.7.0', v;
    END IF;
END
$$;

-- --------------------------------------------------------------------------
-- migration bookkeeping
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- enums
--
-- Closed vocabularies, not free text. `kind` drives retrieval source weighting
-- (ADR-0004) and the outcome codes are the failure classification A5 requires;
-- in both cases a typo would change behaviour silently.
-- --------------------------------------------------------------------------
CREATE TYPE document_kind AS ENUM (
    'website',
    'job_posting',
    'blog_post'
);

-- Mirrors the ROBOTS_* constants in ingest.py. RFC 9309 section 2.3.1:
-- 2xx applies the rules, 4xx permits crawling, 5xx is a full disallow.
-- "we could not read the policy" and "the policy said no" are different facts
-- and must not collapse into one flag (A5, ADR-0006).
CREATE TYPE robots_outcome AS ENUM (
    'ok',            -- 2xx, parsed, rules apply
    'absent',        -- 4xx other than 401/403: no robots.txt exists
    'unreadable',    -- 401/403: exists, withheld from us
    'server_error',  -- 5xx: treated as full disallow
    'fetch_failed'   -- transport error, timeout, DNS
);

CREATE TYPE crawl_outcome AS ENUM (
    'completed',            -- budget spent or queue exhausted, normally
    'aborted_robots',       -- robots.txt said no, or 5xx (full disallow)
    'aborted_unreachable',  -- host did not resolve or refused connections
    'failed'                -- unhandled error; detail column says what
);

-- Why a page did not become a document. Every URL the crawler touched and did
-- not store gets one of these. Without them, "0 documents" is one number that
-- means six different things, and the evaluation set silently inherits
-- ingestion bugs as facts about the company (docs/evaluation.md section 2.5).
--
-- Validated against live crawls on 2026-09-02: ingest.py emits these exact
-- strings as its PAGE_* constants, and a unit test asserts the two sets are
-- equal so they cannot drift. Eight of the ten were observed in live runs;
-- 'timeout' and 'thin_extraction' are covered by unit tests. See ADR-0012.
CREATE TYPE page_outcome AS ENUM (
    'stored',
    'skipped_robots',      -- a Disallow rule matched; recorded, never worked around (A6)
    'dns_failure',
    'timeout',
    'transport_error',
    'http_error',          -- non-200; http_status carries which
    'non_html',            -- content-type was not HTML; detail carries which
    'thin_extraction',     -- extractor produced too little text; detail carries
                           -- 'empty' (no text at all, usually client-rendered)
                           -- or 'thin' (under MIN_WORDS). See ADR-0011.
    'duplicate_content',   -- same content_hash as a page already stored
    'budget_exhausted'     -- queued but never fetched; the page budget ran out
);

-- --------------------------------------------------------------------------
-- prospects
-- --------------------------------------------------------------------------
CREATE TABLE prospects (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_name text        NOT NULL,
    domain       text        NOT NULL UNIQUE,

    -- Computed facts, never inferred (A2, ADR-0003): has_team_page,
    -- people_listed, open_roles_seen, technical_roles_open, latest_post_date,
    -- and so on. JSONB rather than columns because the signal set is still
    -- moving -- three fields changed definition during crawler debugging.
    -- Migrate a field to a column the first time it appears in a WHERE clause
    -- rather than only in a prompt context block.
    signals      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT prospects_domain_lowercase CHECK (domain = lower(domain)),
    CONSTRAINT prospects_signals_is_object CHECK (jsonb_typeof(signals) = 'object')
);

COMMENT ON COLUMN prospects.signals IS
    'Deterministic computed facts (A2). Injected into every answer context '
    'regardless of what retrieval returned.';

-- --------------------------------------------------------------------------
-- documents
-- --------------------------------------------------------------------------
CREATE TABLE documents (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prospect_id  bigint        NOT NULL
                 REFERENCES prospects (id) ON DELETE CASCADE,

    source_url   text          NOT NULL,
    kind         document_kind NOT NULL,
    title        text          NOT NULL DEFAULT '',
    published_at date,
    word_count   integer       NOT NULL DEFAULT 0 CHECK (word_count >= 0),

    -- A7: re-running against the same prospect produces the same result or
    -- fails loudly. An unchanged hash means the chunks and embeddings below
    -- are still valid and are not recomputed. Verified: two consecutive
    -- crawls of fly.io produced identical hashes for all 40 documents.
    content_hash text          NOT NULL,

    fetched_at   timestamptz   NOT NULL,
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT documents_prospect_url_unique UNIQUE (prospect_id, source_url),

    -- Redundant on its own -- id is already the primary key. It exists so that
    -- chunks can reference (document_id, prospect_id) as a composite foreign
    -- key, which is what makes the denormalised prospect_id on chunks
    -- impossible to get wrong. This is the structural enforcement of A1.
    -- Do not drop it. See docs/architecture.md section 4.1.
    CONSTRAINT documents_id_prospect_unique UNIQUE (id, prospect_id)
);

-- --------------------------------------------------------------------------
-- chunks
-- --------------------------------------------------------------------------
CREATE TABLE chunks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  bigint        NOT NULL,

    -- Denormalised from documents. The prospect filter is on the path of every
    -- query and is how A1 is enforced; it must not require a join (ADR-0004).
    prospect_id  bigint        NOT NULL,

    -- Denormalised from documents. A job posting is stronger evidence of
    -- technical capacity than an About page, and weighting retrieval by source
    -- has to be cheap enough to do in SQL (ADR-0004, ADR-0009 step 2).
    kind         document_kind NOT NULL,

    chunk_index  integer       NOT NULL CHECK (chunk_index >= 0),
    content      text          NOT NULL,
    token_count  integer       NOT NULL CHECK (token_count > 0),

    -- halfvec, not vector: half precision halves storage and index size for
    -- text-embedding-3-small at 1536 dimensions. Nullable because a chunk is
    -- inserted before it is embedded.
    embedding       halfvec(1536),
    embedding_model text,

    -- Present now so the schema does not change when lexical search ships
    -- (ADR-0009 step 1). Its GIN index is deliberately NOT created: there is
    -- no lexical query to serve yet, and an index with no query is cost
    -- without benefit (A9).
    --
    -- 'simple', not 'english': the crawler's seed paths include /nosotros,
    -- /equipo and /empleos, so Spanish-language prospects are expected, and
    -- English stemming applied to Spanish text is worse than no stemming.
    -- Unresolved -- see docs/open-questions.md section 3.2. This must be
    -- settled before hybrid search ships, because it decides what lexical
    -- search can match.
    content_tsv  tsvector
                 GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,

    created_at   timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT chunks_document_index_unique UNIQUE (document_id, chunk_index),

    -- The A1 guarantee, enforced by the database rather than by review: a
    -- chunk whose prospect_id disagrees with its document's prospect_id cannot
    -- be inserted, so filtering on the denormalised column is exactly as
    -- correct as joining would have been.
    CONSTRAINT chunks_document_prospect_fk
        FOREIGN KEY (document_id, prospect_id)
        REFERENCES documents (id, prospect_id) ON DELETE CASCADE,

    -- An embedding without the model that produced it is not comparable with
    -- anything, and a model without an embedding is a bookkeeping error.
    CONSTRAINT chunks_embedding_model_paired
        CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);

-- The one index that matters (ADR-0001). Vector search runs exact, inside this
-- filter; there is deliberately no HNSW index on embedding.
CREATE INDEX chunks_prospect_id_idx ON chunks (prospect_id);

-- Not created, on purpose. Uncomment in a later migration when the query that
-- needs it exists, and record the measurement that justified it (A9):
--   CREATE INDEX chunks_content_tsv_idx ON chunks USING gin (content_tsv);
--   CREATE INDEX chunks_embedding_hnsw_idx ON chunks
--       USING hnsw (embedding halfvec_cosine_ops);

-- --------------------------------------------------------------------------
-- crawl bookkeeping
--
-- Not in the original schema sketch. Added because A5 requires failures to be
-- classified rather than counted, and there was nowhere to put a reason code.
-- Without these tables, "this prospect has 0 documents" means at least six
-- different things and the evaluation set inherits ingestion bugs as facts.
-- --------------------------------------------------------------------------
CREATE TABLE crawl_runs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prospect_id      bigint         NOT NULL
                     REFERENCES prospects (id) ON DELETE CASCADE,

    started_at       timestamptz    NOT NULL,
    finished_at      timestamptz,

    robots_reason    robots_outcome NOT NULL,
    outcome          crawl_outcome  NOT NULL,

    max_pages        integer        NOT NULL CHECK (max_pages > 0),
    pages_fetched    integer        NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
    documents_stored integer        NOT NULL DEFAULT 0 CHECK (documents_stored >= 0),

    user_agent       text           NOT NULL,
    detail           text,

    created_at       timestamptz    NOT NULL DEFAULT now()
);

CREATE INDEX crawl_runs_prospect_started_idx
    ON crawl_runs (prospect_id, started_at DESC);

CREATE TABLE crawl_page_outcomes (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    crawl_run_id bigint       NOT NULL
                 REFERENCES crawl_runs (id) ON DELETE CASCADE,

    url          text         NOT NULL,
    outcome      page_outcome NOT NULL,
    http_status  integer,
    detail       text,
    occurred_at  timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT crawl_page_outcomes_run_url_unique UNIQUE (crawl_run_id, url)
);

-- The lookup this table exists for: "the evaluation set expects this URL and
-- it is not in documents -- why not?" (docs/evaluation.md section 2.5).
CREATE INDEX crawl_page_outcomes_run_outcome_idx
    ON crawl_page_outcomes (crawl_run_id, outcome);

-- --------------------------------------------------------------------------
INSERT INTO schema_migrations (version) VALUES ('0001_initial_schema');

COMMIT;
