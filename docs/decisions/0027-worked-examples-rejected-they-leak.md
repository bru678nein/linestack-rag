# ADR-0027 — Worked examples in the prompt: rejected, the model copies them

Status: Rejected · Date: 2026-09-13

## What was tried

Prompt `answer-v5`: answer-v3 plus four worked examples, shown to the model as
earlier turns of the conversation. Two answers (a q2 team page, a q4 job
posting that says what it must fix) and two declines (q3 where "growth" is
employees' careers and no role is open; q4 where the pages state only values
and a problem solved for clients). Invented companies on `.example` domains,
laid out exactly like the real context, written once before measuring. The
system prompt said they were invented and never to use anything from them.

The idea: a small model imitates an example more reliably than it follows a
rule, and unlike ADR-0026 it asks for no new format.

The code is kept on branch `experiment/answer-v5`, not on `main`.

## Measured

**[verified] 2026-09-13**, `Qwen/Qwen3-1.7B`, `queries-v1`, both prospects:

| prompt | declined where it should | declined where it should not |
| --- | --- | --- |
| answer-v3 | 0 of 2 | 0 of 6 |
| answer-v5 | 0 of 2 | 0 of 6 |

The same counts. The answers are not the same.

## Why it is rejected

**The model copied the examples' facts into real answers**, despite the
instruction:

- fly.io q1: fly.io "sells to regional retailers … helping them move legacy
  systems to the cloud without downtime". The first is from the Ferrymark
  example, the second from the Northwind example, word for word.
- fly.io q4: fly.io is "looking for a Data Engineer to rebuild their reporting
  pipeline", which is Ferrymark's posting.
- thoughtbot q2: "technical roles open, such as Data Engineer". thoughtbot
  lists no open roles, and the Data Engineer is from the examples.

thoughtbot's q3 and q4 still answered, now with invented hiring ("actively
recruiting for technical roles", "hiring for roles such as Design Director").

A fact taken from the prompt is worse than anything answer-v3 gets wrong. It
is about another company, it is invisible to every judge-free check because
the citations beside it point at real passages, and it would reach a seller as
a fact about their prospect. The decline counts cannot see it at all, which is
why the answers were read and not only counted.

## What this says about the model

At 1.7B, worked examples teach content, not behaviour, and an instruction not
to reuse them is not followed. Together with ADR-0026, the two cheap prompt
levers that ask more of the model both failed on it, for reasons that come
from the model's size rather than from the design.

## Consequences

- `main` stays on `answer-v3`.
- If examples are ever tried again (a larger model), the harness needs a check
  first: any distinctive phrase from an example appearing in an answer is a
  leak, and it is detectable without a judge.
- Remaining levers, cheapest first: a yes/no gate where the model only
  classifies and the code writes the decline; Qwen3-4B (both branches are
  ready for it); more ground truth, since every result here rests on two
  decline pairs; fine-tuning, after those.
