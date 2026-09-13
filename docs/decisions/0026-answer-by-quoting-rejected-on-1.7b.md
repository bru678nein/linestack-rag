# ADR-0026 — Answer by quoting, and check the quotes: rejected on Qwen3-1.7B

Status: Rejected · Date: 2026-09-13

## What was tried

Prompt `answer-v4`: the model answers with an `EVIDENCE:` block of quotes, each
tagged `[n]` or `[S]` and copied word for word, then an `ANSWER:`, or declines.
The code checks every quote against the passage it names (case, whitespace
and typographic quotes normalised; at least four words; elisions checked piece
by piece). An answer none of whose quotes verify is withheld and counted as a
decline, which charges the check on both counts: correct on an
insufficient_evidence pair, wrong on an answerable one.

It was meant to catch the two failures ADR-0024 and ADR-0025 left: claims with
no sentence behind them, and facts cited to passages that do not contain them.

The code is kept on branch `experiment/answer-v4`, not on `main`.

## Measured

**[verified] 2026-09-13**, `Qwen/Qwen3-1.7B`, `queries-v1`, both prospects,
against the same run with `answer-v3`:

| prompt | max_tokens | declined where it should | declined where it should not |
| --- | --- | --- | --- |
| answer-v3 | 400 | 0 of 2 | 0 of 6 |
| answer-v4 | 400 | 1 of 2 | **4 of 6** |
| answer-v4 | 1000 | 1 of 2 | **3 of 6** |

A reader would have got "Insufficient evidence" on four of the six questions
the corpus answers.

## Why it failed

**The check worked; the model could not use the format.**

- It does not quote a sentence. It pastes a whole passage, often several
  hundred words, runs out of tokens inside the quote, and never writes the
  answer. An unclosed quote cannot be parsed, so the answer is withheld. At
  1000 tokens this happens less, not never.
- It numbers quotes wrongly. fly.io's q4 quoted the Account Manager posting
  exactly and labelled it `[1]`; the check reported it missing from `[1]`, and
  a re-run of the retrieval showed the sentence is in passage `[4]` and nowhere
  else. The check was right.
- The one decline it gained was an accident: thoughtbot's q3 quoted the
  computed facts under passage numbers, so nothing verified.
- thoughtbot's q4 still answered wrongly, from a real playbook passage that
  states no need. A quote proves a sentence exists, not that it answers the
  question, as expected when this was proposed.

## Consequences

- `main` stays on `answer-v3`.
- Not tested: Qwen3-4B, which writes short cited answers where 1.7B pastes
  text (ADR-0024). If it follows the format, this design may hold there, at
  twice the memory and about 1.4 times the time per answer. The branch is
  ready for that run.
- Next on `main`: worked examples in the prompt (ADR-0027), which asks nothing
  of the model's formatting that v3 does not.
