# Ground truth

The evaluation set. It does not exist yet, and it must not be written until the
ingestion defects in `docs/open-questions.md` are fixed — see §6.

Target: **12 prospects × 4 questions ≈ 48 pairs**, hand-written. Not generated
by a model. A set written by a model measures agreement with that model, which
is not the thing under test.

Location: `eval/ground_truth/<domain>.yaml`, one file per prospect, plus the
frozen crawl artifact for that prospect committed alongside it.

---

## 1. Format

```yaml
# eval/ground_truth/thoughtbot_com.yaml
prospect:
  company_name: thoughtbot
  domain: thoughtbot.com
  crawled_at: "2026-09-01T00:00:00+00:00"   # the frozen corpus this refers to
  corpus_artifact: prospect_thoughtbot_com.json
  author: brunoracconto@gmail.com
  written_at: "2026-09-02"

# Computed facts, hand-checked against the live site. Exact comparison,
# no judge involved. See docs/evaluation.md §2.4.
signals:
  has_team_page: true
  people_listed: 54
  open_roles_seen: 0
  technical_roles_open: 0
  latest_post_date: null        # null means "not checked", not "none exists"
  notes: >
    people_listed hand-counted from /team on 2026-09-01. The four pages the
    crawler classifies as job_posting are a jobs landing page, a compensation
    calculator, and two career-ladder playbook pages — none is a vacancy.

questions:
  - id: q1_what_and_to_whom
    question: What does this company do, and who does it sell to?
    reference: >
      A hand-written answer, 2-4 sentences, in the register a colleague would
      use. Only claims that are supported by the pages listed in source_urls.
    source_urls:
      - https://thoughtbot.com/
      - https://thoughtbot.com/about
    acceptable_variants:
      - Naming the customer segment without naming the service category, or
        the reverse, is acceptable for this question.
    must_not_claim:
      - Any client name not present in source_urls.

  - id: q2_technical_capacity
    question: What evidence is there of in-house technical capacity?
    reference: >
      ...
    source_urls:
      - https://thoughtbot.com/team
    supporting_signals: [has_team_page, people_listed]
    must_not_claim:
      - A headcount other than 54.

  - id: q3_growth_signals
    question: What signals are there that they are investing or growing?
    reference: >
      ...
    source_urls: []
    expected_outcome: insufficient_evidence
    notes: >
      No open roles and no recent funding or expansion announcement in the
      crawled corpus. The correct answer is that the corpus does not support
      a growth claim. See §3.

  - id: q4_stated_pain
    question: What pain or problem do they state explicitly?
    reference: >
      ...
    source_urls:
      - https://thoughtbot.com/...
    must_not_claim:
      - Pain inferred from the absence of something. Only pain the company
        states in its own words.
```

### Field meanings

| Field | Required | Purpose |
| --- | --- | --- |
| `corpus_artifact` | yes | Ties the answer to a specific frozen crawl. A reference answer without one is unfalsifiable. |
| `source_urls` | yes | The evidence. This is what retrieval recall is computed against (`docs/evaluation.md` §2.1). May be empty — see §3. |
| `reference` | yes | The hand-written answer. Used for the secondary correctness metric only. |
| `acceptable_variants` | no | Records that two different answers are both right, for the human reading a low correctness score later. |
| `must_not_claim` | no | Specific hallucinations to check for. The highest-value field in the file. |
| `expected_outcome` | no | `answerable` (default) or `insufficient_evidence`. |
| `supporting_signals` | no | Which computed signals the answer should be consistent with. |
| `notes` | no | How the fact was checked, and anything the next author needs. |

---

## 2. How to write one

Per prospect, roughly 30–45 minutes.

1. **Crawl and freeze.** `python ingest.py <domain>`, commit the artifact. Every
   answer below is written against *that* artifact, not against the live site.
2. **Read the crawled documents, not the website.** This is the discipline that
   makes the set useful. If you write a reference answer from something you saw
   on the site that the crawler never fetched, you have written an ingestion
   test and labelled it a retrieval test. When you catch yourself doing it,
   record the missing URL — that is a coverage finding worth more than the pair.
3. **Hand-check the signals** against the live site. Count the people on the team
   page yourself. Open every page the crawler called a job posting and decide
   whether it is a vacancy. Write down how you checked, in `notes`.
4. **Write each reference answer** from the crawled text, 2–4 sentences, and list
   the source URLs you actually used.
5. **Write `must_not_claim`.** Ask what a fluent, confident, wrong answer would
   look like for this company, and write that down. This is where the value is:
   a plausible wrong claim is much more informative than another correct one.
6. **Mark `insufficient_evidence` honestly** when the corpus does not answer the
   question. See §3.

### Choosing the 12

**[assumed]** the set should be deliberately unbalanced rather than
representative, because the interesting failures are at the edges:

- 3–4 companies with an obvious team page and open technical roles — the easy
  case, which must not regress.
- 2–3 with no team page at all, to test that the system says so instead of
  inventing one.
- 2–3 whose site is thin (a landing page and nothing else), to test
  `insufficient_evidence`.
- 1–2 non-English or bilingual sites, because the crawler's seed paths include
  Spanish ones and the text-search configuration question is unresolved.
- 1–2 whose positioning lives in the engineering blog rather than on the About
  page, to test the crawl budget quotas (ADR-0007).

Include at least one company from each of the two already hand-checked
(fly.io, thoughtbot.com), because there is existing ground truth for their
signals.

---

## 3. "Insufficient evidence" is a correct answer

`expected_outcome: insufficient_evidence` marks a question the corpus genuinely
does not answer. These pairs are the most valuable in the set and should be
roughly a quarter of it.

A system that answers all 48 questions confidently is not a better system than
one that declines 12 of them; it is the failure mode this project exists to
prevent, scoring well. Without these pairs, every metric rewards fluency.

Grading: an answer that states the evidence is not present scores as correct. An
answer that produces a plausible claim scores as a failure regardless of whether
that claim happens to be true of the real company.

---

## 4. What must never be in the set

- Anything behind a login, or any personal data about named individuals beyond
  what a company publishes about its own staff on its own site (A6). Count the
  people on a team page; do not record who they are.
- Facts about the company from anywhere other than the frozen corpus — no
  LinkedIn, no Crunchbase, no press coverage. A reference answer citing evidence
  the system was never given makes recall unmeasurable.
- Model-generated reference answers.

---

## 5. Maintenance

The set is versioned with the corpus. When a prospect is re-crawled and the
content hashes change (A7 makes this cheap to detect), every pair for that
prospect is re-reviewed before the new corpus is used. A reference answer
silently pointing at a page that no longer exists produces a recall failure that
looks like a retrieval regression.

**[assumed]** a quarterly re-crawl is the right cadence. Not measured.

---

## 6. Unblocked — start writing

**All three original blockers are cleared as of 2026-09-02.**

- Silent thin-extraction drop — fixed, ADR-0011.
- Unclassified fetch failures — fixed, ADR-0012. A missing `source_url` can now
  be explained by an outcome code rather than guessed at.
- Reshuffled content breaking reproducibility — fixed, ADR-0013. `stable_hash`
  is identical across fetches and across runs, so a pair citing `fly.io/about`
  is reproducible. Cite `stable_hash`, not `content_hash`, when a pair needs to
  pin a document version.

Two known defects remain, and neither blocks authoring — both are visible in
the output rather than silent:

- **§1.1c** `kind` is taken from the surviving URL after deduplication. Affects
  retrieval weighting, not the ingestion record.
- **§1.1b (partial)** a roster listing people by first name only is still
  counted as 0. Neither validation prospect is affected; fly.io
  `people_listed` is hand-counted at **57** and matches.

Signal ground truth (§1 `signals:`) is hand-checked against the live site and
should be written first — it is what revealed §1.1b in the first place.
