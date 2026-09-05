.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV     := .venv
PY       := $(VENV)/bin/python
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff
COMPOSE  := docker compose

# Read from .env when it exists so that targets and the application agree on
# where the database is.
ifneq (,$(wildcard .env))
include .env
export
endif

# One port, read from .env, used by BOTH the container and the sync URL.
#
# These used to be three independent 5432s -- here, in docker-compose.yml, and
# in .env's DATABASE_URL -- and they drifted. **[verified] 2026-09-05** on a
# machine where another project's Postgres already held 5432: `make up` failed
# to bind, and `make test-integration` connected to THAT database and reported
# `InvalidPasswordError`, which reads as a credentials problem rather than as
# "this is not your database". The .env had already been moved to 55432; the
# other two defaults had not, and nothing tied them together.
#
# docker compose substitutes POSTGRES_PORT from .env by itself, so setting it
# in one place now moves the container and this URL together.
POSTGRES_PORT ?= 5432
DATABASE_URL_SYNC ?= postgresql://linestack:linestack@localhost:$(POSTGRES_PORT)/linestack

.PHONY: help
help:  ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
.PHONY: install
install:  ## Create .venv and install pinned dependencies (dev included)
# --allow-existing so a second `make install` updates the environment instead of
# failing with "a virtual environment already exists". Verified 2026-09-02: the
# first form made the target usable exactly once per machine, which is the same
# defect `migrate` had.
	uv venv --python 3.13 --allow-existing $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

.PHONY: install-eval
install-eval:  ## [broken] The evaluation extra does not resolve. See ADR-0020.
	@echo "The [eval] extra does not install: ragas==0.4.3 conflicts with"
	@echo "openai==3.7.0 through instructor. Verified 2026-09-05."
	@echo "Not repaired, because both ragas metrics are LLM-judged and there"
	@echo "is no key by design (ADR-0017) -- they could not run either way."
	@echo "The four metrics that need no judge are in linestack/evaluation/"
	@echo "metrics.py and run with 'make test'. See ADR-0020."
	@echo
	@echo "To see the conflict for yourself:"
	@echo "  uv pip install --python $(PY) -e \".[dev,eval]\""
	@exit 1

# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------
.PHONY: up
up:  ## Start Postgres 17 + pgvector
	$(COMPOSE) up -d postgres
	@$(COMPOSE) exec -T postgres bash -c 'until pg_isready -q; do sleep 1; done'

.PHONY: down
down:  ## Stop all services, keep volumes
	$(COMPOSE) --profile observability down

.PHONY: reset
reset:  ## Destroy the application database and re-apply migrations
	$(COMPOSE) down -v postgres
	$(MAKE) up
	$(MAKE) migrate

.PHONY: observability-up
observability-up:  ## Start the Langfuse stack (six containers; several GB of RAM)
	$(COMPOSE) --profile observability up -d

.PHONY: psql
psql:  ## Open a psql shell on the application database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-linestack} -d $${POSTGRES_DB:-linestack}

# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
.PHONY: migrate
migrate:  ## Apply migrations/*.sql not yet recorded in schema_migrations
# Every file used to be re-run on every invocation. 0001 creates types without
# IF NOT EXISTS, so the second `make migrate` on an existing database died with
# `ERROR: type "document_kind" already exists` and never reached 0002. Verified
# 2026-09-02. schema_migrations already recorded what had been applied; nothing
# consulted it. Now it is consulted. The query is tolerated failing, because on
# a fresh database the table does not exist yet.
	@set -e; for f in migrations/*.sql; do \
	  v=$$(basename "$$f" .sql); \
	  applied=$$($(COMPOSE) exec -T postgres psql -tAq \
	    -U $${POSTGRES_USER:-linestack} -d $${POSTGRES_DB:-linestack} \
	    -c "SELECT 1 FROM schema_migrations WHERE version = '$$v'" \
	    2>/dev/null || true); \
	  if [ "$$applied" = "1" ]; then echo "skipping $$v (already applied)"; continue; fi; \
	  echo "applying $$v"; \
	  $(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 \
	    -U $${POSTGRES_USER:-linestack} -d $${POSTGRES_DB:-linestack} < "$$f"; \
	done

.PHONY: migration-status
migration-status:  ## Show which migrations have been applied
	@$(COMPOSE) exec -T postgres psql -U $${POSTGRES_USER:-linestack} \
	  -d $${POSTGRES_DB:-linestack} \
	  -c "SELECT version, applied_at FROM schema_migrations ORDER BY version"

# --------------------------------------------------------------------------
# code
# --------------------------------------------------------------------------
.PHONY: lint
lint:  ## Lint and check formatting
	$(RUFF) check .
	$(RUFF) format --check .

.PHONY: format
format:  ## Apply formatting and fixable lints
	$(RUFF) check --fix .
	$(RUFF) format .

.PHONY: test
test:  ## Unit tests (no database, no API calls)
	$(PYTEST) -m "not integration and not evaluation"

.PHONY: test-integration
test-integration:  ## Tests that need a running, migrated database
	$(PYTEST) -m integration

# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------
.PHONY: crawl
crawl:  ## Crawl one prospect: make crawl DOMAIN=fly.io
	@test -n "$(DOMAIN)" || { echo "usage: make crawl DOMAIN=example.com"; exit 2; }
	$(PY) ingest.py $(DOMAIN)

.PHONY: load
load:  ## Load crawl artifacts into Postgres: make load ARTIFACTS="a.json b.json"
# Separate from `crawl` on purpose (ADR-0008): a crawl is slow and impolite to
# repeat, so an artifact can be re-loaded and diffed without re-fetching.
	@test -n "$(ARTIFACTS)" || { echo "usage: make load ARTIFACTS=\"prospect_fly_io.json\""; exit 2; }
	$(PY) -m linestack.ingestion.loader $(ARTIFACTS)

# --------------------------------------------------------------------------
# evaluation
#
# Runs the four metrics that need no LLM judge: recall@k, signal accuracy,
# ingestion coverage, and the cross-prospect leakage gate. Faithfulness and
# answer correctness are not computed -- ADR-0020 records why, and it is not
# only the broken [eval] extra.
#
# Needs a loaded, embedded corpus: make up && make migrate && make load ... &&
# make embed PROSPECT=...
# --------------------------------------------------------------------------
.PHONY: eval
eval:  ## Run the ground-truth set. JSON=path also writes the full run record.
	$(PY) -m linestack.evaluation.harness --dir $(or $(DIR),eval/ground_truth) \
	  $(if $(JSON),--json $(JSON),)

.PHONY: eval-report
eval-report:  ## [not implemented] Delta table against the previous run
# The run record `make eval JSON=...` writes is the input this needs, and two
# of them do not exist yet. Building a delta table before there are two runs to
# diff is infrastructure ahead of measurement (A9).
	@echo "Not implemented. Design: docs/evaluation.md section 4."
	@echo "Produce run records first:  make eval JSON=eval/runs/\$$(date +%F).json"
	@exit 1

.PHONY: show
show:  ## Read the frozen corpus: make show ARTIFACT=prospect_x.json [URL=/about]
# docs/ground-truth.md section 2 step 2 says to write every reference answer
# from the CRAWLED text rather than from the live site, and until now that step
# had no command behind it. A rule that is inconvenient to follow is a rule
# that gets followed loosely.
	@test -n "$(ARTIFACT)" || { echo "usage: make show ARTIFACT=prospect_thoughtbot_com.json [URL=/about]"; exit 2; }
	$(PY) -m linestack.evaluation.corpus $(ARTIFACT) $(if $(URL),--url $(URL),)

.PHONY: ground-truth-validate
ground-truth-validate:  ## Structurally validate the ground-truth set
# Structural only: required fields, the four question ids, resolvable corpus
# artifacts, source URLs on the prospect's own domain. No model calls, no cost,
# so CI runs it on every push. It cannot tell whether a reference answer is
# RIGHT -- that is docs/ground-truth.md sections 2 and 4, guarded by discipline.
	$(PY) -m linestack.evaluation.dataset --validate $(or $(DIR),eval/ground_truth)

.PHONY: ground-truth-new
ground-truth-new:  ## Scaffold a ground-truth file: make ground-truth-new DOMAIN=fly.io
# Fills in only what is mechanical: the prospect block and the candidate source
# URLs from the frozen artifact. It does NOT fill in the signals (hand-check
# those against the live site -- the crawler's numbers are what they test) and
# it does NOT write reference answers (a set written by a model measures
# agreement with that model). Every TODO it leaves is rejected by validate.
	@test -n "$(DOMAIN)" || { echo "usage: make ground-truth-new DOMAIN=fly.io"; exit 2; }
	$(PY) -m linestack.evaluation.dataset \
	  --scaffold prospect_$(subst .,_,$(DOMAIN)).json

.PHONY: embed
embed:  ## Embed pending chunks: make embed PROSPECT=fly.io [DRY=1]
# DRY=1 reports the pending chunk count and token total and makes no API call.
# This is the first target that spends money; it can always say how much first.
	@test -n "$(PROSPECT)" || { echo "usage: make embed PROSPECT=fly.io [DRY=1]"; exit 2; }
	$(PY) -m linestack.retrieval.embedding --prospect $(PROSPECT) $(if $(DRY),--dry-run,)

.PHONY: ask
ask:  ## Retrieve for one question: make ask PROSPECT=fly.io Q="..."
# Shows retrieved chunks and their scores. There is no generation yet, and that
# is deliberate: A8 says the first hypothesis for a wrong answer is that the
# right chunk was never retrieved, so this is where that gets checked by eye.
	@test -n "$(PROSPECT)" || { echo 'usage: make ask PROSPECT=fly.io Q="your question"'; exit 2; }
	@test -n "$(Q)" || { echo 'usage: make ask PROSPECT=fly.io Q="your question"'; exit 2; }
	$(PY) -m linestack.retrieval.ask --prospect $(PROSPECT) --question "$(Q)" $(if $(K),-k $(K),)
