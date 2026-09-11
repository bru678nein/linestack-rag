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
PROMPT_VERSION = "answer-v3"

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
correct answer, not a failure. Either answer or decline, never both: a decline \
starts with "{INSUFFICIENT}" and says nothing else about the company.
4. Material that is about the same topic but does not answer the question is \
not an answer. Decline instead of describing it.
5. Do not infer. If a sentence needs "suggesting", "indicating", "implies" or \
"shows that" to connect the material to the question, the material does not \
answer it. The absence of something is not evidence either.
6. Do not guess, estimate, or generalise past what is written. A plausible \
claim with no support is wrong even when it happens to be true.
7. Answer in two to four plain sentences."""

# What counts as an answer to each evaluated question. These are the grading
# rules from docs/ground-truth.md and ADR-0023, stated for every prospect alike:
# a model graded by a rule it was never shown is measured on guessing the rule.
# Nothing here may name or describe one company; that would tune the prompt to
# the ground-truth set instead of to the question.
QUESTION_SCOPE = {
    "q1_what_and_to_whom": (
        "What the company sells, and to which kinds of customers. A problem it "
        "solves for its clients belongs here."
    ),
    "q2_technical_capacity": (
        "Evidence that the company itself employs people who build technology: "
        "engineering or technical job titles, technical job postings, or "
        "engineering work its own staff describe."
    ),
    "q3_growth_signals": (
        "Growth or investment by the company itself: roles it is hiring for now, "
        "funding, new offices, new products or teams. A headcount on its own is "
        "not growth, because it says nothing about change. Employees' personal "
        "career development is not company growth."
    ),
    "q4_stated_need": (
        "A need the company states in its own words: something it says it cannot "
        "yet do, such as a role it is hiring for and what that role must fix, or "
        "a system it says it is rebuilding. A problem it solves for clients is "
        "not its need. A value, commitment, policy or programme it already has "
        "is not a need."
    ),
}

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
    question: str,
    context: str,
    *,
    is_suggestion: bool = False,
    question_id: str | None = None,
) -> list[dict[str, str]]:
    """Chat messages for one question. The context comes BEFORE the question,
    so the last thing the model reads is what it is being asked. An evaluated
    question also gets what counts as an answer to it, just before it."""
    system = SYSTEM_PROMPT + ("\n\n" + SUGGESTION_ADDENDUM if is_suggestion else "")
    scope = QUESTION_SCOPE.get(question_id or "")
    counts = f"WHAT COUNTS AS AN ANSWER: {scope}\n\n" if scope else ""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{context}\n\n{counts}QUESTION: {question}"},
    ]
