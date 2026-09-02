"""Responsibility: the endpoints.

Planned surface:
  POST /prospects                       register a prospect
  POST /prospects/{id}/ingest           crawl and load
  POST /prospects/{id}/ask              streamed answer plus retrieved chunks
  GET  /prospects/{id}/signals          the computed facts, on their own

Every route that touches chunks takes prospect_id from the path and passes it
into a scope object. No route accepts a chunk id without a prospect id (A1).

The ask endpoint returns the retrieved chunks and their scores alongside the
answer. They are not decoration: they are how a retrieval failure becomes
diagnosable instead of being blamed on the model.
"""
