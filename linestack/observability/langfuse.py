"""Responsibility: Langfuse client setup and the span structure for a query.

Owns: one trace per question, with child spans for embed, retrieve and
generate, carrying the retrieved chunk ids and their scores.

Aggregate metrics say a run got worse. The trace says which answer, and why.
Both are needed; neither substitutes for the other.

Self-hosted, and off by default in local development: the stack is six
containers (docker compose --profile observability up -d). Absent credentials
must degrade to a no-op rather than failing a request -- observability that can
take down the serving path is a liability.
"""
