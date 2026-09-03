"""Unit tests for `linestack.retrieval.scope`. No database, no network.

The chokepoint's job is that a chunk query cannot be written anywhere else and
cannot be written here for the wrong prospect. Most of that is enforced by
shape rather than by assertion -- `prospect_id` is not a parameter, so there is
no call site to get wrong -- and these tests pin the parts that shape alone
does not cover.
"""

import inspect
import re
from pathlib import Path

import pytest

from linestack.retrieval import scope as scope_module
from linestack.retrieval.scope import (
    RANKING_SQL,
    EmbeddingBatch,
    ProspectScope,
    _as_vector_literal,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_0009 = REPO_ROOT / "docs" / "decisions" / "0009-retrieval-strategy.md"


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_the_ranking_query_is_the_one_adr_0009_froze() -> None:
    """The ADR is the spec; this asserts the code still matches it.

    ADR-0009 freezes the first retrieval implementation at cosine distance,
    exact search, single stage. Hybrid, source weighting and reranking are
    planned in that order and each ships with a recorded before-and-after,
    because shipped together their effects cannot be told apart (A3).

    So this test is not pedantry about a string. It is what makes adding a
    stage require editing the ADR that says not to.
    """
    adr = ADR_0009.read_text(encoding="utf-8")
    block = re.search(r"```sql\n(.*?)```", adr, re.S)
    assert block, "ADR-0009 no longer contains a ```sql block"

    assert _normalise(RANKING_SQL) == _normalise(block.group(1))


@pytest.mark.parametrize(
    "forbidden",
    ["rerank", "ts_rank", "content_tsv", "union", "rank_fusion", "case when"],
)
def test_the_ranking_query_has_not_grown_a_second_stage(forbidden: str) -> None:
    assert forbidden not in RANKING_SQL.lower()


def test_prospect_id_is_never_a_method_argument() -> None:
    """The isolation mechanism is the absence of a parameter.

    Every public method reads self._prospect_id. If one took a prospect_id,
    there would immediately be a call site where the wrong one could be
    passed, and the chokepoint would be a convention rather than a shape.
    """
    offenders = []
    for name, member in inspect.getmembers(ProspectScope, inspect.isfunction):
        if name.startswith("_") and name != "__init__":
            continue
        params = list(inspect.signature(member).parameters)
        if name in ("__init__", "open"):
            continue
        if any("prospect" in p for p in params):
            offenders.append(f"{name}({', '.join(params)})")

    assert offenders == [], (
        f"these take a prospect id as an argument: {offenders}. It belongs in "
        f"the constructor, where it cannot be passed wrongly."
    )


def test_write_embeddings_will_not_accept_a_bare_list_of_vectors() -> None:
    """The model name travels with the vectors or they do not travel.

    The database has a CHECK enforcing the pairing. This makes the unpaired
    case unrepresentable in Python, so the CHECK never has to fire.
    """
    signature = inspect.signature(ProspectScope.write_embeddings)
    annotation = signature.parameters["batch"].annotation
    assert annotation is EmbeddingBatch or annotation == "EmbeddingBatch"


def test_a_vector_of_the_wrong_length_is_refused_before_the_database_sees_it() -> None:
    """A dimension mismatch would otherwise surface as an opaque cast error
    instead of "config says 1536, the model returned 512"."""
    batch = EmbeddingBatch(model="m", dimensions=1536, vectors=[[0.0] * 512])
    scope = ProspectScope(session=None, prospect_id=1)

    with pytest.raises(ValueError, match="1536-dimension"):
        import asyncio

        asyncio.run(scope.write_embeddings([1], batch))


def test_a_mismatched_id_and_vector_count_is_refused_rather_than_guessed() -> None:
    batch = EmbeddingBatch(
        model="m", dimensions=3, vectors=[[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]
    )
    scope = ProspectScope(session=None, prospect_id=1)

    with pytest.raises(ValueError, match="refusing to guess"):
        import asyncio

        asyncio.run(scope.write_embeddings([7], batch))


def test_the_vector_literal_is_pgvector_text_not_a_bound_list() -> None:
    """linestack/db.py explains why: HALFVEC and the asyncpg binary codec are
    alternatives, not layers, and registering both breaks every insert."""
    assert _as_vector_literal([0.5, -0.25]) == "[0.5,-0.25]"


def test_scope_is_the_only_module_holding_the_ranking_sql() -> None:
    package = REPO_ROOT / "linestack"
    holders = [
        path.relative_to(REPO_ROOT)
        for path in package.rglob("*.py")
        if "embedding <=>" in path.read_text(encoding="utf-8")
    ]
    assert holders == [Path("linestack/retrieval/scope.py")], (
        f"the cosine operator appears in {holders}; the ranking query lives "
        f"in scope.py alone"
    )


def test_the_module_docstring_still_names_the_stronger_mechanism() -> None:
    """The composite foreign key is the real enforcement; this object is the
    weaker, second one. Anyone editing here should read that first."""
    # Whitespace-normalised: the docstring is wrapped, so "row-level\nsecurity"
    # is one phrase across two lines.
    doc = re.sub(r"\s+", " ", scope_module.__doc__ or "")
    assert "composite foreign key" in doc
    assert "row-level security" in doc
