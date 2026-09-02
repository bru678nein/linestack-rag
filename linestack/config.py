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

from __future__ import annotations

from functools import cached_property

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings. Field names match .env.example exactly."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- database ----------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://linestack:linestack@localhost:5432/linestack"
    )
    database_url_sync: str = "postgresql://linestack:linestack@localhost:5432/linestack"

    # -- embedding and generation -----------------------------------------
    # Every secret is optional and defaults to None. A required secret makes
    # Settings() raise at import, which takes the whole unit suite down on any
    # machine without a key -- including CI, which must run without one. The
    # cost is deferred failure, so the code that needs a key asks for it
    # through require_openai_key() rather than reading the field directly.
    openai_api_key: SecretStr | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    generation_model: str = "gpt-4o-mini"
    generation_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # -- retrieval ---------------------------------------------------------
    retrieval_top_k: int = Field(default=5, gt=0)

    # ADR-0005: 800-1200 tokens with roughly 150 of overlap. .env.example
    # carries the target and the overlap; the band is derived from the target
    # rather than configured separately, so the two cannot drift apart.
    chunk_target_tokens: int = Field(default=1000, gt=0)
    chunk_overlap_tokens: int = Field(default=150, ge=0)

    # Not in .env.example. A single atomic block -- one table, one list -- can
    # exceed the embedding model's 8191-token input limit, and no heading or
    # paragraph boundary exists inside it to split on. Measured: fly.io's
    # pricing table is one block of roughly 13,000 tokens. Above this a block
    # is force-split at row boundaries, and every force-split is counted.
    chunk_hard_max_tokens: int = Field(default=6000, gt=0)

    # -- ingestion ---------------------------------------------------------
    # These duplicate constants that currently live in ingest.py. The
    # duplication is deliberate and temporary (ADR-0010): ingest.py moves into
    # the package later. Until it does, a unit test asserts the two agree, so
    # a divergence fails a test rather than silently mislabelling a crawl_run.
    crawl_user_agent: str = (
        "Linestack-Research/1.0 "
        "(+https://linestack.dev; contact: brunoracconto@live.com)"
    )
    crawl_delay_seconds: float = Field(default=1.5, ge=0)
    crawl_timeout_seconds: float = Field(default=20.0, gt=0)
    crawl_max_pages: int = Field(default=40, gt=0)

    # Not in .env.example. ADR-0008: "the loader should refuse an artifact
    # older than a configured threshold", because a stale artifact loaded
    # silently is a corpus that disagrees with the site for reasons nobody
    # can see. 720 hours is 30 days, matching docs/ground-truth.md section 5's
    # assumed quarterly re-crawl with room to spare.
    artifact_max_age_hours: int = Field(default=720, gt=0)

    # -- observability -----------------------------------------------------
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    # -- evaluation --------------------------------------------------------
    eval_judge_model: str = "gpt-4o-mini"
    eval_ground_truth_dir: str = "eval/ground_truth"

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def chunk_min_tokens(self) -> int:
        """Lower edge of the ADR-0005 band: the target less 20 percent."""
        return int(self.chunk_target_tokens * 0.8)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def chunk_max_tokens(self) -> int:
        """Upper edge of the ADR-0005 band: the target plus 20 percent."""
        return int(self.chunk_target_tokens * 1.2)

    def require_openai_key(self) -> str:
        """The OpenAI key, or a readable failure at the point of use.

        Called by the code that is about to spend money, never at import.
        """
        if self.openai_api_key is None:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and fill "
                "it in; see README.md. Nothing that calls an API can run "
                "without it."
            )
        return self.openai_api_key.get_secret_value()


settings = Settings()
