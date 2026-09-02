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

DATABASE_URL_SYNC ?= postgresql://linestack:linestack@localhost:5432/linestack

.PHONY: help
help:  ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
.PHONY: install
install:  ## Create .venv and install pinned dependencies (dev included)
	uv venv --python 3.13 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

.PHONY: install-eval
install-eval:  ## Additionally install the evaluation extra (ragas)
	uv pip install --python $(PY) -e ".[dev,eval]"

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

# --------------------------------------------------------------------------
# evaluation
#
# Not implemented. These targets exist so the interface is settled before the
# harness is written; each fails loudly rather than pretending to run.
# See docs/evaluation.md.
# --------------------------------------------------------------------------
.PHONY: eval
eval:  ## [not implemented] Run the evaluation harness
	@echo "Not implemented. Design: docs/evaluation.md."
	@echo "Blocked on the ingestion defects in docs/open-questions.md section 1."
	@exit 1

.PHONY: eval-report
eval-report:  ## [not implemented] Delta table against the previous run
	@echo "Not implemented. Design: docs/evaluation.md section 4."
	@exit 1

.PHONY: ground-truth-validate
ground-truth-validate:  ## [not implemented] Structurally validate the ground-truth set
	@echo "Not implemented. Format: docs/ground-truth.md."
	@exit 1
