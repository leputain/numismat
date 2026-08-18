# Numismat Project Description

## Overview

Numismat is a private, self-hosted Telegram bot for a single owner to track income and expenses. The internal Python package and Docker service are named `finbot`. The bot provides deterministic text input, persistent review drafts, reports, CSV export, local receipt and bank-list OCR, an optional explicit local Ollama suggestion path, recovery workflows, and encrypted backup/restore operations without sending financial data to external AI services.

The package version remains `0.45.0`; the M0/M1 platform foundation is unreleased. This document describes the checked-out source tree, not a live deployment.

## Product Scope

- Accept owner-only commands and data exclusively in a private Telegram chat.
- Parse expenses and income into positive integer minor units; never use `float` for money.
- Require an explicit review-and-save action before creating a transaction.
- Generate due recurring operations only as review drafts from bounded daily/weekly/monthly schedules.
- Manage accounts, categories, deterministic categorization rules, history, editing, soft deletion, restore, and audit-based undo.
- Produce daily and monthly reports plus bounded CSV exports with spreadsheet-formula protection.
- Process JPEG, PNG, and WebP images locally through Pillow and Tesseract `rus+eng` with conservative bounded parsing.
- Preserve update idempotency and durable Telegram response delivery across process restarts.
- Support encrypted Restic backups and isolated `_test` restore drills.

## Technology Stack

- **Runtime:** CPython `>=3.14,<3.15`
- **Telegram framework:** aiogram 3.30
- **HTTP framework/server:** FastAPI 0.141.1 and minimal Uvicorn 0.52.3
- **Frontend:** React 19.2.8, React Router 8.3.0, Vite 8.2.1, TypeScript 5.9.3, Tailwind CSS 4.3.3, and TanStack Query 5.101.4
- **Configuration and validation:** Pydantic 2 and pydantic-settings
- **Database:** PostgreSQL 18
- **Persistence:** SQLAlchemy 2 asyncio with psycopg 3
- **Schema migrations:** Alembic
- **OCR:** Pillow and local Tesseract 5 (`rus+eng`)
- **Optional local AI:** Ollama HTTP adapter, disabled by default and invoked only by explicit `/ai`
- **Dependency management:** uv with a committed frozen `uv.lock`; npm with exact pins and lockfile v3 for `web/`
- **Quality gates:** Ruff, mypy strict, pytest, Hypothesis, pip-audit, TypeScript strict mode, Vitest, OpenAPI drift check, and Vite production build
- **Runtime packaging:** hardened Docker images and Docker Compose profiles for development, production, integration, and operations
- **Backup:** pg_dump plus encrypted Restic repositories and automated restore drills

## Architecture

The codebase follows a ports-and-adapters structure:

**AI Factory pattern:** Explicit Architecture (Technical Layer). Detailed dependency and migration guidance is maintained in `.ai-factory/ARCHITECTURE.md`.

- `src/finbot/domain/` owns money, transaction, date, category, and rule invariants. It is framework-independent.
- `src/finbot/application/` owns immutable DTOs, typed errors, protocols, policies, interaction codecs, queries, and executable use cases for drafts, transactions, catalogs, reports, and OCR queues. It must not import adapters, aiogram, or SQLAlchemy.
- `src/finbot/adapters/database/` implements PostgreSQL repositories, reporting, migrations support, Telegram presentation persistence, and runtime-role provisioning.
- `src/finbot/adapters/telegram/` implements authorization middleware, safe polling, focused routers, thin controllers, an atomic mutation executor, post-commit delivery/outbox integration, parsers, presenters, and UI components.
- `src/finbot/adapters/http/` owns the versioned FastAPI surface, structured HTTP errors, privacy-safe route-template logging, and bounded liveness/readiness probes.
- `src/finbot/adapters/ocr/` validates untrusted images and integrates local Tesseract.
- `src/finbot/adapters/ai/` implements the no-network disabled default and bounded local Ollama suggestion adapter.
- `src/finbot/bootstrap.py` is the composition root and router-registration boundary.
- `web/` is the exact-pinned browser adapter. Its API types are generated from the offline FastAPI OpenAPI document; production source maps and telemetry are disabled.

Architecture tests enforce dependency direction for the domain and application layers and prohibit database/composition-root imports from Telegram routers and controllers. Current Telegram flows are registered through focused modules for the main menu, finance queries/messages, settings and catalog mutations, typed text input, transaction lifecycle/editing, compact draft interactions, OCR image/queue handling, undo, and CSV export. `bootstrap.py` remains oversized because it still owns substantial construction, rendering, and dependency wiring, but it no longer owns business handler bodies, direct ORM queries, or manual commits.

## Data and Reliability Contracts

- Money is stored as positive integer minor units within PostgreSQL `BIGINT`; `Decimal` is permitted only at explicit input boundaries.
- Timestamps are timezone-aware. Report ranges use inclusive UTC start and exclusive UTC end.
- PostgreSQL is the source of truth for transactions, channel-neutral drafts, adapter-specific Telegram draft presentation, processed Telegram updates, response outbox entries, and audit events.
- Alembic `0009_recurring_transactions` stores bounded schedules, unique due instances and unique transaction
  provenance. Its DB-only runner uses two advisory-locked phases and never creates a transaction directly.
- Long polling runs as exactly one instance, processes updates sequentially, and advances the offset only after successful handling.
- Business mutations and typed Telegram response outbox records commit atomically. External Telegram delivery remains honestly at-least-once in the narrow crash window after Telegram accepts a request but before `sent_at` is persisted.
- Alembic `0005_channel_neutral_drafts` stores Telegram chat/message binding, rendered revision, and history/pending-history navigation context outside the business draft. Compact callbacks carry draft UUID/revision; the adapter validates the exact projected presentation under lock.
- Alembic `0006_csv_export_outbox_job` persists only a bounded export job marker. CSV bytes, filename, and row count are generated in memory from current owner-scoped data at delivery time, limited to 10,000 rows and 16 MiB, and never stored in the outbox. Delivery is at-least-once and is not a request-time snapshot. Downgrade is rejected while any CSV export job rows exist.
- HTTP mutations use one transaction and the lock order `shared session -> owner -> idempotency -> domain -> completion`. Successful replay returns only the stored minimal status/result reference without rereading mutable domain state; failed mutations roll back their claim.
- Alembic migrations are mandatory for schema changes. Integration and restore tests may connect only to databases whose names end in `_test`.

## Security and Privacy Requirements

- Authorize by exact numeric `OWNER_TELEGRAM_USER_ID` and `chat.type == private`; usernames are never access-control identities.
- Never commit secrets, real Telegram data, production exports, database dumps, private keys, or runtime credentials.
- Never log tokens, credentials, Telegram IDs, financial amounts, currencies, descriptions, message text, OCR text, SQL, or database URLs.
- Structured logs use bounded allowlisted event codes and metadata; free-form exception and traceback text is discarded.
- Treat uploaded images as untrusted input and enforce content type, size, pixel, dimension, timeout, and output bounds.
- Keep OCR images, raw OCR text, local-AI prompts and model outputs in memory only and never send financial content
  to external AI providers. Local AI may create only a review draft after exact-schema validation.
- Run production containers as non-root with a read-only root filesystem, dropped capabilities, `no-new-privileges`, bounded PIDs, and no Docker socket.
- Separate migration and runtime database credentials; the runtime role must have no DDL or cluster-level privileges.
- Store backups encrypted and verify recovery in an isolated `_test` database.

## Development Workflow

Primary commands are defined in `Makefile`:

- `uv sync --frozen` or `make setup` installs the exact dependency graph.
- `make format`, `make lint`, `make typecheck`, and `make test` run local quality checks.
- `make integration` runs disposable PostgreSQL, Telegram, migration, and OCR integration coverage.
- `make compose-config` validates every Compose profile.
- `make ops-test` performs a synthetic encrypted backup/restore test.
- `make check` is the full handoff gate.

Before handoff, run `uv sync --frozen`, `make check`, and `docker compose config --quiet` when the required local runtimes are available. Every feature requires unit tests, and persistence behavior that depends on PostgreSQL belongs in isolated integration tests.

## Current State and Direction

The `0.45.0` baseline includes the core finance workflow, persistent drafts, optimistic interaction versions, deterministic rule learning, local OCR, safe polling, durable Telegram responses, least-privilege PostgreSQL provisioning, hardened Compose definitions, and backup/restore operations.

The current unreleased M0/M1 foundation is implemented: shared use cases are tested through deterministic fakes, PostgreSQL repositories return application DTOs, drafts are channel-neutral, Telegram presentation state is revision-safe, and all current Telegram handler families route through focused controllers/routers and the atomic mutation/outbox envelope. The complete handoff gate passed on the checked-out tree, including unit/integration tests, dependency audit, Compose validation, production image build, and the synthetic encrypted backup/restore drill. No production deployment was performed.

M2-M4 are implemented through the current Task27 scope. The checkout contains the owner-only FastAPI/Mini App
surface, exact-Origin/CSRF/session/idempotency controls, bounded finance/catalog/budget reads and revision-safe writes,
recurring schedules, explicit exchange rates and staged review-first bank CSV imports through Alembic `0011`.
The canonical OpenAPI contains 62 path items. Task21 completed the consolidated unit, PostgreSQL/cross-channel,
migration, dependency, production-image, edge-security and encrypted recovery gates; no production deployment was
performed.
