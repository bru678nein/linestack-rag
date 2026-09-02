"""Responsibility: SQLAlchemy declarative models mirroring migrations/*.sql.

Owns: Prospect, Document, Chunk, CrawlRun, CrawlPageOutcome.

Does not own: the schema itself. The SQL migrations are the source of truth;
these models follow them. Constraints are declared here for readability, but
the constraint that enforces A1 -- the composite foreign key on
chunks(document_id, prospect_id) to documents(id, prospect_id) -- exists in the
database whether or not it is declared here, and must never be relaxed here to
make a test pass.
"""
