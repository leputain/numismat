# Architecture: Explicit Architecture (Technical Layer)

## Overview

Numismat uses a pragmatic ports-and-adapters architecture organized by technical layer. Financial invariants and deterministic policies live in framework-independent inner modules; Telegram, HTTP/browser, PostgreSQL, OCR, container runtime, and backup tooling remain replaceable outer details. One codebase/image and one database expose separate polling and ASGI process roles; this is process isolation, not independently owned microservices.

This document describes the existing application and its intended incremental direction. It does not require a rewrite. The M0/M1 shared application foundation, PostgreSQL repositories, and focused Telegram routers/controllers are implemented. `src/finbot/bootstrap.py` remains a large composition root because it owns construction, renderer/context factories, and registration order, but business handler bodies, direct ORM queries, and manual commits live outside it. Further cleanup should reduce wiring volume without weakening observable behavior or reliability guarantees.

## Decision Rationale

- **Project type:** Security-sensitive private personal-finance Telegram bot with an operator-managed allowlist of up to 32 isolated ledgers, local OCR, and operational backup workflows.
- **Tech stack:** Python 3.14, aiogram, FastAPI/Uvicorn, PostgreSQL, SQLAlchemy asyncio, Alembic, Pydantic, React/Vite/TypeScript/Tailwind/TanStack Query, Pillow/Tesseract, Docker Compose, and Restic.
- **Key factor:** Money, draft, update-idempotency, authorization, and privacy rules require testable framework-independent boundaries, while the product still benefits from a single deployable process and database.
- **Rejected alternative:** Independently deployed business microservices add network, consistency, deployment, and observability failure modes without a scaling or ownership need. Polling, API, and static-serving processes still share one application model and database.

## Folder Structure

```text
src/finbot/
├── domain/                         # Innermost layer: pure invariants and value semantics
│   ├── money.py                    # Integer-minor-unit validation and formatting
│   ├── transactions.py             # Transaction DTOs, types, and period boundaries
│   ├── category_rules.py           # Pure deterministic matching rules
│   ├── categories.py               # Built-in catalog domain data
│   ├── dates.py                    # Date parsing and timezone rules
│   └── errors.py                   # Recoverable domain errors
├── application/                    # Use-case contracts and persistence-neutral orchestration
│   ├── dto.py                      # Immutable owner, catalog, draft, transaction and OCR snapshots
│   ├── errors.py                   # Stable adapter-neutral error taxonomy
│   ├── ports.py                    # Parser and persistence/query protocols
│   ├── interactions.py             # Versioned interaction codec
│   ├── ocr.py                      # OCR port and bounded result contracts
│   ├── ocr_queue.py                # Channel-neutral sequential OCR queue codec
│   ├── export.py                   # Bounded privacy-safe CSV export contracts
│   ├── draft_preparation.py        # Canonical prepared-draft contracts
│   ├── rules.py                    # Category-rule ports and policies
│   ├── queries/                    # Persistence-neutral read contracts
│   ├── services/                   # Compatibility commands and pure services
│   └── use_cases/                  # Draft, transaction, catalog, query and OCR orchestration
├── adapters/                       # Framework and infrastructure implementations
│   ├── database/
│   │   ├── models.py               # SQLAlchemy mappings
│   │   ├── session.py              # Database lifecycle/unit-of-work boundary
│   │   ├── queries/                # PostgreSQL read adapters
│   │   ├── repositories/           # Application ports plus web-session/idempotency state
│   │   ├── services/               # PostgreSQL command adapters
│   │   └── provision.py            # Least-privilege runtime-role provisioning
│   ├── telegram/
│   │   ├── routers/                # Bounded parsing, ordering, acknowledgement, and fallback
│   │   ├── controllers/            # Thin framework-boundary orchestration over use cases
│   │   ├── executor.py             # Atomic claim/mutation/outbox/commit envelope
│   │   ├── middlewares/auth.py     # Bounded-allowlist/private actor-chat authorization
│   │   ├── principal.py            # Immutable verified Telegram actor/chat context
│   │   ├── polling.py              # Sequential safe polling and offset semantics
│   │   ├── delivery.py             # Bounded Telegram retry behavior
│   │   ├── outbox.py               # Typed durable response delivery
│   │   ├── input_delivery.py        # Post-commit text/image delivery and binding
│   │   ├── export_delivery.py       # In-memory delivery of durable CSV jobs
│   │   ├── main_menu_delivery.py    # Main-menu post-commit presentation
│   │   ├── miniapp_menu.py          # Per-allowlisted-user private Mini App launch menu
│   │   ├── parser.py               # Deterministic text-input adapter
│   │   ├── presenters.py           # Escaped Telegram presentation
│   │   └── ui.py                   # Callback/keypad construction
│   ├── http/
│   │   ├── app.py                  # FastAPI composition and versioned OpenAPI
│   │   ├── server.py               # Minimal privacy-safe Uvicorn entry point
│   │   ├── errors.py               # Fixed structured HTTP error mapping
│   │   ├── request_logging.py      # Route-template/status/duration-only events
│   │   ├── maintenance.py          # Bounded advisory-singleton HTTP cleanup
│   │   └── routes/                 # Framework boundary without database imports
│   ├── ai/                          # Disabled default and bounded local Ollama suggestions
│   └── ocr/tesseract.py            # Untrusted-image and local Tesseract adapter
├── observability/logging.py        # Privacy-safe allowlisted JSON logging
├── config.py                       # Validated env/Docker-secret adapter
├── bootstrap.py                    # Composition root and focused-router registration
├── healthcheck.py                  # Safe operational probe
└── __main__.py                     # Process entry point

migrations/                         # Alembic schema history
tests/unit/                         # Inner-layer and isolated adapter behavior
tests/integration/                  # Disposable PostgreSQL/Telegram/OCR behavior
deploy/postgres/                    # Initial database privilege boundary
deploy/web/                         # Canonical TLS edge and secret preflight
scripts/                            # Integration, backup, and restore orchestration
ops/systemd/                        # Timer/service deployment artifacts
web/                               # React/Vite Mini App and generated OpenAPI types
Dockerfile.web                     # Multi-stage immutable Mini App edge image
```

The router boundary is concrete, not speculative: it owns the current main-menu, finance, settings/catalog, text/image input, transaction lifecycle, compact draft interaction, OCR, undo, export, and fallback families. Add another router only with a real bounded flow and its registration-order tests.

## Dependency Rules

Dependencies point inward:

```text
__main__ / bootstrap / config
              ↓
Telegram, PostgreSQL, OCR, operations adapters
              ↓ implements/calls
Application ports, DTOs, policies, and use cases
              ↓
Domain invariants and value semantics
```

- ✅ `domain` may depend on the Python standard library and other domain modules.
- ✅ `application` may depend on `domain` and declare `Protocol` interfaces for required capabilities.
- ✅ adapters may depend on `application`, `domain`, and their own frameworks.
- ✅ the composition root may construct adapters and inject them into application-facing contracts.
- ✅ migrations may use Alembic/SQLAlchemy but must remain version-pinned and independent of mutable runtime policy where historical behavior matters.
- ❌ `domain` and `application` must not import aiogram, SQLAlchemy, `finbot.adapters`, Docker/runtime helpers, or concrete external clients.
- ❌ Telegram handlers must not become the owner of money calculations, categorization policy, transaction invariants, or database schema semantics.
- ❌ domain objects must not accept `AsyncSession`, Telegram objects, ORM entities, settings objects, or raw secrets.
- ❌ adapters must not bypass owner scoping, optimistic versions, draft revisions, idempotency claims, or typed response-outbox semantics.

The architecture test in `tests/unit/test_architecture.py` is a required enforcement gate. Extend it when new inner-layer roots or forbidden framework dependencies are introduced.

## Layer and Module Communication

### Telegram mutation flow

1. Authorization and outbox middleware validate the exact numeric owner/private-chat boundary and deliver any pending response before replay.
2. A focused Telegram router parses bounded command, callback, text, or image metadata and selects one controller. Broad text routing falls through only on typed `NotApplicable` results; other failures are fail-closed.
3. The controller enters `TelegramMutationExecutor`; no Telegram network I/O occurs inside the mutation callback.
4. The executor owns one SQLAlchemy session, claims the update, resolves and locks the owner, and invokes application use cases through PostgreSQL ports.
5. Business mutation, audit/draft changes, presentation context, and a typed response-outbox record commit atomically.
6. Post-commit delivery sends the response and conditionally binds only the still-current Telegram presentation. External delivery remains at-least-once.

### Channel-neutral drafts and Telegram presentation

- Application draft snapshots contain only draft identity, revision, state, schema version, suspension state, timestamps, and business payload.
- All mutations compare both draft UUID and expected revision; PostgreSQL serializes the absent-row/create race by locking the owner row.
- Telegram chat/message binding plus `history_page` and `pending_history_page` navigation context live in `telegram_draft_presentations`, not in the application DTO. The same bounded context can travel in the response outbox only when tied to an exact draft revision.
- Compact callbacks carry draft UUID/revision and bounded action/object/page data. The adapter locks and validates the exact projected chat/message before mutation; presentation identity is not an application callback field.
- The legacy `presentation_ref`/payload keys remain temporarily for rolling compatibility. New application code neither exposes nor relies on them.
- OCR queue confirmation, skip, and cancel lock the exact draft and its current Telegram presentation in the same transaction before mutation and outbox enqueue.

### Tenant identity and ownership boundary

- `OWNER_TELEGRAM_USER_ID` remains the required primary/MCP/rollback principal. Optional
  `TELEGRAM_ALLOWED_USER_IDS` is the complete canonical allowlist of 1..32 unique IDs and must include primary;
  absence preserves singleton behavior.
- `OwnerOnlyMiddleware` admits only allowlisted `event_from_user`, `chat.type == private`, and exact
  `actor_id == chat_id`, then creates an immutable `TelegramPrincipal`. Callback code uses this scoped principal rather
  than the callback message's bot-owned `from_user`.
- `ensure_owner_user` creates one separate owner/catalog namespace and allows only first `NULL -> actor_id` chat
  binding. A different later chat fails closed. Downstream repositories derive owner UUID server-side and retain
  owner predicates across catalogs, drafts, transactions, reports, budgets, schedules, rates, imports, sessions, and
  idempotency records.
- Migration `0012_multitenant_integrity` performs an online fail-closed legacy scan and adds the private actor/chat
  check plus composite ownership references for default account, category parent, audit transaction,
  recurring/import draft, and Telegram outbox. PostgreSQL RLS is deferred until pre-auth, cleanup, recurring, outbox,
  and process-fixed MCP roles/context can be designed as one boundary.
- Telegram delivery performs a draft-owner/private-chat preflight before every network send for an outbox row with a
  draft reference. A mismatch fails before Telegram I/O and remains unsent; presentation binding repeats the same
  owner/chat predicate. This is the application guard for draft references that cannot be fully expressed by the
  existing outbox ownership foreign key alone.
- Shared household state, cross-user transfers, RBAC, self-registration, and destructive offboarding are outside this
  phase. Revoking an ID retains its rows; the DB-only recurring runner does not consume the Telegram allowlist.

### OCR flow

1. `OcrImageRouter` checks replay state before downloading and obtains bounded bytes through the Telegram adapter.
2. `OcrImageController` runs the shared `ProcessOcrImage` use case inside the mutation/outbox envelope.
3. The OCR adapter validates actual image format, dimensions, pixel count, decoding, timeout, and output limits; local Tesseract returns text only in memory.
4. Deterministic parsing creates one review draft or a bounded sequential draft queue in the same committed flow as its typed Telegram response.
5. Every candidate still requires explicit owner review; OCR never writes a transaction directly.

### Optional local-AI suggestion flow

1. Only `/ai <text>` enters the feature; deterministic text parsing and OCR never call it as fallback.
2. `SuggestLocalTransaction` calls a disabled-by-default provider before `TelegramMutationExecutor` opens a database
   UoW. A read-only processed-update probe prevents normal replay from repeating the model request.
3. The Ollama adapter accepts only literal loopback or `ollama:11434`, disables proxy/redirect/streaming/model
   keep-alive, and enforces an eight-second total timeout plus 1024-character/16-KiB bounds.
4. Strict UTF-8 structured output with no extra/duplicate fields becomes a typed `TransactionDraft`; money is parsed
   as integer minor units and raw prompt/output remains in memory only.
5. Inside the atomic Telegram envelope, `CreateLocalAiDraft` reuses `PrepareParsedDraft`. It either creates a shared
   review draft or commits a fixed active-draft receipt without changing/binding/suspending/replacing existing work.
6. Production Ollama is operator-supplied, preseeded, `OLLAMA_NO_CLOUD=1`, and attached only to the internal
   `local-ai` network; the base topology performs no model download.

### CSV export flow

1. The export controller atomically claims the update, optionally suspends the active draft, and enqueues only the constant `csv_export:v1` job marker.
2. `CsvExportDelivery` resolves the exact owner by Telegram user/chat and generates the file from current owner-scoped data after commit.
3. CSV bytes, filename, and row count exist only in memory; output is limited to 10,000 rows and 16 MiB and protects owner-controlled cells from spreadsheet formulas.
4. The job is durable but not a point-in-time data snapshot. A crash after Telegram accepts `send_document` but before `sent_at` commits may produce another, potentially later-state export on replay.
5. Migration `0006_csv_export_outbox_job` refuses downgrade while any CSV job rows remain, including delivered rows.

### Recurring review-draft flow

1. HTTP/Mini App commands create versioned daily/weekly/monthly schedules under the owner lock; the owner timezone is
   copied once and remains immutable. Telegram exposes only a bounded overview/detail and a Mini App manage link.
2. `SqlAlchemyRecurringRunner` materializes due instances and stages drafts in two separately committed phases with
   distinct transaction advisory locks. Schedule and owner batches, SQL timeouts and pending probes are bounded;
   materialization selects at most one nearest due schedule per owner before applying the global 32-item cap.
3. Both phases acquire the owner row before schedule/instance/draft rows. `(schedule_id, occurrence_index)` prevents
   duplicate due work; the unstaged pending cap is 32 per owner.
4. Staging creates only `Draft(state="review", flow="recurring")`. Any existing active draft leaves the instance
   pending with backoff; hidden intents, suspension, replacement and transaction auto-save are forbidden.
5. Existing draft confirmation writes `source=recurring` plus unique `recurring_instance_id`. Draft cancellation
   clears the FK and derives `dismissed`; schedule/instance reads remain bounded and owner-scoped.

### Query and reporting flow

- Application query protocols expose persistence-neutral result types.
- PostgreSQL query adapters own SQLAlchemy expressions and database-specific aggregation.
- Presenters escape user-controlled text and format integer minor units without reimplementing financial rules.

### HTTP process, authentication, and readiness

- The HTTP adapter is a separate ASGI process entry point over the same package and database; it is not started inside the Telegram polling loop.
- FastAPI routes depend on injected adapter protocols. Database construction stays in `adapters/http/app.py`, while `routes/` cannot import SQLAlchemy or database adapters.
- `/health/live` proves only process liveness. `/health/ready` applies a bounded timeout and requires both a successful PostgreSQL query and exact Alembic-head match.
- Uvicorn access logging and proxy-header trust are disabled. The allowlisted completion event contains only server-generated correlation ID, route template, HTTP status, latency bucket, result class, and fixed error code.
- Swagger/ReDoc are disabled because their default pages load third-party CDN assets; `/api/v1/openapi.json` remains the backend contract source.
- The runtime HTTP app fails closed unless `MINIAPP_PUBLIC_URL` is one exact canonical HTTPS origin and `HTTP_SECURITY_KEY` decodes to an independent 32-byte key. Test-only skeleton construction is not a production auth bypass.
- `POST /api/v1/auth/telegram` verifies the official Telegram HMAC before parsing trusted identity fields, applies
  bounded `auth_date` and allowlist membership checks, and denies replay by a digest derived from the verified
  Telegram hash rather than raw query encoding. Mini App startup always presents current signed `initData` before
  trusting the WebView cookie jar; an active same-subject session may be retained, while a stale cross-subject cookie
  is revoked and replaced only after a successful fresh proof claim.
- Session and CSRF tokens are independent opaque values. Only domain-separated keyed digests reach PostgreSQL; the session uses a host-only `HttpOnly; Secure; SameSite=Strict` cookie and state changes additionally require the readable CSRF cookie plus exact header value.
- Every successful auth response returns `X-Session-Binding`, a privacy-safe domain-separated HMAC over the raw
  HttpOnly session token. The frontend keeps it only in page memory and sends it with every protected GET,
  mutation, and logout. The backend recomputes it from the current cookie before tenant lookup; mutation still
  requires double-submit CSRF. A mismatch returns `401` without clearing the current cookies, so stale page A cannot
  erase the valid session of page B in the shared WebView cookie jar.
- `Set-Cookie` is emitted only after a successful login commits a newly created session. Same-subject session reuse
  returns the binding without cookie rotation. Failed login, every protected error, and successful logout never emit
  cookie deletion; logout revokes the row under its exclusive lock and returns `204` without `Set-Cookie`. The page
  clears its in-memory binding and protected view, while the inert revoked cookie is overwritten by the next successful
  signed login. This prevents a late response from page A from deleting page B's newer shared-jar cookie.
- Authenticated reads, mutations, and logout re-check the session owner's current process-local allowlist membership.
  Mutations must hold the web-session shared row lock in the same SQLAlchemy transaction as owner, idempotency, and
  domain writes. Logout takes the exclusive session lock; new-session response/cookie construction happens only after
  commit. One bounded ASCII Origin, if present, is untrusted metadata rather than an authorization signal.

### Mini App launch and production edge

- `MiniAppMenuConfigurator` rejects a global Main Mini App, keeps the default menu inert, and installs
  `MenuButtonWebApp` separately for each allowlisted private chat only after that user's onboarding transaction
  commits. A missing chat is retried at a later `/start` or `/menu` without exposing a global launch surface.
- The frontend associates every authentication attempt with a monotonic epoch and applies an async result only while
  that epoch is current. `hidden` and every `pagehide` synchronously remove the protected UI, replace the tenant
  QueryClient, and invalidate the epoch. If the page was authenticated, its binding remains memory-only solely for a
  resume `/auth/me` check with the current cookie; `visible`/persisted `pageshow` never replay cached `initData`.
  Suspending an in-flight authentication clears the binding, so failed resume requires a fresh Telegram launch.
- `Dockerfile.web` builds source-map-free Vite output and copies only immutable `dist` plus static Nginx config into
  a pinned unprivileged image. Runtime config is rendered into tmpfs; rootfs and TLS secret mounts stay read-only.
- `public-edge` contains only web and accepts host TCP/443. Web reaches API over separate internal `api-edge`; API
  never joins the public network. `/api/*` remains generic so an OpenAPI path addition cannot silently hit SPA fallback.
- Edge logs only closed event/route-group/result/status values. It strips client identity forwarding and never logs
  URI/query, client address, cookies, headers, bodies, upstream values, bytes, or timing.
- HTTP security cleanup is a bounded FastAPI lifespan concern. A transaction-scoped PostgreSQL advisory lock elects
  one caller, while batch, transaction, statement and wall-clock limits prevent a scheduler from competing with writes.

### HTTP finance query boundary

- Dashboard, period/comparison reports, transaction detail, and active transaction pagination call the shared application query use cases; routes do not import SQLAlchemy or database adapters.
- Authentication and every multi-query finance read share one `READ ONLY REPEATABLE READ` transaction. The database adapters cap currencies, category rows, report items, and page size before response construction.
- Money in HTTP JSON is a canonical decimal string so PostgreSQL `BIGINT` values cannot lose precision in JavaScript. UUIDs and bounded optimistic versions retain their native schema types.
- Transaction pagination is active-only keyset order `(occurred_at DESC, id DESC)` with `limit + 1`. The 76-character owner/domain-bound HMAC cursor is API-opaque and tamper-evident, not encrypted; it contains only the timestamp and UUID already present in the preceding item.
- Pagination is deterministic for an unchanged dataset and retry-stable. It is a live view, not a historical snapshot across requests; after any transaction mutation the client invalidates the list and starts again.
- All `/api/v1/` responses replace cache/referrer headers with `no-store`, `no-cache`, and `no-referrer`; request completion logs retain only route template, status, duration bucket/result, and fixed error code.

### HTTP revision-safe mutation boundary

- Routes accept a closed typed action union and call `HttpRevisionMutationService`; generic draft payload updates and direct transaction creation are not part of the HTTP surface.
- Public draft responses are bounded projections. Raw draft payload, pending intent contents, learned-rule patterns, and Telegram presentation metadata never cross the HTTP boundary. Unknown, OCR/settings-specialized, and future-schema drafts fail closed as `supported=false`.
- Every protected write uses one SQLAlchemy transaction and one lock order:

```text
shared web-session lock -> owner row lock -> idempotency claim -> domain rows -> completion -> commit
```

- A committed replay is reconstructed only from stored status, result kind, UUID, and revision/version. The same key and semantic fingerprint returns that exact result; mismatched reuse returns typed `409`; a failed mutation rolls back its claim.
- Same-key concurrent confirm returns the stored `201` result without a second transaction. With different keys, the winner consumes the draft and the loser receives owner-scoped `404`; no tombstone is introduced only to synthesize `409`.
- A draft with an unsupported schema version can be read only through the bounded unsupported projection. PATCH, confirm, cancel, conflict resolution, and conflict-staging ingress all reject it without mutation.

### HTTP catalog and contract boundary

- Account/category reads authenticate and query within one `READ ONLY REPEATABLE READ` UoW. SQL fetches `cap + 1` rows; application and response schemas either return the complete set up to 200 or fail closed without truncation.
- Catalog writes reuse the shared mutation executor and stored-only idempotency receipts. Every operation that grows an active or archived destination checks capacity after owner/target/version validation and before the state change; Telegram and HTTP custom-input writers use the same probe.
- `/reports/today` derives one owner-local date from the server clock and converts it to inclusive-start/exclusive-end UTC bounds through the shared report use case.
- `app.openapi()` is the only backend contract source. The offline exporter injects inert dependencies, validates every local JSON Pointer, canonicalizes JSON, and atomically replaces the output without reading runtime configuration or infrastructure.
- Learned category resolution fetches at most `512 + 1` rules per owner/kind. Overflow is a fixed application failure, never a truncated precedence decision; new-rule insertion is serialized by the owner row lock and existing-rule update remains available at the cap.
- Development publishes the API only on loopback. Production runs polling and ASGI as separate hardened containers, gives the API runtime-only secrets, and exposes port 8080 solely to the internal Compose network pending the M3 serving layer.

## Key Principles

1. **Money correctness before convenience.** Integer minor units, positive stored amounts, explicit transaction type, and bounded conversions are system invariants.
2. **Fail closed at trust boundaries.** Reject ambiguous identity, callback state, database target, currency, OCR content, or migration/runtime privilege configuration.
3. **Explicit user intent.** Quick input, OCR, optional local-AI suggestions, repeat, edits, delete, restore, and learned category rules retain their documented review/confirmation semantics.
4. **Transactional reliability.** Preserve sequential polling, update replay protection, row locks, optimistic versions, atomic response outbox writes, and honest at-least-once external delivery.
5. **Privacy by data minimization.** Sensitive payloads must not enter logs, external AI services, temporary persistent storage, healthchecks, test fixtures, or committed artifacts.
6. **Infrastructure is replaceable, historical migrations are not.** New adapters follow current contracts; old Alembic revisions remain reproducible and must not silently depend on later mutable helpers.
7. **One deployable until evidence says otherwise.** Prefer a well-structured monolith; split services only for demonstrated independent scaling, ownership, or failure-isolation requirements.
8. **One verified subject, one ledger.** Never accept tenant identity from request object IDs, callback payloads, a
   cookie without its page-scoped session binding, or client-selected owner fields; derive it from the verified
   Telegram principal/session and preserve it in every query, lock, idempotency key, and ownership constraint.

## Code Organization Note

- **New features:** Put invariants in `domain`, orchestration/contracts in `application`, and framework work in adapters. Add Telegram behavior to a focused router/controller and call an application use case rather than placing a handler body in `bootstrap.py`.
- **Existing code:** Treat `bootstrap.py` as composition and compatibility wiring only. All current handler families use extracted routers/controllers; further changes should reduce helper construction without moving business logic back across the boundary.
- **Incremental cleanup:** Change one coherent wiring or compatibility boundary at a time, retain fake-port unit tests and PostgreSQL integration coverage, and verify callback/replay/outbox ordering before removing rolling-compatibility code.
- **Interoperability:** Keep using small adapter functions, protocols, and persistence-neutral DTOs. The M2 HTTP process grows through these contracts rather than creating parallel draft, finance, or catalog models. HTTP auth/session/idempotency repositories accept only validated keyed digests, never own commit/rollback, and rely on Alembic constraints plus PostgreSQL locks for bounded expiry, logout/mutation ordering, proof replay denial, request replay, and downgrade safety.

## Code Examples

### Keep money invariants in the domain

```python
from dataclasses import dataclass

from finbot.domain.money import validate_minor


@dataclass(frozen=True, slots=True)
class Amount:
    minor: int

    def __post_init__(self) -> None:
        validate_minor(self.minor)
```

Framework handlers and ORM adapters may consume the validated integer, but they must not create a competing money parser or use `float`.

### Depend on an application port, not a concrete adapter

```python
from finbot.application.ports import TransactionParser
from finbot.domain.transactions import TransactionDraft


def prepare_draft(parser: TransactionParser, text: str) -> TransactionDraft:
    return parser.parse(text)
```

```python
from finbot.adapters.telegram.parser import DeterministicParser
from finbot.application.ports import TransactionParser


parser: TransactionParser = DeterministicParser(timezone="Europe/Moscow")
draft = prepare_draft(parser, "1450 restaurant")
```

The application-facing function knows only the protocol and domain DTO. Adapter construction stays in the composition root.

### Keep ORM types outside application contracts

```python
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DeleteTransaction:
    transaction_id: UUID
    expected_version: int


class TransactionWriter(Protocol):
    async def delete(self, owner_id: UUID, command: DeleteTransaction) -> None: ...
```

The PostgreSQL implementation may use `AsyncSession`, row locks, and SQLAlchemy models; the command and port must not.

## Anti-Patterns

- ❌ Adding aiogram or SQLAlchemy imports to `domain` or `application`.
- ❌ Performing money arithmetic, authorization, stale-callback decisions, or categorization learning inside a presenter or keyboard builder.
- ❌ Passing ORM models, Telegram messages, sessions, or settings into domain rules.
- ❌ Treating `bootstrap.py` as the default home for every new feature.
- ❌ Splitting into microservices while sharing one database or requiring coordinated deployment.
- ❌ Logging first and attempting to redact later; Numismat requires allowlisted structured events instead.
- ❌ Retrying business mutations without the processed-update and response-outbox contracts.
- ❌ Using production/developer databases as a fallback when an integration or restore `_test` database is unavailable.
