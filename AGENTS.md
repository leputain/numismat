# AGENTS.md

> Structural map and non-negotiable engineering rules for agents working on Numismat. Update it when architecture, entry points, or quality gates materially change.

## Project Overview

Numismat is a private, self-hosted Telegram bot for a bounded operator-managed allowlist of independent income and expense ledgers. The Python package and Docker service retain the internal name `finbot`; the system keeps financial processing local and requires explicit review before every transaction is saved. It does not provide a shared household ledger, cross-user transfers, RBAC, or self-registration.

## Technology Stack

- **Runtime:** CPython 3.14 with uv and a frozen `uv.lock`
- **Telegram:** aiogram 3
- **HTTP:** FastAPI with minimal Uvicorn; HTTPX2 is test-only
- **MCP:** official Python MCP SDK v2 over local stdio only; first milestone is read-only
- **Database:** PostgreSQL 18 with SQLAlchemy asyncio and psycopg 3
- **Migrations:** Alembic
- **Configuration:** Pydantic 2 and pydantic-settings
- **OCR:** Pillow and local Tesseract 5 (`rus+eng`)
- **Quality:** Ruff, mypy strict, pytest, Hypothesis, pip-audit
- **Operations:** hardened Docker/Compose and encrypted Restic backup/restore drills

## Project Structure

```text
src/finbot/
├── domain/                 # Framework-independent money and business invariants
├── application/            # DTOs, ports, queries, policies, services, and use cases
├── adapters/
│   ├── ai/                 # Disabled-by-default local Ollama suggestion adapter
│   ├── database/           # PostgreSQL queries, repositories, services, and provisioning
│   ├── http/               # Versioned API, errors, health, logging, and ASGI server
│   ├── mcp/                # Local owner-fixed read-only stdio query adapter
│   ├── ocr/                # Untrusted-image validation and Tesseract adapter
│   └── telegram/           # Routers, controllers, executor, delivery, polling, and UI
├── observability/          # Allowlisted privacy-safe JSON logging
├── bootstrap.py            # Composition root, dependency wiring, and router registration
├── config.py               # Validated environment/Docker secret settings
├── recurring_runner.py     # Focused DB-only recurring materialize/stage process
└── healthcheck.py          # Bounded migration/database health probe
migrations/                 # Alembic revisions
tests/unit/                 # Isolated domain/application/adapter tests
tests/integration/          # Disposable PostgreSQL and Telegram/OCR E2E tests
deploy/postgres/            # Initial PostgreSQL privilege boundary
deploy/web/                 # TLS edge config, privacy-safe logs, and secret preflight
scripts/                    # Integration, backup, and restore automation
ops/systemd/                # Backup/check/restore timer units
web/                        # React/Vite Mini App and generated OpenAPI types
```

## Key Entry Points

| File | Purpose |
|---|---|
| `src/finbot/__main__.py` | Loads validated settings, configures safe logging, and starts the bot. |
| `src/finbot/bootstrap.py` | Wires aiogram, application use cases, adapters, delivery, polling, and focused routers. |
| `src/finbot/adapters/http/server.py` | Starts the separately deployed ASGI API with access logging and proxy trust disabled. |
| `src/finbot/adapters/http/app.py` | Composes FastAPI, `/api/v1`, structured errors, OpenAPI, and injected readiness. |
| `src/finbot/adapters/mcp/__main__.py` | Starts the optional local read-only MCP stdio server with a dedicated DB role. |
| `src/finbot/recurring_runner.py` | Runs bounded two-phase recurring ticks without Telegram/API secrets. |
| `src/finbot/config.py` | Loads secrets/environment values with bounded validation and hidden sensitive fields. |
| `src/finbot/healthcheck.py` | Verifies database reachability and the expected Alembic head without leaking connection details. |
| `migrations/env.py` | Alembic runtime entry point. |
| `Makefile` | Canonical setup, quality, integration, image, and operations commands. |
| `compose.yaml` | Local development services. |
| `compose.production.yaml` | Hardened production topology and least-privilege secret boundaries. |
| `web/` | Exact-pinned React/Vite Mini App, generated OpenAPI types, and frontend checks. |
| `Dockerfile.web` | Pinned multi-stage immutable Mini App/TLS edge image. |

## Documentation

| Document | Purpose |
|---|---|
| `README.md` | Product landing page, quick start, architecture, and documentation index. |
| `SPEC.md` | Product behavior and UX contract. |
| `SECURITY.md` | Threat model, privacy, production secrets, recovery, and incident response. |
| `DECISIONS.md` | Architecture and reliability decisions. |
| `PLAN.md` | Evidence-based current status and remaining engineering work. |
| `CONTRIBUTING.md` | Contribution workflow and change requirements. |
| `CHANGELOG.md` | Public release history. |

## AI Context Files

| File | Purpose |
|---|---|
| `AGENTS.md` | Repository map and mandatory engineering constraints. |
| `.ai-factory/DESCRIPTION.md` | AI Factory product, stack, security, and workflow context. |
| `.ai-factory/ARCHITECTURE.md` | Dependency rules, component boundaries, and migration direction. |
| `.ai-factory/config.yaml` | Project-local AI Factory paths, language, Git, and verification policy. |
| `.ai-factory/rules/base.md` | Detected conventions and reusable project quality rules. |

## Agent Rules

- Run `uv sync --frozen`, `make check`, and `docker compose config --quiet` before handoff when available.
- Keep domain and application layers independent of Telegram, SQLAlchemy, and adapters. Handlers stay thin.
- Never use `float` for money; use integer minor units and `Decimal` only at input boundaries.
- Never commit secrets, real Telegram data, runtime logs, database dumps, private material, or production exports.
- Database changes require Alembic migrations with test coverage.
- Tenant-owned foreign keys must include the owner dimension where PostgreSQL can enforce it. Preserve migration `0012_multitenant_integrity` fail-closed legacy scan, private user/chat constraint, and composite ownership references. PostgreSQL RLS remains deferred until pre-auth, cleanup, recurring, outbox, and MCP database-role/context contracts are designed together.
- HTTP session, CSRF, and idempotency persistence accepts only typed 32-byte keyed digests; raw values and request bodies never cross into database models or logs. Repositories never own commit or rollback.
- Treat Telegram Mini App `initData` as an untrusted bounded wire payload: verify the official HMAC before using identity fields, require current bounded-allowlist membership and bounded `auth_date`, and key replay denial from the verified Telegram hash rather than raw query encoding. Every Mini App launch must present its current signed `initData` before any cookie is trusted.
- Production HTTP auth requires one canonical HTTPS `MINIAPP_PUBLIC_URL`, an independent 32-byte `HTTP_SECURITY_KEY`, host-only Secure cookies, and double-submit CSRF. Signed login, logout, and protected mutations treat a single bounded ASCII Origin as optional transport metadata because native WebViews do not provide one portable Origin contract; duplicate, empty, oversized, and non-ASCII values fail closed, while the Origin value itself is never authority. Verified Telegram HMAC, allowlisted subject, TTL, and replay denial authorize login; an active same-subject session may be retained, while a stale cross-subject cookie may be replaced only after a fresh proof. The host-only session, matching page binding, and double-submit CSRF authorize logout and protected writes, with idempotency required for mutations. Re-check session subject allowlist membership on every protected operation. Never add an auth bypass or move session validation into a transaction separate from the protected mutation.
- Treat the WebView cookie jar as shared transport state, not sufficient page/tenant authority. Every successful auth response returns a privacy-safe domain-separated `X-Session-Binding` HMAC of the raw HttpOnly session token; keep it only in page memory and require it on every protected GET, write, and logout before owner lookup. Mutations still require double-submit CSRF. Only a successful login that creates a new session may emit `Set-Cookie`; same-subject reuse does not rotate it, and failed auth, every protected error, and successful logout must never delete ambient cookies. Logout revokes server-side and returns `204` without `Set-Cookie`. Never log or persist the binding. On `hidden` and every `pagehide`, synchronously clear protected UI/query state and invalidate stale auth results; resume an already authenticated page only through `/auth/me` with its existing memory binding, never by replaying stale `initData`. An in-flight-auth suspend clears the binding and requires reopen.
- HTTP finance routes reuse application query use cases, serialize every minor-unit value as a decimal string, bound every collection, and keep authentication plus multi-query reads in one read-only repeatable transaction. Never log cursor or result contents.
- MCP remains a local stdio read adapter: no network listener, write tools, owner/tool credentials, arbitrary SQL, or argument/result logging. Keep `OWNER_TELEGRAM_USER_ID` process-fixed as the primary MCP/rollback principal and production access on the dedicated SELECT-only role; it must not grant cross-tenant HTTP/Telegram access.
- Treat transaction cursors as signed API-opaque tokens: do not parse them in clients, do not claim encryption, and restart live keyset pagination after any mutation.
- HTTP mutations must use the closed typed revision facade and one transaction for session lock, owner lock, idempotency claim, domain write, completion, and commit. Never expose raw draft payload, add a direct transaction-create bypass, or reconstruct an idempotent replay by rereading mutable domain state.
- HTTP catalog reads are complete-but-bounded: fetch `cap + 1`, return at most 200, and fail closed on overflow. Every account/category writer, including cross-channel draft input, must preserve the destination cap under the owner lock; never silently truncate a catalog.
- FastAPI `app.openapi()` is the single backend contract source. Offline export must not load runtime settings, secrets, database state, or network resources, and every emitted `$ref` must resolve locally.
- Learned category rules are bounded per owner/kind. Fetch `cap + 1` and fail closed on overflow; never silently truncate candidates or change deterministic scope/phrase/recency precedence. New-rule capacity checks require the owner row lock, while an existing normalized rule remains updatable at the cap.
- Exchange-rate versions are append-only and owner-scoped. Store exact integer coefficient/scale, never `float`; converted reports must pin one explicit immutable version, preserve original transactions, and never infer inverse or triangulated rates.
- Bank imports are staged, owner/account-scoped, and review-first. Keep raw CSV and bank references in memory only, persist only normalized fields plus independent keyed 32-byte digests, preserve import provenance atomically, and never auto-link or auto-post a transaction.
- Dependency security gates audit the hashed runtime graph exported from frozen `uv.lock`, not whichever packages happen to be installed in the developer environment.
- Frontend API types are generated only from the canonical offline FastAPI OpenAPI document. Never hand-maintain parallel finance/draft DTOs; `web` checks must fail on contract drift, and production builds must not emit source maps or telemetry payloads.
- Recurring schedules are review-first draft generators. Keep owner timezone immutable, due uniqueness and transaction provenance in PostgreSQL; runner phases use transaction advisory locks, bounded owner/schedule batches, fair at-most-one due schedule per owner per materialization tick, and owner-first locking. An active draft leaves the instance pending with backoff—never auto-post, suspend, replace, or stage a hidden intent. The DB-only runner does not read the Telegram allowlist, so revocation must pause that tenant's schedules or stop the runner.
- Production Mini App serving has one canonical HTTPS host and same-origin `/api`; keep API off the public network, keep the edge non-root/read-only/cap-drop, never log URI/query/client identity, and do not rely on ignored Compose `uid/gid/mode` for file-backed TLS secrets. Keep the global Main Mini App disabled and install per-user menu buttons only after committed onboarding.
- Every feature needs unit tests; PostgreSQL integration tests must use a database name ending in `_test`.
- Logs are allowlisted JSON and must not contain tokens, credentials, Telegram IDs, amounts, currencies, account/category names, descriptions, message text, OCR text, SQL, or database URLs.
- Preserve the full bounded `TELEGRAM_ALLOWED_USER_IDS` policy (unset means singleton primary), exact numeric actor plus private `actor_id == chat_id` authorization, immutable chat binding, per-tenant data ownership, idempotent updates, optimistic interaction versions, and durable response-outbox semantics. Before Telegram network I/O, validate that any referenced outbox draft belongs to the recorded owner and immutable private chat; fail closed without `sent_at`, and repeat the same guard when binding a presentation. Never log configured IDs or allowlist contents.
- Keep external AI providers outside the financial-data path. Optional local Ollama is `/ai`-only, bounded,
  disabled by default, and may create only a reviewed shared draft; deterministic parsing/OCR remain the default.
  Prompts, outputs, raw images/text, model names, and provider URLs remain in memory only and never enter logs.
- Decompose shell operations into reviewable commands; do not hide checkout/pull/build/deploy transitions in opaque command chains.

## Definition of Done

Frozen dependencies, compatible migrations, validated Docker Compose files, allowlist-only private actor/chat access with isolated ledgers, idempotent updates, safe reports and CSV export, tested backup/restore documentation, passing available checks, and an accurate `PLAN.md`.
