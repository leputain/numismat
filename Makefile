SHELL := /usr/bin/env bash

PYTHON ?= python3
UV ?= $(if $(wildcard .tool-venv/bin/uv),.tool-venv/bin/uv,uv)
NPM ?= npm
COMPOSE ?= docker compose
OPS_PROJECT ?= finbot-ops
OPS_COMPOSE = $(COMPOSE) -p $(OPS_PROJECT) -f compose.ops.yaml

.PHONY: setup dev-up dev-down migrate run run-api run-mcp run-recurring-tick format lint typecheck test integration audit \
	compose-config ops-test check build backup backup-age restic-init restic-check \
	restore-drill healthcheck openapi frontend-install frontend-api-generate \
	openapi-check frontend-api-check frontend-typecheck frontend-test frontend-build \
	frontend-check frontend-audit \
	web-image web-edge-smoke web-secrets-check release-gate release-gate-ci \
	release-gate-ci-timed release-gate-timed check-timed

COMPOSE_CONFIG_HTTP_ENV = MINIAPP_PUBLIC_URL=https://numismat.invalid \
	HTTP_SECURITY_KEY=invalid-compose-config-placeholder \
	BANK_IMPORT_SECURITY_KEY=invalid-compose-config-placeholder

setup:
	$(UV) sync --frozen

dev-up:
	$(COMPOSE) up -d db migrate bot api recurring-runner

dev-down:
	$(COMPOSE) down

migrate:
	$(UV) run alembic upgrade head

run:
	PYTHONPATH=src $(UV) run python -m finbot

run-api:
	PYTHONPATH=src $(UV) run python -m finbot.adapters.http.server

run-mcp:
	PYTHONPATH=src $(UV) run python -m finbot.adapters.mcp

run-recurring-tick:
	PYTHONPATH=src $(UV) run python -m finbot.recurring_runner --once

format:
	$(UV) run ruff format .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy src

test:
	$(UV) run pytest tests/unit

frontend-install:
	cd web && $(NPM) ci

frontend-api-generate:
	cd web && $(NPM) run api:generate

frontend-api-check:
	cd web && $(NPM) run api:check

frontend-typecheck:
	cd web && $(NPM) run typecheck

frontend-test:
	cd web && $(NPM) run test

frontend-build:
	cd web && $(NPM) run build

frontend-check:
	cd web && $(NPM) run check

frontend-audit:
	cd web && $(NPM) audit --audit-level=high

integration:
	./scripts/run-integration.sh

compose-config:
	$(COMPOSE_CONFIG_HTTP_ENV) $(COMPOSE) -f compose.yaml config --quiet
	$(COMPOSE_CONFIG_HTTP_ENV) $(COMPOSE) -f compose.production.yaml config --quiet
	$(COMPOSE) -f compose.integration.yaml config --quiet
	$(COMPOSE) -f compose.ops.yaml config --quiet
	$(COMPOSE) -f compose.ops-test.yaml config --quiet

ops-test:
	./scripts/test-ops.sh

check:
	$(UV) sync --frozen
	$(MAKE) frontend-install
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	git diff --check
	$(UV) run mypy src
	$(UV) run pytest tests/unit
	$(MAKE) openapi-check
	$(MAKE) frontend-check
	$(MAKE) frontend-audit
	$(MAKE) integration
	$(MAKE) audit
	$(MAKE) compose-config
	docker build -t finbot:check .
	$(MAKE) web-edge-smoke
	$(MAKE) ops-test

check-timed:
	bash scripts/release-gate-timed.sh "uv sync --frozen" $(UV) sync --frozen
	bash scripts/release-gate-timed.sh "make frontend-install" $(MAKE) frontend-install
	bash scripts/release-gate-timed.sh "ruff format --check" $(UV) run ruff format --check .
	bash scripts/release-gate-timed.sh "ruff check" $(UV) run ruff check .
	bash scripts/release-gate-timed.sh "git diff --check" git diff --check
	bash scripts/release-gate-timed.sh "mypy" $(UV) run mypy src
	bash scripts/release-gate-timed.sh "unit tests" $(UV) run pytest tests/unit
	bash scripts/release-gate-timed.sh "openapi-check" $(MAKE) openapi-check
	bash scripts/release-gate-timed.sh "frontend-check" $(MAKE) frontend-check
	bash scripts/release-gate-timed.sh "frontend-audit" $(MAKE) frontend-audit
	bash scripts/release-gate-timed.sh "integration" $(MAKE) integration
	bash scripts/release-gate-timed.sh "audit" $(MAKE) audit
	bash scripts/release-gate-timed.sh "compose-config" $(MAKE) compose-config
	bash scripts/release-gate-timed.sh "docker build (finbot:check)" docker build -t finbot:check .
	bash scripts/release-gate-timed.sh "web-edge-smoke" $(MAKE) web-edge-smoke
	bash scripts/release-gate-timed.sh "ops-test" $(MAKE) ops-test

build:
	docker build -t finbot:local .
	docker build -f Dockerfile.web -t finbot-web:local .

web-image:
	docker build -f Dockerfile.web -t finbot-web:check .

web-edge-smoke: web-image
	WEB_EDGE_IMAGE=finbot-web:check bash ./scripts/run-web-edge-smoke.sh

web-secrets-check:
	sh ./deploy/web/preflight.sh

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

openapi:
	PYTHONPATH=src $(UV) run python scripts/export_openapi.py

openapi-check:
	@set -euo pipefail; \
	contract="$$(mktemp)"; \
	trap 'rm -f "$$contract"' EXIT; \
	PYTHONPATH=src $(UV) run python scripts/export_openapi.py --output "$$contract"; \
	cmp -s openapi/numismat-v1.json "$$contract"

release-gate:
	$(UV) sync --frozen
	$(MAKE) compose-config
	$(MAKE) openapi-check
	$(MAKE) frontend-api-check
	$(MAKE) web-secrets-check
	$(MAKE) web-edge-smoke
	$(MAKE) frontend-audit
	$(MAKE) healthcheck
	$(MAKE) audit

release-gate-ci:
	$(UV) sync --frozen
	$(MAKE) compose-config
	$(MAKE) openapi-check
	$(MAKE) frontend-api-check
	$(MAKE) frontend-audit

release-gate-ci-timed:
	bash scripts/release-gate-timed.sh "uv sync --frozen" $(UV) sync --frozen
	bash scripts/release-gate-timed.sh "compose-config" $(MAKE) compose-config
	bash scripts/release-gate-timed.sh "openapi-check" $(MAKE) openapi-check
	bash scripts/release-gate-timed.sh "frontend-api-check" $(MAKE) frontend-api-check
	bash scripts/release-gate-timed.sh "frontend-audit" $(MAKE) frontend-audit

release-gate-timed:
	bash scripts/release-gate-timed.sh "uv sync --frozen" $(UV) sync --frozen
	bash scripts/release-gate-timed.sh "compose-config" $(MAKE) compose-config
	bash scripts/release-gate-timed.sh "openapi-check" $(MAKE) openapi-check
	bash scripts/release-gate-timed.sh "frontend-api-check" $(MAKE) frontend-api-check
	bash scripts/release-gate-timed.sh "web-secrets-check" $(MAKE) web-secrets-check
	bash scripts/release-gate-timed.sh "web-edge-smoke" $(MAKE) web-edge-smoke
	bash scripts/release-gate-timed.sh "frontend-audit" $(MAKE) frontend-audit
	bash scripts/release-gate-timed.sh "healthcheck" $(MAKE) healthcheck
	bash scripts/release-gate-timed.sh "audit" $(MAKE) audit

audit:
	@set -euo pipefail; \
	requirements="$$(mktemp)"; \
	trap 'rm -f "$$requirements"' EXIT; \
	$(UV) export --frozen --no-dev --no-emit-project --no-annotate \
		--output-file "$$requirements"; \
	$(UV) run --frozen pip-audit --require-hashes --no-deps --requirement "$$requirements"
