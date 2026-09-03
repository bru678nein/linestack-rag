-- 0004_embedding_dimension_384.sql
--
-- Narrows chunks.embedding to halfvec(384) so that a local sentence-transformers
-- model can be used without an OpenAI account (ADR-0017).
--
-- Safe to apply now, and only now, precisely because NO VECTORS EXIST: every
-- chunks.embedding is NULL. Once vectors are stored this becomes a re-embed,
-- not a migration. This is the cheapest moment the change will ever have.
--
-- The dimension is a schema commitment, not a setting. halfvec(N) fixes N in
-- the column, so switching back to text-embedding-3-small (1536) is migration
-- 0005 plus a full re-embed -- not an environment variable. A unit test asserts
-- that this number equals settings.embedding_dimensions, so the two cannot
-- drift apart into a silent cast error at the first INSERT.

BEGIN;

-- Refuse rather than destroy. If someone applies this after embedding a corpus,
-- the vectors would be silently discarded by the cast; better to stop and make
-- the re-embed a decision someone takes deliberately.
DO $$
DECLARE
    stored bigint;
BEGIN
    SELECT count(*) INTO stored FROM chunks WHERE embedding IS NOT NULL;
    IF stored > 0 THEN
        RAISE EXCEPTION
            'refusing to change the embedding dimension: % chunks already have '
            'vectors, and narrowing the column would discard them. Re-embed '
            'deliberately: UPDATE chunks SET embedding = NULL, embedding_model '
            '= NULL; then apply this migration and run make embed.', stored;
    END IF;
END
$$;

ALTER TABLE chunks
    ALTER COLUMN embedding TYPE halfvec(384);

COMMENT ON COLUMN chunks.embedding IS
    'halfvec(384), matching BAAI/bge-small-en-v1.5 (ADR-0017). The dimension is '
    'a schema commitment: changing the embedding model to one with a different '
    'width requires a migration and a full re-embed, because vectors from two '
    'models are not comparable in the first place.';

INSERT INTO schema_migrations (version) VALUES ('0004_embedding_dimension_384')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
