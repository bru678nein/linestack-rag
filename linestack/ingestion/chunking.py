"""Responsibility: splitting a document into embeddable chunks.

Owns: the policy in ADR-0005 -- 800-1200 tokens, roughly 150 tokens of overlap,
split on heading structure rather than character counts, job postings never
split, publication date carried into chunk metadata -- and token counting via
tiktoken so that "800-1200 tokens" is a measured quantity rather than an
estimate.

Does not own: embedding. A chunk is written to the database before it has a
vector; chunks.embedding is nullable for that reason.

Every parameter here is a measured variable, not a constant. Chunk size and
overlap are recorded with every evaluation run, and a change to either ships
only with a recorded before-and-after (A3).
"""
