"""Unit tests for `linestack.config`.

No database, no network, no API key. These must pass on a machine that has
never seen a `.env` file, because CI is such a machine.
"""

import inspect

import pytest

import ingest
from linestack.config import Settings, settings


def test_settings_construct_without_any_environment(monkeypatch) -> None:
    """A missing key must not take the whole suite down at import time.

    Every secret is optional and defaults to None. If OPENAI_API_KEY were
    required, `Settings()` would raise while `linestack.config` was being
    imported, and every unit test in the project would fail on a machine
    without a key -- reporting a configuration problem as 62 test failures.
    """
    for name in ("OPENAI_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    s = Settings(_env_file=None)

    assert s.openai_api_key is None
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_dimensions == 1536


def test_a_missing_openai_key_fails_where_it_is_used_not_at_import() -> None:
    s = Settings(_env_file=None, openai_api_key=None)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        s.require_openai_key()


def test_a_secret_does_not_appear_in_the_settings_repr() -> None:
    """config.py's docstring: nothing secret enough to leak through a repr.

    A settings object reaches logs and tracebacks; SecretStr is what keeps a
    key out of both.
    """
    s = Settings(_env_file=None, openai_api_key="sk-not-a-real-key-000")

    assert "sk-not-a-real-key-000" not in repr(s)
    assert s.require_openai_key() == "sk-not-a-real-key-000"


def test_the_chunk_band_is_derived_from_the_target_not_configured_twice() -> None:
    """ADR-0005 is 800-1200 tokens. .env.example carries only the target.

    Deriving the band means the two cannot drift apart: someone who changes
    CHUNK_TARGET_TOKENS to 2000 and forgets a separate min/max would otherwise
    get a band that no longer brackets the target.
    """
    s = Settings(_env_file=None, chunk_target_tokens=1000)
    assert (s.chunk_min_tokens, s.chunk_max_tokens) == (800, 1200)

    wider = Settings(_env_file=None, chunk_target_tokens=2000)
    assert wider.chunk_min_tokens < 2000 < wider.chunk_max_tokens


def test_the_hard_max_is_below_the_embedding_input_limit() -> None:
    """text-embedding-3-small accepts 8191 tokens. A block above the hard max
    is force-split; a block above 8191 would be rejected by the API instead.
    """
    assert settings.chunk_hard_max_tokens < 8191
    assert settings.chunk_hard_max_tokens > settings.chunk_max_tokens


@pytest.mark.parametrize(
    ("setting_name", "ingest_name"),
    [
        ("crawl_max_pages", "MAX_PAGES"),
        ("crawl_user_agent", "USER_AGENT"),
        ("crawl_delay_seconds", "DELAY_SECONDS"),
        ("crawl_timeout_seconds", "TIMEOUT"),
    ],
)
def test_config_agrees_with_the_constants_still_living_in_ingest(
    setting_name: str, ingest_name: str
) -> None:
    """These values exist in two places until ingest.py moves into the package.

    `crawl_runs.max_pages` and `crawl_runs.user_agent` cannot be read from the
    crawl artifact -- it does not carry them -- so the loader takes them from
    config. If the two ever disagree, every crawl_run row is labelled with a
    value the crawl did not actually use. This test makes that divergence a
    test failure rather than a silent mislabelling. Delete it when ingest.py
    moves and reads config directly (ADR-0010).
    """
    assert getattr(settings, setting_name) == getattr(ingest, ingest_name)


def test_nothing_outside_config_reads_the_environment_directly() -> None:
    """config.py's docstring: settings are "never read from os.environ
    anywhere else". A second reader is a second source of truth."""
    import pathlib

    package = pathlib.Path(__file__).resolve().parent.parent / "linestack"
    offenders = []
    for path in package.rglob("*.py"):
        if path.name == "config.py":
            continue
        text = path.read_text()
        if "os.environ" in text or "os.getenv" in text:
            offenders.append(path.name)

    assert offenders == [], (
        f"{offenders} read the environment directly. Add the value to "
        f"linestack/config.py instead; one reader, one source of truth."
    )


def test_settings_is_instantiated_once_at_module_level() -> None:
    """Importers get the same object, so an override in one place is visible
    everywhere rather than silently local."""
    from linestack import config

    assert isinstance(config.settings, Settings)
    assert inspect.getmodule(type(config.settings)) is config
