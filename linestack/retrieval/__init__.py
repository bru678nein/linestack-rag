"""Responsibility: getting the right chunks for a question, within one prospect.

A8: retrieval is the bottleneck, not the model. When an answer is wrong, the
first hypothesis is that the right chunk was never retrieved, so everything in
this package is built to be measurable in isolation from generation.
"""
