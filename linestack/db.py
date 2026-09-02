"""Responsibility: the async SQLAlchemy engine and session factory, and the
pgvector type registration that makes halfvec round-trip correctly.

Owns: engine construction from config, session lifecycle, and the FastAPI
dependency that yields a session.

Does not own: any query. Queries against `chunks` live in
linestack/retrieval/scope.py and nowhere else, because that is one of the
mechanisms enforcing A1 (docs/architecture.md section 4.2).
"""
