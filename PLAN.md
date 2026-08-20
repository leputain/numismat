# Plan

Последнее обновление: 2026-08-14.

Статус ниже описывает только состояние файлов этого checkout. Он не является заявлением о live deployment.
Итоговый M0/M1 handoff gate выполнен на изолированных test-контейнерах, включая Docker/Compose, dependency audit и
synthetic encrypted backup/restore. Production deployment не выполнялся.

## Стабильная основа

- [x] Pinned CPython/uv/application stack и `uv.lock` находятся в проекте.
- [x] PostgreSQL, Alembic, UUIDv7, integer minor units, timezone-aware timestamps и optimistic transaction versions.
- [x] Owner-only middleware требует точный numeric owner ID и private chat.
- [x] Persistent transactions, drafts, processed updates и минимальные audit events.
- [x] Счета и категории поддерживают создание, переименование, основной счёт, soft archive и restore.
- [x] История, карточки операций, базовые day/month reports и CSV export существуют.

## Reliability и UX — 2026-08-12

- [x] Parser трактует ввод без знака как расход, `+` как доход и `-` как расход; amount остаётся положительным.
- [x] Parser поддерживает quoted многословные `@account` и `#category`, отклоняет пустые/сломанные/дублированные
  markers и не использует substring-match внутри слова.
- [x] Quick input больше не сохраняет transaction напрямую: он создаёт `quick_confirm` draft и показывает review.
- [x] Wizard завершается тем же обязательным review; legacy `fast_mode` скрыт из пользовательских настроек.
- [x] На review доступны изменение типа, суммы, категории, счёта, даты и комментария.
- [x] «Повторить сегодня» создаёт новый review draft с локальной текущей датой и не меняет исходную операцию.
- [x] Удаление разделено на request/confirm; добавлены корзина, просмотр и optimistic restore.
- [x] Undo без подходящего audit event безопасно возвращает «нечего отменять» и не удаляет последнюю legacy-операцию.
- [x] CSV форматирует деньги без `float`, использует timezone владельца и нейтрализует formula prefixes в текстовых
  ячейках.
- [x] Подпись report balance исправлена на «Итог периода».
- [x] Текущий месячный отчёт и сравнение с предыдущим месяцем используют два сопоставимых month-to-date интервала.

## Explicit category learning

- [x] Чистые domain primitives реализуют NFKC/casefold/`ё` normalization, whole-phrase matching, bounded candidates
  и детерминированный precedence.
- [x] Application ports/policies и PostgreSQL repository поддерживают чтение и scope-aware upsert правил.
- [x] Review предлагает правило только после явного исправления автоматически выбранной категории; explicit
  `#category` и обычный выбор без коррекции не считаются обучением.
- [x] Владелец отдельно выбирает global или current-account scope; rule записывается вместе с confirm/save.
- [x] Quick categorization проверяет learned rules до встроенного fallback и игнорирует правила архивных категорий.
- [x] Unit/PostgreSQL/Telegram tests покрывают precedence, scope-aware upsert, atomic rollback/save и последующее
  применение выученного правила вместо встроенной категории.

## Локальный OCR — 2026-08-12

- [x] Telegram принимает photo и JPEG/PNG/WebP document до 10 MiB; фактический формат, 20 MP и 5000 px проверяются.
- [x] Tesseract 5 с `rus+eng` встроен в application/test Docker images и вызывается локально через bounded stdin/stdout.
- [x] Детерминированный parser сохраняет чек одной операцией с итогом и извлекает до 20 сильных строк банковского списка с
  явным знаком и денежной дробной суммой; money parsing не использует `float`.
- [x] Повреждённая/пропущенная OCR валюта заменяется валютой счёта; распознанная чужая или смешанная
  валюта отклоняет весь пакет.
- [x] Неполный или неоднозначный пакет отвергается целиком; частичный молчаливый импорт запрещён.
- [x] Каждая операция, включая последнюю, проходит versioned review и явный save/skip; массового save нет.
- [x] Отмена прекращает очередь и оставляет уже явно сохранённые операции без изменений.
- [x] Изображения и сырой OCR-текст не сохраняются и не попадают в allowlisted JSON logs.
- [x] Unit tests покрывают parsing/limits/spoofed content/errors и batch ordering/completeness; integration
  проверяет реальный русский Tesseract и
  полный Telegram photo → sequential review flow.

## Persistent draft safety

- [x] Alembic `0003_reliability_and_smart_input` добавляет draft schema version/revision/suspended/presentation reference,
  versioned category rules, owner/type foreign keys и reliability indexes.
- [x] Draft writes используют row lock, monotonic revision и могут начать новый flow с новым draft UUID.
- [x] Application interaction codec умеет компактно кодировать draft UUID/revision и проверять stale interaction.
- [x] Wizard и repeat обнаруживают существующий draft и предлагают resume/replace/keep вместо молчаливой перезаписи.
- [x] Wizard/review/conflict/resume callbacks несут draft UUID/revision/presentation reference; legacy и stale
  callbacks отклоняются до мутации, а write/delete повторно проверяют id/revision под row lock.
- [x] Wizard, quick text, repeat и transaction edit используют arbitration; неожиданный typed input не очищает
  текущий draft.
- [x] Read-only переходы приостанавливают draft и показывают единый versioned «Продолжить ввод»; продолженная форма
  сохраняет новый presentation reference и остаётся кликабельной.
- [x] PostgreSQL/Telegram integration покрывает legacy, stale revision, wrong draft ID, suspend/resume и продолжение
  формы после resume.
- [x] Transaction-edit использует draft interaction codec; catalog/settings mutation несут object UUID/version,
  typed rename сохраняет expected version, timezone проверяет state token, а двухсессионные PostgreSQL tests покрывают
  stale identity map, onboarding и catalog namespace races.

## Idempotency, polling и privacy

- [x] Custom polling dispatches updates последовательно и увеличивает Telegram offset только после успешного handler.
- [x] Failed fetch/update повторяется с bounded exponential backoff; graceful shutdown не подтверждает незавершённый
  update.
- [x] `processed_updates` и unique transaction update index обеспечивают replay protection на уровне БД.
- [x] Alembic `0004_telegram_response_outbox` атомарно сохраняет ответы для финансовых, каталожных и settings
  mutations; pending response доставляется до replay handler, а crash/replay E2E проверяет отсутствие повторной
  операции. Внешняя доставка имеет честную at-least-once семантику при crash после принятия ответа Telegram.
- [x] JSON formatter сериализует только allowlisted event codes и bounded metadata; free-form message, args и
  traceback text отбрасываются.
- [x] Safe polling/logging unit tests покрывают retry ordering, shutdown и попытки передать секретные значения.
- [x] Первичный `ensure_user` сериализован transaction advisory lock и атомарно создаёт canonical seed catalog;
  business-mutation receipts закрыты durable outbox. Read-only ответы и незавершённые draft transitions допускают
  безопасный повторный запрос.

## Layers и handlers

- [x] Domain/application imports защищены architecture test от aiogram, SQLAlchemy и adapter dependencies.
- [x] Persistence-neutral report/transaction DTO и command/query ports находятся в application layer; SQLAlchemy
  implementations вынесены в database adapters.
- [x] Telegram registration разделён на focused routers/controllers; `bootstrap.py` оставлен composition root без
  handler decorators, business SQLAlchemy/ORM операций, ручных commit и network-before-commit внутри handlers.
- [x] Application use cases тестируются через deterministic fake ports; PostgreSQL locking, migrations и concurrency
  остаются в integration suite.

## Shared platform foundation — 2026-08-13

- [x] Добавлены framework-neutral typed errors, immutable owner/catalog/draft/transaction/OCR DTO и command/query
  contracts без aiogram, FastAPI, Pydantic HTTP, SQLAlchemy и adapters.
- [x] Реализованы shared queries для transactions, reports, owner settings, accounts/categories и PostgreSQL query
  repository, не возвращающий ORM entities.
- [x] Channel-neutral draft lifecycle проверяет одновременно UUID и revision, сериализует absent-row race owner-lock,
  обновляет preloaded ORM state через `populate_existing` и не раскрывает Telegram presentation metadata.
- [x] Alembic `0005_channel_neutral_drafts` добавляет `telegram_draft_presentations`, revision snapshot и bounded
  `history_page`/`pending_history_page` в adapter projection/outbox; delivery не меняет business draft, а downgrade
  восстанавливает current binding в rolling legacy fields.
- [x] Transaction/catalog command use cases сохраняют optimistic versions, owner scope, review-first confirm,
  soft-delete/restore, audit и explicit staged category-rule semantics.
- [x] Общий OCR queue application flow атомарно подтверждает, пропускает и отменяет элементы; staged rule,
  transaction/audit, следующий draft и Telegram receipt фиксируются одной транзакцией.
- [x] `TelegramMutationExecutor` задаёт единый envelope `claim -> owner -> mutation -> receipt/outbox -> commit`;
  direct delivery при test-only `update_id=None` выполняется только после commit.
- [x] Focused controllers/routers покрывают main menu, finance messages/queries, settings/catalog, typed text input,
  transaction lifecycle/edit, compact draft interactions/conflicts, OCR image/queue, undo и CSV export; architecture
  tests запрещают controllers/routers импортировать database/bootstrap/SQLAlchemy.
- [x] Legacy `ContextVar` callback bridge и двухсессионная prevalidation удалены; compact callback actions проверяют
  exact draft UUID/revision и Telegram chat/message projection внутри executor-owned transaction.
- [x] Telegram image ingress использует общий `ProcessOcrImage`: bounded download остаётся Telegram adapter concern,
  OCR/parse/draft orchestration выполняются application use case и PostgreSQL ports.
- [x] Alembic `0006_csv_export_outbox_job` добавляет privacy-safe durable CSV job: outbox хранит только marker,
  export генерируется в памяти при доставке с bounded limits и не сохраняет финансовый payload/файл.
- [x] M2 начат отдельным exact-pinned FastAPI/Uvicorn process: `/api/v1`, OpenAPI, fixed errors, privacy-safe request
  events и раздельные live/database+Alembic readiness probes.
- [x] Alembic `0007_http_security_state` добавляет bounded web sessions и HTTP idempotency с keyed digests,
  cleanup indexes, атомарными no-commit repositories, guarded downgrade и least-privilege runtime grants.
- [x] Telegram Mini App auth проверяет официальный raw `initData` HMAC, bounded `auth_date` и exact owner, запрещает
  повтор signed proof, выдаёт одночасовую host-only Secure cookie-session, требует session + double-submit CSRF для
  writes и атомарно инвалидирует сессию при logout; Origin из native WebView валидируется как optional bounded metadata.
- [x] HTTP finance reads дают month-to-date dashboard, bounded period/comparison reports, owner-scoped detail и
  signed keyset pagination в одной read-only repeatable transaction; money возвращается decimal strings.
- [x] Revision-safe HTTP draft/transaction mutations реализуют active/get/create/update/confirm/cancel/resume/replace
  и repeat/edit-draft/delete/restore через закрытый typed facade; прямого transaction create без review нет.
- [x] Shared session lock, owner lock, idempotency claim, domain mutation и completion выполняются одной транзакцией.
  Успешный replay использует только сохранённые status/result kind/UUID/revision; semantic mismatch возвращает typed
  `409`, а неуспешная mutation откатывает claim.
- [x] Concurrency coverage фиксирует same-key replay, different-key confirm race `201 + 404`, transaction version race
  `200 + 409`, owner-scoped foreign/missing `404` и fail-closed projection/write behavior для будущей draft schema.
- [x] Owner-scoped HTTP catalog API возвращает complete-but-bounded account/category lists через SQL `LIMIT 201` и
  fail-closed cap 200; девять mutation routes используют общий Origin/CSRF/idempotency UoW и минимальные receipts.
- [x] Все catalog writers, включая Telegram/HTTP draft custom input, проверяют destination capacity под owner lock;
  create/restore/archive не могут перевести API в постоянный overflow, а existing lookup при cap остаётся доступен.
- [x] `/reports/today` вычисляет UTC-границы из локальной даты владельца; offline OpenAPI exporter детерминированно и
  атомарно создаёт self-contained JSON без environment, secrets, database или network dependencies.
- [x] Learned category rules ограничены 512 строками на owner/kind: SQL читает максимум 513 и fail closed при legacy
  overflow, новый upsert сериализован owner lock, а existing upsert и deterministic precedence сохранены.
- [x] Отдельный production API container использует runtime DB/bot/security secrets, internal-only port, non-root,
  read-only rootfs, noexec tmpfs, dropped capabilities и bounded `/health/ready`; dev bind ограничен `127.0.0.1`.
- [x] Dependency audit получает exact frozen runtime graph из `uv.lock` с hashes, отключает повторный resolver и
  fail-fast очищает временный requirements artifact; сетевой audit выполнен в консолидированном финальном gate.
- [x] FastAPI lifespan запускает owner-safe bounded cleanup web-session/idempotency rows: advisory singleton,
  `SKIP LOCKED`, максимум 500+500 строк, 10-секундный tick и privacy-safe event/result log.
- [x] Browser Mini App реализует dashboard, transaction/detail/trash и общий draft review flow; bot устанавливает
  owner-only per-chat launch menu и fail closed запрещает глобальный BotFather Main Mini App.
- [x] Pinned non-root web edge отдаёт source-map-free SPA/immutable assets, same-origin proxy `/api/*`, TLS/CSP/cache
  headers и privacy-safe access log; API остаётся только на internal `api-edge` network.
- [x] Frontend foundation использует exact-pinned React 19/Vite 8/TypeScript 5/Tailwind 4/TanStack Query 5,
  BrowserRouter и generated OpenAPI types; drift блокирует CI/check, production source maps выключены.
- [x] Task22 реализует expense-only budgets: Alembic `0008`, integer minor-unit limits, owner-timezone periods,
  currency-isolated progress без FX, optimistic/idempotent HTTP CRUD, Telegram overview и Mini App management.
  Реальный PostgreSQL и cross-feature contract подтверждены единым Task21 gate.
- [x] Task23 реализует review-first recurring transactions: Alembic `0009`, bounded owner-scoped schedules/instances,
  immutable timezone с monthly clamp и DST policy, двухфазный advisory-locked DB runner, unique transaction provenance,
  signed-cursor HTTP CRUD/lifecycle, Telegram list/detail и Mini App management. Runner не пишет transactions,
  active draft не suspend/replace, а остающийся pending instance получает backoff.
- [x] Task24 реализует explicit exchange rates: Alembic `0010`, owner-scoped immutable manual sources/versions,
  integer coefficient/scale и HALF_EVEN без `float`, optimistic/idempotent publish, signed version pagination,
  Telegram read-only entrypoint и Mini App source/version/publish/converted screens. Отчёт pinned к явной версии,
  не меняет transactions и fail closed запрещает inverse/triangulation; PostgreSQL contract подтверждён Task21.
- [x] Task25 реализует локальный read-only MCP adapter через stdio: owner фиксирован при старте, шесть bounded tools
  переиспользуют application queries в `READ ONLY REPEATABLE READ`, network listener и write tools отсутствуют,
  production использует отдельную SELECT-only PostgreSQL role. Task21 подтвердил provisioning, SELECT и DML denial.
- [x] Task26 реализует optional local Ollama, выключенный по умолчанию и доступный только через `/ai`; provider
  вызывается до mutation UoW, output проходит strict schema/integer validation и создаёт только shared review draft.
  Active draft не меняется и не получает pending intent; tracked outcome фиксируется через update claim/outbox.
- [x] Task27 реализует staged bank CSV import/reconciliation: Alembic `0011`, strict bounded parser без
  `float`, keyed digests без raw банковских реквизитов, owner/account-scoped batches/rows, до пяти
  deterministic reconciliation candidates и только явные create-review/link/skip actions. HTTP raw upload,
  Telegram document ingress и Mini App review сохраняют provenance и не создают transaction без confirm;
  canonical OpenAPI содержит 62 path items. Полный PostgreSQL/cross-channel gate подтверждён Task21.
- [x] Task21 закрыл единый release gate: backend/frontend static и unit, guarded migrations до Alembic `0011`,
  146 PostgreSQL/cross-channel integration сценариев без skips, production images, edge/TLS/privacy smoke,
  frozen Python/npm dependency audits и encrypted Restic backup/restore drill. Production deployment не выполнялся.

## Isolated tests и container operations

- [x] `make integration` запускает отдельный Compose project с disposable PostgreSQL `finbot_test` в tmpfs.
- [x] Test runner требует `TEST_DATABASE_URL`, проверяет configured и connected DB suffix `_test` и падает при skip.
- [x] `Dockerfile.test` использует frozen dependencies, non-root user, read-only container и внутреннюю test network.
- [x] Hardened `Dockerfile.ops` содержит PostgreSQL client и pinned Restic; backup/restore скрипты запрещают host mode.
- [x] Backup workflow: verified temporary `pg_dump -Fc` → encrypted Restic snapshot → retention → удаление dump.
- [x] Добавлены `restic-init`, freshness check, `restic check` и isolated `_test` restore с Alembic + healthcheck.
- [x] `make ops-test` выполняет synthetic end-to-end backup/restore probe без production данных и credentials.
- [x] Systemd timers описывают nightly backup, six-hour freshness check, weekly repository check и restore drill.
- [x] README/SECURITY документируют шесть ops secrets, network/volume/DB overrides и clean-start workflow.
- [x] Production clean-start создаёт отдельную least-privilege runtime DB-role после migrations и fail-closed проверяет
  effective privileges, server identity, DDL denial и запрет mutation `alembic_version`.

## Проверки checkout — 2026-08-14

- [x] `uv sync --frozen` на итоговом дереве.
- [x] Ruff format/check (`447` файлов) и mypy strict (`266` source-файлов) на итоговом дереве.
- [x] Unit suite: `1838 passed`.
- [x] PostgreSQL integration/Telegram/OCR/cross-channel suite: `146 passed`, без skips.
- [x] Alembic clean upgrade до `0011`; retained-state downgrade guards и cleanup для `0011`→`0010`→`0009`→
  `0008`→`0007`, существующие security/export guards и последующие downgrade/upgrade roundtrips; `alembic check`
  не обнаружил новых операций.
- [x] Offline OpenAPI byte-drift check: `62` path items; generated TypeScript, strict frontend typecheck,
  `8` Vitest smoke tests и source-map-free Vite production build.
- [x] `docker compose config` для development, production, integration, ops и ops-test.
- [x] Application и pinned non-root web Docker builds; synthetic TLS edge smoke проверил routing, headers,
  forwarded-header stripping, отсутствие `ETag`/source maps и privacy-safe logs.
- [x] Synthetic encrypted Restic backup/restore `make ops-test` после финальных изменений.
- [x] Frozen hashed runtime graph прошёл `pip-audit`, npm lock прошёл `npm audit --audit-level=high`; известных
  уязвимостей нет.
- [x] Source/config/docs scan не нашёл credential patterns; test/export runner проверяет `_test`, ignored legacy dump
  относится к `finbot_test`, production exports не создавались.
