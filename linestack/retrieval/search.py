"""Responsibility: ranking chunks for a question inside a single prospect scope.

The first implementation is deliberately naive: cosine distance, exact search
under the prospect filter, single stage, no reranking, no query rewriting, no
source weighting (ADR-0009). There is no HNSW index; the candidate set after
the prospect filter is small enough for exact search, which additionally has
perfect recall (ADR-0001).

Hybrid search, source weighting by kind, and reranking are planned in that
order, and each ships only with a recorded before-and-after on the evaluation
set (A3). Do not add two at once: their effects are not separable afterwards.

Every result carries its score outward, unchanged, all the way to the UI. A
retrieval failure that is not visible gets attributed to the model.
"""
