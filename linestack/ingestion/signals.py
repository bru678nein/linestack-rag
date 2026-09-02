"""Responsibility: computing the deterministic facts that must never be inferred
by a model (A2, ADR-0003).

Owns: has_team_page, people_listed, technical_roles_named, has_careers_page,
open_roles_seen, technical_roles_open, blog_posts_seen, latest_post_date,
pages_crawled, total_words -- and, for each, the reason code explaining why a
value is absent.

Does not own: anything that requires reading prose for meaning. A signal that
cannot be established structurally does not belong here; it belongs in the
answer, marked as inferred.

Currently implemented inside `ingest.py`. Two of these counters were measurably
wrong against live sites before they were fixed (162 people against a ground
truth of 54; 4 open roles against 0). Read ADR-0003 before changing any of
them: the fixes are not obvious and the reasons are recorded there.
"""
