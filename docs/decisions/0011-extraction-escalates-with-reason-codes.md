# ADR-0011 — Extraction escalates through three passes and records which one won

Status: Accepted · Date: 2026-09-02

## Decision

`extract()` no longer makes one attempt against one threshold. It tries three
strategies in order and keeps the first whose output clears `MIN_WORDS` (30):

1. **trafilatura, `favor_precision=True`** — the default, and still the one we
   want for most pages.
2. **trafilatura, `favor_precision=False`** — recall mode.
3. **`dom_text()`** — `selectolax` over `main`/`article`/`body` with
   `script`, `style`, `noscript`, `svg`, `template`, `iframe`, `nav`, `footer`
   and `form` removed first. No new dependency; `selectolax` was already there.

Every outcome carries a reason code: `ok`, `recovered_recall`, `recovered_dom`,
`thin`, `empty`. The winning code is stored on `Document.extract_reason` and
travels with the document into the JSON artifact. Pages that clear no pass are
recorded on `Prospect.dropped_pages` with their reason instead of vanishing.

## Why

**[verified] 2026-09-02.** `fly.io/about` is 249,893 bytes containing the whole
team roster. Precision mode extracted **29 words** of it — one word under the
threshold. The page was dropped, and `has_team_page: False` for fly.io was an
artifact of our own configuration.

The page is **not** client-rendered. That was our first hypothesis and it was
wrong. The names are in the HTML. Recall mode on the identical bytes returns
**316 words**. Precision mode reads a roster of short, link-heavy cards as
navigation, which for most pages is exactly the behaviour we want — which is
why the answer is to escalate, not to turn precision off.

Keeping precision first matters for the corpus. Recall mode on `fly.io/`
returns 635 words against precision's 598, and the DOM fallback 761; the extra
words are nav and footer chrome. Embedding that as though it were content is
the failure this project exists to avoid. Escalation pays that cost only on the
pages that would otherwise be lost entirely.

Recording *which* pass won is not bookkeeping. A document recovered by the DOM
fallback is real content held to a lower standard than one trafilatura
extracted cleanly, and A4 requires that a consumer be able to tell them apart
rather than having to assume.

## Alternatives

- **Lower the threshold.** Would have admitted the 29-word extraction, but as
  29 words — the roster stays lost, and the page enters the corpus as a stub
  that looks like content. Treats the symptom.
- **`favor_precision=False` everywhere.** Recovers this page and pollutes every
  other one with chrome. Measured above.
- **Headless rendering.** Answers a question the site was not asking; the
  content was in the HTML the whole time. Large dependency, real cost per page.
- **Keep the silent drop.** Rejected under A5: a dropped page and a 404 were
  indistinguishable in the output.

## Consequences

- Recovered pages consume budget slots. **[verified]** the two recovered fly.io
  pages displaced two blog posts; on thoughtbot one playbook page displaced
  another. Under ADR-0007 that is the budget working, and trading two blog
  posts for a team roster favours the four questions.
- Two extra parses on pages that fail the first pass. No extra requests.
- Surfacing `fly.io/about` exposed two further defects, recorded in
  `docs/open-questions.md` §1.1a (the roster is shuffled per request, which
  breaks A7 idempotency and defeats `content_hash` dedup between `/about` and
  `/team`) and §1.1b (`people_listed` is 0 on a page that lists people).
