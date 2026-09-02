"""Responsibility: loading and validating the hand-written ground-truth set from
eval/ground_truth/*.yaml.

Owns: schema validation (required fields, resolvable prospect references,
well-formed source URLs, valid expected_outcome values) and the check that each
file's corpus_artifact exists. Structural validation only -- no model calls, so
it runs on every push in CI.

Does not own: judging answers. That is metrics.py.

A reference answer whose corpus_artifact is missing is unfalsifiable and must
be rejected rather than skipped.
"""
