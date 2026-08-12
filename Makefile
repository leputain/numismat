SHELL := /usr/bin/env bash

PYTHON ?= python3
UV ?= $(if $(wildcard .tool-venv/bin/uv),.tool-venv/bin/uv,uv)
COMPOSE ?= docker compose
OPS_PROJECT ?= finbot-ops
OPS_COMPOSE = $(COMPOSE) -p $(OPS_PROJECT) -f compose.ops.yaml

.PHONY: setup dev-up dev-down migrate run format lint typecheck test integration \
	compose-config ops-test check build backup backup-age restic-init restic-check \
	restore-drill healthcheck

setup:
	$(UV) sync --frozen

dev-up:
	$(COMPOSE) up -d db migrate bot

dev-down:
	$(COMPOSE) down

migrate:
	$(UV) run alembic upgrade head

run:
	PYTHONPATH=src $(UV) run python -m finbot

format:
	$(UV) run ruff format .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy src

test:
	$(UV) run pytest tests/unit

integration:
	./scripts/run-integration.sh

compose-config:
	$(COMPOSE) -f compose.yaml config --quiet
	$(COMPOSE) -f compose.production.yaml config --quiet
	$(COMPOSE) -f compose.integration.yaml config --quiet
	$(COMPOSE) -f compose.ops.yaml config --quiet
	$(COMPOSE) -f compose.ops-test.yaml config --quiet

ops-test:
	./scripts/test-ops.sh

check:
	$(UV) sync --frozen
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run mypy src
	$(UV) run pytest tests/unit
	$(MAKE) integration
	$(UV) run pip-audit
	$(MAKE) compose-config
	docker build -t finbot:check .
	$(MAKE) ops-test

build:
	docker build -t finbot:local .

backup:
	$(OPS_COMPOSE) run --build --rm --no-deps backup

backup-age:
	$(OPS_COMPOSE) run --build --rm --no-deps backup-age

restic-init:
	$(OPS_COMPOSE) run --build --rm --no-deps restic-init

restic-check:
	$(OPS_COMPOSE) run --build --rm --no-deps restic-check

restore-drill:
	./scripts/run-restore-drill.sh

healthcheck:
	PYTHONPATH=src $(UV) run python -m finbot.healthcheck
