# Finbot specification

## Product

Finbot — русскоязычный single-owner Telegram-бот для личного учёта доходов и расходов. Он работает одним процессом
через long polling, использует PostgreSQL как единственный источник истины и обслуживает только configured numeric
owner ID в личном чате.

Defaults: `ru_RU`, `RUB`, `Europe/Moscow`, счёт `Основная карта`. В настройках доступны основной счёт, timezone и
управление справочниками. Режима сохранения без проверки нет: legacy-колонка `fast_mode`, пока она существует для
совместимости схемы, не является пользовательской функцией.

## Ввод и единый review contract

Быстрый ввод поддерживает пробелы и decimal comma/point, `сегодня`, `вчера`, `DD.MM[.YYYY]`, `@account`, `#category`
и deterministic categorization.

- отсутствие знака — расход;
- `+` — доход;
- `-` — расход;
- сумма в payload и БД всегда положительна;
- `@наличные` и `#рестораны` задают однословные значения;
- `@"Карта Мир"` и `#"Кафе и рестораны"` задают многословные значения;
- malformed/duplicate markers, неизвестные explicit objects, неоднозначная сумма или подозрительная дата требуют
  исправления, выбора либо дополнительного подтверждения.

Быстрый ввод, guided wizard и «Повторить сегодня» обязаны завершаться одним экраном review. До нажатия «Сохранить»
transaction row не создаётся. На review редактируются все поля: тип, сумма, категория, счёт, дата и комментарий.
Wizard сохраняет каждый шаг в PostgreSQL и предоставляет «Назад» без потери уже введённых upstream-данных.

Фото и JPEG/PNG/WebP-документы до 10 MiB принимаются как локальный OCR-ввод. Адаптер проверяет фактический
формат, pixel/edge limits, нормализует изображение в памяти и запускает Tesseract `rus+eng` без сети и временных
файлов. Детерминированный parser разбирает фискальный чек как одну операцию с итоговой суммой. На банковском скриншоте он может выделить до 20
сильных строк-кандидатов с явным знаком и денежной дробной суммой. Валюта, повреждённая или пропущенная OCR, заменяется валютой выбранного счёта. Любая распознанная в строке или заголовке
валюта обязана совпадать с ней. Порядок и одинаковые суммы сохраняются. Попытка пакетного разбора с частично неоднозначными строками, смешанной/чужой валютой или превышением лимита завершается без очереди и требует ручного ввода.

Одиночный OCR-результат создаёт обычный review draft. Пакетный результат создаёт в черновике нормализованную
очередь, но не строки транзакций. Каждая операция, включая последнюю, показывается в review и требует отдельного «Сохранить» или «Пропустить»;
массового сохранения нет. Отмена удаляет текущую и оставшуюся очередь, но не удаляет уже сохранённые операции. Исходное изображение и сырой OCR-текст не сохраняются.

«Повторить сегодня» копирует тип, сумму, категорию, счёт и комментарий существующей операции, заменяет дату текущим
локальным временем владельца и создаёт новый review draft. Исходная операция не изменяется.

## Категоризация и explicit learning

Сначала применяются сохранённые explicit rules, затем встроенные deterministic keywords, затем fallback/уточнение.
Rule сопоставляется только с целыми последовательностями нормализованных токенов; NFKC, casefold и равенство `ё/е`
не должны превращать частичное слово в совпадение.

После исправления категории на review бот может предложить до четырёх фраз, реально присутствующих в описании.
Владелец отдельно выбирает область правила: все счета или текущий счёт. Простое изменение категории, сохранение
операции или статистика прошлых действий не создают правило автоматически. Для подходящих правил приоритет имеют
scope текущего счёта, более длинная фраза и более новая версия в детерминированном порядке.

Правило привязано к владельцу, типу операции и активной категории; account-scoped rule также привязан к счёту.
Архивная категория не предлагается. Upsert того же normalized pattern обновляет назначение и optimistic version.

## Persistent drafts и callbacks

У владельца может быть один active persistent draft. Попытка начать несовместимый wizard, quick input или repeat не
перезаписывает его молча: новое намерение сохраняется, а UI предлагает продолжить старый, заменить его или оставить.
Read-only действия могут временно скрыть editor, но черновик остаётся resumable.

Draft содержит schema version, monotonic revision, suspended flag и presentation reference. Mutating callback должен
соответствовать владельцу, ожидаемому state/revision и актуальному message presentation; stale/replayed callback
отклоняется без изменения данных. Typed input применяется только к ожидающему его state.

## История, удаление и undo

История имеет пагинацию и карточку с полным редактированием. Удаление всегда требует отдельного подтверждения и
устанавливает `deleted_at`; активные reports/history/export исключают такие строки. Корзина показывает удалённые
операции с пагинацией и позволяет восстановить их по UUID и optimistic version.

`/undo` отменяет последний поддерживаемый owner action только по audit event: create, edit, delete или restore. Audit
event хранит минимальный prior state. Если подходящего события нет, команда завершается без mutation; fallback
«удалить последнюю транзакцию» запрещён.

## Reports и CSV

Required reports: сегодня, текущий месяц, income/expense/итог периода по валютам, расходы по категориям, последние
операции и сопоставимое сравнение с предыдущим периодом. Границы пользовательского дня/месяца рассчитываются в его
timezone как inclusive UTC start и exclusive UTC end.

CSV содержит все активные операции и колонки: тип, точная сумма, валюта, счёт, категория, local ISO 8601 datetime и
описание. Формат: UTF-8 BOM, `;`, decimal comma. Money форматируется целочисленно. Текстовые ячейки, начинающиеся после
пробелов с `=`, `+`, `-` или `@`, экранируются от spreadsheet formula injection.

## Commands

Поддерживаются `/start`, `/help`, `/menu`, `/wizard`, `/today`, `/month`, `/last`, `/undo`, `/export`, `/settings` и
постоянная reply keyboard. В BotFather command menu может быть пустым; ручные slash-команды продолжают работать.

## Data invariants

PostgreSQL — единственный source of truth. UUIDv7 идентифицирует domain records. Transaction amount — `BIGINT > 0`;
валюта — uppercase 3-character code; timezone-aware timestamps сохраняются в UTC. Для money запрещены `float` и
scientific notation; `Decimal` допустим только на input boundary.

Archived account/category нельзя назначить новой операции; category kind обязан совпадать с transaction kind.
Исторические foreign keys сохраняются. Optimistic versions предотвращают lost updates. Partial unique indexes
обеспечивают idempotent transaction creation по Telegram update и уникальность normalized learned rules в scope.
Финансовая mutation и её typed Telegram response outbox фиксируются атомарно; replay сначала доставляет pending
receipt и не повторяет business handler. Доставка receipt at-least-once, поскольку Telegram API не предоставляет
idempotency key.

Persistent tables: users, accounts, categories, transactions, drafts, category rules, processed updates, audit events
и typed Telegram response outbox. Raw Telegram updates после обработки не сохраняются.

## Layers

`domain` и `application` не импортируют aiogram, SQLAlchemy или `finbot.adapters`. Application объявляет DTO/ports и
политики; PostgreSQL и Telegram реализуют adapters. Telegram handlers остаются thin и не владеют domain invariants,
SQLAlchemy queries или транзакционными правилами.

## Reliability, privacy and operations

Polling обрабатывает один update за раз, увеличивает Telegram offset только после успеха и повторяет failed update с
bounded backoff. Owner/private authorization выполняется до handlers. Idempotency не допускает повторного business
write для одного `update_id`; durable outbox закрывает crash gap между финансовым commit и receipt.

JSON logs являются allowlist API: разрешены только безопасные event codes и bounded metadata. Free-form messages,
exception text, traceback, SQL, Telegram IDs/payloads, суммы, описания, категории, account names, token, credentials и
URLs не сериализуются.

Production работает non-root с read-only rootfs, tmpfs, dropped capabilities, no-new-privileges, без Docker socket и
без published PostgreSQL port. Runtime и migrations используют разные database credentials.

Integration tests всегда запускаются в отдельном Compose окружении и в реально подключённой БД с suffix `_test`;
отсутствующий `TEST_DATABASE_URL`, неверное имя или любой skipped integration test являются ошибкой.

Backup workflow использует hardened ops-container: verified `pg_dump -Fc` во временном tmpfs, encrypted Restic
snapshot, retention, freshness и repository check. Restore drill допускает только isolated `_test` database, затем
выполняет Alembic upgrade и application healthcheck. Required commands and secrets описаны в `README.md`.

## Fixed stack

CPython 3.14.7, uv 0.12.2, aiogram 3.30.0, Pydantic 2.13.4, pydantic-settings 2.14.2, SQLAlchemy 2.0.51, Alembic
1.19.0, Psycopg 3.3.4 и PostgreSQL 18.4. Используется один frozen resolver/lockfile; pre-release dependencies и
параллельные Python package managers запрещены.

## Non-goals

Нет AI parser/OpenAI API, voice, прямой банковской/API-интеграции, инвестиций, семейного доступа, Mini App, FastAPI, Redis, Celery, Kafka, Prometheus, OpenTelemetry
Collector или Sentry. OCR намеренно является локальным и детерминированным.
