# Implementation Plan: Secure Multi-Tenant Access

Branch: codex/multi-tenant-access
Created: 2026-08-20

## Original Request

Разработай план, выполни его, чтобы можно было нескольким людям использовать это мини-приложение.

## Settings

- Testing: yes
- Logging: standard, privacy-safe allowlisted events only
- Docs: yes

## Scope and Decisions

- Keep `OWNER_TELEGRAM_USER_ID` as the primary administrator and the fixed MCP principal.
- Add a bounded, explicit `TELEGRAM_ALLOWED_USER_IDS` allowlist; no open self-registration.
- Preserve backward compatibility: without the new variable the installation remains single-user.
- Treat every allowed person as an independent tenant owning separate accounts, categories, drafts, transactions, budgets, schedules, rates, imports, sessions, and idempotency records.
- Do not implement shared household wallets, cross-user transfers, RBAC, or destructive offboarding in this phase.
- Keep PostgreSQL RLS as a later defense-in-depth phase because pre-auth, cleanup, recurring, outbox, and MCP workers require a separate role/context design.

## Commit Plan

- **Commit 1** (after tasks 1-3): `feat(auth): add explicit multi-user Telegram principals`
- **Commit 2** (after tasks 4-6): `feat(platform): isolate Mini App sessions and tenant data`
- **Commit 3** (after tasks 7-9): `test(platform): verify and document multi-tenant isolation`

## Tasks

### Phase 1: Access policy and Telegram identity

- [x] Task 1: Add a canonical bounded Telegram allowlist to `src/finbot/config.py`, Compose and `.env.example`, retaining the legacy primary owner fallback. Add focused config tests. Logging: never log the configured IDs or the allowlist contents; configuration errors remain fixed and value-free.
- [x] Task 2: Replace the singleton Telegram authorization source with an immutable middleware principal and propagate the verified actor through all message/callback context factories and outbox requests. Enforce `private` chat plus actor/chat equality and remove runtime owner lookup from `Settings`. Logging: retain fixed authorization event codes only; no actor/chat/update identifiers.
- [x] Task 3: Make onboarding and Mini App menu configuration tenant-safe: fail closed on chat rebind, seed each user independently, configure bounded per-chat menus after committed onboarding, and keep global Main Mini App disabled. Logging: one fixed menu result event without IDs or counts.

### Phase 2: HTTP subject binding and persistence integrity

- [x] Task 4: Generalize signed Telegram HMAC verification and HTTP session authorization to the allowlist, immediately rejecting removed users even with an existing cookie. Add same-user active-session proof reuse without weakening fresh/replayed proof denial. Logging: fixed reason codes only; no initData, Telegram ID, cookie, proof, or session values.
- [x] Task 5: Change the Mini App bootstrap to present signed `initData` on every launch before trusting `/auth/me`, clear protected query state before subject rebinding, and retain bounded one-shot unknown-result handling. Add frontend authentication regressions for stale cross-user cookies and same-user reloads. Logging: production client telemetry remains no-op and never records identity or payloads.
- [x] Task 6: Add migration `0012_multitenant_integrity` and ORM/repository changes for fail-closed tenant ownership constraints, including chat binding and composite ownership references that can otherwise cross tenants. Make recurring materialization fair across owners. Logging: migration and runner events remain count/status-only and never expose tenant or financial identifiers.

### Phase 3: Isolation proof, contract, documentation and rollout

- [x] Task 7: Add two-user unit and PostgreSQL integration coverage for independent onboarding, drafts, catalogs, budgets, transactions, reports, imports, idempotency and forbidden copied UUID/callback access. Verify equal foreign/missing responses and unchanged owner data. Logging tests must prove IDs and financial values are absent.
- [x] Task 8: Regenerate the canonical OpenAPI/frontend types if the auth contract changes and update `README.md`, `SPEC.md`, `SECURITY.md`, `DECISIONS.md`, `PLAN.md`, `AGENTS.md`, `.ai-factory/DESCRIPTION.md`, and `.ai-factory/ARCHITECTURE.md` from single-owner to bounded private multi-user semantics. Logging: document the value-free audit model and rollback behavior.
- [x] Task 9: Run scoped static/unit/frontend/Compose/migration checks, then deploy bot and API together with singleton fallback first. Verify current owner behavior before enabling additional IDs. Logging: inspect only allowlisted route/result/status events; do not print runtime configuration or secrets.
