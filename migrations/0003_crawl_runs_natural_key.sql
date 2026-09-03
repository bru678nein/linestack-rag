-- 0003_crawl_runs_natural_key.sql
--
-- Gives crawl_runs a natural key so that re-loading the same artifact
-- conflicts instead of inserting a second run.
--
-- A7 is "re-running produces the same result or fails loudly". Without this,
-- loading prospect_fly_io.json twice produced two crawl_runs describing one
-- crawl, which is neither. Every page outcome hangs off crawl_run_id, so a
-- duplicate run silently doubles the recorded outcomes as well, and
-- docs/evaluation.md section 2.5 -- "is there a crawl_page_outcomes row
-- explaining why this URL is missing?" -- starts answering twice.
--
-- The guarantee belongs in the database rather than in a select-then-insert in
-- application code, for the same reason A1's isolation does: a check that lives
-- in review is a check that eventually is not performed.
--
-- (prospect_id, started_at) is the natural key because started_at comes from
-- the artifact's crawled_at, which identifies one crawl of one prospect.

BEGIN;

ALTER TABLE crawl_runs
    ADD CONSTRAINT crawl_runs_prospect_started_unique
    UNIQUE (prospect_id, started_at);

COMMENT ON CONSTRAINT crawl_runs_prospect_started_unique ON crawl_runs IS
    'One crawl_run per (prospect, crawl start). Re-loading an artifact must '
    'conflict, not duplicate. A7. See migrations/0003.';

INSERT INTO schema_migrations (version) VALUES ('0003_crawl_runs_natural_key')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
