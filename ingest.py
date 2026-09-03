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

# Page kind from the URL path. Matched against whole path SEGMENTS, never as a
# substring: `careers?` anywhere in the path classified
# /playbook/our-company/career-paths as a job posting, and
# /playbook/strategy/design-sprints/02-pre-product-validation/jobs-profile too.
# Both verified on thoughtbot.com. `kind` is denormalised onto chunks for
# retrieval source weighting (ADR-0004), so a playbook article carrying
# job-posting weight is a retrieval defect, not a cosmetic one.
KIND_PATTERNS = [
    ("job_posting", r"^(careers?|jobs?|vacantes?|empleos?|join-us|trabaja[\w-]*)$"),
    ("blog_post",   r"^(blog|news|insights|posts?|articles?|noticias|novedades)$"),
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

# How the crawl as a whole ended. These strings ARE the `crawl_outcome` enum in
# migrations/0001_initial_schema.sql, the same shared-vocabulary rule as
# PAGE_* above (ADR-0012).
CRAWL_COMPLETED = "completed"
CRAWL_ABORTED_ROBOTS = "aborted_robots"
CRAWL_ABORTED_UNREACHABLE = "aborted_unreachable"
CRAWL_FAILED = "failed"

# Consecutive transport failures, with nothing yet fetched, before a host is
# declared unreachable. A host that does not resolve is settled after one
# attempt and does not wait for this; a host that HANGS cannot be, because a
# single timeout may be transient. Three bounds that case at 3 x TIMEOUT.
UNREACHABLE_STREAK = 3


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
        # Set by _load_robots when robots.txt fails at the transport level.
        self.robots_failure: PageOutcome | None = None
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

    def _transport_outcome(self, url: str, exc: Exception) -> PageOutcome:
        """Classify a failed request. DNS is confirmed with the resolver."""
        if isinstance(exc, httpx.TimeoutException):
            return PageOutcome(url, PAGE_TIMEOUT, detail=type(exc).__name__)
        if isinstance(exc, httpx.ConnectError):
            host = urllib.parse.urlparse(url).hostname or ""
            if not host_resolves(host):
                return PageOutcome(url, PAGE_DNS_FAILURE, detail=host)
        return PageOutcome(url, PAGE_TRANSPORT_ERROR, detail=type(exc).__name__)

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
        except Exception as exc:
            # Keep WHY it failed, not just that it did. robots.txt is the first
            # request of every crawl, so its transport failure is the earliest
            # evidence that the host is unreachable -- and the cheapest place
            # to stop (see ingest()).
            self.robots_failure = self._transport_outcome(url, exc)
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
        except Exception as exc:
            return None, self._transport_outcome(url, exc)
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
    # Kinds claimed by the URLs that lost deduplication, when they disagree
    # with `kind`. Empty is the normal case and means the classification is
    # uncontested; a non-empty list means two of the site's own URLs classify
    # one page differently and `kind` is the alphabetically-first URL's claim,
    # not a decision between them. Downstream weights `kind` (ADR-0004), so
    # "contested" and "settled" must be distinguishable (A4). See ADR-0019.
    kind_conflicts: list[str] = field(default_factory=list)

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


def classify(url: str) -> str:
    """
    Page kind from the URL path alone. The title is deliberately not used.

    The signature used to take a `title` it ignored. Before dropping it, the
    title was measured as a signal: **[verified]** 2026-09-02, across both
    validation corpora the only three pages whose `<title>` matches
    career/job/hiring words are thoughtbot playbook ARTICLES -- "Career Paths |
    thoughtbot's Playbook", "Jobs Profile | ...", "Hiring | ...". Three false
    positives, zero true positives. Using the title would have reintroduced
    exactly the misclassification that segment matching just fixed.

    So the parameter is gone rather than wired up, and this comment is the
    reason, so nobody re-adds it on the reasonable-sounding theory that a
    title saying "Careers" means a careers page.
    """
    segments = [seg for seg in
                urllib.parse.urlparse(url).path.lower().split("/") if seg]
    for kind, pat in KIND_PATTERNS:
        if any(re.match(pat, seg) for seg in segments):
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

    return Document(url=url, kind=classify(url), title=title,
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

# Paths that MIGHT hold a roster without saying so. A page here counts as the
# team page only if a roster is actually found on it, because these words are
# also the URL of a narrative company story with nobody named on it --
# basecamp.com/about is exactly that, and is the negative control for this
# rule. TEAM_PATH_RE needs no such proof: a page at /team is a team page even
# when its roster is markup we cannot parse, and calling it one is the honest
# answer. See ADR-0018.
ABOUT_PATH_RE = re.compile(
    r"/(about|about-us|company|quienes-somos|sobre-nosotros)")

PERSON_SELECTORS = ("[class*=team]", "[class*=member]", "[class*=staff]",
                    "[class*=person]", "[class*=bio]", "[class*=profile]")


_PERSON_KEYWORD_RE = re.compile(r"\[class\*=([a-z]+)\]")


# A person entry names a person: two or more capitalised words, allowing
# initials ("A. S.") and hyphenated or apostrophised surnames.
PERSON_NAME_RE = re.compile(r"^[A-Z][\w.'\u2019-]*(?:\s+[A-Z][\w.'\u2019-]*)+")

# A card holds a name and usually a role and a couple of links. Anything much
# longer is a paragraph that happens to start with a name; anything shorter is
# not a card. Bounds are inclusive, in words.
PERSON_CARD_WORDS = (2, 20)

# Fewer repetitions than this is not a roster, it is a coincidence.
MIN_ROSTER = 3


def count_people(html: str) -> int:
    """
    Count person-cards on a team page.

    Two independent strategies, because the class-based one alone is not
    enough. **[verified]** 2026-09-02: fly.io/about lists 57 people and styles
    them entirely with Tailwind utility classes -- `<figure>` inside a grid
    `<div class="grid md:grid-cols-2 ...">`. There is no `team`, `member`,
    `person` or `bio` anywhere in the markup, so the class-based count was 0
    for a page listing 57 people by name.

    `count_people_structurally` is tried first and the class-based count is the
    fallback. Structural matched hand-counted truth exactly on both validation
    sites -- fly.io 57, thoughtbot 54 -- while the class-based count got
    thoughtbot right and fly.io wrong, so where the two disagree the structural
    one has the better record. The class-based path is kept because it needs no
    name-shaped text, and so still covers rosters this one cannot see.

    **Fixed 2026-09-03, [verified]:** buttondown.com/about lists its team by
    first name only, and both strategies returned 0. The earlier note here
    reasoned that relaxing the pattern to a single capitalised word would
    count every navigation item ("Features", "Pricing") -- true, and the
    reason the fix is not a looser name pattern. `count_people_by_portrait`
    keys on a different property entirely: one distinct image per repeated
    sibling. Navigation items do not carry a portrait each.

    Order is deliberate: the two strategies with the longest verified record
    run first, and the portrait pass only sees pages where both found nothing.
    """
    return (count_people_structurally(html)
            or count_people_by_class(html)
            or count_people_by_portrait(html))


def count_people_structurally(html: str) -> int:
    """
    Count repeated sibling elements whose text reads like a person entry.

    A roster is a CMS emitting one identical element per person, so the signal
    is repetition among siblings of the same tag, independent of what the site
    calls its classes. Siblings are grouped from the parent, not by inspecting
    each node's parent: selectolax does not give stable identity to a repeated
    `.parent` access, so grouping on it silently produces no groups at all.
    """
    tree = HTMLParser(html)
    for tag in DOM_NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()

    lo, hi = PERSON_CARD_WORDS
    best = 0
    for parent in tree.css("*"):
        by_tag: dict[str, int] = {}
        for child in parent.iter():
            text = re.sub(r"\s+", " ", child.text(separator=" ")).strip()
            if lo <= len(text.split()) <= hi and PERSON_NAME_RE.match(text):
                by_tag[child.tag] = by_tag.get(child.tag, 0) + 1
        for count in by_tag.values():
            if count >= MIN_ROSTER:
                best = max(best, count)
    return best


def count_people_by_portrait(html: str) -> int:
    """
    Count repeated sibling elements that each carry their own portrait.

    The last resort, for a roster whose entries are named by first name only.
    **[verified]** 2026-09-03: buttondown.com/about lists 14 people as
    "Anita", "Ben", "Justin", "nickd" -- one word each, one of them not even
    capitalised. `count_people_structurally` needs two capitalised words and
    `count_people_by_class` needs a person-ish class name; buttondown offers
    neither, and both returned 0 for a page listing 14 people.

    The discriminator is NOT a looser name pattern. Section 1.1b was right
    that accepting one capitalised word counts every navigation item, and
    capitalisation buys nothing against a navigation bar anyway -- "Features"
    and "Pricing" are capitalised too. What a navigation item does not have is
    a portrait of its own. So: repeated siblings of the same tag, each holding
    exactly one image, each image DISTINCT, and almost no text. A CMS emitting
    a roster gives every person a different photograph; a menu that repeats an
    icon, or repeats none, matches nothing here.

    `nickd` is the point of dropping the capitalisation requirement entirely
    rather than relaxing it to one word: the image is doing the work, so the
    text only has to be short. That is what makes the count 14 and not 13.

    **Known limitation, deliberate.** A marketing grid of feature cards --
    distinct illustration, two-word caption, four of them -- matches this
    shape and would be counted as people. Two things bound it: the pass runs
    only when the other two strategies both found nothing, and `count_people`
    is only ever called on a page already identified as a roster page
    (compute_signals). It is a fallback on a narrow surface, not a detector
    turned loose on a whole site.
    """
    tree = HTMLParser(html)
    for tag in DOM_NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()

    # Same parent-side grouping as count_people_structurally, and for the same
    # reason: selectolax gives no stable identity to a repeated `.parent`.
    _, hi = PERSON_CARD_WORDS
    best = 0
    for parent in tree.css("*"):
        by_tag: dict[str, list[str]] = {}
        for child in parent.iter():
            images = child.css("img")
            if len(images) != 1:
                continue
            text = re.sub(r"\s+", " ", child.text(separator=" ")).strip()
            if not text or len(text.split()) > hi:
                continue
            by_tag.setdefault(child.tag, []).append(
                images[0].attributes.get("src") or "")
        for sources in by_tag.values():
            # All distinct, not merely mostly: a repeated src is a shared icon,
            # which is the shape this pass exists to refuse.
            if len(sources) >= MIN_ROSTER and len(set(sources)) == len(sources):
                best = max(best, len(sources))
    return best


def count_people_by_class(html: str) -> int:
    """
    Count person-cards by the class names a CMS gives them.

    Structural, not semantic: repeated elements whose class suggests a person,
    which is how nearly every CMS renders a team grid.

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


@dataclass
class RosterPage:
    """The page `has_team_page` and `people_listed` both describe."""
    url: str
    document: "Document"
    people: int
    on_team_path: bool


def choose_roster_page(docs: list[Document],
                       raw: dict[str, str]) -> RosterPage | None:
    """
    Pick the one page the team signals describe, or None.

    Two rules, and the asymmetry between them is the whole point.

    A URL the site files under /team (TEAM_PATH_RE) is a team page whether or
    not we can parse its roster. Reporting `has_team_page: False` because the
    markup defeated us would be an ingestion artifact reported as a fact about
    the company -- the same defect as section 1.1.

    A URL under /about qualifies only on evidence: MIN_ROSTER people actually
    found on it. **[verified]** 2026-09-03, this is what separates
    buttondown.com/about (14 people, a roster) from basecamp.com/about (a
    narrative story, nobody named). Admitting /about unconditionally would
    turn basecamp's correct `has_team_page: False` into a false positive, and
    a signal that is true for everyone is worth nothing to qualification.

    Every URL that served a document is considered, not just the one that
    survived deduplication: fly.io's roster is at /about and /team both, and
    which one won dedup is an accident of crawl order (ADR-0013).

    The choice is deterministic and independent of crawl order, the same
    requirement section 1.1c settled for canonical_document: most people wins,
    a /team path breaks a tie, and the alphabetically first URL breaks what is
    left.
    """
    candidates: list[RosterPage] = []
    seen: set[str] = set()
    for d in docs:
        for url in (d.url, *d.duplicate_urls):
            if url in seen:
                continue
            path = urllib.parse.urlparse(url).path.lower()
            on_team_path = bool(TEAM_PATH_RE.search(path))
            if not on_team_path and not ABOUT_PATH_RE.search(path):
                continue
            seen.add(url)
            people = count_people(raw.get(url, ""))
            if on_team_path or people >= MIN_ROSTER:
                candidates.append(RosterPage(url, d, people, on_team_path))

    if not candidates:
        return None
    # Sorted by URL first, so that `max` -- which returns the FIRST maximal
    # element -- resolves a remaining tie to the alphabetically first URL.
    candidates.sort(key=lambda c: c.url)
    return max(candidates, key=lambda c: (c.people, c.on_team_path))


def compute_signals(docs: list[Document], raw: dict[str, str]) -> Signals:
    s = Signals(pages_crawled=len(docs))
    s.total_words = sum(len(d.text.split()) for d in docs)

    roster = choose_roster_page(docs, raw)
    if roster:
        s.has_team_page = True
        s.team_page_url = roster.url
        s.people_listed = roster.people

    for d in docs:
        # Role words are counted on the roster page and on any page the site
        # itself files under /team, which are not always the same document.
        on_team_path = any(
            TEAM_PATH_RE.search(urllib.parse.urlparse(u).path.lower())
            for u in (d.url, *d.duplicate_urls))
        if on_team_path or (roster and d is roster.document):
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
    # How the crawl ended, in the schema's own `crawl_outcome` vocabulary.
    # "No documents" is not the same fact as "we stopped looking" (A5).
    crawl_outcome: str = CRAWL_COMPLETED
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
    aborted = False

    # Fast-fail. robots.txt is the first request of every crawl, so a transport
    # failure on it is the earliest evidence the host is unreachable. A host
    # that does not resolve is settled by one attempt -- checked against the
    # resolver, not guessed -- and no seed path will fare better.
    #
    # Before this, a non-resolving domain cost 26 attempts at DELAY_SECONDS
    # apart: [verified] 39.5 s of politeness extended to a host that does not
    # exist. A host that HANGS was worse, at roughly 24 x TIMEOUT.
    fail = client.robots_failure
    if fail is not None and fail.outcome in (PAGE_DNS_FAILURE,
                                             PAGE_TRANSPORT_ERROR):
        client.close()
        return _unreachable(base_url, company_name, domain, fail,
                            CRAWL_ABORTED_UNREACHABLE,
                            client.robots_reason, verbose)

    # A 5xx robots.txt is a full disallow (RFC 9309 s2.3.1.4), so every URL
    # would be skipped one at a time. Say so once instead.
    if client.robots_reason == ROBOTS_SERVER_ERROR:
        client.close()
        return _unreachable(
            base_url, company_name, domain,
            PageOutcome(f"{base_url}/robots.txt", PAGE_SKIPPED_ROBOTS,
                        detail="robots.txt returned 5xx: full disallow"),
            CRAWL_ABORTED_ROBOTS, client.robots_reason, verbose)

    # Consecutive transport failures with nothing fetched yet. A host that
    # accepts connections and then hangs cannot be settled by one attempt the
    # way a non-resolving one can, so it is bounded rather than diagnosed.
    streak = 0

    while queue and len(docs) < max_pages:
        # Serve the highest-value kind that is still under its cap, oldest URL
        # within it. Kind comes from the path, so this costs no extra request.
        idx = min(range(len(queue)),
                  key=lambda i: (queue_rank(classify(queue[i]),
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
            if outcome.outcome in (PAGE_DNS_FAILURE, PAGE_TIMEOUT,
                                   PAGE_TRANSPORT_ERROR):
                streak += 1
                if not docs and streak >= UNREACHABLE_STREAK:
                    aborted = True
                    break
            continue
        streak = 0

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
    docs = deduplicate(docs, outcomes)

    return Prospect(
        company_name=company_name or domain,
        domain=domain,
        base_url=base_url,
        documents=docs,
        signals=compute_signals(docs, raw),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        robots_reason=client.robots_reason,
        page_outcomes=[asdict(o) for o in outcomes.values()],
        crawl_outcome=(CRAWL_ABORTED_UNREACHABLE if aborted
                       else CRAWL_COMPLETED),
    )


def deduplicate(docs: list[Document],
                outcomes: dict[str, PageOutcome]) -> list[Document]:
    """
    Collapse URLs serving the same page into one document, recording what was
    lost. Mutates `outcomes`, and returns the survivors.

    Extracted from `ingest()` so it can be tested at all. It was reachable
    only through a live crawl, which meant the kind-conflict branch below --
    the whole point of section 1.1c -- had never once executed. A branch that
    has never run is not known to work, the same standard the isolation guards
    are held to.
    """
    # Keyed on stable_hash, not content_hash: a site that shuffles repeated
    # records serves one page under two URLs with two different exact hashes,
    # and exact-hash dedup keeps both (ADR-0013).
    groups: dict[str, list[Document]] = {}
    for d in docs:
        groups.setdefault(d.stable_hash, []).append(d)

    survivors: list[Document] = []
    for group in groups.values():
        canonical = canonical_document(group)
        for other in group:
            if other is canonical:
                continue
            canonical.duplicate_urls.append(other.url)
            # Same words, different exact hash, proves the source reorders its
            # content between requests. Worth recording rather than collapsing
            # into a plain duplicate: it is the observable evidence of an A7
            # violation on that URL, and the only place we can see it without
            # fetching the same URL twice.
            how = ("reordered" if other.content_hash != canonical.content_hash
                   else "identical")
            # A disagreement about `kind` between two URLs for one page is
            # the measurement §1.1c said it did not have. Record it; do not
            # invent a winner. It used to be recorded ONLY here, inside a
            # human-readable detail string on a page outcome -- which meant
            # the surviving document carried a contested `kind` that looked
            # exactly like a settled one, and the only trace was a sentence
            # nobody parses. The conflict now rides on the document itself as
            # well, so `kind` being uncertain is a fact the artifact states
            # rather than one a reader has to reconstruct (A4, A5).
            if other.kind != canonical.kind:
                how += f", kind conflict {other.kind} vs {canonical.kind}"
                if other.kind not in canonical.kind_conflicts:
                    canonical.kind_conflicts.append(other.kind)
                    canonical.kind_conflicts.sort()
            outcomes[other.url] = PageOutcome(other.url, PAGE_DUPLICATE_CONTENT,
                                              http_status=200,
                                              detail=f"{how} of {canonical.url}")
        survivors.append(canonical)
    return survivors


def explain_empty_crawl(p: Prospect) -> str:
    """
    Why a prospect ended with no documents.

    "No documents" is not a fact about a company until the reason is known.
    A dead domain, a site that refused us, and a business with no web presence
    are three different answers to question 1, and before this they were all
    reported as a successful crawl of nothing (A5).
    """
    if p.crawl_outcome == CRAWL_ABORTED_ROBOTS:
        return "robots.txt returned 5xx; RFC 9309 treats that as full disallow"

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


def _unreachable(base_url: str, company_name: str, domain: str,
                 outcome: PageOutcome, crawl_outcome: str,
                 robots_reason: str, verbose: bool) -> Prospect:
    """A crawl that stopped before it started, with the reason attached."""
    if verbose:
        print(f"  aborted: {crawl_outcome} ({outcome.outcome})")
    return Prospect(
        company_name=company_name or domain,
        domain=domain,
        base_url=base_url,
        documents=[],
        signals=compute_signals([], {}),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        robots_reason=robots_reason,
        page_outcomes=[asdict(outcome)],
        crawl_outcome=crawl_outcome,
    )


def canonical_document(group: list[Document]) -> Document:
    """
    Pick which of several URLs serving one page is the canonical one.

    Deduplication used to keep whichever copy was crawled first, which made
    both `source_url` and `kind` an accident of queue order: a page reachable
    at /handbook/x and /careers/x carried whichever classification happened to
    be fetched first, and could change between runs as a site's links change.

    The order is now the URL itself, so the same page yields the same canonical
    URL on every crawl. That is a determinism fix, NOT a semantic one -- it
    makes no claim that the alphabetically first URL is the *right* one, and
    deliberately does not prefer a more specific `kind`.

    Preferring the more specific kind is the obvious-sounding rule, and it is
    the one this function must not adopt. `kind` is denormalised onto chunks
    for source weighting (ADR-0004), and the measured failure this project has
    actually seen runs the other way: substring matching over-classified two
    thoughtbot playbook articles as job postings, which KIND_PATTERNS calls a
    retrieval defect rather than a cosmetic one. A rule that resolves every
    tie towards the more specific kind is a rule that prefers exactly that
    error. There is no measurement for the opposite direction either, so
    neither is chosen.

    What DID change is that the uncertainty is no longer invisible. A losing
    URL's kind is recorded on `Document.kind_conflicts`, so a contested `kind`
    is distinguishable from a settled one by anything reading the artifact,
    and the crawl says so on the run that produces it. Which URL *should* win
    stays open, deliberately -- but it can no longer pass unnoticed, which is
    what kept it from ever being measured (A3, A5). See ADR-0019.
    """
    return min(group, key=lambda d: d.url)


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
        print(f"  crawl:     {prospect.crawl_outcome}")
        # Printed only when it happens, and it has never happened. The first
        # crawl that produces one is the measurement docs/open-questions.md
        # section 1.1c has been waiting for, and it should not have to be dug
        # out of the artifact afterwards.
        contested = [d for d in prospect.documents if d.kind_conflicts]
        for d in contested:
            print(f"  kind?:     {d.url} is {d.kind}, also classified "
                  f"{', '.join(d.kind_conflicts)} -- section 1.1c")
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
