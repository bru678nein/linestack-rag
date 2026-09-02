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

What it does not yet own, and must before this module is considered done:
  - reason codes for DNS failure, timeout, non-200, non-HTML, and thin
    extraction (A5; docs/open-questions.md section 1.2);
  - fast-fail on an unreachable host (section 1.3).
"""
