# ADR-0024 — The prompt states what counts as an answer

Status: Accepted · Date: 2026-09-11

## Decision

Prompt `answer-v3` replaces `answer-v1`. Two changes:

1. **Each evaluated question carries what counts as an answer to it**
   (`QUESTION_SCOPE` in `linestack/generation/prompts.py`), placed just before
   the question. The text is the grading rule from docs/ground-truth.md and
   ADR-0023, the same for every prospect. q3 says a headcount alone is not
   growth and employees' career development is not company growth; q4 says a
   problem solved for clients and a value or programme the company already has
   are not needs.
2. **The system prompt forbids inference and mixed replies.** Material on the
   same topic that does not answer the question is a reason to decline. A
   sentence that needs "suggesting", "indicating", "implies" or "shows that"
   to reach the question is not supported. An absence is not evidence. A
   reply either answers or declines, never both.

The harness also gains a judge-free problem: **"answers and declines in the
same reply"**, for a reply that contains `Insufficient evidence.` but does not
start with it.

## Why

answer-v1 declined on 0 of 2 `insufficient_evidence` pairs (ADR-0022,
Measured). Reading the two answers showed three causes:

- **It linked true facts to invented conclusions.** q3: a careers page with no
  open roles, "suggesting a focus on long-term planning and strategic hiring".
- **Retrieval offered same-word bait.** For "are they growing", the top
  passages included `/career-paths`, about employees growing in their careers.
- **It was graded by a rule it had never seen.** The q4 rule (ADR-0023) lived
  in docs/ground-truth.md only, so the model described a promotion policy as a
  stated priority. A reasonable reading of the question, graded as wrong.

The third is the one this ADR is really about. A grading rule the model is
never shown measures whether it guesses the rule.

## Measured

**[verified] 2026-09-11**, `Qwen/Qwen3-1.7B`, greedy, thoughtbot, same corpus
and retrieval in all three runs (recall@5 0.50 each time).

| prompt | declined where it should | declined where it should not | notes |
| --- | --- | --- | --- |
| answer-v1 | 0 of 2 | 0 of 2 | baseline |
| answer-v2 | 1 of 2 | 0 of 2 | q1 answered, then ended "Insufficient evidence." |
| answer-v3 | 1 of 2 | 0 of 2 | v2 plus "never both" |

What moved, answer by answer:

- **q3 declines, in v2 and v3.** "The material does not provide specific
  information about the company's investment or growth signals, such as new
  products, teams, or funding." The fix this ADR was for.
- **q2 got better.** v1 cited five sources, including "no open roles,
  suggesting the company has the technical capacity to hire". v3 states 53
  people and 37 technical titles, cites `[S]`, and stops.
- **q1 regressed in v2**, invisibly to the counts: it answered, listed every
  signal, then declined. is_decline reads the start, so the run said
  "answered". That is why the new problem exists. v3 stopped the decline tail,
  but q1 still appends the signals block as a list of `[S]` lines.
- **q4 still answers, and v3's answer is wrong in a new way.** It says the
  careers page has "open roles", with `open_roles_seen = 0` in the context it
  was given, and concludes that is the stated need. A 1.7B model contradicting
  a computed fact placed first in its context is not a wording problem.

## What this does not show

- **Four pairs, one company.** A prompt tuned against four answers is tuned to
  those four until other prospects say otherwise. fly.io's references are the
  next check. Until then this is a direction, not an improvement.
- **The counts miss what the answers say.** q4 is "answered" in v1 and v3,
  and v3's is worse: it now contradicts a computed fact. Only reading the
  answers showed that. A contradiction of a signal is checkable without a
  judge; not built yet.

## The model lever, measured — added 2026-09-11

**[verified]** `Qwen/Qwen3-4B`, same prompt (`answer-v3`), corpus and
retrieval, run with `GENERATION_MODEL` overridden for one run.

| model | declined where it should | declined where it should not | per answer | max RSS | download |
| --- | --- | --- | --- | --- | --- |
| Qwen3-1.7B | 1 of 2 | 0 of 2 | ~7.5 s | 2.97 GB (ADR-0022) | 4.08 GB |
| Qwen3-4B | **0 of 2** | 0 of 2 | ~10 s | 5.93 GB | 7.5 GB |

The bigger model is worse on the metric this ADR is about.

- **q3 answers and then declines**, and the answer part is false: "active
  recruitment efforts indicate a commitment to growing its workforce". The
  jobs page says "We currently have no open positions."
- **q4 takes a client pitch as the company's need**: "it needs to accelerate
  progress without waiting on full-time hires". That line opens
  `/services/fractional-product-technical-leadership`, a list of what clients
  get ("De-risk your roadmap"). The scope in the prompt names exactly this case
  as not a need.
- **q1 and q2 are better written**: one cited sentence each, no signals block
  pasted on the end.

So 4B writes better answers and declines less. One reading is that a model
more confident in its own reading of the material is more willing to answer.
Another is that answer-v3 was written against 1.7B's failures. Neither is
tested; both need more than four pairs.

## Consequences

- The default stays `Qwen/Qwen3-1.7B`. Qwen3-4B lost on declines, the metric
  it was tried for, at twice the memory and about 1.4 times the time.
- q4 remains the failure. Neither lever tried here fixed it; fly.io's pairs
  come before the next attempt.
- Run records written before this ADR carry `answer-v1` and are not comparable
  with later runs on the declines. The version in the record is what makes that
  visible.
- `QUESTION_SCOPE` may never name or describe one company. Its tests pin one
  scope per evaluated question and none for a free-text or q5 question.
