"""Responsibility: loading and validating the hand-written ground-truth set from
eval/ground_truth/*.yaml.

Owns: schema validation (required fields, resolvable prospect references,
well-formed source URLs, valid expected_outcome values) and the check that each
file's corpus_artifact exists. Structural validation only -- no model calls, so
it runs on every push in CI.

Does not own: judging answers. That is metrics.py.

A reference answer whose corpus_artifact is missing is unfalsifiable and must
be rejected rather than skipped.

WHAT THIS CAN AND CANNOT CATCH, because the difference decides how much the
green tick is worth. It catches shape: a missing field, a question id that is
not one of the four, an `expected_outcome` that is neither of the two words, a
`source_urls` entry that is not a URL or points at another company's domain, a
`corpus_artifact` that is not on disk. It cannot catch a reference answer that
is simply wrong, one written from the live site rather than the frozen corpus,
or one a model wrote. Those are the failures `docs/ground-truth.md` §2 and §4
guard against, and they are guarded by discipline rather than by this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from linestack.config import settings

# The four questions are fixed. A fifth is generated but never evaluated, and a
# typo'd id would otherwise create a silent fifth column in every metric.
QUESTION_IDS = (
    "q1_what_and_to_whom",
    "q2_technical_capacity",
    "q3_growth_signals",
    "q4_stated_pain",
)

OUTCOMES = ("answerable", "insufficient_evidence")

# A scaffolded file is not a written one. The placeholder is rejected so that a
# skeleton cannot be committed and reported green: half the value of this
# validator is that "it passes" means someone did the work.
TODO = "TODO"

REQUIRED_PROSPECT_FIELDS = ("company_name", "domain", "corpus_artifact")
REQUIRED_QUESTION_FIELDS = ("id", "question", "reference", "source_urls")

# docs/ground-truth.md §3: pairs the corpus genuinely cannot answer "should be
# roughly a quarter" of the set. Below this, the set rewards fluency.
MIN_INSUFFICIENT_SHARE = 0.15

_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass
class Finding:
    """One problem, located precisely enough to fix without hunting."""

    path: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.where}: {self.message}"


@dataclass
class ValidationReport:
    files: int = 0
    pairs: int = 0
    insufficient: int = 0
    findings: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_lines(self) -> list[str]:
        lines = [
            f"  files:    {self.files}",
            f"  pairs:    {self.pairs}",
            f"  of which insufficient_evidence: {self.insufficient}"
            + (f" ({self.insufficient / self.pairs:.0%})" if self.pairs else ""),
        ]
        for finding in self.warnings:
            lines.append(f"  warning:  {finding}")
        for finding in self.findings:
            lines.append(f"  ERROR:    {finding}")
        if self.ok:
            lines.append("  structurally valid")
        else:
            lines += [
                f"  {len(self.findings)} to fix.",
                "  Order that wastes least time: hand-check the signals against",
                "  the live site first, then write each reference from the",
                "  CRAWLED text (docs/ground-truth.md §2 steps 3 and 4).",
            ]
        return lines


def validate_directory(
    directory: str | Path, repo_root: Path | None = None
) -> ValidationReport:
    """Validate every ground-truth file in a directory.

    An empty directory is not an error. The set is written by hand over hours,
    so CI has to stay green while it is being written -- otherwise the first
    commit of the first file turns the build red for a week and everyone learns
    to ignore it.
    """
    directory = Path(directory)
    root = repo_root or Path(directory).resolve().parent.parent
    report = ValidationReport()

    for path in sorted(directory.glob("*.yaml")):
        report.files += 1
        _validate_file(path, root, report)

    _check_the_set_as_a_whole(directory, report)
    return report


def _validate_file(path: Path, root: Path, report: ValidationReport) -> None:
    def fail(where: str, message: str) -> None:
        report.findings.append(Finding(path.name, where, message))

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail("file", f"is not valid YAML: {str(exc).splitlines()[0]}")
        return

    if not isinstance(data, dict):
        fail("file", "does not contain a mapping at the top level")
        return

    prospect = data.get("prospect")
    if not isinstance(prospect, dict):
        fail("prospect", "missing or not a mapping")
        return

    for key in REQUIRED_PROSPECT_FIELDS:
        value = prospect.get(key)
        if not value:
            fail("prospect", f"{key} is required")
        elif isinstance(value, str) and TODO in value:
            fail("prospect", f"{key} still holds the {TODO} placeholder")

    signals = data.get("signals")
    if isinstance(signals, dict):
        unfilled = [
            key
            for key, value in signals.items()
            if isinstance(value, str) and TODO in value
        ]
        if unfilled:
            # One finding for the whole block, not one per field. Repeating the
            # same paragraph six times buries the other errors and teaches the
            # reader to skim -- which is how a real finding gets missed.
            #
            # `notes` is free text, not a number to check against the site, so
            # it is named separately rather than lumped in with the claim.
            numbers = [k for k in unfilled if k != "notes"]
            if numbers:
                fail(
                    "signals",
                    f"{', '.join(numbers)} still hold {TODO}. Hand-check these "
                    f"against the LIVE site; the crawler's values are shown "
                    f"beside each one and are the claim under test, not the "
                    f"answer (docs/ground-truth.md §2 step 3).",
                )
            if "notes" in unfilled:
                fail("signals.notes", f"still holds {TODO}: record how you checked")

    domain = str(prospect.get("domain", "")).lower()
    if domain and not _DOMAIN_RE.match(domain):
        fail("prospect.domain", f"{domain!r} does not look like a domain")

    # The artifact is what makes a reference answer falsifiable. Without it,
    # nobody can tell whether an answer was wrong or the corpus had changed.
    artifact = prospect.get("corpus_artifact")
    if artifact and not (root / str(artifact)).exists():
        fail(
            "prospect.corpus_artifact",
            f"{artifact} is not on disk. A reference answer without its frozen "
            f"corpus is unfalsifiable (docs/ground-truth.md §1).",
        )

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        fail("questions", "missing, empty, or not a list")
        return

    seen: set[str] = set()
    for index, question in enumerate(questions):
        where = f"questions[{index}]"
        if not isinstance(question, dict):
            fail(where, "is not a mapping")
            continue

        unfilled: list[str] = []
        for key in REQUIRED_QUESTION_FIELDS:
            if key not in question:
                fail(where, f"{key} is required")
            elif isinstance(question[key], str) and TODO in question[key]:
                unfilled.append(key)
        if unfilled:
            fail(
                f"{where} ({question.get('id', '?')})",
                f"not written yet: {', '.join(unfilled)}",
            )

        qid = question.get("id")
        if qid not in QUESTION_IDS:
            fail(where, f"id {qid!r} is not one of {list(QUESTION_IDS)}")
        elif qid in seen:
            fail(where, f"id {qid!r} appears twice in this file")
        else:
            seen.add(qid)

        outcome = question.get("expected_outcome", "answerable")
        if outcome not in OUTCOMES:
            fail(where, f"expected_outcome {outcome!r} is not one of {list(OUTCOMES)}")

        report.pairs += 1
        if outcome == "insufficient_evidence":
            report.insufficient += 1

        _validate_sources(question, where, domain, outcome, path, report)

    missing = [q for q in QUESTION_IDS if q not in seen]
    if missing and not report.findings:
        report.warnings.append(
            Finding(path.name, "questions", f"no pair yet for {missing}")
        )


def _validate_sources(
    question: dict,
    where: str,
    domain: str,
    outcome: str,
    path: Path,
    report: ValidationReport,
) -> None:
    """source_urls is what recall is computed against, so it has to be exact."""

    def fail(message: str) -> None:
        report.findings.append(Finding(path.name, f"{where}.source_urls", message))

    urls = question.get("source_urls")
    if urls is None:
        return
    if not isinstance(urls, list):
        fail("must be a list (it may be empty)")
        return

    if not urls and outcome != "insufficient_evidence":
        fail(
            "is empty, but expected_outcome is 'answerable'. An answerable "
            "question with no evidence makes recall unmeasurable; mark it "
            "insufficient_evidence instead (docs/ground-truth.md §3)."
        )

    for url in urls:
        if TODO in str(url):
            # Not "malformed URL": unwritten. Saying the wrong thing here sends
            # the reader looking for a typo instead of doing the work.
            continue
        parsed = urlparse(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            fail(f"{url!r} is not an absolute http(s) URL")
            continue
        # A5/A1 in the dataset: a reference answer citing another company's
        # page makes recall unmeasurable and quietly crosses prospects.
        host = parsed.netloc.lower().removeprefix("www.")
        if domain and host != domain and not host.endswith("." + domain):
            fail(
                f"{url!r} is not on {domain}. Evidence must come from the "
                f"prospect's own frozen corpus (docs/ground-truth.md §4)."
            )


def _check_the_set_as_a_whole(directory: Path, report: ValidationReport) -> None:
    """Properties of the set, not of any one file.

    Warnings rather than errors: the set is written over hours, and a rule about
    its final shape must not fail the build while it is half-written.
    """
    if not report.pairs:
        return

    share = report.insufficient / report.pairs
    if share < MIN_INSUFFICIENT_SHARE:
        report.warnings.append(
            Finding(
                str(directory),
                "set",
                f"only {share:.0%} of pairs are insufficient_evidence. "
                f"docs/ground-truth.md §3 wants roughly a quarter: without "
                f"them every metric rewards fluency, and a system that answers "
                f"all 48 questions confidently scores well for the exact "
                f"failure this project exists to prevent.",
            )
        )


SIGNALS_TO_CHECK = (
    "has_team_page",
    "people_listed",
    "open_roles_seen",
    "technical_roles_open",
    "latest_post_date",
)


def scaffold(artifact_path: str | Path, author: str = "TODO your email") -> str:
    """Build an unfilled ground-truth file from a frozen crawl artifact.

    Fills in only what is mechanical: the prospect block, the artifact
    reference, and the candidate source URLs grouped by page kind so nobody has
    to grep a 400 KB JSON file by hand.

    Deliberately does NOT fill in:

    - **The signals.** `docs/ground-truth.md` §2 step 3 says to hand-check them
      against the live site. Copying the crawler's numbers here would make
      signal accuracy 100% by construction, because the crawler's numbers are
      exactly what those pairs test. They appear as comments, labelled as the
      crawler's claim, for you to confirm or refute.
    - **The reference answers.** §4 forbids model-generated ones outright: a set
      written by a model measures agreement with that model, which is not the
      thing under test.

    Every unfilled field carries a TODO that the validator rejects, so a
    scaffold cannot be committed and pass.
    """
    import json

    artifact_path = Path(artifact_path)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    crawled = artifact.get("signals", {})

    by_kind: dict[str, list[str]] = {}
    for document in artifact.get("documents", []):
        by_kind.setdefault(document["kind"], []).append(document["url"])

    lines = [
        f"# {artifact['domain']} — ground truth",
        "#",
        "# Scaffolded from the frozen artifact. Every TODO below is rejected by",
        "# `make ground-truth-validate` until you replace it.",
        "#",
        "# Read docs/ground-truth.md §2 before filling this in. The rule that",
        "# matters most: write the reference answers from the CRAWLED text, not",
        "# from the live site. An answer written from something the crawler",
        "# never fetched is an ingestion test wearing a retrieval test's label.",
        "",
        "prospect:",
        f"  company_name: {artifact['company_name']}",
        f"  domain: {artifact['domain']}",
        f'  crawled_at: "{artifact["crawled_at"]}"',
        f"  corpus_artifact: {artifact_path.name}",
        f"  author: {author}",
        "  written_at: TODO yyyy-mm-dd",
        "",
        "# Hand-check these against the LIVE site and replace each TODO.",
        "# The values the crawler reported are shown beside each one: they are",
        "# the claim under test, not the answer. Count the people yourself; open",
        "# every page it called a job posting and decide if it is a vacancy.",
        "signals:",
    ]
    for name in SIGNALS_TO_CHECK:
        claimed = crawled.get(name, "not reported")
        lines.append(f"  {name}: TODO          # the crawler says: {claimed}")
    lines += [
        "  notes: >",
        "    TODO how you checked, and anything the next author needs.",
        "",
        "questions:",
    ]

    prompts = {
        "q1_what_and_to_whom": "What does this company do, and who does it sell to?",
        "q2_technical_capacity": (
            "What evidence is there of in-house technical capacity?"
        ),
        "q3_growth_signals": (
            "What signals are there that they are investing or growing?"
        ),
        "q4_stated_pain": "What pain or problem do they state explicitly?",
    }
    for qid, prompt in prompts.items():
        lines += [
            f"  - id: {qid}",
            f"    question: {prompt}",
            "    reference: >",
            "      TODO 2-4 sentences, in the register a colleague would use,",
            "      claiming only what the pages in source_urls support.",
            "    source_urls:",
            "      - TODO https://... the pages you actually used",
            "    # expected_outcome: insufficient_evidence   # if the corpus",
            "    #   genuinely does not answer this. That is a CORRECT answer",
            "    #   and roughly a quarter of the set should be these (§3).",
            "    must_not_claim:",
            "      - TODO what a fluent, confident, wrong answer would say here.",
            "      #  This is the highest-value field in the file (§1).",
            "",
        ]

    lines += [
        "# ---------------------------------------------------------------",
        f"# Candidate source URLs, from the {len(artifact.get('documents', []))} "
        "documents in this artifact.",
        "# Only these were crawled. If the page you want is missing, that is a",
        "# coverage finding worth more than the pair (§2 step 2) -- check",
        "# crawl_page_outcomes for the reason before assuming it does not exist.",
    ]
    for kind in sorted(by_kind):
        lines.append(f"#   {kind} ({len(by_kind[kind])}):")
        for url in sorted(by_kind[kind]):
            lines.append(f"#     {url}")
    return "\n".join(lines) + "\n"


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="linestack.evaluation.dataset")
    parser.add_argument(
        "--validate",
        default=settings.eval_ground_truth_dir,
        help="directory of ground-truth YAML files",
    )
    parser.add_argument(
        "--scaffold",
        metavar="ARTIFACT",
        help="write an unfilled ground-truth file from a crawl artifact",
    )
    args = parser.parse_args(argv)

    if args.scaffold:
        artifact = Path(args.scaffold)
        if not artifact.exists():
            print(f"  {artifact} does not exist. Crawl it first: make crawl DOMAIN=...")
            return 2
        import json

        domain = json.loads(artifact.read_text(encoding="utf-8"))["domain"]
        out = Path(args.validate) / f"{domain.replace('.', '_')}.yaml"
        if out.exists():
            print(f"  {out} already exists; refusing to overwrite your work")
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(scaffold(artifact), encoding="utf-8")
        print(f"\n  wrote {out}")
        print("  Every TODO in it is rejected by `make ground-truth-validate`")
        print("  until you replace it. Read docs/ground-truth.md §2 first.")
        return 0

    directory = Path(args.validate)
    if not directory.exists():
        print(f"  {directory} does not exist")
        return 2

    report = validate_directory(directory)
    print(f"\n=== {directory}")
    for line in report.as_lines():
        print(line)
    if not report.files:
        print("  nothing to validate yet; see docs/ground-truth.md §2")
    return 0 if report.ok else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
