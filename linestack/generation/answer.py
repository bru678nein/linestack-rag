"""Responsibility: assembling context and producing a streamed, cited answer.

Owns: context assembly order, the token budget, and the rule that
prospects.signals is injected on every answer regardless of what retrieval
returned -- a computed fact does not compete with vector similarity for a place
in the context window (A2).

Does not own: deciding what is true. An answer that the retrieved context does
not support must say so. "Insufficient evidence" is a correct answer and is
graded as one (docs/ground-truth.md section 3).

The fifth question -- a concrete angle for a first approach -- is generated
here and is deliberately excluded from evaluation, because it has no ground
truth. Anything generated for it is marked as such in the output (A4).
"""
