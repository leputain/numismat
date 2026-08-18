# Implementation Plan: Numismat Shared Finance Platform

Branch: feature/platform-foundation
Created: 2026-08-13

## Original Request

[https://github.com/leputain/numismat](https://github.com/leputain/numismat) будет развивать это приложение. скопируй все с гита

используй aifactory в проекте

The attached pasted text file(s) contain the user's request. Read and act on that content.

## Settings

- Testing: yes
- Logging: standard, strict privacy-safe allowlist only
- Docs: yes

## Scope and invariants

- Evolve the existing Telegram product incrementally; do not rewrite it or weaken its working behavior.
- Keep money in integer minor units and preserve owner isolation, optimistic concurrency, mandatory draft review, Telegram update idempotency, durable response outbox, least-privilege PostgreSQL roles, non-root/read-only containers, and Restic recovery checks.
- Keep `domain` and `application` independent of aiogram, FastAPI, Pydantic HTTP schemas, SQLAlchemy, React, MCP, and network clients.
- Reuse one application API and one PostgreSQL-backed draft lifecycle across Telegram and HTTP. Never expose Telegram presentation metadata as a domain/application draft field.
- Never log Telegram identifiers, `initData`, auth/session/CSRF tokens, idempotency keys, amounts, descriptions, OCR content, filenames, database URLs, cookies, or authorization headers.
- Complete milestones in order. Do not begin M4-M7 until M0-M3 are complete and verified.

## Commit Plan

- **Commit 1** (after tasks 1-4): `refactor(application): выделить общие контракты и read-модель`
- **Commit 2** (after tasks 5-8): `refactor(telegram): перевести сценарии на application use cases`
- **Commit 3** (after tasks 9-12): `feat(api): добавить защищённый HTTP-контур Mini App`
- **Commit 4** (after tasks 13-16): `feat(api): реализовать drafts и finance queries`
- **Commit 5** (after tasks 17-20): `feat(miniapp): добавить Telegram Mini App MVP`
- **Commit 6** (after tasks 21-24): `feat(finance): добавить расширенные финансовые сценарии`
- **Commit 7** (after tasks 25-27): `feat(platform): добавить MCP, локальный AI и импорт банков`

## Tasks

### Phase 0: Evidence and safety baseline

- [x] **Task 1: Capture the repository baseline and dependency map.** Inspect the current architecture, Telegram flows, PostgreSQL/Alembic model, security controls, test/operations scripts, and existing docs; run the complete quality gate before implementation. Files inspected: `src/finbot/**`, `migrations/**`, `tests/**`, `Makefile`, Compose/Docker files, `README.md`, `PLAN.md`, `DECISIONS.md`, `SECURITY.md`. Logging: do not read runtime logs or secret files; record only pass/fail counts and safe component names. Deliverable: confirmed green baseline plus extraction order that preserves `claim update -> mutation/audit/draft -> Telegram outbox -> commit`.

### Phase 1: M0/M1 application foundation

- [x] **Task 2: Add stable framework-neutral application errors, owner/catalog/draft DTOs, and ports.** Create typed error codes and immutable command/query/result types for drafts, transactions, catalogs, reports, dashboard, and OCR without importing adapter frameworks. Add deterministic in-memory fakes for application unit tests. Files: `src/finbot/application/errors.py`, `src/finbot/application/dto.py`, `src/finbot/application/ports/**`, `tests/fakes/**`, `tests/unit/test_application_contracts.py`. Logging: application contracts emit no logs and contain no presentation/auth fields; adapter errors may later log only safe error codes.

- [x] **Task 3: Implement shared read-only finance use cases and PostgreSQL query adapters.** Provide `Get/ListTransaction`, dashboard/period comparison, account/category listing, and owner-settings queries as injected application services; wrap existing proven SQL functions in concrete repository classes and return DTOs only. Files: `src/finbot/application/use_cases/queries.py`, `src/finbot/adapters/database/repositories/queries.py`, existing query modules, unit and PostgreSQL integration tests. Logging: no amounts/descriptions/owner IDs in logs; only bounded query event code, outcome class, and result count where count is safe.

- [x] **Task 4: Make the persistent draft lifecycle channel-neutral.** Define typed draft snapshots/commands for get/create/update/resume/replace/cancel with UUID, revision, state, and optimistic concurrency; adapt the existing single-row PostgreSQL draft without creating a parallel web draft store. Keep `presentation_ref` and Telegram UI keys adapter-only. Files: `src/finbot/application/use_cases/drafts.py`, `src/finbot/application/ports/drafts.py`, `src/finbot/adapters/database/repositories/drafts.py`, draft service compatibility layer, unit/integration tests. Logging: log only safe event codes and conflict reason codes; never draft payload, revision tokens combined with owner data, presentation refs, or finance values.

- [x] **Task 5: Strengthen architecture and characterization tests before rewiring Telegram.** Cover stale draft revision/object version, confirm+outbox rollback, OCR advance/skip/cancel, repeat, and catalog races; forbid HTTP/framework imports in inner layers and SQLAlchemy/database-service imports in migrated Telegram controllers. Files: `tests/unit/test_architecture.py`, relevant Telegram/PostgreSQL integration tests. Logging: tests assert sensitive values cannot enter structured logs and never print fixtures containing financial or auth data.

- [x] **Task 6: Implement shared transaction and catalog command use cases.** Add create/confirm/edit/delete/restore/repeat and account/category create/update/archive/restore orchestration over ports, typed errors, optimistic versions, and mutation results. Preserve review-first creation and existing database/audit semantics. Files: `src/finbot/application/use_cases/transactions.py`, `catalogs.py`, command ports, PostgreSQL repositories, unit/integration tests. Logging: allowlist mutation event code and success/conflict/failure status only; never input/output DTO contents.

- [x] **Task 7: Extract draft preparation, confirmation, and OCR queue orchestration.** Move deterministic quick/wizard/repeat/OCR decisions behind application use cases while reusing the current parser, categorization policy, and `ImageTextExtractor`; keep image download and Telegram presentation in adapters. Files: `src/finbot/application/use_cases/draft_preparation.py`, `ocr.py`, injected ports/adapters, unit/integration tests. Logging: never image bytes, OCR text, filenames, descriptions, amounts, rule patterns, or parser input/output; log bounded stage/error codes only.

- [x] **Task 8: Rewire Telegram controllers and split registration by bounded flow.** Convert Telegram handlers to parse Telegram input, call application use cases within the existing atomic unit-of-work/outbox envelope, and render results. Remove direct SQLAlchemy/ORM access from migrated controllers, eliminate the two-session versioned-callback TOCTOU seam, and split routing into focused modules while leaving `bootstrap.py` as composition root. Files: `src/finbot/adapters/telegram/controllers/**`, `routers/**`, `src/finbot/bootstrap.py`, Telegram integration tests. Logging: preserve the current strict formatter/allowlist; controllers must not log update payloads, callback data, Telegram IDs, or application DTO values.

- [x] **Task 9: Close M0/M1 with full verification and documentation.** Run all backend, PostgreSQL, Docker, Compose, dependency-audit, and backup/restore gates; update `README.md`, `PLAN.md`, `DECISIONS.md`, `SECURITY.md`, `CHANGELOG.md`, and AI Factory architecture artifacts to match only completed behavior. Logging: document the privacy allowlist and new boundaries without example secrets or realistic financial data. Acceptance: Telegram behavior remains green and shared use cases are independently unit-tested through fakes.

### Phase 2: M2 HTTP API

- [x] **Task 10: Verify and pin the stable HTTP dependency set, then create the FastAPI skeleton.** Check official compatibility for Python 3.14, pin exact stable FastAPI/server dependencies, update `uv.lock`, add `/api/v1`, OpenAPI metadata, structured error mapping, and `/health/live` plus database-backed `/health/ready`. Files: `pyproject.toml`, `uv.lock`, `src/finbot/adapters/http/**`, config/tests. Logging: request logging is route-template/status/latency/error-code only; suppress query strings, headers, cookies, bodies, client identity, and exception text.

- [x] **Task 11: Add PostgreSQL web sessions and HTTP idempotency storage through Alembic.** Store only keyed hashes of opaque session/CSRF/idempotency secrets, bounded expiry, request fingerprint/status/result reference, and cleanup indexes; provision runtime grants through existing least-privilege tooling. Files: new Alembic revision, database models/repositories/provisioning, migration tests. Logging: migrations and repositories log no token/hash/key/request-body/owner/DB URL values; only safe migration/repository event codes.

- [x] **Task 12: Implement Telegram Mini App authentication and cookie session endpoints.** Verify raw `initData` signature and bounded `auth_date`, enforce exact configured owner, issue short-lived opaque `HttpOnly; Secure` cookies with compatible strict SameSite policy, require CSRF on state changes, and invalidate sessions on logout. Implement `POST /api/v1/auth/telegram`, `GET /api/v1/auth/me`, and `POST /api/v1/auth/logout`; no production auth bypass. Files: `src/finbot/adapters/http/auth/**`, routes/schemas/config/tests. Logging: never Telegram IDs, raw/canonical initData, bot token, cookie/session/CSRF values, auth headers, or validation payloads; emit safe auth outcome/reason codes only.

- [x] **Task 13: Implement dashboard/report and deterministic transaction query endpoints.** Expose current/comparable period, totals, top categories, recent transactions, transaction detail, and opaque cursor pagination using shared use cases; never expose an unbounded list. Files: HTTP routes/schemas, application pagination DTO/codec if needed, tests/OpenAPI snapshot. Logging: no finance values, descriptions, cursor contents, or owner identity; route/status/error code and bounded result count only.

- [x] **Task 14: Implement revision-safe draft and transaction mutation endpoints with HTTP idempotency.** Add active/get/create/update/confirm/cancel/resume/replace drafts and repeat/edit-draft/delete/restore transactions. Do not add direct transaction creation that bypasses review. Same idempotency key plus same fingerprint replays the stored result; mismatched reuse returns a typed conflict. Files: HTTP routes/schemas, idempotency middleware/executor, application adapters, tests. Logging: never request bodies, draft contents, keys/fingerprints, revisions linked to users, or stored results; safe operation/outcome/error codes only. Acceptance: stored-only minimal replay, typed key mismatch, consuming confirm `201/404` race, owner-scoped `404`, stale same-ID `409`, and future-schema fail-closed behavior are covered. Accepted non-blocking P2 debt: bound or prefilter learned-rule candidates without changing deterministic precedence.

- [x] **Task 15: Implement account/category/report APIs and API contract generation input.** Map the remaining M2 Mini App surface to shared use cases, return structured machine-readable errors, and make FastAPI OpenAPI the single backend contract source. Files: HTTP routes/schemas/errors, OpenAPI export script/tests. Logging: no entity names or finance contents; safe endpoint template, outcome, and validation code only. Acceptance: account/category reads are complete-but-bounded (`200 + 1` fail-closed probe), every cross-channel writer preserves the cap under the owner lock, catalog writes reuse stored-only idempotent receipts, `/reports/today` uses the owner timezone, and the offline exporter emits one deterministic self-contained OpenAPI document.

- [x] **Task 16: Close M2 with integration, retry, security, deployment, and docs gates.** Add isolated PostgreSQL API tests for auth/session/CSRF/idempotency/concurrency, production-safe API process topology and health checks, exact dependency audit, OpenAPI docs, env placeholders including `MINIAPP_PUBLIC_URL`, and full regression gate. Files: tests, Docker/Compose, `.env.example`, docs and release artifacts. Logging: explicitly test denial of sensitive HTTP data and secrets in logs. Acceptance: M2 endpoints are working and no endpoint bypasses draft review. Implementation/security/deployment completed with P0/P1=0; Task21 supplied the intentionally consolidated dependency, PostgreSQL, image, edge and recovery evidence.

### Phase 3: M3 Telegram Mini App MVP

- [x] **Task 17: Create the pinned React/TypeScript/Vite frontend and generated API types.** Verify stable official versions, pin exact production/dev dependencies, add Tailwind and TanStack Query, generate types from OpenAPI, and fail CI/check when generated output drifts. Files: `web/**`, lockfiles, build/check scripts, Makefile/CI. Logging: frontend production telemetry must not emit initData, transaction data, OCR content, tokens, or API bodies; development diagnostics are bounded event names only. Acceptance: exact npm lock, deterministic generated contract drift check, strict TypeScript, one focused smoke, source-map-free production build, SHA-pinned CI, and zero install audit findings.

- [x] **Task 18: Implement the Telegram WebApp adapter and authenticated application shell.** Isolate Telegram SDK calls, forward raw `initData` only to the auth endpoint, call ready/expand/theme hooks, bootstrap cookie auth, handle 401/CSRF refresh safely, and provide accessible navigation/loading/error states. Files: `web/src/adapters/telegram/**`, auth/query shell/tests. Logging: never log SDK payloads, initData, user data, cookies, CSRF, or responses; safe lifecycle/error codes only.

- [x] **Task 19: Implement dashboard, transaction history/detail, and shared-draft review flows.** Build responsive premium-quality screens for overview, deterministic pagination, active draft conflict decisions, create/edit/repeat/review/confirm/cancel, optimistic conflict recovery, and account/category selectors using generated types. Files: `web/src/features/**`, components/tests. Logging: no finance values or user text; safe UI action name, outcome, and HTTP error code only.

- [x] **Task 20: Integrate bot launch, production serving, security headers, and cross-channel E2E.** Add owner-only Mini App launch button using `MINIAPP_PUBLIC_URL`, serve immutable assets with hardened headers/CSP and correct API routing, preserve non-root/read-only/cap-drop posture, and test Bot draft -> Mini App and Mini App draft -> Bot transitions plus mobile retry. Files: Telegram UI/config, frontend serving config, Docker/Compose, E2E tests, docs. Logging: launch and serving logs contain no URL query/initData/owner/cookie or finance data; only safe component/status codes. Implementation, production edge smoke and the consolidated cross-channel gate are complete.

- [x] **Task 21: Close M3 and release the verified Mini App MVP.** Run backend/frontend typecheck, unit/integration/E2E, production builds, Compose, migration, security/dependency, and backup/restore gates; update all required docs and `PLAN.md` truthfully. Logging: verify source maps, browser console, reverse-proxy access/error logs, and backend logs do not expose sensitive data. Acceptance: M0-M3 are complete; Mini App supports auth, dashboard, transactions, and cross-channel draft review without weakening Telegram. Final evidence: 1838 unit and 146 PostgreSQL integration scenarios passed without skips; Alembic `0011` guards/roundtrips, 62-path OpenAPI drift, Python/npm audits, application/web images, edge/TLS privacy smoke and encrypted restore drill are green.

### Phase 4: M4 advanced finance

- [x] **Task 22: Implement budgets as shared application features.** Add only the necessary Alembic schema, integer-minor-unit budget rules, period progress/read APIs, Telegram/Mini App presentation, and unit/PostgreSQL/E2E tests. Logging: no budget amounts, names, owner IDs, or period contents; safe operation/outcome codes only. Migration `0008`, shared application/HTTP/Telegram/Mini App flows and the generated OpenAPI contract are implemented and confirmed by the Task21 PostgreSQL gate.

- [x] **Task 23: Implement recurring transactions as review-first draft generators.** Add schedules and due-instance idempotency without auto-posting financial transactions; generated items enter the existing draft/review lifecycle. Logging: no schedule payloads, amounts, descriptions, or owner identity; safe scheduler/use-case outcome codes only. Migration `0009`, bounded two-phase runner, shared application/HTTP/Telegram/Mini App flows, and generated OpenAPI types are implemented and confirmed by the Task21 PostgreSQL gate.

- [x] **Task 24: Implement explicit exchange-rate support without float.** Add versioned rate sources and conversion reporting using integer/Decimal boundary math, never rewrite original transaction currency/amount, and provide deterministic tests. Logging: no rates tied to user finance data or converted values; safe source/status codes only. Migration `0010`, immutable manual sources/versions, explicit-version HTTP reporting, Telegram read-only access and Mini App management are implemented and confirmed by the Task21 PostgreSQL gate.

### Phase 5: M5-M7 optional platform adapters

- [x] **Task 25: Add a read-only MCP adapter after the shared API is stable.** Expose bounded finance queries through application use cases with local explicit authorization, schema-safe outputs, and no write tools in the first milestone. Logging: no MCP arguments/results, finance data, owner IDs, tokens, or transport payloads; safe tool/outcome codes only. The pinned MCP v2 adapter is local stdio-only, fixes owner identity at startup, exposes six bounded finance/catalog query tools, uses one read-only repeatable-read UoW and a dedicated SELECT-only PostgreSQL role, and has no listener or write tool; Task21 confirmed role provisioning, reads and write denial against PostgreSQL.

- [x] **Task 26: Add optional disabled-by-default local AI/Ollama adapters.** Keep deterministic parser/OCR behavior as the default, treat model output as untrusted suggestions, enforce timeouts/resource limits/no external network, and require explicit review before any financial mutation. Logging: never prompts, model outputs, descriptions, OCR text, identifiers, or amounts; safe provider/stage/error codes only.

- [x] **Task 27: Add bank import and reconciliation as staged, auditable workflows.** Parse untrusted files into a quarantine/import batch, normalize money without float, deduplicate/reconcile deterministically, and require explicit review before creating transactions. Add only milestone-required migrations and full rollback/security tests. Logging: never filenames, file contents, bank data, amounts, descriptions, account identifiers, or mapping artifacts; safe format/stage/count/outcome codes only. Migration `0011`, bounded HTTP/Telegram ingestion, review-first reconciliation, provenance-safe PostgreSQL writes and Mini App screens are implemented; static/frontend/OpenAPI/Nginx/Compose and consolidated Task21 PostgreSQL/cross-channel gates are green.
