# ADR-0002 — No LangChain in the core pipeline

Status: Accepted · Date: 2026-09-02

## Decision

The ingestion, retrieval, and generation pipeline is written directly against
`httpx`, `trafilatura`, `selectolax`, SQLAlchemy, and the OpenAI SDK. LangChain
and LlamaIndex are not dependencies of the core.

A parallel LangGraph implementation of the same pipeline may be added later as a
comparison, in a separate module with its own optional dependency group. It
would not be on the serving path.

## Alternatives

- **LangChain for the whole pipeline.** Fewer lines to write for the happy path.
  Costs: an abstraction layer between us and the SQL, which is the layer where
  A1 is enforced; retriever objects that hide the query being executed; and a
  dependency whose API has broken across minor versions more than once.
- **LlamaIndex.** Same trade-off, with a stronger ingestion story and a weaker
  fit for a pipeline that is already written.

## Why

The pipeline is short. It is: crawl, extract, chunk, embed, insert, filter,
search, assemble a prompt, stream. Each step is tens of lines. A framework does
not save meaningful work at this size.

**[assumed]** the framework would cost more than it saves here, for two specific
reasons rather than as a general preference:

1. **A1 is enforced in the SQL.** A chunk from prospect B must be unreachable
   when answering about prospect A. That guarantee lives in a composite foreign
   key and in a single query builder that requires a `prospect_id`. A retriever
   abstraction that constructs its own query is a place where that guarantee can
   be lost without anyone noticing, and the failure it produces — a fluent
   answer about the wrong company — is exactly the failure this project exists
   to prevent.
2. **A8 requires that retrieval is measurable separately from generation.** That
   is easier when the retrieval call is a function that returns rows and scores
   than when it is a chain step whose intermediate state has to be extracted
   through callbacks.

The counter-argument is real and should be recorded: writing it directly means
writing our own retry, batching, and streaming handling, and those are places
where a mature library is better than a first attempt. That is accepted.

## What would reverse it

- The pipeline grows a control-flow shape that is genuinely graph-like — branch
  on question type, loop on insufficient retrieval, re-query with a rewritten
  question — and the hand-written state handling for it exceeds roughly 300
  lines. At that point LangGraph is doing real work.
- The comparison implementation, when it is built, measurably beats the direct
  one on the evaluation set. "Measurably" means on the metrics in
  `docs/evaluation.md`, not on lines of code.
- A specific integration we need (a reranker, an evaluator) exists only as a
  LangChain component. Vendoring one component is still cheaper than adopting
  the framework.
