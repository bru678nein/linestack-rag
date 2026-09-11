# ADR-0023 — q4 asks what a company says it needs, not what hurts

Status: Accepted · Date: 2026-09-10

## Decision

Question 4 changes from

> What pain or problem do they state explicitly? (`q4_stated_pain`)

to

> What need or priority do they state explicitly? (`q4_stated_need`)

It stays explicit-only. A need inferred from the absence of something is not a
stated need, and declining is a correct answer (docs/ground-truth.md §3).

## Why

This came up while writing the first reference answer. Companies don't publish
their pain. Showing weakness to clients or competitors gains them nothing, and
a marketing site is written to do the opposite.

**[verified] 2026-09-10**, searching both crawled corpora for the phrasing
companies use when they do describe a problem ("we need", "we're looking for",
"help us", "the problem we", "we struggled"):

- **thoughtbot.com.** Every match is a problem thoughtbot solves for its
  clients: "the problem we're solving for", "you will need to upgrade". The
  one self-stated item is incidental, in the hiring playbook: "Unable to find
  off-the-shelf software to help us do this, we've created some of our own."
- **fly.io.** The job postings state needs in plain words. The networking
  posting says "We're looking for someone to work on that networking team",
  then describes the problem the team owns: "We need to get it to the closest
  VM."

The old wording had two readings, and neither worked:

- **The company's own pain.** Rarely published, so nearly every prospect
  would be insufficient_evidence, and the pair would mostly re-test declining,
  which q3 already covers.
- **The pain it solves for its customers.** Nearly always answerable, and a
  restatement of q1, because a service page is a list of the problems a
  company solves.

A stated need is something a company publishes on purpose, through hiring,
engineering posts and announcements. For a seller qualifying the lead it is
the useful stand-in for pain: what the prospect says it can't yet do on its
own.

## Boundaries with the other questions

- **q1** is what they sell. A problem they solve for clients is q1 evidence,
  not q4.
- **q3** is whether they are investing or growing. **q4** is what,
  specifically, they say they need. One job posting can be evidence for both:
  q3 takes the fact that they're hiring, q4 takes what the posting says the
  role has to do.

## What counts as a stated need — added 2026-09-11

Settled while writing thoughtbot's q4. Searching the crawled pages for
thoughtbot describing its own priorities found commitments it already acts on:
its DEI work ("works to create a more diverse, equitable, inclusive…
environment", a DEI council, a CEO diversity pledge) and "our commitment to
sharing what we learn". It also found one past need, already solved: it built
its own hiring software because nothing off the shelf fit. It found nothing
thoughtbot says it currently needs or cannot yet do.

A commitment a company already acts on does not count as a stated need. Nearly
every company publishes its values, so counting them would make q4 answerable
from boilerplate for every prospect, the same failure as reading the old
wording as "pain solved for customers". It would also tell a seller nothing
about what the prospect lacks. thoughtbot's q4 is therefore
insufficient_evidence, and the rule is in docs/ground-truth.md so every
prospect is judged the same way.

## Why the id changed too

Rewording while keeping `q4_stated_pain` would let a run record of the old
question be compared with a run of the new one, and nothing would flag it. A
different question gets a different id.

## Consequences

- No written work is lost. No q4 reference existed yet.
- The q4 answer the model gave during ADR-0022's measurement answered the old
  question, from a blog post about designers translating their work into
  code. It isn't comparable with any run against the new wording.
- The recall assumption `docs/evaluation.md` made about q4 was written for the
  old wording, and is restated for the new one, still marked **[assumed]**.
