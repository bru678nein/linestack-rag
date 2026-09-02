"""
Prospect ingestion for the Linestack lead-gen RAG.

Crawls a single company domain, extracts readable content, classifies each page,
and computes the structured signals that should NOT be left to an LLM.

Design notes
------------
Politeness is not optional: robots.txt is honoured and requests are rate limited
per domain. This crawls company marketing sites, not user data.

Two kinds of output, deliberately separated:

  documents  -> text destined for chunking and embedding (retrieval answers)
  signals    -> computed facts (has a team page, N people listed, N open roles,
                most recent post date). These are deterministic. Passing them to
                the model as context beats asking it to infer them from prose,
                because "does this company have an in-house team" is exactly the
                question an LLM will answer confidently and wrongly.

No database writes here. This produces JSON; loading is a separate step so the
crawl can be rerun and diffed without touching Postgres.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable

import httpx
import trafilatura
from selectolax.parser import HTMLParser

USER_AGENT = "Linestack-Research/1.0 (+https://linestack.dev; contact: brunoracconto@live.com)"
DELAY_SECONDS = 1.5
TIMEOUT = 20.0
MAX_PAGES = 40

# Paths worth trying directly before falling back to link discovery.
SEED_PATHS = [
    "/", "/about", "/about-us", "/nosotros", "/quienes-somos", "/company",
    "/team", "/our-team", "/equipo", "/people", "/leadership",
    "/careers", "/jobs", "/join-us", "/trabaja-con-nosotros", "/empleos",
    "/blog", "/news", "/insights", "/noticias",
    "/services", "/products", "/solutions", "/servicios",
    "/contact", "/contacto",
]

KIND_PATTERNS = [
    ("job_posting", r"/(careers?|jobs?|vacan|empleos?|join-us|trabaja)"),
    ("blog_post",   r"/(blog|news|insights|posts?|articles?|noticias|novedades)"),
]

# Role words that indicate technical capacity when they appear on a team page.
TECH_ROLE_RE = re.compile(
    r"\b(developer|engineer|cto|programmer|architect|devops|sre|data\s+scientist|"
    r"desarrollador|ingenier[oa]|programador|tech\s+lead)\b", re.I)

DATE_RE = re.compile(
    r"\b(20[12]\d)[-/](\d{1,2})[-/](\d{1,2})\b")


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
# Outcome of the robots.txt fetch. "Could not read the policy" and "the policy
# said no" are different facts and must never collapse into one flag (A5).
ROBOTS_OK = "ok"                      # 2xx, parsed; its rules apply
ROBOTS_ABSENT = "absent"              # 4xx (not 401/403): no robots.txt exists
ROBOTS_UNREADABLE = "unreadable"      # 401/403: exists, withheld from us
ROBOTS_SERVER_ERROR = "server_error"  # 5xx: site is unwell, do not crawl
ROBOTS_FETCH_FAILED = "fetch_failed"  # transport error, timeout, DNS

# What happened to one URL. These strings ARE the `page_outcome` enum in
# migrations/0001_initial_schema.sql -- the crawler and the schema share one
# vocabulary so that `crawl_page_outcomes` can be loaded without translation.
# A prospect with no documents must say WHY (A5): a dead domain, a site that
# blocked us and a company with no website are three different facts.
PAGE_STORED = "stored"
PAGE_SKIPPED_ROBOTS = "skipped_robots"
PAGE_DNS_FAILURE = "dns_failure"
PAGE_TIMEOUT = "timeout"
PAGE_TRANSPORT_ERROR = "transport_error"
PAGE_HTTP_ERROR = "http_error"
PAGE_NON_HTML = "non_html"
PAGE_THIN_EXTRACTION = "thin_extraction"
PAGE_DUPLICATE_CONTENT = "duplicate_content"
PAGE_BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass
class PageOutcome:
    """One row of `crawl_page_outcomes`, mirrored field for field."""
    url: str
    outcome: str
    http_status: int | None = None
    detail: str = ""


def host_resolves(host: str) -> bool:
    """
    Whether `host` resolves. Used only to explain a failure, never to gate one.

    httpx flattens `socket.gaierror` into a plain `ConnectError` and drops the
    cause, so a non-resolving host and a refused connection arrive identical
    apart from an errno string. Matching that string is fragile; asking the
    resolver is not. Returning True when we cannot tell is deliberate: claiming
    `dns_failure` without evidence is exactly the invented measurement A5 and
    A4 exist to prevent.
    """
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False
    except Exception:
        return True


class PoliteClient:
    """One client per prospect. Honours robots.txt, rate limits per domain."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.domain = urllib.parse.urlparse(self.base).netloc.lower()
        self._last_hit = 0.0
        # The client must exist before robots.txt is fetched: robots.txt is
        # fetched through it, with our real User-Agent. See _load_robots.
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/html,application/xhtml+xml"},
            timeout=TIMEOUT, follow_redirects=True)
        self._robots, self.robots_reason = self._load_robots()

    def _request(self, url: str) -> httpx.Response:
        """Rate-limited GET. Raises on transport failure."""
        wait = DELAY_SECONDS - (time.monotonic() - self._last_hit)
        if wait > 0:
            time.sleep(wait)
        self._last_hit = time.monotonic()
        return self._client.get(url)

    def _load_robots(self) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        """
        Fetch robots.txt through our own client, with our own User-Agent.

        RobotFileParser.read() is deliberately NOT used. It fetches with
        urllib's User-Agent ("Python-urllib/x.y"), which bot protection
        commonly answers with 403; read() then swallows that error internally
        and sets disallow_all, without raising. The caller sees a healthy
        parser that denies every URL, the crawl returns nothing, and the run
        is recorded as "blocked by robots.txt" for a block the site never
        declared. Measured at 4 of 18 real company domains.

        Status handling follows RFC 9309 s2.3.1: 2xx applies the rules, 4xx
        permits crawling, 5xx means stay off the site entirely. The reason is
        returned alongside so the caller can record which case occurred.
        """
        url = f"{self.base}/robots.txt"
        try:
            r = self._request(url)
        except Exception:
            return None, ROBOTS_FETCH_FAILED

        if r.status_code in (401, 403):
            # robots.txt exists but is withheld. RFC 9309 s2.3.1.3 permits
            # crawling; what matters is that this is recorded as an unread
            # policy, not as a policy that denied us.
            return None, ROBOTS_UNREADABLE
        if 400 <= r.status_code < 500:
            return None, ROBOTS_ABSENT
        if r.status_code >= 500:
            return None, ROBOTS_SERVER_ERROR
        if r.status_code != 200:
            return None, ROBOTS_FETCH_FAILED

        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())  # parse(), not read(): no second fetch
        return rp, ROBOTS_OK

    def allowed(self, url: str) -> bool:
        if self.robots_reason == ROBOTS_SERVER_ERROR:
            return False  # RFC 9309 s2.3.1.4: treat as full disallow
        if self._robots is None:
            return True
        try:
            return self._robots.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def get(self, url: str) -> tuple[str | None, PageOutcome]:
        """Fetch one page. Every failure returns a classified outcome (A5)."""
        if not self.allowed(url):
            return None, PageOutcome(url, PAGE_SKIPPED_ROBOTS)
        try:
            r = self._request(url)
        except httpx.TimeoutException as exc:
            return None, PageOutcome(url, PAGE_TIMEOUT,
                                     detail=type(exc).__name__)
        except httpx.ConnectError as exc:
            host = urllib.parse.urlparse(url).hostname or ""
            if not host_resolves(host):
                return None, PageOutcome(url, PAGE_DNS_FAILURE, detail=host)
            return None, PageOutcome(url, PAGE_TRANSPORT_ERROR,
                                     detail=type(exc).__name__)
        except Exception as exc:
            return None, PageOutcome(url, PAGE_TRANSPORT_ERROR,
                                     detail=type(exc).__name__)
        if r.status_code != 200:
            return None, PageOutcome(url, PAGE_HTTP_ERROR,
                                     http_status=r.status_code)
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return None, PageOutcome(url, PAGE_NON_HTML,
                                     http_status=r.status_code,
                                     detail=ctype.split(";")[0].strip())
        return r.text, PageOutcome(url, PAGE_STORED, http_status=r.status_code)

    def close(self):
        self._client.close()


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
@dataclass
class Document:
    url: str
    kind: str
    title: str
    text: str
    published: str | None = None
    # Which extraction strategy produced `text`. A document recovered by the
    # DOM fallback is real content but lower confidence than one trafilatura
    # extracted cleanly, and downstream must be able to tell them apart (A4).
    extract_reason: str = ""
    # Exact hash of `text`. Any change at all changes it, reordering included.
    content_hash: str = ""
    # Hash of the same text with word order removed. Two texts that are
    # permutations of each other share it. See stable_digest and ADR-0013.
    stable_hash: str = ""
    # Other URLs that served this same content. Deduplication stores the text
    # once, but the URLs are evidence in their own right -- `has_team_page`
    # reads the path, not the text -- and discarding them turns a real team
    # page into a missing one. See ADR-0013.
    duplicate_urls: list[str] = field(default_factory=list)

    def finalise(self) -> "Document":
        self.content_hash = hashlib.sha256(self.text.encode()).hexdigest()[:16]
        self.stable_hash = stable_digest(self.text)
        return self


def stable_digest(text: str) -> str:
    """
    Hash `text` with word order removed, for "is this the same content?".

    Some sites shuffle repeated records on every request. **[verified]**
    2026-09-02: fly.io/about returns its team roster in a different order each
    time -- four consecutive fetches gave 316 words, four different exact
    hashes, and one identical word multiset. `/team` redirects to `/about` and
    matches that multiset too.

    An exact hash therefore reports a change on every crawl of a page that has
    not changed, which breaks A7, and fails to dedup two URLs serving one page.

    Word level is not a preference, it is what survives. The shuffle destroys
    adjacency at every record boundary, so bigrams and shingles do not survive
    it, and this page's extracted text has no line or block structure to sort
    instead: trafilatura returns all 316 words on a single line, and the DOM
    has one top-level block.

    The cost is a real collision mode: same words, different arrangement.
    "5 engineers and 2 designers" and "2 engineers and 5 designers" share a
    multiset. **[verified]** across the 78 documents of the two validation
    crawls this produced exactly one collision group, and it was the genuine
    duplicate. That is evidence, not a proof; `content_hash` stays exact so the
    reordering is always still visible.
    """
    return hashlib.sha256(
        "\x00".join(sorted(text.split())).encode()).hexdigest()[:16]


def classify(url: str, title: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for kind, pat in KIND_PATTERNS:
        if re.search(pat, path):
            return kind
    return "website"


# Minimum words for a page to be worth keeping. Below this there is nothing
# to embed, and a chunk of pure navigation is worse than no chunk at all.
MIN_WORDS = 30

# Outcome of extraction. "We got nothing" and "we got nothing the easy way"
# are different facts and must not collapse into one None (A5).
EXTRACT_OK = "ok"                    # precision pass produced usable text
EXTRACT_RECALL = "recovered_recall"  # precision was thin, recall pass recovered
EXTRACT_DOM = "recovered_dom"        # both trafilatura passes thin, DOM used
EXTRACT_THIN = "thin"                # every strategy came in under MIN_WORDS
EXTRACT_EMPTY = "empty"              # no strategy produced any text at all

# Structural elements whose text is chrome, not content. Stripped before the
# DOM fallback, which unlike trafilatura has no readability model of its own.
DOM_NOISE_TAGS = ("script", "style", "noscript", "svg", "template",
                  "iframe", "nav", "footer", "form")


def _trafilatura_text(html: str, url: str, *, precision: bool) -> str:
    return trafilatura.extract(
        html, include_comments=False, include_tables=True,
        favor_precision=precision, url=url) or ""


def dom_text(html: str) -> str:
    """Last-resort extraction: main/article/body text with chrome removed."""
    tree = HTMLParser(html)
    for tag in DOM_NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()
    root = tree.css_first("main") or tree.css_first("article") or tree.body
    if root is None:
        return ""
    return re.sub(r"\s+", " ", root.text(separator=" ")).strip()


def extract(url: str, html: str) -> tuple[Document | None, str]:
    """
    Extract readable text, escalating when precision mode throws it away.

    favor_precision=True is the right default for a RAG corpus: it drops
    navigation, boilerplate and related-post lists that would otherwise be
    embedded as though they were content. But it also discards real content
    on heavily templated pages.

    Measured 2026-09-02: fly.io/about is 249,893 bytes containing the entire
    team roster, and precision mode extracts 29 words of it -- one word below
    the old threshold, so the page vanished and `has_team_page` read False.
    The page is not client-rendered; the names are in the HTML. Recall mode
    returns 316 words of it.

    So try precision, then recall, then raw DOM text. Keep whichever clears
    MIN_WORDS first, and record which one it was.
    """
    for reason, text in (
        (EXTRACT_OK, _trafilatura_text(html, url, precision=True)),
        (EXTRACT_RECALL, _trafilatura_text(html, url, precision=False)),
        (EXTRACT_DOM, dom_text(html)),
    ):
        if len(text.split()) >= MIN_WORDS:
            break
    else:
        return None, EXTRACT_THIN if text.strip() else EXTRACT_EMPTY

    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""

    published = None
    meta = trafilatura.extract_metadata(html)
    if meta and meta.date:
        published = meta.date
    else:
        m = DATE_RE.search(text[:400])
        if m:
            published = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    return Document(url=url, kind=classify(url, title), title=title,
                    text=text, published=published,
                    extract_reason=reason).finalise(), reason


def discover_links(html: str, base_url: str, domain: str) -> list[str]:
    """Same-domain links that look like content worth reading."""
    out: list[str] = []
    keep = re.compile(
        r"/(about|nosotros|quienes|team|equipo|people|careers?|jobs?|empleos?|"
        r"blog|news|noticias|insights|services?|servicios|products?|solutions?)",
        re.I)
    for node in HTMLParser(html).css("a[href]"):
        href = node.attributes.get("href") or ""
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full)
        if parsed.netloc.lower() != domain:
            continue
        full = normalise(parsed._replace(fragment="", query="").geturl())
        if keep.search(parsed.path) and full not in out:
            out.append(full)
    return out


def normalise(url: str) -> str:
    """
    Collapse /about and /about/ to one URL. Without this the crawler spends
    two requests per page on sites that serve both, which is wasted budget
    against someone else's server.
    """
    p = urllib.parse.urlparse(url)
    path = p.path.rstrip("/") or "/"
    return p._replace(path=path, fragment="", query="").geturl()


# --------------------------------------------------------------------------- #
# computed signals
# --------------------------------------------------------------------------- #
@dataclass
class Signals:
    """Deterministic facts. Never inferred, always computed and citable."""
    has_team_page: bool = False
    team_page_url: str | None = None
    people_listed: int = 0
    technical_roles_named: int = 0
    has_careers_page: bool = False
    open_roles_seen: int = 0
    technical_roles_open: int = 0
    blog_posts_seen: int = 0
    latest_post_date: str | None = None
    pages_crawled: int = 0
    total_words: int = 0


TEAM_PATH_RE = re.compile(r"/(team|equipo|people|leadership|nosotros)")

PERSON_SELECTORS = ("[class*=team]", "[class*=member]", "[class*=staff]",
                    "[class*=person]", "[class*=bio]", "[class*=profile]")


_PERSON_KEYWORD_RE = re.compile(r"\[class\*=([a-z]+)\]")


def count_people(html: str) -> int:
    """
    Count person-cards on a team page. Structural, not semantic: repeated
    elements whose class suggests a person, which is how nearly every CMS
    renders a team grid.

    Two traps, in opposite directions.

    A wrapper matches too: <div class="team-grid"> containing four
    <div class="team-member"> makes a naive count return 5. So only leaf-most
    matches count.

    But one card usually holds SEVERAL matching leaves. thoughtbot's team page
    renders 54 people as 54 x {person-photo, person-info-name,
    person-info-title}: 162 leaves for 54 people. Counting leaves overcounts by
    exactly the number of matching elements per card.

    So leaves are grouped by their signature -- tag plus the class tokens that
    matched -- and the largest single group wins. A CMS emits one identical
    card per person, so the repeated signature occurs exactly once per person.
    Utility classes ("u-margin-bottom-2") are excluded from the signature so
    they cannot split a group.
    """
    tree = HTMLParser(html)
    best = 0
    for sel in PERSON_SELECTORS:
        m = _PERSON_KEYWORD_RE.search(sel)
        if not m:
            continue
        keyword = m.group(1)
        nodes = tree.css(sel)
        leaves = [n for n in nodes if not n.css(sel)[1:]]  # css() includes self
        groups: dict[tuple, int] = {}
        for n in leaves:
            tokens = tuple(sorted(
                t for t in (n.attributes.get("class") or "").split()
                if keyword in t.lower()))
            key = (n.tag, tokens)
            groups[key] = groups.get(key, 0) + 1
        if groups:
            best = max(best, max(groups.values()))
    return best


# A careers listing lives AT the careers path; a posting lives below it.
ROLE_LISTING_RE = re.compile(
    r"^/(careers?|jobs?|empleos?|vacantes?|join-us|trabaja[\w-]*)/?$", re.I)

# Sub-paths that are files, not vacancies.
NON_ROLE_EXT_RE = re.compile(r"\.(xml|json|rss|atom|pdf|ics|txt)$", re.I)

# " . Fly", " | Acme", " - Acme" trailing site names. Plain hyphen is excluded
# on purpose: it would mangle "Front-End Developer".
TITLE_SUFFIX_RE = re.compile(
    r"\s*[\u00b7|\u00bb]\s*[^\u00b7|\u00bb]{1,40}$|\s+[\u2014\u2013]\s+[^\u2014\u2013]{1,40}$")

JOB_TITLE_RE = re.compile(
    r"\b(manager|engineer|developer|coordinator|analyst|specialist|director|"
    r"designer|associate|assistant|lead|architect|consultant|representative|"
    r"gerente|desarrollador|analista|coordinador|responsable)\b", re.I)


def extract_role_headings(html: str) -> list[str]:
    """Role titles listed on a careers page, one heading each."""
    if not html:
        return []
    out = []
    for node in HTMLParser(html).css("h2, h3, h4, [class*=job], [class*=position], [class*=vacan]"):
        t = node.text(strip=True)
        if t and 3 < len(t) < 80 and JOB_TITLE_RE.search(t) and t not in out:
            out.append(t)
    return out


def role_name(url: str, title: str) -> str:
    """Best available human name for a role: the page title, else the URL slug."""
    if title:
        return TITLE_SUFFIX_RE.sub("", title).strip()
    slug = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").replace("_", " ").strip()


def listing_role_links(html: str, listing_url: str) -> list[str]:
    """Same-domain links from a careers listing that go deeper than the listing."""
    if not html:
        return []
    parsed = urllib.parse.urlparse(listing_url)
    base_path = parsed.path.rstrip("/").lower()
    domain = parsed.netloc.lower()
    out: list[str] = []
    for node in HTMLParser(html).css("a[href]"):
        href = node.attributes.get("href") or ""
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = normalise(urllib.parse.urljoin(listing_url, href))
        p = urllib.parse.urlparse(full)
        if p.netloc.lower() != domain:
            continue
        if p.path.lower().startswith(base_path + "/") and full not in out:
            out.append(full)
    return out


def compute_signals(docs: list[Document], raw: dict[str, str]) -> Signals:
    s = Signals(pages_crawled=len(docs))
    s.total_words = sum(len(d.text.split()) for d in docs)

    for d in docs:
        # Every URL that served this document, not just the surviving one. A
        # page reachable at both /about and /team is a team page, and which of
        # the two won deduplication is an accident of crawl order.
        team_url = next(
            (u for u in (d.url, *d.duplicate_urls)
             if TEAM_PATH_RE.search(urllib.parse.urlparse(u).path.lower())),
            None)
        if team_url:
            if not s.has_team_page:
                s.has_team_page = True
                s.team_page_url = team_url
                s.people_listed = count_people(raw.get(team_url, ""))
            s.technical_roles_named += len(TECH_ROLE_RE.findall(d.text))

        if d.kind == "blog_post":
            s.blog_posts_seen += 1
            if d.published and (s.latest_post_date is None
                                or d.published > s.latest_post_date):
                s.latest_post_date = d.published

    _count_open_roles(s, docs, raw)
    return s


def _count_open_roles(s: Signals, docs: list[Document],
                      raw: dict[str, str]) -> None:
    """
    Count vacancies by role IDENTITY, never by page.

    Counting pages classified as job_posting is wrong three ways at once. It
    counts the careers listing as a vacancy alongside the postings it links to
    (fly.io: 3 for 2 real roles). It counts policy pages -- compensation
    calculators, internal career ladders -- as vacancies (thoughtbot: 4 for 0
    real roles). And it misses roles that were listed but never crawled,
    because the page budget ran out.

    So: a listing contributes the roles it links to, never itself; a posting
    contributes itself; both are keyed by URL and deduplicated; and a candidate
    only survives if its name reads like a job title (JOB_TITLE_RE), which is
    what excludes "Compensation calculator" and "Career Paths".
    """
    listings: list[str] = []
    candidates: dict[str, str] = {}          # url (or heading key) -> role name

    for d in docs:
        if d.kind != "job_posting":
            continue
        s.has_careers_page = True
        if ROLE_LISTING_RE.match(urllib.parse.urlparse(d.url).path):
            listings.append(d.url)
        else:
            candidates[normalise(d.url)] = role_name(d.url, d.title)

    for lu in listings:
        html = raw.get(lu, "")
        links = listing_role_links(html, lu)
        for link in links:
            candidates.setdefault(normalise(link), role_name(link, ""))
        if not links:
            # Some listings name roles inline without linking them.
            for h in extract_role_headings(html):
                candidates.setdefault("heading:" + h.lower(), h)

    roles = [n for key, n in candidates.items()
             if not NON_ROLE_EXT_RE.search(key) and JOB_TITLE_RE.search(n)]
    s.open_roles_seen = len(roles)
    s.technical_roles_open = sum(1 for n in roles if TECH_ROLE_RE.search(n))


# --------------------------------------------------------------------------- #
# crawl budget
# --------------------------------------------------------------------------- #
# Share of the page budget each kind may claim before the others are served.
#
# A FIFO queue does not divide the budget between the four questions -- it lets
# link volume divide it. A blog index links to dozens of posts; an About page
# links to none. Measured on fly.io: 34 of 40 pages came back blog posts and the
# crawl never reached a team page, so question 2 ("evidence of in-house
# technical capacity") was unanswerable from a full-budget crawl. That reads as
# a retrieval failure during evaluation when it is really an ingestion one.
#
# A quota is a CAP on a kind, never a floor. Balancing fill ratios instead --
# always serving whichever kind is furthest below its share -- was measured and
# reverted (A3): on thoughtbot it promoted blog tag-index pages over real
# content, taking websites 22 -> 18 and blogs 12 -> 17. Being under quota must
# not earn a low-value page priority over a high-value one.
#
# So: pages are served by how directly they answer the four questions, and a
# kind that has hit its cap yields to every kind that has not. Leftover budget
# still goes to capped kinds rather than going unspent.
KIND_QUOTA = {"website": 0.45, "job_posting": 0.30, "blog_post": 0.25}
DEFAULT_QUOTA = 0.05

# An About/Services page and a job posting answer questions 1, 2 and 4
# directly. A blog post is corroboration and recency evidence -- valuable, but
# not at the price of never fetching the team page.
KIND_PRIORITY = {"website": 0, "job_posting": 0, "blog_post": 1}
DEFAULT_PRIORITY = 2


def queue_rank(kind: str, counts: dict[str, int], max_pages: int) -> tuple[int, int]:
    """Sort key for the crawl queue. Lower is fetched sooner."""
    cap = max(1, int(max_pages * KIND_QUOTA.get(kind, DEFAULT_QUOTA)))
    over_cap = 1 if counts.get(kind, 0) >= cap else 0
    return (over_cap, KIND_PRIORITY.get(kind, DEFAULT_PRIORITY))


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
@dataclass
class Prospect:
    company_name: str
    domain: str
    base_url: str
    documents: list[Document] = field(default_factory=list)
    signals: Signals = field(default_factory=Signals)
    crawled_at: str = ""
    robots_reason: str = ""
    # One classified outcome per URL the crawl touched, stored and failed
    # alike. Replaces the earlier `skipped_by_robots` and `dropped_pages`
    # lists: one vocabulary, one row per URL, matching `crawl_page_outcomes`
    # (which is UNIQUE on (crawl_run_id, url), so a URL gets exactly one).
    page_outcomes: list[dict] = field(default_factory=list)

    def outcomes(self, kind: str) -> list[dict]:
        return [o for o in self.page_outcomes if o["outcome"] == kind]


def ingest(base_url: str, company_name: str = "",
           max_pages: int = MAX_PAGES, verbose: bool = True) -> Prospect:
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    client = PoliteClient(base_url)
    domain = client.domain
    if verbose:
        print(f"  robots.txt: {client.robots_reason}")

    queue: list[str] = [normalise(urllib.parse.urljoin(base_url, p)) for p in SEED_PATHS]
    seen: set[str] = set()
    docs: list[Document] = []
    raw: dict[str, str] = {}
    # url -> PageOutcome. A dict, not a list: the destination table is UNIQUE
    # on (crawl_run_id, url), and a later outcome supersedes an earlier one --
    # a stored page that turns out to be a duplicate is a duplicate, not both.
    outcomes: dict[str, PageOutcome] = {}

    kind_counts: dict[str, int] = {}

    while queue and len(docs) < max_pages:
        # Serve the highest-value kind that is still under its cap, oldest URL
        # within it. Kind comes from the path, so this costs no extra request.
        idx = min(range(len(queue)),
                  key=lambda i: (queue_rank(classify(queue[i], ""),
                                            kind_counts, max_pages), i))
        url = normalise(queue.pop(idx))
        if url in seen:
            continue
        seen.add(url)

        if not client.allowed(url):
            outcomes[url] = PageOutcome(url, PAGE_SKIPPED_ROBOTS)
            continue

        html, outcome = client.get(url)
        outcomes[url] = outcome
        if html is None:
            continue

        raw[url] = html
        doc, reason = extract(url, html)
        if doc:
            docs.append(doc)
            kind_counts[doc.kind] = kind_counts.get(doc.kind, 0) + 1
            if verbose:
                mark = "" if reason == EXTRACT_OK else f" <{reason}>"
                print(f"  [{doc.kind:11}] {doc.url}{mark}")
        else:
            # `thin` and `empty` are the word-count distinction the schema
            # comment asks for, at the granularity that changes a diagnosis:
            # empty usually means client-rendered, thin means we nearly had it.
            outcomes[url] = PageOutcome(url, PAGE_THIN_EXTRACTION,
                                        http_status=200, detail=reason)

        for link in discover_links(html, url, domain):
            if link not in seen and len(seen) + len(queue) < max_pages * 3:
                queue.append(link)

    client.close()

    # Anything still queued was never fetched: the budget ran out. This is the
    # difference between "we looked and there was nothing" and "we stopped
    # looking", and only one of those is a fact about the company.
    for leftover in queue:
        outcomes.setdefault(leftover, PageOutcome(leftover,
                                                  PAGE_BUDGET_EXHAUSTED))

    # Drop near-duplicate pages (same content on /about and /about-us).
    # Keyed on stable_hash, not content_hash: a site that shuffles repeated
    # records serves one page under two URLs with two different exact hashes,
    # and exact-hash dedup keeps both (ADR-0013).
    unique: dict[str, Document] = {}
    for d in docs:
        first = unique.setdefault(d.stable_hash, d)
        if first is not d:
            # Same words, different exact hash, proves the source reorders its
            # content between requests. Worth recording rather than collapsing
            # into a plain duplicate: it is the observable evidence of an A7
            # violation on that URL, and the only place we can see it without
            # fetching the same URL twice.
            first.duplicate_urls.append(d.url)
            how = ("reordered" if d.content_hash != first.content_hash
                   else "identical")
            outcomes[d.url] = PageOutcome(d.url, PAGE_DUPLICATE_CONTENT,
                                          http_status=200,
                                          detail=f"{how} of {first.url}")
    docs = list(unique.values())

    return Prospect(
        company_name=company_name or domain,
        domain=domain,
        base_url=base_url,
        documents=docs,
        signals=compute_signals(docs, raw),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        robots_reason=client.robots_reason,
        page_outcomes=[asdict(o) for o in outcomes.values()],
    )


def explain_empty_crawl(p: Prospect) -> str:
    """
    Why a prospect ended with no documents.

    "No documents" is not a fact about a company until the reason is known.
    A dead domain, a site that refused us, and a business with no web presence
    are three different answers to question 1, and before this they were all
    reported as a successful crawl of nothing (A5).
    """
    tally: dict[str, int] = {}
    for o in p.page_outcomes:
        tally[o["outcome"]] = tally.get(o["outcome"], 0) + 1

    # Transport failures come first, and deliberately outrank the robots.txt
    # reason. A robots.txt that "could not be fetched" on a domain that does
    # not resolve is a symptom being reported as the cause, which sends the
    # reader to check a robots policy on a host that does not exist.
    for transport in (PAGE_DNS_FAILURE, PAGE_TIMEOUT, PAGE_TRANSPORT_ERROR):
        if tally.get(transport):
            return (f"{tally[transport]} of {len(p.page_outcomes)} URLs ended "
                    f"in {transport}")
    if p.robots_reason == ROBOTS_SERVER_ERROR:
        return "robots.txt returned 5xx; RFC 9309 treats that as full disallow"
    if p.robots_reason == ROBOTS_FETCH_FAILED:
        return "robots.txt could not be fetched at all"
    if not tally:
        return "no URL was even attempted"
    dominant, count = max(tally.items(), key=lambda kv: kv[1])
    return f"{count} of {len(p.page_outcomes)} URLs ended in {dominant}"


def to_json(p: Prospect) -> str:
    d = asdict(p)
    return json.dumps(d, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    failures = 0
    for target in sys.argv[1:]:
        print(f"\n=== {target} ===")
        prospect = ingest(target)
        out = f"prospect_{prospect.domain.replace('.', '_')}.json"
        with open(out, "w", encoding="utf-8") as f:
            f.write(to_json(prospect))
        s = prospect.signals
        print(f"  robots:    {prospect.robots_reason} "
              f"({len(prospect.outcomes(PAGE_SKIPPED_ROBOTS))} paths skipped)")
        print(f"  {s.pages_crawled} pages, {s.total_words} words")
        recovered = sum(1 for d in prospect.documents
                        if d.extract_reason != EXTRACT_OK)
        print(f"  extract:   {recovered} recovered, "
              f"{len(prospect.outcomes(PAGE_THIN_EXTRACTION))} too thin")
        tally: dict[str, int] = {}
        for o in prospect.page_outcomes:
            tally[o["outcome"]] = tally.get(o["outcome"], 0) + 1
        print("  outcomes:  " + ", ".join(
            f"{k} {v}" for k, v in sorted(tally.items())))
        print(f"  team page: {s.has_team_page} ({s.people_listed} people, "
              f"{s.technical_roles_named} technical mentions)")
        print(f"  careers:   {s.has_careers_page} ({s.open_roles_seen} roles, "
              f"{s.technical_roles_open} technical)")
        print(f"  blog:      {s.blog_posts_seen} posts, latest {s.latest_post_date}")
        print(f"  -> {out}")
        if not prospect.documents:
            # A crawl that found nothing must not look like a crawl that
            # worked. Exit non-zero so a pipeline notices.
            print(f"  FAILED: no documents -- {explain_empty_crawl(prospect)}")
            failures += 1
    if failures:
        raise SystemExit(1)
