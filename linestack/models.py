"""Responsibility: SQLAlchemy declarative models mirroring migrations/*.sql.

Owns: Prospect, Document, Chunk, CrawlRun, CrawlPageOutcome.

Does not own: the schema itself. The SQL migrations are the source of truth;
these models follow them. Constraints are declared here for readability, but
the constraint that enforces A1 -- the composite foreign key on
chunks(document_id, prospect_id) to documents(id, prospect_id) -- exists in the
database whether or not it is declared here, and must never be relaxed here to
make a test pass.
"""

from __future__ import annotations

import datetime as dt

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from linestack.config import settings

# The enum values are the migration's, not a parallel vocabulary. ingest.py
# holds the same strings as its PAGE_*, ROBOTS_* and CRAWL_* constants, and
# tests assert all three sets agree (ADR-0012, ADR-0016). create_type=False
# because the migration already created them; SQLAlchemy must not try again.
DocumentKind = Enum(
    "website",
    "job_posting",
    "blog_post",
    name="document_kind",
    create_type=False,
)
RobotsOutcome = Enum(
    "ok",
    "absent",
    "unreadable",
    "server_error",
    "fetch_failed",
    name="robots_outcome",
    create_type=False,
)
CrawlOutcome = Enum(
    "completed",
    "aborted_robots",
    "aborted_unreachable",
    "failed",
    name="crawl_outcome",
    create_type=False,
)
PageOutcome = Enum(
    "stored",
    "skipped_robots",
    "dns_failure",
    "timeout",
    "transport_error",
    "http_error",
    "non_html",
    "thin_extraction",
    "duplicate_content",
    "budget_exhausted",
    name="page_outcome",
    create_type=False,
)


class Base(DeclarativeBase):
    pass


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Deterministic computed facts (A2, ADR-0003), injected into every answer
    # context regardless of what retrieval returned.
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    documents: Mapped[list[Document]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("domain = lower(domain)", name="prospects_domain_lowercase"),
        CheckConstraint(
            "jsonb_typeof(signals) = 'object'", name="prospects_signals_is_object"
        ),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prospects.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(DocumentKind, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    published_at: Mapped[dt.date | None] = mapped_column(Date)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Exact hash of the extracted text. Any change changes it, a pure
    # reordering included -- which is why this is NOT the column compared for
    # A7 idempotency. See stable_hash (ADR-0013).
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Hash with word order removed. THIS is the A7 comparison: some sites
    # reshuffle repeated records on every request, so an unchanged page
    # produces a new content_hash every crawl. Nullable because migration 0002
    # added it to a table that already had rows; a NULL means "unknown", which
    # the loader treats as changed rather than skipping.
    stable_hash: Mapped[str | None] = mapped_column(Text)
    # Other URLs observed serving this content. Evidence in its own right:
    # has_team_page reads the URL path, not the text (ADR-0013).
    duplicate_urls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )

    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    prospect: Mapped[Prospect] = relationship(back_populates="documents")
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "prospect_id", "source_url", name="documents_prospect_url_unique"
        ),
        # Redundant on its own -- id is already the primary key. It exists so
        # chunks can reference (document_id, prospect_id) as a composite
        # foreign key. This is the structural enforcement of A1. Do not drop.
        UniqueConstraint("id", "prospect_id", name="documents_id_prospect_unique"),
        CheckConstraint("word_count >= 0", name="documents_word_count_check"),
        Index("documents_prospect_stable_hash_idx", "prospect_id", "stable_hash"),
    )


class Chunk(Base):
    """One embeddable span of a document.

    Every query against this table lives in linestack/retrieval/scope.py and
    nowhere else. That is not a style preference: it is one of the two
    mechanisms enforcing A1, the other being the composite foreign key below.
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Denormalised from documents. The prospect filter is on the path of every
    # query and is how A1 is enforced; it must not require a join (ADR-0004).
    prospect_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(DocumentKind, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Nullable BY DESIGN: a chunk is written before it has a vector, so the
    # embedding pass is separate and resumable (WHERE embedding IS NULL).
    embedding: Mapped[list[float] | None] = mapped_column(
        HALFVEC(settings.embedding_dimensions)
    )
    # Never stored without its model name -- vectors from two models are not
    # comparable, and mixing them silently degrades retrieval in a way that
    # looks like a chunking problem. The database enforces the pairing; the
    # EmbeddingBatch type makes the unpaired case unrepresentable in Python.
    embedding_model: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        # A1, structurally. A chunk cannot claim one prospect while pointing at
        # another prospect's document: the pair must exist in documents.
        # Verified 2026-09-02 against a live database.
        ForeignKeyConstraint(
            ["document_id", "prospect_id"],
            ["documents.id", "documents.prospect_id"],
            ondelete="CASCADE",
            name="chunks_document_prospect_fk",
        ),
        UniqueConstraint(
            "document_id", "chunk_index", name="chunks_document_index_unique"
        ),
        CheckConstraint("token_count > 0", name="chunks_token_count_check"),
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="chunks_embedding_model_paired",
        ),
        Index("chunks_prospect_id_idx", "prospect_id"),
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prospects.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    robots_reason: Mapped[str] = mapped_column(RobotsOutcome, nullable=False)
    outcome: Mapped[str] = mapped_column(CrawlOutcome, nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    documents_stored: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    user_agent: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    page_outcomes: Mapped[list[CrawlPageOutcome]] = relationship(
        back_populates="crawl_run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("max_pages > 0", name="crawl_runs_max_pages_check"),
        CheckConstraint("pages_fetched >= 0", name="crawl_runs_pages_fetched_check"),
        CheckConstraint(
            "documents_stored >= 0", name="crawl_runs_documents_stored_check"
        ),
        Index("crawl_runs_prospect_started_idx", "prospect_id", "started_at"),
    )


class CrawlPageOutcome(Base):
    """Why one URL did or did not become a document (A5).

    Without these, "0 documents" is one number meaning several different
    things, and the evaluation set inherits ingestion bugs as facts about the
    company (docs/evaluation.md section 2.5).
    """

    __tablename__ = "crawl_page_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crawl_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(PageOutcome, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    crawl_run: Mapped[CrawlRun] = relationship(back_populates="page_outcomes")

    __table_args__ = (
        UniqueConstraint(
            "crawl_run_id", "url", name="crawl_page_outcomes_run_url_unique"
        ),
        Index("crawl_page_outcomes_run_outcome_idx", "crawl_run_id", "outcome"),
    )


__all__ = [
    "Base",
    "Chunk",
    "CrawlPageOutcome",
    "CrawlRun",
    "Document",
    "Prospect",
]
