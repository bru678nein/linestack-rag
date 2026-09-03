"""Responsibility: the polite, budget-allocating crawl of one prospect domain.

Destination module for `ingest.py`, which currently sits at the repository root
and is working, exercised code. Do not write a second crawler here.

What the existing implementation owns, for reference:
  - robots.txt fetched through the project's own client and User-Agent, with a
    five-value outcome code (ADR-0006);
  - per-domain rate limiting at 1.5 s;
  - a crawl queue ordered by how directly a page kind answers the four
    questions, with per-kind quotas acting as caps rather than floors
    (ADR-0007);
  - deduplication by content hash.

Both items this stub used to list as missing have shipped and are measured:
reason codes for every fetch and extraction outcome (ADR-0012), and fast-fail on
an unreachable host (ADR-0016, 26 attempts and 39.5 s down to 1 and 0.25 s).
What remains is the move itself -- ingest.py into this module, with the
fixture-based unit tests that ADR-0010 makes the condition for it.
"""
