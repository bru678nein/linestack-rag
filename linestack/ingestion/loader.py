"""Responsibility: loading a crawl artifact (prospect_<domain>.json) into
Postgres, idempotently.

Owns: upsert of prospects, documents and chunks keyed on the natural keys the
schema declares; skipping re-chunking and re-embedding for documents whose
content_hash is unchanged (A7); and writing crawl_runs and crawl_page_outcomes
so that a document that is absent has a recorded reason (A5).

Does not own: crawling. The two steps are deliberately separate so a crawl can
be re-run and diffed without touching the database (ADR-0008).

Must fail loudly rather than duplicate. "Re-running produces the same result or
fails loudly" is the whole of A7; a loader that silently inserts a second copy
of a document is the failure this module exists to prevent.
"""
