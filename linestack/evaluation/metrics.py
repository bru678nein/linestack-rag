"""Responsibility: computing the metrics, and keeping them separate.

Owns:
  - retrieval recall@k, per question, at document granularity so the metric
    does not change meaning when chunk size changes;
  - faithfulness and answer correctness via ragas, with the judge model
    recorded;
  - signal accuracy, an exact comparison against hand-checked ground truth;
  - ingestion coverage, which answers "was the evidence even crawled?" before
    any failure is attributed to retrieval;
  - the cross-prospect leakage assertion, which is a gate and not a metric: a
    run in which any retrieved chunk belongs to another prospect is a failed
    run, not a lower score.

Does not own: combining any of these into a single number. A change is accepted
or rejected on recall and faithfulness; correctness is recorded and is too
noisy to decide anything on its own.

Before writing this module, verify the pinned ragas version's actual API. The
metric names in docs/evaluation.md come from the library's general shape, not
from that release's documentation (docs/open-questions.md section 3.1).
"""
