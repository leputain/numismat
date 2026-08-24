# История изменений

Формат основан на Keep a Changelog. До стабильной `1.0` minor-версия может включать несовместимые изменения.

## [Unreleased]

Версия пакета остаётся `0.45.0`; перечисленные изменения ещё не выпущены отдельным релизом.

- критический Mini App путь «быстрый ввод → операции → карточка» больше не зависит от поздней
  загрузки route chunk; глобальный React fallback различает устаревший chunk и прочий локальный
  сбой, предлагает безопасную перезагрузку без повторения финансовой мутации, а обращения к
  необязательным Telegram SDK capabilities и native scroll больше не выпускают синхронные ошибки в React;
- Telegram принимает строгий amount-only ввод (`500`, `500,50`, `500.50`, `1 200`) без
  предварительной кнопки и проводит его через тип, категорию, счёт, дату, комментарий и
  обязательный review; неоднозначные, signed-only, zero, exponent, currency и word формы fail closed,
  а legacy `500 кофе`/маркеры/даты остались совместимыми;
- добавлены review-only HTTP capture routes `/api/v1/drafts/quick` и `/api/v1/drafts/compose`, основной
  быстрый ввод Mini App, одноэкранная форма, три последние операции и review-first
  «Повторить»; Telegram keyboard сокращён до добавления, отчёта за сегодня, операций и «Ещё»;
- active transaction pagination получила owner-scoped фильтры периода, типа, счёта, категории и валюты;
  signed cursor теперь привязан к fingerprint фильтров, а Mini App перезапускает pagination при их смене;
- Dashboard пересобран вокруг быстрого ввода, результата месяца, бюджетного сигнала и трёх
  последних операций; добавлены Mini App разделы счетов, категорий и прогресс/автопереход импорта;
- миграция `0013_settings_version` и `PUT /api/v1/settings/timezone` добавили optimistic numeric version
  для owner timezone; Mini App показывает базовую валюту read-only и безопасно повторяет
  тот же prepared request после outcome-unknown;
- budget progress рассчитывает integer-only факт, известные регулярные обязательства,
  commitment-only прогноз и безопасный дневной расход по каждой валюте; UI использует
  серверные `on_track`, `watch` и `over`, а не клиентскую эвристику;
- миграция `0014_notifications` добавила tenant-owned opt-in настройки 80/100% бюджета,
  готовой регулярной операции и недельного дайджеста, quiet hours, keyed-digest dedupe, lease recovery и
  bounded retry; отдельные hardened scheduler/delivery jobs не сохраняют финансовые значения или тексты
  в очереди и повторно проверяют allowlist, private chat, preference и owner-owned reference перед Telegram I/O;
- добавлен закрытый multi-user режим: полный `TELEGRAM_ALLOWED_USER_IDS` ограничен 32 canonical ID, пустое значение
  сохраняет singleton primary owner, а каждый разрешённый private actor получает отдельный финансовый ledger без
  shared household, cross-user transfers, RBAC или self-registration;
- immutable Telegram principal теперь проходит через message/callback/outbox paths; actor обязан совпадать с private
  chat, onboarding запрещает rebind, а per-user Mini App menu устанавливается только после committed `/start`/`/menu`;
- Mini App при каждом запуске предъявляет signed `initData` до доверия к cookie, очищает protected cache перед subject
  rebinding и безопасно различает same-subject session reuse и stale cross-subject cookie replacement; удалённый из
  allowlist subject отклоняется API даже с прежней session после restart;
- все 66 SessionCookie operations требуют page-memory `X-Session-Binding`; `Set-Cookie` выдаётся только при создании
  новой successful login session, а failed auth/protected responses и successful server-side logout не удаляют shared
  WebView cookies. `hidden`/любой `pagehide` синхронно убирают tenant UI/cache; resume использует только `/auth/me` и
  не воспроизводит stale `initData`;
- миграция `0012_multitenant_integrity` fail closed проверяет legacy rows и добавляет private chat constraint плюс
  composite ownership references для default account, category parent, audit transaction, recurring/import draft и
  Telegram outbox; recurring materialization выбирает максимум одну due-схему на owner за tick;
- outbox delivery теперь проверяет owner и immutable private chat связанного draft до Telegram network send;
  mismatch fail closed не отправляется и не помечается доставленным, presentation binding повторяет тот же guard;
- добавлены framework-neutral finance/draft/catalog/OCR use cases, deterministic fake ports и PostgreSQL
  repositories/queries для общего application API;
- focused Telegram handlers вынесены в routers/controllers без прямых SQLAlchemy queries и commits; mutation flows
  используют единый `TelegramMutationExecutor` envelope для update claim, owner resolution, business/audit/draft
  mutation и typed response outbox;
- exact `DraftRef` (UUID + expected revision) дополнен owner-scoped Telegram projection guard по
  chat/message/rendered revision, чтобы stale callback отклонялся в той же транзакции до mutation;
- миграция `0005` отделила Telegram presentation от channel-neutral draft; `history_page` и
  `pending_history_page` остаются adapter-only projection/outbox context, а downgrade восстанавливает только binding
  текущей revision;
- image ingress переведён на application use case `ProcessOcrImage`; fixed typed-text precedence finance →
  transaction edit → settings → quick input пропускает следующий controller только по явному `NotApplicable`;
- добавлен disabled-by-default local Ollama adapter и explicit `/ai <text>`: no-proxy/no-redirect bounded request,
  strict structured output, integer-minor conversion и общий review-first draft; active draft остаётся неизменным,
  а tracked success/failure/conflict ответы проходят update claim и durable outbox;
- добавлен local stdio-only MCP v2 adapter с шестью bounded read-only finance/catalog tools, process-fixed owner,
  `READ ONLY REPEATABLE READ` UoW и отдельной production PostgreSQL role без DML/DDL/sequence privileges;
- миграция `0009` добавила review-first recurring schedules/instances и уникальный transaction provenance; bounded
  DB-only runner с advisory locks создаёт только обычные review drafts, учитывает immutable timezone, monthly clamp и
  DST gap/fold, а Telegram/Mini App/HTTP дают owner-scoped CRUD, lifecycle и bounded instance history;
- миграция `0010` добавила owner-scoped ручные источники и append-only версии курсов: точное integer
  `coefficient + scale` хранение без `float`, optimistic/idempotent publish, Telegram read-only entrypoint, Mini App
  management и converted-period report, всегда привязанный к явной неизменяемой версии без inverse/triangulation;
- миграция `0011` добавила owner-scoped staged bank imports и transaction provenance: strict bounded raw CSV,
  integer money, keyed fingerprint/reference digests без raw реквизитов, deterministic bounded reconciliation и
  только явные review-confirm/link/skip через HTTP, Telegram и Mini App;
- миграция `0006` добавила durable CSV job с marker `csv_export:v1`: generated bytes/filename/row count не
  записываются в БД, логи или временные файлы, owner проверяется по точной Telegram user/chat pair, current-state
  export создаётся в памяти с лимитами 10 000 строк/16 MiB и spreadsheet-formula defense;
- документирована честная семантика CSV delivery: это не request-time snapshot, а crash после принятия Telegram file
  до `sent_at` может дать дубликат; downgrade блокируется, пока не удалены все export job rows, включая доставленные;
- добавлено unit и PostgreSQL integration/concurrency покрытие для draft CAS, catalog selection, transaction
  lifecycle, OCR ingress, typed routing и CSV job/migration contracts;
- добавлен первый M2 HTTP-каркас: exact-pinned FastAPI/Uvicorn, versioned OpenAPI, structured fixed errors,
  privacy-safe request events и раздельные live/database+Alembic readiness probes;
- добавлена миграция `0007_http_security_state`: bounded web sessions и HTTP idempotency хранят только keyed digests,
  используют transaction-external repositories, cleanup indexes, fail-closed downgrade и проверенные runtime grants;
- реализован allowlist-bound Telegram Mini App auth: строгая проверка bounded raw `initData`/`auth_date`, retained replay
  denial по verified Telegram hash, одночасовые opaque host-only Secure cookies и double-submit CSRF; нестабильный
  native WebView Origin принимается только как optional bounded metadata, атомарный logout и production bypass
  отсутствуют;
- реализован owner-scoped HTTP finance read API: month-to-date dashboard, bounded period/comparison reports,
  owner-local day/week/month timeseries, transaction detail и active keyset pagination; money передаётся decimal
  strings, cursor owner-bound/HMAC-signed,
  а auth и все запросы одного ответа используют `READ ONLY REPEATABLE READ` snapshot;
- реализован revision-safe owner-scoped HTTP mutation API для draft lifecycle и repeat/edit-draft/delete/restore:
  creation остаётся review-first без direct transaction-create route, public draft projection не раскрывает payload,
  same-key semantic replay возвращает только сохранённый минимальный result, mismatch даёт typed `409`; PostgreSQL
  concurrency tests фиксируют consuming confirm race `201/404`, rollback проигравшего claim и future-schema
  fail-closed behavior;
- добавлен bounded HTTP catalog API: complete account/category reads используют `LIMIT 201` и fail closed при cap 200,
  девять versioned writes возвращают только stored minimal receipts, а capacity invariant действует также для
  Telegram/HTTP draft custom input под общим owner lock;
- добавлен owner-timezone `/reports/today` и offline OpenAPI exporter: контракт строится без secrets/БД/network,
  валидирует все local `$ref` и записывается детерминированно через atomic replace;
- добавлен отдельный hardened FastAPI container: development публикует только loopback port, production оставляет API
  во внутренней сети и передаёт runtime DB, bot token и независимый HTTP key через Compose secrets;
- learned-rule candidates ограничены owner-locked cap 512/513 с fail-closed legacy overflow; frozen dependency audit
  теперь проверяет hashed runtime graph из `uv.lock`, не текущее случайное окружение;
- создан exact-pinned React/Vite/Tailwind/TanStack Query frontend foundation с BrowserRouter, deterministic
  OpenAPI-to-TypeScript generation/drift check, SHA-pinned CI и source-map-free production build;
- реализован authenticated Mini App shell и finance UI: same-origin cookie/CSRF client, mobile-first overview,
  currency-isolated analytics с точным дневным SVG-графиком, history/detail, trash и revision-safe shared draft flow
  без хранения `initData` или финансовой telemetry;
- добавлен allowlist-bound per-chat Mini App menu с fail-closed запретом глобального Main Mini App и ленивым retry
  после первого private `/start`/`/menu`;
- добавлен pinned non-root TLS web edge с immutable assets, BrowserRouter fallback, CSP/security/cache headers,
  same-origin internal API proxy и privacy-safe event-code-only access log;
- FastAPI lifespan выполняет bounded advisory-singleton cleanup expired HTTP security state;
- ранее завершён baseline Task21 release gate: 1838 unit и 146 PostgreSQL/cross-channel integration сценариев без
  skips, Alembic `0011` retained-state guards/roundtrips, dependency audits, production images, edge/TLS privacy smoke
  и encrypted Restic restore drill прошли успешно;
- multi-user/`0012` release-slice дополнительно прошёл scoped static/unit/frontend checks, PostgreSQL 18
  isolation/migration gate, dependency audits, production image build и singleton-first public auth/write-envelope
  smoke без создания финансовых записей;

## [v0.45] — 2026-08-12

Первый публичный релиз Numismat:

- review-first ввод доходов и расходов через Telegram;
- локальный OCR чеков и пакетный разбор банковских скриншотов;
- отчёты, CSV, категории, счета, корзина, restore и audit-based undo;
- persistent drafts, versioned callbacks, idempotent updates и durable response outbox;
- PostgreSQL/Alembic, hardened Docker Compose и encrypted Restic backup/restore;
- публичная документация, contribution guide и оригинальный hero-баннер.

[v0.45]: https://github.com/leputain/numismat/releases/tag/v0.45
