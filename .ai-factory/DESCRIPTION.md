# Numismat Project Description

## Overview

Numismat is a private, self-hosted Telegram bot for an explicit bounded allowlist of up to 32 people, each with an independent income-and-expense ledger. The internal Python package and Docker service are named `finbot`. The bot provides deterministic text input, persistent review drafts, reports, CSV export, local receipt and bank-list OCR, an optional explicit local Ollama suggestion path, recovery workflows, and encrypted backup/restore operations without sending financial data to external AI services. It does not provide a shared household ledger, cross-user transfers, RBAC, or self-registration.

The package version remains `0.45.0`; the M0/M1 platform foundation is unreleased. This document describes the checked-out source tree, not a live deployment.

## Product Scope

- Accept commands only from a configured numeric allowlist in each actor's exact private Telegram chat; keep every allowed user's accounts, categories, drafts, transactions, budgets, schedules, rates, imports, sessions, and idempotency records isolated.
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
- **Frontend:** React 19.2.8, React Router 8.3.0, Vite 8.2.1, TypeScript 5.9.3, Tailwind CSS 4.3.3, TanStack Query 5.101.4, and Recharts 3.10.1
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
  provenance. Its DB-only runner uses two advisory-locked phases, selects at most one due schedule per owner during
  materialization, and never creates a transaction directly.
- Alembic `0012_multitenant_integrity` fail-closed checks legacy rows and enforces private user/chat binding plus
  composite owner references for default accounts, category parents, audit transactions, recurring/import drafts,
  and Telegram outbox delivery.
- Long polling runs as exactly one instance, processes updates sequentially, and advances the offset only after successful handling.
- Business mutations and typed Telegram response outbox records commit atomically. External Telegram delivery remains honestly at-least-once in the narrow crash window after Telegram accepts a request but before `sent_at` is persisted.
- Alembic `0005_channel_neutral_drafts` stores Telegram chat/message binding, rendered revision, and history/pending-history navigation context outside the business draft. Compact callbacks carry draft UUID/revision; the adapter validates the exact projected presentation under lock.
- Alembic `0006_csv_export_outbox_job` persists only a bounded export job marker. CSV bytes, filename, and row count are generated in memory from current owner-scoped data at delivery time, limited to 10,000 rows and 16 MiB, and never stored in the outbox. Delivery is at-least-once and is not a request-time snapshot. Downgrade is rejected while any CSV export job rows exist.
- HTTP mutations use one transaction and the lock order `shared session -> owner -> idempotency -> domain -> completion`. Successful replay returns only the stored minimal status/result reference without rereading mutable domain state; failed mutations roll back their claim.
- Alembic migrations are mandatory for schema changes. Integration and restore tests may connect only to databases whose names end in `_test`.

## Security and Privacy Requirements

- Treat `TELEGRAM_ALLOWED_USER_IDS` as the full canonical allowlist of 1..32 unique numeric IDs; when absent, permit only `OWNER_TELEGRAM_USER_ID`. Require `chat.type == private` and `actor_id == chat_id`; usernames are never access-control identities.
- Keep `OWNER_TELEGRAM_USER_ID` as the required primary/MCP/rollback principal, not a cross-tenant administrator. Removing another ID takes effect for bot/API after restart and must reject existing HTTP sessions, but retains that tenant's data; pause recurring schedules separately because the DB-only runner does not read the Telegram allowlist.
- Require current signed Telegram `initData` on every Mini App launch before trusting a cookie. Allow same-subject active-session reuse, but replace a stale cross-subject cookie only after successful fresh-proof verification and clear protected frontend query state before rebinding.
- Bind every authenticated page to its exact cookie session with a privacy-safe domain-separated `X-Session-Binding`
  HMAC returned by auth and held only in page memory. Require it on every protected GET/write/logout before tenant
  lookup; keep double-submit CSRF for mutations. Only a successful login that creates a session may emit `Set-Cookie`;
  same-subject reuse does not rotate cookies, and failed auth, every protected error, and successful logout never
  delete ambient cookies. Logout revokes server-side and returns `204` without `Set-Cookie`. A stale page/cookie
  mismatch returns `401` without clearing the newer cookie. `hidden` and every `pagehide` synchronously clear protected
  UI/query state; resume an already authenticated page only through `/auth/me` with its in-memory binding, never by
  replaying stale `initData`. Suspending an in-flight authentication clears the binding and requires reopen.
- Before Telegram network I/O, require every outbox response that references a draft to match the recorded owner and
  immutable private chat. Fail closed without `sent_at` on mismatch and repeat the same owner/chat guard when binding
  the delivered Telegram presentation.
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

The unreleased M0/M1 baseline is implemented: shared use cases are tested through deterministic fakes, PostgreSQL repositories return application DTOs, drafts are channel-neutral, Telegram presentation state is revision-safe, and all current Telegram handler families route through focused controllers/routers and the atomic mutation/outbox envelope. Its historical handoff gate included unit/integration tests, dependency audit, Compose validation, production image build, and a synthetic encrypted backup/restore drill. The current multi-user diff has not yet completed the consolidated handoff gate or production rollout.

M2-M4 are implemented through the current Task27 scope. The checkout now adds a bounded allowlist and tenant-scoped
FastAPI/Mini App surface, immutable private Telegram principals, per-user menus, signed-initData-first bootstrap,
page-scoped session binding with shared-cookie-safe error/logout and lifecycle handling, bounded WebView-Origin
metadata plus CSRF/session/idempotency controls, bounded finance/catalog/budget reads and revision-safe writes,
recurring schedules, explicit exchange rates, staged review-first bank CSV imports, and multi-tenant integrity through
Alembic `0012`.
The canonical OpenAPI contains 63 path items. Task21 completed the consolidated unit, PostgreSQL/cross-channel,
migration, dependency, production-image, edge-security and encrypted recovery gates for the previous `0011`
baseline; those results do not certify the new multi-user diff.
