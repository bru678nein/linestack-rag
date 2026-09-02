-- 0002_documents_stable_hash.sql
--
-- Adds the order-insensitive content hash that A7 idempotency actually needs,
-- and the duplicate-URL aliases that deduplication must not throw away.
--
-- STATUS: applied and exercised 2026-09-02 against pgvector/pgvector:pg17.
-- Verified: stable_hash text NULL, duplicate_urls text[] NOT NULL DEFAULT '{}',
-- documents_prospect_stable_hash_idx present, both columns accepting real
-- values in an insert that also re-exercised the A1 composite foreign key.
--
-- Background: some sites serve repeated records in a different order on every
-- request. Verified 2026-09-02 -- fly.io/about returns its team roster
-- reshuffled each time: four consecutive fetches, 316 words each, four
-- different exact hashes, one identical word multiset. /team redirects to
-- /about and shares that multiset. See docs/decisions/0013-*.md.

BEGIN;

-- Order-insensitive digest of the document text: sha256 over the text's words
-- sorted. Two texts that are permutations of each other share it, so a
-- reshuffled page is recognised as unchanged (A7) and two URLs serving one
-- page deduplicate. content_hash stays exact, so the reordering is still
-- visible rather than hidden.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS stable_hash text;

COMMENT ON COLUMN documents.stable_hash IS
    'sha256 of the text with word order removed. Compare THIS to decide '
    'whether a re-crawled document changed (A7); compare content_hash to see '
    'whether it was merely reordered. ADR-0013.';

-- Other URLs observed serving this same content. Deduplication stores the text
-- once, but the URLs are evidence on their own: has_team_page reads the URL
-- path, not the text, so discarding /team because /about won deduplication
-- turns a real team page into a missing one.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS duplicate_urls text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN documents.duplicate_urls IS
    'Other URLs that served this content. Kept because a URL is evidence '
    'independent of the text it returned. ADR-0013.';

-- Deliberately NOT unique. Two prospects may legitimately share a stable_hash
-- (boilerplate, a shared template), and uniqueness here would silently drop
-- one prospect's document -- an A1 isolation failure wearing a constraint.
CREATE INDEX IF NOT EXISTS documents_prospect_stable_hash_idx
    ON documents (prospect_id, stable_hash);

INSERT INTO schema_migrations (version) VALUES ('0002_documents_stable_hash')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
