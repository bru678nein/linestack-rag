# ADR-0020 — The harness computes the metrics that need no judge

Status: Accepted · Date: 2026-09-05

## Decision

`linestack/evaluation/metrics.py` implements four of the six metrics in
[docs/evaluation.md](../evaluation.md) §1:

- **retrieval recall@k**, the primary metric, at k = 1, 3, 5, 10, per question,
  at document granularity, reported with the rank of the first correct
  document;
- **signal accuracy**, an exact comparison against hand-checked truth;
- **ingestion coverage**, which asks whether the evidence was crawled at all;
- **the cross-prospect leakage gate**, which raises rather than scoring.

**Faithfulness and answer correctness are not implemented.** The `eval` extra
is not installable and is marked as such rather than left looking optional.

Every function takes data that has already been fetched and returns a value.
No I/O anywhere in the module.

## Why

### The pin does not resolve

**[verified] 2026-09-05.** `make install-eval` has never worked:

```
Because instructor>=1.4.0,<=1.4.1 depends on openai>=1.40.0,<2.0.0
and ragas>=0.4.3 depends on instructor, we can conclude that
ragas>=0.4.3 depends on one of:
    openai>=0.27.8,<0.29.0
    openai>=1.1.0,<3.0.0
And because linestack-rag[eval]==0.1.0 depends on ragas==0.4.3 and
linestack-rag==0.1.0 depends on openai==3.7.0, we can conclude that
linestack-rag[eval]==0.1.0 cannot be used.
```

`ragas` depends unconditionally on `instructor`, and the newest `instructor`
(1.16.0) requires `openai>=2.0.0,<3.0.0`. This is not fixable by choosing a
different ragas: **0.4.3 is the latest release**, and ragas itself asks only
for `openai>=1.0.0`. The ceiling comes from instructor.

The pin was marked `RESOLVED`, which in this repository means "exists on
PyPI". It does exist. It has never been installable next to our own `openai`
pin, and nobody found out because the extra was never installed — the same
shape as the `greenlet` and `numpy` findings, and the reason
`docs/open-questions.md` §3.1 distinguishes resolved from exercised.

### Even resolved, neither metric could run

This is the reason the dependency is not simply repaired.

Faithfulness and answer correctness are LLM-judged. `settings.eval_judge_model`
is `gpt-4o-mini`, and this project runs with no OpenAI key by design
(ADR-0017). Fixing the resolver would buy two metrics that cannot execute.

Installing a large transitive tree for metrics that cannot run is
infrastructure ahead of measurement, which A9 forbids.

### What is left is not a consolation prize

Recall@k is the **primary** metric in docs/evaluation.md §2.1, and it is the
exact number ADR-0009 names as its trigger for hybrid search: recall@5 below
0.8 on a question, with manual inspection showing matter present in the corpus
that vector search ranked outside the top 5. That trigger is now computable.

It also needs no judge, no key, no network and no money, which means it can run
on every push rather than on the occasions someone decides to pay for a run.
docs/evaluation.md §6 keeps the harness off the default CI path because
LLM-judged metrics vary run to run; without a judge, that objection does not
apply to these four.

Signal accuracy has the best track record of anything in this project. It is
how 162 people against a hand-counted 54 was found, and 4 open roles against 0
(ADR-0003), and 0 against 57 (ADR-0014), and 0 against 14 (ADR-0018).

## Consequences

- A retrieval change can be accepted or rejected on recall today. It cannot be
  judged on whether the generated answer is *supported*, which is a real gap
  and is recorded as one, not papered over.
- `docs/evaluation.md` §2.2 and §2.3 stay in the document describing metrics
  that are specified but absent. The design is not wrong; it is unbuilt.
- When a key exists, faithfulness is the first thing to add, and the resolver
  conflict has to be settled first — most likely by moving `openai` out of the
  base dependencies, since the only calls this project makes are
  `AsyncOpenAI(...)` and `.embeddings.create`, which are stable across 1.x to
  3.x.
- Recall is **undefined**, not zero, for an `insufficient_evidence` pair.
  Those pairs cite nothing by design and there is nothing to retrieve. Scoring
  them 0 would drag the primary metric down for pairs that are *correct*, and
  docs/ground-truth.md §3 wants roughly a quarter of the set to be those — so
  the error would grow with the set. `recall_at_k` raises rather than
  returning a number it cannot mean.

## Alternatives

- **Repair the resolver first.** Move `openai` into an extra so `[eval]`
  resolves with openai 2.x. Cheap and correct, and it still buys metrics that
  cannot execute without a key. Deferred to when there is one.
- **Write our own faithfulness judge.** A local model could score it. That is
  a new model, a new prompt, and a new source of variance to characterise
  before any of its numbers mean anything — a project of its own, and not one
  to start while the ground-truth set is four pairs of a planned forty-eight.
- **Report faithfulness as null and keep the ragas import.** Rejected: an
  import that cannot be installed is a build failure waiting for whoever next
  runs `make install-eval`, and a metric column full of nulls invites someone
  to average around it.
