"""Responsibility: turning text into a halfvec(1536) with text-embedding-3-small,
and recording which model produced it.

Owns: batching, retry, and the invariant that an embedding is never stored
without its model name -- the schema enforces the pairing, this module supplies
it.

Does not own: the choice of model. That is configuration, because changing it
invalidates every embedding already stored.
"""
