"""Responsibility: showing a frozen crawl artifact to the person writing ground
truth, exactly as the crawler stored it.

Owns: reading a `prospect_*.json` artifact and printing what is in it -- the
document list, and the text of one document.

Does not own: the database. It reads the ARTIFACT, deliberately, because that
is the thing a ground-truth file cites (`corpus_artifact`) and the thing every
reference answer is written against. Reading the loaded corpus instead would
mean the text you write from and the text the file names could drift apart
without anyone noticing.

## Why this exists

`docs/ground-truth.md` §2 step 2 is the discipline the whole set rests on:

> **Read the crawled documents, not the website.** If you write a reference
> answer from something you saw on the site that the crawler never fetched, you
> have written an ingestion test and labelled it a retrieval test.

That step had no command behind it. Following it meant a hand-written Python
one-liner against the JSON, and a rule that is inconvenient to follow is a rule
that gets followed loosely -- which here means a ground-truth set that quietly
measures the wrong thing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load(artifact: str | Path) -> dict[str, Any]:
    path = Path(artifact)
    if not path.exists():
        raise SystemExit(
            f"no such artifact: {path}\n  crawl one first:  make crawl DOMAIN=<domain>"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def document_lines(data: dict[str, Any]) -> list[str]:
    """Every document, longest first.

    Longest first rather than alphabetical because the question being answered
    is "where is there enough text to answer anything", and a 24-word
    navigation stub sorted next to a 1,100-word article hides that.
    """
    documents = data.get("documents") or []
    lines = [
        f"  {len(documents)} documents, "
        f"{sum(len(d['text'].split()) for d in documents)} words total"
    ]
    for doc in sorted(documents, key=lambda d: -len(d["text"].split())):
        words = len(doc["text"].split())
        lines.append(f"  {words:5}w  [{doc['kind']:11}] {doc['url']}")
    return lines


def find(data: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    """Documents whose URL contains `needle`. Substring, not exact.

    Returns every match rather than the first: `/blog` matches the index and
    nine posts, and silently showing one of them would be the kind of quiet
    wrong answer this project keeps finding.
    """
    return [d for d in (data.get("documents") or []) if needle in d["url"]]


def text_lines(doc: dict[str, Any], width: int = 88) -> list[str]:
    """One document, wrapped, with the provenance a citation needs."""
    import textwrap

    lines = [
        f"  url:       {doc['url']}",
        f"  kind:      {doc['kind']}",
        f"  published: {doc.get('published')}",
        f"  extracted: {doc.get('extract_reason')}",
        f"  words:     {len(doc['text'].split())}",
    ]
    if doc.get("duplicate_urls"):
        lines.append(f"  also at:   {', '.join(doc['duplicate_urls'])}")
    if doc.get("kind_conflicts"):
        # A contested kind is worth seeing here: it means two of the site's own
        # URLs classify this page differently (ADR-0019).
        lines.append(f"  kind also: {', '.join(doc['kind_conflicts'])}")
    lines.append("")
    lines += textwrap.wrap(" ".join(doc["text"].split()), width=width)
    return lines


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Show a frozen crawl artifact, for writing ground truth."
    )
    parser.add_argument("artifact", help="path to prospect_*.json")
    parser.add_argument(
        "--url",
        help="show the text of documents whose URL contains this; omit to list",
    )
    args = parser.parse_args(argv)

    data = load(args.artifact)
    if not args.url:
        print("\n".join(document_lines(data)))
        return 0

    matches = find(data, args.url)
    if not matches:
        print(f"  no crawled document matching {args.url!r}.")
        print("  If the page exists on the live site, it was never crawled --")
        print("  that is a coverage finding worth more than the pair")
        print("  (docs/ground-truth.md §2 step 2). Check why:")
        print("    make psql   # then: SELECT url, outcome, detail")
        print("                #       FROM crawl_page_outcomes ...")
        return 1

    for index, doc in enumerate(matches):
        if index:
            print()
        print("\n".join(text_lines(doc)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
