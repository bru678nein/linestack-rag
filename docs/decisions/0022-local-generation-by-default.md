# ADR-0022 — Local generation by default; OpenAI as the alternative

Status: Accepted · Date: 2026-09-10 (proposed and measured the same day)

Extends [ADR-0017](0017-local-embeddings-by-default.md) from embeddings to
generation. Measured on this machine the day it was proposed; see *Measured*.

## Decision

`linestack/generation/` answers questions with a local instruct model by
default, loaded with `transformers` on Apple's GPU (MPS), and routes to the
OpenAI chat API only when `GENERATION_MODEL` starts with an OpenAI prefix
(`gpt-`, `o3`, …). Default: **`Qwen/Qwen3-1.7B`**.

- **Greedy decoding** at `generation_temperature = 0`, so the same context
  gives the same answer and a before-and-after compares like with like.
- **Reasoning off.** Qwen3 thinks aloud inside `<think>…</think>` by default.
  It is disabled at the chat template (`enable_thinking=False`) and stripped
  again from the output, because a reasoning block that reached the answer
  would be read by the citation checker as claims.
- **Not streamed.** Both existing callers — the CLI and the harness — need the
  whole text before they can check one citation. Streaming belongs with the
  API route, which has a client to stream to.
- **Lazy load**, timed separately from generation, for the reason the harness's
  embedder had to learn: a run that answers nothing should load nothing.

## Why

This machine has no OpenAI key by design (ADR-0017), and
`generation_model = "gpt-4o-mini"` meant the G in RAG could not run here at all
— the same position embeddings were in before ADR-0017.

torch 2.14 and transformers 5.16 are **already installed** by the `[local]`
extra for the embedder, with MPS available. Local generation therefore costs a
model download and no new dependency. Ollama, MLX and llama.cpp are all absent
on this machine; each would add one.

### Candidates, from metadata only — nothing was downloaded to choose

**[verified] 2026-09-10**, Hugging Face API:

| model | weights | license | gated |
| --- | --- | --- | --- |
| **Qwen/Qwen3-1.7B** | 4.1 GB | apache-2.0 | no |
| Qwen/Qwen3-4B | 8.0 GB | apache-2.0 | no |
| Qwen/Qwen2.5-1.5B-Instruct | 3.1 GB | apache-2.0 | no |
| Qwen/Qwen2.5-3B-Instruct | 6.2 GB | **other** | no |
| HuggingFaceTB/SmolLM2-1.7B-Instruct | 3.4 GB | apache-2.0 | no |
| microsoft/Phi-3.5-mini-instruct | 7.6 GB | mit | no |

`Qwen2.5-3B-Instruct` is excluded on its license, which is not Apache-2.0 like
the rest of its family. The machine has 16 GB and is also holding the embedder,
so 8 GB models are possible but tight.

`Qwen3-1.7B` is the default because it is the smallest current-generation
Apache-2.0 model on the list, and **it is a starting point, not a conclusion**.
`Qwen3-4B` is the obvious next step if 1.7B fails the checks below. That choice
is made by measurement.

## What can be checked without a judge

ADR-0020 left faithfulness unmeasured because it needs an LLM judge. Three
failures do not, and `GeneratedAnswer.problems` reports all three:

- **A fabricated citation** — `[7]` when five passages were sent. The one
  faithfulness failure that is detectable mechanically.
- **An uncited claim** — an answer that neither declines nor cites anything
  breaks rule 2 of the prompt, and is exactly the confident unsupported answer
  docs/ground-truth.md §3 says this project exists to prevent.
- **A decline**, recognised by the fixed phrase `Insufficient evidence.` so
  that §3's grading — "the evidence is not present" is *correct* on an
  insufficient_evidence pair — can be applied without reading intent.

An empty `problems` list means *not detectably wrong*. It does not mean
correct, and nothing here claims it does.

## What the context carries, and why

- **Signals first and always** (A2), before the budget is consulted.
- **Each signal with its meaning.** `people_listed = 53` alone invites "53
  employees", which the ground-truth notes warn is not the same thing; the
  context says *a page count, not a headcount*.
- **No crawl bookkeeping.** `total_words` and `pages_crawled` describe the
  crawl, and a model handed them will eventually report them as facts about the
  company.
- **Passages as a rank prefix.** The first passage that does not fit ends
  admission. Skipping it for a shorter one further down would turn "top k by
  similarity" into "top k that happened to be short" — a retrieval change made
  by the budget.

## What acceptance requires

Measured on this machine, recorded here, before the status changes:

1. Model load time and resident memory alongside the embedder.
2. Generation latency for one answer at the default budget.
3. On the four thoughtbot questions: how many answers have `problems`, how many
   decline, and whether any decline is on a question the corpus plainly
   answers.
4. Whether `enable_thinking=False` actually suppresses reasoning on this
   transformers version — the template accepting the variable is verified;
   the model honouring it is not.

## Measured

**[verified] 2026-09-10**, M5 MacBook Air with 16 GB, `Qwen/Qwen3-1.7B` at
prompt `answer-v1`, on the thoughtbot corpus reloaded after ADR-0021.

| | |
| --- | --- |
| download | 4.08 GB in 203 s, once |
| model load, from cache | 7.5 s |
| memory, alongside the embedder | 2.97 GB RSS, 4.83 GB GPU peak |
| generation | 5.3 to 7.8 s per answer |
| retrieval | 0.01 to 0.04 s |

**Reasoning is suppressed.** With `enable_thinking=False` the model answers
"Four." with no `<think>` block. With it on, the tag survives
`skip_special_tokens=True` (it is not a special token), so `strip_reasoning`
would still see it if suppression ever failed.

### Five answers, no detectable problem, no declines, and that is the finding

All five cited real passages, so every judge-free check passed. Two claims were
then checked against the chunk text.

- **q5** says thoughtbot prototypes with "Claude Code" and cites the MVP page.
  **Supported**: both chunks of `/services/mvp-development` mention it.
- **q3** says the company shows "investment in team development and strategic
  growth". **Not supported.** Every growth-related match in the chunks it
  cites is about an individual's career: "We expect team members to grow and
  progress in their career path", "areas for growth", "a long-term plan for
  your growth". Nothing there is about the company growing, hiring or
  investing, and the careers page says there are no open positions. The model
  read personal growth as company growth and built a confident paragraph on
  it. On this corpus the answer should almost certainly have been a decline.

To the checks, those two answers are indistinguishable, because both cite
valid passages. **That is the limit of judge-free checking, measured: it
verifies that a citation exists, not that it supports the claim.** A plausible
claim with a real citation passes.

What catches q3 mechanically is not a better citation check. It is ground
truth: an `insufficient_evidence` pair on q3, scored by whether the answer
declined (`is_decline`). That needs no judge. It needs the pair written.

**Not acted on, deliberately.** The obvious moves are Qwen3-4B or a stricter
prompt, and both are the tuning A3 forbids without a metric to judge them by.
The metric is decline accuracy on insufficient_evidence pairs, and it waits on
the ground-truth set.

**Measured since, [verified] 2026-09-11.** `make eval` now answers every pair
and counts declines (docs/evaluation.md §2.6). On thoughtbot, with both
`insufficient_evidence` pairs written by hand, the model declined on **0 of 2**
(q3 and q4), and wrongly declined on 0 of 2 answerable pairs. It never
declines. The q3 failure described above is now a number re-checked on every
run, and the baseline any change to the model or the prompt has to beat.
ADR-0024 acts on the prompt side, measured against it.

Accepted means local generation works on this machine, measured. It does not
mean the answers are good, and this section is the evidence that they are not
yet.

## Alternatives

- **Ollama.** The easiest local serving story, and an external daemon this
  machine does not have. Worth revisiting if generation moves off the laptop.
- **MLX (`mlx-lm`).** Apple-native and likely the fastest option on Apple
  Silicon. A new dependency, and exactly the kind of optimisation A3 says to
  make after there is a number to improve, not before.
- **llama.cpp.** Quantised models in less memory; a compiled dependency.
- **Keep OpenAI as the default.** Leaves the pipeline unable to answer on the
  only machine it runs on.
