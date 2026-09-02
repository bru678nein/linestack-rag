"""Responsibility: being the only place in the codebase that builds a query
against the `chunks` table.

This is the A1 chokepoint (docs/architecture.md section 4.2). The object here
takes a prospect_id in its constructor, and every retrieval function takes that
object rather than a raw session. A query that does not go through it does not
exist, because there is no other function that returns chunk rows.

This is the weaker of the two isolation mechanisms and it is deliberately
second. The stronger one is in the database: chunks(document_id, prospect_id)
is a composite foreign key onto documents(id, prospect_id), so a chunk whose
prospect_id disagrees with its document's cannot be inserted at all.

If you are about to write SQL against `chunks` in another module, that is the
trigger recorded in docs/open-questions.md section 3.3 for adopting row-level
security. Read it before proceeding.
"""
