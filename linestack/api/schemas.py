"""Responsibility: Pydantic v2 request and response models.

Owns: the wire contract. Notably, a retrieved chunk in a response carries its
score, its source_url, and its kind, because the frontend renders all three.

A response field that reports a computed signal and a response field that
reports a model-generated claim are different types, named differently. "I
measured X" and "I expect X" are different claims and must be written
differently (A4); that applies to the API surface as much as to prose.
"""
