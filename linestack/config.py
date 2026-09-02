"""Responsibility: application settings, loaded from the environment, validated
once at import and never read from os.environ anywhere else.

Owns: database URLs, embedding model and dimension, generation model and
temperature, retrieval k, chunk target and overlap, crawl politeness constants,
Langfuse credentials. The canonical list with explanations is .env.example.

Does not own: anything derived from a request, and anything secret enough that
it should not appear in a settings repr.

Note: the embedding model name is configuration rather than a constant because
it is recorded on every row in chunks.embedding_model. Two models' vectors are
not comparable, so changing it invalidates every existing embedding, and the
recorded value is what makes that detectable rather than silent.
"""
