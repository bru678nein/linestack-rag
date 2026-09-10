"""Responsibility: the prompt templates, versioned.

Owns: the system prompt, the words each question is asked in, the instruction
that unsupported claims must not be made, the fixed phrase that marks a
declined answer, and the citation format that ties each claim to a numbered
passage or to the computed facts.

PROMPT_VERSION is recorded with every answer and every evaluation run. A prompt
change is a change like any other and needs a recorded before-and-after (A3);
without the version in the record, a delta cannot be attributed to the prompt
rather than to retrieval or to the model.

The four evaluated questions are taken from `linestack.evaluation.dataset`
rather than written here. The ground-truth set defines what the questions ARE,
because it is what the answers are graded against; generation asks them in
those exact words.
"""

from __future__ import annotations

from linestack.evaluation.dataset import QUESTIONS

# Bump on ANY change to the text below, including a comma. Two runs with the
# same version and different wording are two runs nobody can compare.
PROMPT_VERSION = "answer-v1"

# The exact opening of a declined answer. Fixed so that declining is detectable
# without a judge: docs/ground-truth.md §3 grades "the evidence is not present"
# as CORRECT on an insufficient_evidence pair, and that grading only works if a
# decline can be recognised mechanically rather than by reading intent.
INSUFFICIENT = "Insufficient evidence."

# The citation label for the computed facts. Passages are cited [1], [2], ...
SIGNALS_CITATION = "S"

# The fifth question: generated, never evaluated, because it has no ground
# truth. Anything answered for it is marked as a suggestion (A4).
SUGGESTION_ID = "q5_first_approach"
SUGGESTION_QUESTION = (
    "Based only on this material, what is one concrete angle for a first "
    "conversation with this company?"
)

SYSTEM_PROMPT = f"""You answer one question about one company, using only the \
material you are given.

Rules, in order of importance:
1. Use only the COMPUTED FACTS and the RETRIEVED PASSAGES. Do not use anything \
you know about this company from anywhere else, even if you are sure of it.
2. After every claim, cite where it came from: [{SIGNALS_CITATION}] for a \
computed fact, [1], [2] and so on for a passage. A claim without a citation is \
not allowed.
3. If the material does not answer the question, reply with exactly \
"{INSUFFICIENT}" followed by one sentence saying what is missing. That is a \
correct answer, not a failure.
4. Do not guess, estimate, or generalise past what is written. A plausible \
claim with no support is wrong even when it happens to be true.
5. Answer in two to four plain sentences."""

SUGGESTION_ADDENDUM = (
    "This question asks for a suggestion, not a fact. Ground it in the "
    "material and cite what it rests on, the same as any other claim."
)


def question_text(question_id: str) -> str:
    """The words a question is asked in. Raises on an unknown id rather than
    asking something nobody wrote a reference for."""
    if question_id == SUGGESTION_ID:
        return SUGGESTION_QUESTION
    try:
        return QUESTIONS[question_id]
    except KeyError:
        known = [*QUESTIONS, SUGGESTION_ID]
        raise ValueError(
            f"unknown question id {question_id!r}; one of {known}"
        ) from None


def build_messages(
    question: str, context: str, *, is_suggestion: bool = False
) -> list[dict[str, str]]:
    """Chat messages for one question. The context comes BEFORE the question,
    so the last thing the model reads is what it is being asked."""
    system = SYSTEM_PROMPT + ("\n\n" + SUGGESTION_ADDENDUM if is_suggestion else "")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{context}\n\nQUESTION: {question}"},
    ]
