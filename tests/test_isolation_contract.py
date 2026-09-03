"""Tests for the A1 prospect-isolation boundary.

A1: a chunk belonging to prospect B must never be reachable when answering
about prospect A. That guarantee is enforced by two mechanisms, and this file
tests both.

The database mechanism is tested against a real Postgres and is marked
`integration`. The source-level mechanism costs nothing and runs everywhere.

docs/architecture.md section 4.
"""

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "linestack"
MIGRATION = REPO_ROOT / "migrations" / "0001_initial_schema.sql"

# The only module permitted to build a query against `chunks`.
SCOPE_MODULE = PACKAGE / "retrieval" / "scope.py"


# ---------------------------------------------------------------------------
# Mechanism 1: the schema makes a mismatched chunk unrepresentable
# ---------------------------------------------------------------------------
def test_migration_declares_the_composite_foreign_key() -> None:
    """The strongest of the two mechanisms. A chunk whose prospect_id disagrees
    with its document's prospect_id must be rejected by the database, not by
    review. Dropping this constraint means the denormalised column can drift,
    at which point filtering on it is no longer equivalent to joining."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert re.search(r"UNIQUE\s*\(\s*id\s*,\s*prospect_id\s*\)", sql, re.I), (
        "documents needs UNIQUE (id, prospect_id) for the composite key to reference"
    )

    assert re.search(
        r"FOREIGN\s+KEY\s*\(\s*document_id\s*,\s*prospect_id\s*\)\s*"
        r"REFERENCES\s+documents\s*\(\s*id\s*,\s*prospect_id\s*\)",
        sql,
        re.I,
    ), "chunks must reference documents by (id, prospect_id), not by id alone"


def test_migration_indexes_the_prospect_filter() -> None:
    """ADR-0001 depends on this: there is no approximate vector index, so the
    prospect filter is what keeps the exact search cheap."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert re.search(
        r"CREATE\s+INDEX\s+\w+\s+ON\s+chunks\s*\(\s*prospect_id\s*\)", sql, re.I
    )


def test_migration_does_not_create_an_ann_index() -> None:
    """ADR-0001 and A9: no HNSW index until a measurement justifies one. When
    that measurement exists, it goes in a new migration and this test is
    updated with the number that justified it."""
    sql = MIGRATION.read_text(encoding="utf-8")
    active = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "USING hnsw" not in active
    assert "USING ivfflat" not in active


# ---------------------------------------------------------------------------
# Mechanism 2: one chokepoint for chunk queries
# ---------------------------------------------------------------------------
CHUNK_QUERY = re.compile(r"\bfrom\s+chunks\b|\bjoin\s+chunks\b", re.I)


def test_only_the_scope_module_queries_chunks() -> None:
    """Weaker than the composite key, and second for that reason: it is
    enforced by this test rather than by the database.

    If this fails, the fix is to move the query into
    linestack/retrieval/scope.py -- not to add the module to an allowlist. A
    second place that builds chunk queries is the trigger recorded in
    docs/open-questions.md section 3.3 for adopting row-level security.
    """
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in PACKAGE.rglob("*.py")
        if path != SCOPE_MODULE and CHUNK_QUERY.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"chunk queries outside scope.py: {offenders}"


# The regex above only catches raw SQL. Once linestack/models.py exists, a
# SQLAlchemy ORM query -- select(Chunk).where(...) -- matches neither
# "from chunks" nor "join chunks", so the guard above would pass while a
# second module queried chunks freely. Verified 2026-09-02: adding
# `select(Chunk)` to loader.py left the SQL guard green.
MODELS_MODULE = PACKAGE / "models.py"


def _imports_the_chunk_model(source: str) -> bool:
    """Whether this module actually imports Chunk, by parsing rather than grep.

    A regex for the word "Chunk" matches prose: chunking.py's own docstring
    says "Chunk" and tripped an earlier version of this test. A guard that
    fires on a docstring is a guard someone deletes, so this one reads the
    import statements instead.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
        return False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and "models" in node.module
            and any(alias.name == "Chunk" for alias in node.names)
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "Chunk":
            return True
    return False


def test_only_the_scope_module_imports_the_chunk_model() -> None:
    """The ORM half of the chunk-query rule.

    The regex guard above only catches raw SQL. A SQLAlchemy ORM query --
    select(Chunk).where(...) -- matches neither "from chunks" nor "join
    chunks", so without this a second module could query chunks freely while
    the SQL guard stayed green.

    models.py may import nothing (it defines Chunk); scope.py is the
    chokepoint. A third module importing it is a second place that can build a
    chunk query. If this fails, move the query into scope.py -- do not add the
    module to an allowlist. See docs/open-questions.md section 3.3.
    """
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in PACKAGE.rglob("*.py")
        if path not in {SCOPE_MODULE, MODELS_MODULE}
        and _imports_the_chunk_model(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"the Chunk model is imported outside scope.py: {offenders}"


SEARCH_MODULE = PACKAGE / "retrieval" / "search.py"


def test_search_never_touches_a_session() -> None:
    """search.py orders the call; it does not make one.

    A session in search.py is a query one edit away from existing, and it
    would be the natural place to add "just one more filter" -- which is
    exactly how a prospect filter gets forgotten.
    """
    if not SEARCH_MODULE.exists():
        return
    source = SEARCH_MODULE.read_text(encoding="utf-8")
    for forbidden in ("AsyncSession", "session", "execute("):
        assert forbidden not in source, (
            f"search.py references {forbidden!r}. It takes a ProspectScope, "
            f"never a session: the scope is what carries the prospect filter."
        )


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------
def test_every_module_states_its_responsibility() -> None:
    """Scaffolding contract: an empty module without a docstring is a file
    nobody knows what to do with."""
    missing = [
        path.relative_to(REPO_ROOT)
        for path in PACKAGE.rglob("*.py")
        if not path.read_text(encoding="utf-8").lstrip().startswith('"""')
    ]
    assert not missing, f"modules with no responsibility docstring: {missing}"


# ---------------------------------------------------------------------------
# The same guarantee, against a real database
# ---------------------------------------------------------------------------
DSN = os.environ.get(
    "DATABASE_URL_SYNC", "postgresql://linestack:linestack@localhost:5432/linestack"
)


@pytest.mark.integration
async def test_database_rejects_a_chunk_from_the_wrong_prospect() -> None:
    """The A1 boundary, exercised rather than asserted about.

    Requires a migrated database: `make up && make migrate`.
    """
    asyncpg = pytest.importorskip("asyncpg")

    conn = await asyncpg.connect(DSN.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        async with conn.transaction():
            a = await conn.fetchval(
                "INSERT INTO prospects (company_name, domain) "
                "VALUES ('A', 'a.test') RETURNING id"
            )
            b = await conn.fetchval(
                "INSERT INTO prospects (company_name, domain) "
                "VALUES ('B', 'b.test') RETURNING id"
            )
            doc_a = await conn.fetchval(
                "INSERT INTO documents "
                "  (prospect_id, source_url, kind, content_hash, fetched_at) "
                "VALUES ($1, 'https://a.test/', 'website', 'hash-a', now()) "
                "RETURNING id",
                a,
            )

            # The honest case still works.
            await conn.execute(
                "INSERT INTO chunks "
                "  (document_id, prospect_id, kind, chunk_index, content, token_count) "
                "VALUES ($1, $2, 'website', 0, 'text', 10)",
                doc_a,
                a,
            )

            # Prospect B claiming a chunk of prospect A's document must fail.
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO chunks (document_id, prospect_id, kind, "
                    "  chunk_index, content, token_count) "
                    "VALUES ($1, $2, 'website', 1, 'leaked', 10)",
                    doc_a,
                    b,
                )

            raise _Rollback
    except _Rollback:
        pass
    finally:
        await conn.close()


class _Rollback(Exception):
    """Unwinds the test transaction so the database is left as it was found."""
