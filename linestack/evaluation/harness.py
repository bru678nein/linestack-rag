"""Responsibility: running the ground-truth set against a fixed configuration and
recording the result.

Owns: the run record -- corpus version, retrieval configuration, embedding
model, generation model and prompt version, judge model, and timings split into
embed / retrieve / generate -- and the delta table against the previous run of
the same corpus.

A3: no retrieval improvement ships without a recorded before-and-after, and a
change with no measured effect is reverted. This module is what makes that rule
enforceable rather than aspirational, so the run record is not optional output.

The corpus is frozen. If the harness re-crawls between runs, the corpus and the
configuration both changed and the delta means nothing.
"""
