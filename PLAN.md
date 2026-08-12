# Plan

Последнее обновление: 2026-08-12.

Статус ниже описывает только состояние файлов этого checkout. Он не является заявлением о live deployment. Итоговый
handoff check выполнен на изолированных test-контейнерах; production deployment не запускался.

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
- [ ] Разделить монолитный `bootstrap.py` на thin routers/controllers и application use cases; убрать прямые
  SQLAlchemy queries из Telegram handlers.
- [ ] Добавить unit tests use cases через fake ports, оставив PostgreSQL-specific semantics integration tests.

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

## Handoff checks

- [x] `uv sync --frozen` на итоговом дереве.
- [x] Ruff format/check и mypy strict на итоговом дереве.
- [x] Unit suite без skips.
- [x] PostgreSQL integration/Telegram/OCR E2E suite без skips.
- [x] Alembic clean upgrade, legacy-data `0002 → 0003`, upgrade до `0004`, downgrade/upgrade и `alembic check` внутри
  isolated test DB.
- [x] `docker compose config` для development, production, integration, ops и ops-test.
- [x] Application Docker build и healthcheck после isolated restore.
- [x] Synthetic encrypted Restic backup/restore `make ops-test` после финальных изменений.
- [x] `pip-audit` без известных уязвимостей; в `uv.lock` нет pre-release версий.
- [x] Source/config/docs scan не нашёл credential patterns; test/export runner проверяет `_test`, ignored legacy dump
  относится к `finbot_test`, production exports не создавались.
