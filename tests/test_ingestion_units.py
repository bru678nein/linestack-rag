"""Unit tests for the pure functions in `ingest.py`.

These are regression tests for defects that were found by running the crawler
against live sites, not by reading it (ADR-0010). Each test names the
measurement it encodes, so that a future change that reintroduces one of these
bugs fails with an explanation rather than with a bare assertion error.

No network, no database. The HTML fixtures below reproduce the *shape* of the
markup that broke each function; they are not copies of anyone's pages.
"""

import pytest

import ingest


# ---------------------------------------------------------------------------
# URL normalisation and classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://ex.com/about/", "https://ex.com/about"),
        ("https://ex.com/about", "https://ex.com/about"),
        ("https://ex.com/", "https://ex.com/"),
        ("https://ex.com/about#team", "https://ex.com/about"),
        ("https://ex.com/about?utm_source=x", "https://ex.com/about"),
    ],
)
def test_normalise_collapses_equivalent_urls(raw: str, expected: str) -> None:
    """Without this, a site serving both /about and /about/ costs two requests
    per page against someone else's server (A6)."""
    assert ingest.normalise(raw) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ex.com/", "website"),
        ("https://ex.com/about", "website"),
        ("https://ex.com/careers", "job_posting"),
        ("https://ex.com/jobs/senior-engineer", "job_posting"),
        ("https://ex.com/empleos", "job_posting"),
        ("https://ex.com/blog/a-post", "blog_post"),
        ("https://ex.com/noticias", "blog_post"),
    ],
)
def test_classify_by_path(url: str, expected: str) -> None:
    assert ingest.classify(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Verified on thoughtbot.com: both were classified job_posting because
        # `careers?` and `jobs?` matched inside a path segment.
        "https://thoughtbot.com/playbook/our-company/career-paths",
        "https://thoughtbot.com/playbook/strategy/design-sprints/jobs-profile",
        # Same shape, the other direction.
        "https://ex.com/newsletter-archive",
        "https://ex.com/handbook/blogging-policy",
    ],
)
def test_a_word_inside_a_path_segment_does_not_classify_the_page(url: str) -> None:
    """Patterns match whole path segments, never substrings.

    `kind` is denormalised onto `chunks` for retrieval source weighting
    (ADR-0004), so a playbook article classified job_posting would carry
    job-posting weight into retrieval. See ADR-0015.
    """
    assert ingest.classify(url) == "website"


def test_classify_does_not_take_a_title() -> None:
    """The title was measured as a signal and rejected, not overlooked.

    **[verified]** across both validation corpora the only three pages whose
    <title> matches career/job/hiring words are thoughtbot playbook ARTICLES
    ("Career Paths | ...", "Jobs Profile | ...", "Hiring | ..."). Three false
    positives, zero true positives: using the title would have reintroduced
    the very misclassification segment matching fixes. See ADR-0015.
    """
    import inspect

    assert list(inspect.signature(ingest.classify).parameters) == ["url"]


# ---------------------------------------------------------------------------
# Person counting
# ---------------------------------------------------------------------------
TEAM_PAGE = """
<html><body>
  <div class="team-grid">
    <div class="team-member">
      <img class="person-photo" src="a.jpg">
      <h3 class="person-info-name">Person A</h3>
      <p class="person-info-title">Engineer</p>
    </div>
    <div class="team-member">
      <img class="person-photo" src="b.jpg">
      <h3 class="person-info-name">Person B</h3>
      <p class="person-info-title">Designer</p>
    </div>
    <div class="team-member">
      <img class="person-photo" src="c.jpg">
      <h3 class="person-info-name">Person C</h3>
      <p class="person-info-title">Developer</p>
    </div>
  </div>
</body></html>
"""


def test_count_people_counts_cards_not_matching_leaves() -> None:
    """Regression for the measured 162-against-54 overcount.

    thoughtbot.com/team renders each person as three matching leaves
    (person-photo, person-info-name, person-info-title). Counting leaves
    multiplied the headcount by three. The fixture above has the same shape at
    a smaller size: three people, nine matching leaves, plus a wrapper that
    also matches.
    """
    assert ingest.count_people(TEAM_PAGE) == 3


def test_count_people_ignores_the_wrapper() -> None:
    """The opposite trap: a matching wrapper around N matching cards makes a
    naive count return N + 1."""
    single = """
    <div class="team-grid"><div class="team-member"><span>One</span></div></div>
    """
    assert ingest.count_people(single) == 1


def test_count_people_returns_zero_when_there_is_nothing_person_shaped() -> None:
    assert ingest.count_people("<html><body><p>No people here.</p></body></html>") == 0


# ---------------------------------------------------------------------------
# Role counting
# ---------------------------------------------------------------------------
LISTING_URL = "https://ex.com/jobs"

LISTING_HTML = """
<html><body>
  <a href="/jobs/senior-engineer">Senior Engineer</a>
  <a href="/jobs/compensation-calculator">How we set pay</a>
  <a href="/about">About us</a>
  <a href="https://elsewhere.example/jobs/other">Someone else's job</a>
</body></html>
"""


def _docs() -> tuple[list[ingest.Document], dict[str, str]]:
    listing = ingest.Document(
        url=LISTING_URL,
        kind="job_posting",
        title="Jobs · Ex",
        text="We are hiring. " * 20,
    ).finalise()
    posting = ingest.Document(
        url="https://ex.com/jobs/senior-engineer",
        kind="job_posting",
        title="Senior Engineer · Ex",
        text="You will build things. " * 20,
    ).finalise()
    policy = ingest.Document(
        url="https://ex.com/jobs/compensation-calculator",
        kind="job_posting",
        title="Compensation calculator · Ex",
        text="How we set pay. " * 20,
    ).finalise()
    raw = {LISTING_URL: LISTING_HTML}
    return [listing, posting, policy], raw


def test_open_roles_counts_identities_not_pages() -> None:
    """Regression for two measured overcounts: fly.io reported 3 roles for 2,
    and thoughtbot reported 4 for 0.

    Three pages classified `job_posting` here. Exactly one is a vacancy: the
    listing contributes the roles it links to and never itself, and a
    compensation calculator does not have a job-title-shaped name.
    """
    docs, raw = _docs()
    signals = ingest.Signals()
    ingest._count_open_roles(signals, docs, raw)

    assert signals.has_careers_page is True
    assert signals.open_roles_seen == 1
    assert signals.technical_roles_open == 1


def test_listing_role_links_stays_on_domain_and_below_the_listing() -> None:
    links = ingest.listing_role_links(LISTING_HTML, LISTING_URL)
    assert links == [
        "https://ex.com/jobs/senior-engineer",
        "https://ex.com/jobs/compensation-calculator",
    ]


def test_role_name_strips_a_trailing_site_name_but_not_a_hyphenated_title() -> None:
    assert (
        ingest.role_name("https://ex.com/jobs/x", "Senior Engineer · Ex")
        == "Senior Engineer"
    )
    assert (
        ingest.role_name("https://ex.com/jobs/x", "Front-End Developer")
        == "Front-End Developer"
    )
    assert (
        ingest.role_name("https://ex.com/jobs/staff-designer", "") == "staff designer"
    )


def test_extract_role_headings_is_a_fallback_that_rarely_fires() -> None:
    """Verified: returns zero headings on both fly.io/jobs and
    thoughtbot.com/jobs. Per-role links are the reliable signal. It is kept
    only for listings that name roles inline without linking them."""
    assert ingest.extract_role_headings(LISTING_HTML) == []
    inline = "<h3>Backend Engineer</h3><h3>Our benefits</h3>"
    assert ingest.extract_role_headings(inline) == ["Backend Engineer"]


# ---------------------------------------------------------------------------
# Crawl budget
# ---------------------------------------------------------------------------
def test_queue_rank_prefers_pages_that_answer_the_four_questions() -> None:
    """With an empty crawl, a marketing page and a job posting outrank a blog
    post. Under FIFO, fly.io's blog took 34 of 40 pages and the crawl never
    reached a team page (ADR-0007)."""
    counts: dict[str, int] = {}
    assert ingest.queue_rank("website", counts, 40) < ingest.queue_rank(
        "blog_post", counts, 40
    )
    assert ingest.queue_rank("job_posting", counts, 40) < ingest.queue_rank(
        "blog_post", counts, 40
    )


def test_queue_rank_treats_the_quota_as_a_cap_not_a_floor() -> None:
    """A kind at its cap yields to every kind that is not.

    The rejected alternative served whichever kind was furthest *below* its
    share, which promoted blog tag-index pages over real content and measurably
    made thoughtbot worse (websites 22 -> 18). Being under quota must not earn
    a low-value page priority over a high-value one.
    """
    at_cap = {"website": int(40 * ingest.KIND_QUOTA["website"])}
    assert ingest.queue_rank("blog_post", at_cap, 40) < ingest.queue_rank(
        "website", at_cap, 40
    )


def test_unknown_kinds_are_served_last() -> None:
    counts: dict[str, int] = {}
    assert ingest.queue_rank("something_else", counts, 40) > ingest.queue_rank(
        "blog_post", counts, 40
    )


# ---------------------------------------------------------------------------
# robots.txt policy (ADR-0006)
# ---------------------------------------------------------------------------
def test_robots_reason_codes_are_distinct_values() -> None:
    """A5: "we could not read the policy" and "the policy said no" are
    different facts. A single boolean hid a bug that silently zeroed 4 of 18
    domains."""
    codes = {
        ingest.ROBOTS_OK,
        ingest.ROBOTS_ABSENT,
        ingest.ROBOTS_UNREADABLE,
        ingest.ROBOTS_SERVER_ERROR,
        ingest.ROBOTS_FETCH_FAILED,
    }
    assert len(codes) == 5


def test_robotparser_read_is_not_used() -> None:
    """The bug this project measured: RobotFileParser.read() fetches with
    urllib's User-Agent, gets a 403 from bot protection, swallows the error,
    and sets disallow_all without raising. Verified at 4 of 18 domains;
    thoughtbot.com went from 0 pages to 37 after the fix.

    Asserting on the source is crude, and it is the only way to catch a
    reintroduction without making a network request.
    """
    from pathlib import Path

    source = Path(ingest.__file__).read_text(encoding="utf-8")
    assert "rp.parse(" in source, "robots.txt must be parsed from a response we fetched"
    assert "rp.read()" not in source, (
        "RobotFileParser.read() denies everything on a 403"
    )


# ---------------------------------------------------------------------------
# Idempotency (A7)
# ---------------------------------------------------------------------------
def test_content_hash_is_stable_for_identical_text() -> None:
    """Verified in the field: two consecutive crawls of fly.io produced
    identical hashes for all 40 documents. This asserts the mechanism, not the
    crawl."""
    a = ingest.Document(
        url="https://ex.com/", kind="website", title="t", text="same text"
    ).finalise()
    b = ingest.Document(
        url="https://ex.com/", kind="website", title="t", text="same text"
    ).finalise()
    c = ingest.Document(
        url="https://ex.com/", kind="website", title="t", text="other text"
    ).finalise()

    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash
    assert a.content_hash


# ---------------------------------------------------------------------------
# Extraction escalation (Finding 8)
# ---------------------------------------------------------------------------
# Measured 2026-09-02: fly.io/about is 249,893 bytes holding the whole team
# roster, and trafilatura with favor_precision=True extracts 29 words of it --
# one word under the old threshold, so the page vanished and `has_team_page`
# read False. The page is not client-rendered; recall mode returns 316 words.
_PROSE = " ".join(f"word{i}" for i in range(60))


def test_clean_prose_is_extracted_by_the_precision_pass() -> None:
    html = f"<html><body><article><p>{_PROSE}</p></article></body></html>"
    doc, reason = ingest.extract("https://ex.com/a", html)
    assert reason == ingest.EXTRACT_OK
    assert doc is not None and doc.extract_reason == ingest.EXTRACT_OK


def test_a_page_with_no_text_is_reported_as_empty_not_as_a_bare_none() -> None:
    doc, reason = ingest.extract("https://ex.com/a", "<html><body></body></html>")
    assert doc is None
    assert reason == ingest.EXTRACT_EMPTY


def test_a_genuinely_thin_page_is_distinguished_from_an_empty_one() -> None:
    html = "<html><body><main><p>Three words here.</p></main></body></html>"
    doc, reason = ingest.extract("https://ex.com/a", html)
    assert doc is None
    assert reason == ingest.EXTRACT_THIN


def test_dom_fallback_strips_chrome_and_keeps_content() -> None:
    html = (
        "<html><body>"
        "<nav>Home About Careers Blog Contact</nav>"
        "<script>var tracking = 1;</script>"
        f"<main><p>{_PROSE}</p></main>"
        "<footer>Copyright notice all rights reserved</footer>"
        "</body></html>"
    )
    text = ingest.dom_text(html)
    assert "word0" in text and "word59" in text
    assert "tracking" not in text
    assert "Copyright" not in text


def test_every_extraction_reason_is_a_distinct_value() -> None:
    reasons = [
        ingest.EXTRACT_OK,
        ingest.EXTRACT_RECALL,
        ingest.EXTRACT_DOM,
        ingest.EXTRACT_THIN,
        ingest.EXTRACT_EMPTY,
    ]
    assert len(set(reasons)) == len(reasons), "reasons must not collapse (A5)"


def test_a_link_heavy_roster_is_recovered_by_the_recall_pass() -> None:
    """The fly.io/about shape: many short cards, each mostly links.

    favor_precision=True reads this as navigation and discards it. Recall mode
    keeps it. Without the escalation the page is dropped and `has_team_page`
    reads False for a company whose team page plainly exists.
    """
    people = [
        ("Ada Lovelace", "Developer"),
        ("Grace Hopper", "Engineer"),
        ("Alan Turing", "Researcher"),
        ("Katherine Johnson", "Mathematician"),
        ("Barbara Liskov", "Architect"),
        ("Donald Knuth", "Author"),
        ("Radia Perlman", "Network Engineer"),
        ("Margaret Hamilton", "Lead"),
        ("Tim Berners-Lee", "Web Lead"),
        ("Linus Torvalds", "Kernel Developer"),
    ]
    cards = "".join(
        f'<div class="card"><h3>{name}</h3><span>{role}</span>'
        f'<a href="https://twitter.com/x">Twitter</a>'
        f'<a href="https://github.com/x">GitHub</a></div>'
        for name, role in people
    )
    html = f"<html><body><main><h1>Meet the team</h1>{cards}</main></body></html>"

    doc, reason = ingest.extract("https://ex.com/about", html)

    assert reason == ingest.EXTRACT_RECALL
    assert doc is not None
    assert "Ada Lovelace" in doc.text
    assert doc.extract_reason == ingest.EXTRACT_RECALL, (
        "the document must carry how it was extracted, not just that it was"
    )


# ---------------------------------------------------------------------------
# Page outcome classification (Finding 3)
# ---------------------------------------------------------------------------
# Before this, every fetch failure was a bare `None`. A prospect with zero
# documents was reported identically whether the domain did not resolve, the
# site blocked us, or the company has no website -- and the run exited 0.


def _client_without_network() -> ingest.PoliteClient:
    """A PoliteClient with robots.txt loading and rate limiting stubbed out."""
    client = object.__new__(ingest.PoliteClient)
    client.base = "https://ex.com"
    client.domain = "ex.com"
    client._last_hit = 0.0
    client._robots = None
    client.robots_reason = ingest.ROBOTS_ABSENT
    return client


class _Response:
    def __init__(self, status: int, ctype: str = "text/html", text: str = "hi"):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.text = text


def test_the_outcome_vocabulary_matches_the_schema_enum() -> None:
    """These strings are loaded straight into `page_outcome`; drift breaks it."""
    import re
    from pathlib import Path

    sql = Path("migrations/0001_initial_schema.sql").read_text()
    block = re.search(r"CREATE TYPE page_outcome AS ENUM \((.*?)\);", sql, re.S).group(
        1
    )
    # Strip SQL comments first: they quote example detail values, which are not
    # enum members. Without this the test fails for the wrong reason.
    block = re.sub(r"--[^\n]*", "", block)
    in_schema = set(re.findall(r"'([a-z_]+)'", block))
    in_code = {
        ingest.PAGE_STORED,
        ingest.PAGE_SKIPPED_ROBOTS,
        ingest.PAGE_DNS_FAILURE,
        ingest.PAGE_TIMEOUT,
        ingest.PAGE_TRANSPORT_ERROR,
        ingest.PAGE_HTTP_ERROR,
        ingest.PAGE_NON_HTML,
        ingest.PAGE_THIN_EXTRACTION,
        ingest.PAGE_DUPLICATE_CONTENT,
        ingest.PAGE_BUDGET_EXHAUSTED,
    }
    assert in_code == in_schema


def test_a_timeout_is_not_reported_as_a_transport_error(monkeypatch) -> None:
    import httpx

    client = _client_without_network()
    monkeypatch.setattr(
        client, "_request", lambda url: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
    )

    html, outcome = client.get("https://ex.com/a")

    assert html is None
    assert outcome.outcome == ingest.PAGE_TIMEOUT
    assert outcome.detail == "ReadTimeout"


def test_dns_failure_is_only_claimed_when_the_host_really_does_not_resolve(
    monkeypatch,
) -> None:
    """httpx reports both as ConnectError, so resolution is checked separately.

    Claiming `dns_failure` for a refused connection would be an invented
    measurement (A4). Verified live 2026-09-02: a non-resolving domain gives
    26/26 dns_failure, while 127.0.0.1:9 gives 26/26 transport_error.
    """
    import httpx

    client = _client_without_network()
    monkeypatch.setattr(
        client,
        "_request",
        lambda url: (_ for _ in ()).throw(httpx.ConnectError("nope")),
    )

    monkeypatch.setattr(ingest, "host_resolves", lambda host: False)
    assert client.get("https://ex.com/a")[1].outcome == ingest.PAGE_DNS_FAILURE

    monkeypatch.setattr(ingest, "host_resolves", lambda host: True)
    assert client.get("https://ex.com/a")[1].outcome == (ingest.PAGE_TRANSPORT_ERROR)


def test_host_resolves_does_not_claim_failure_when_it_cannot_tell(
    monkeypatch,
) -> None:
    def boom(*a, **kw):
        raise OSError("resolver unavailable")

    monkeypatch.setattr(ingest.socket, "getaddrinfo", boom)
    assert ingest.host_resolves("ex.com") is True


def test_a_non_200_carries_the_status_that_caused_it(monkeypatch) -> None:
    client = _client_without_network()
    monkeypatch.setattr(client, "_request", lambda url: _Response(404))

    html, outcome = client.get("https://ex.com/a")

    assert html is None
    assert outcome.outcome == ingest.PAGE_HTTP_ERROR
    assert outcome.http_status == 404


def test_a_non_html_response_carries_the_content_type(monkeypatch) -> None:
    client = _client_without_network()
    monkeypatch.setattr(
        client,
        "_request",
        lambda url: _Response(200, "application/rss+xml; charset=utf-8"),
    )

    html, outcome = client.get("https://ex.com/feed.xml")

    assert html is None
    assert outcome.outcome == ingest.PAGE_NON_HTML
    assert outcome.detail == "application/rss+xml"


def test_an_empty_crawl_blames_the_transport_not_robots_txt() -> None:
    """A robots.txt that could not be fetched on a dead host is a symptom.

    Reporting it as the cause sends the reader to check a robots policy on a
    host that does not exist.
    """
    p = ingest.Prospect(
        company_name="x",
        domain="ex.com",
        base_url="https://ex.com",
        robots_reason=ingest.ROBOTS_FETCH_FAILED,
        page_outcomes=[
            {
                "url": "https://ex.com/",
                "outcome": ingest.PAGE_DNS_FAILURE,
                "http_status": None,
                "detail": "ex.com",
            }
        ],
    )
    assert ingest.PAGE_DNS_FAILURE in ingest.explain_empty_crawl(p)


def test_an_empty_crawl_is_never_reported_without_a_reason() -> None:
    p = ingest.Prospect(company_name="x", domain="ex.com", base_url="https://ex.com")
    assert ingest.explain_empty_crawl(p).strip() != ""


# ---------------------------------------------------------------------------
# Reordered content (Finding 1.1a)
# ---------------------------------------------------------------------------
# Measured 2026-09-02: fly.io/about returns its team roster in a different order
# on every request. Four consecutive fetches gave 316 words, four different
# content hashes, and one identical word multiset. /team redirects to /about and
# matches that multiset. Exact hashing therefore reported a change on every
# crawl of a page that had not changed (A7) and kept both URLs as documents.


def test_stable_hash_ignores_word_order_and_content_hash_does_not() -> None:
    a = ingest.Document(
        url="https://ex.com/about",
        kind="website",
        title="",
        text="Ada Developer Grace Engineer",
    ).finalise()
    b = ingest.Document(
        url="https://ex.com/team",
        kind="website",
        title="",
        text="Grace Engineer Ada Developer",
    ).finalise()

    assert a.stable_hash == b.stable_hash
    assert a.content_hash != b.content_hash, (
        "content_hash must stay exact so the reordering is still visible"
    )


def test_stable_hash_still_changes_when_the_words_change() -> None:
    a = ingest.Document(
        url="https://ex.com/a",
        kind="website",
        title="",
        text="Ada Developer Grace Engineer",
    ).finalise()
    b = ingest.Document(
        url="https://ex.com/a",
        kind="website",
        title="",
        text="Ada Developer Grace Designer",
    ).finalise()

    assert a.stable_hash != b.stable_hash


def test_stable_hash_counts_repeats_so_it_is_a_multiset_not_a_set() -> None:
    a = ingest.Document(
        url="https://ex.com/a",
        kind="website",
        title="",
        text="engineer engineer designer",
    ).finalise()
    b = ingest.Document(
        url="https://ex.com/a",
        kind="website",
        title="",
        text="engineer designer designer",
    ).finalise()

    assert a.stable_hash != b.stable_hash


def test_deduplication_keeps_the_urls_of_the_pages_it_drops() -> None:
    """Dropping a duplicate must not drop the evidence its URL carried.

    `has_team_page` reads the URL path, not the text. Deduplicating
    fly.io/team into fly.io/about turned a real team page into a missing one
    until the dropped URL was kept as an alias -- caught by running the crawl,
    where has_team_page flipped True -> False.
    """
    roster = "Ada Developer Grace Engineer " * 10
    about = ingest.Document(
        url="https://ex.com/about", kind="website", title="", text=roster
    ).finalise()
    team = ingest.Document(
        url="https://ex.com/team", kind="website", title="", text=roster
    ).finalise()

    unique: dict[str, ingest.Document] = {}
    for d in (about, team):
        first = unique.setdefault(d.stable_hash, d)
        if first is not d:
            first.duplicate_urls.append(d.url)

    assert len(unique) == 1
    survivor = next(iter(unique.values()))
    assert survivor.duplicate_urls == ["https://ex.com/team"]

    signals = ingest.compute_signals([survivor], {})
    assert signals.has_team_page is True
    assert signals.team_page_url == "https://ex.com/team"


def test_a_team_page_is_found_under_its_own_url_without_any_alias() -> None:
    d = ingest.Document(
        url="https://ex.com/team", kind="website", title="", text="Ada Developer"
    ).finalise()
    assert ingest.compute_signals([d], {}).has_team_page is True


# ---------------------------------------------------------------------------
# Structural person counting (Finding 1.1b)
# ---------------------------------------------------------------------------
# Measured 2026-09-02: fly.io/about lists 57 people styled entirely with
# Tailwind utility classes -- <figure> inside a grid <div>, with no "team",
# "member", "person" or "bio" anywhere in the markup. The class-based count
# returned 0 for a page listing 57 people by name.

_FLY_SHAPED = "".join(
    f'<figure class="w-full grid grid-cols-auto-span items-center">'
    f'<figcaption class="relative pl-6">'
    f'<div class="flex items-center">{name}</div>'
    f'<div class="text-sm">{role}</div>'
    f'<a href="#">GitHub</a></figcaption></figure>'
    for name, role in [
        ("Thomas Ptacek", "Developer"),
        ("Kurt Mackey", "CEO"),
        ("Ben Johnson", "VP of Product"),
        ("Chris McCord", "Developer"),
        ("Annie Sexton", "JavaScript Specialist"),
    ]
)


def test_people_are_counted_without_any_person_shaped_class_name() -> None:
    html = f'<html><body><div class="grid gap-x-8">{_FLY_SHAPED}</div></body></html>'

    assert ingest.count_people_by_class(html) == 0, (
        "precondition: this markup has no person-ish class names"
    )
    assert ingest.count_people_structurally(html) == 5
    assert ingest.count_people(html) == 5


def test_the_class_based_count_still_runs_when_structure_finds_nothing() -> None:
    """It needs no name-shaped text, so it covers rosters structure misses."""
    cards = "".join(
        '<div class="team-member"><div class="person-photo"></div>'
        '<div class="person-info-name">someone lowercase</div></div>'
        for _ in range(4)
    )
    html = f"<html><body><div class='team-grid'>{cards}</div></body></html>"

    assert ingest.count_people_structurally(html) == 0
    assert ingest.count_people(html) == 4


def test_a_narrative_about_page_counts_nobody() -> None:
    """basecamp.com/about is prose, not a roster. 0 is the correct answer."""
    html = (
        "<html><body><main>"
        "<p>Born out of desperate necessity to stop embarrassing ourselves.</p>"
        "<p>Hey there, I am Jason Fried, and this is the story of Basecamp.</p>"
        "</main></body></html>"
    )
    assert ingest.count_people(html) == 0


def test_navigation_is_not_mistaken_for_a_roster() -> None:
    nav = "".join(
        f"<li><a href='#'>{w}</a></li>"
        for w in ("Features", "Pricing", "Resources", "Changelog")
    )
    html = f"<html><body><nav><ul>{nav}</ul></nav></body></html>"
    assert ingest.count_people(html) == 0


def test_two_repeated_names_are_a_coincidence_not_a_roster() -> None:
    html = (
        "<html><body><div>"
        "<p>Ada Lovelace Developer</p><p>Grace Hopper Engineer</p>"
        "</div></body></html>"
    )
    assert ingest.count_people_structurally(html) == 0


def test_a_paragraph_starting_with_a_name_is_not_a_card() -> None:
    """Bounded by word count, so prose beginning with a name does not count."""
    prose = " ".join(f"word{i}" for i in range(40))
    html = (
        "<html><body><div>"
        + "".join(f"<p>Ada Lovelace {prose}</p>" for _ in range(4))
        + "</div></body></html>"
    )
    assert ingest.count_people_structurally(html) == 0


# ---------------------------------------------------------------------------
# Canonical URL choice (Finding 1.1c)
# ---------------------------------------------------------------------------


def _doc(url: str, text: str = "shared roster text here") -> ingest.Document:
    return ingest.Document(
        url=url, kind=ingest.classify(url), title="", text=text
    ).finalise()


def test_the_canonical_url_does_not_depend_on_crawl_order() -> None:
    """Which URL survives deduplication must not be an accident of the queue.

    Before this, `source_url` and `kind` were both decided by whichever copy
    happened to be fetched first, so a site reordering its own links could
    change a document's classification between runs.
    """
    a, b, c = (
        _doc("https://ex.com/careers/x"),
        _doc("https://ex.com/about"),
        _doc("https://ex.com/handbook/x"),
    )

    assert ingest.canonical_document([a, b, c]).url == "https://ex.com/about"
    assert ingest.canonical_document([c, a, b]).url == "https://ex.com/about"
    assert ingest.canonical_document([b, c, a]).url == "https://ex.com/about"


def test_the_canonical_choice_does_not_prefer_a_more_specific_kind() -> None:
    """Deliberate. No measurement says a specific kind should win.

    Neither validation site has produced two URLs for one page that disagree
    about `kind`. Inventing a winner would be an assumption dressed as a rule;
    the disagreement is recorded on the duplicate_content outcome instead.
    """
    specific = _doc("https://ex.com/careers/x")
    generic = _doc("https://ex.com/about")

    assert specific.kind == "job_posting" and generic.kind == "website"
    assert ingest.canonical_document([specific, generic]) is generic
