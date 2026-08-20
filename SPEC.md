# Finbot specification

## Product

Finbot — русскоязычный single-owner сервис личного учёта доходов и расходов. Telegram adapter работает отдельным
long-polling процессом, а owner-only FastAPI adapter — отдельным ASGI process над тем же application API и PostgreSQL.
Оба контура обслуживают только configured numeric owner; Telegram дополнительно требует личный чат.

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

### Банковский CSV-импорт и сверка

Mini App принимает raw CSV до 2 MiB, 2000 строк, 32 колонок и 500 символов в поле. V1
требует точный canonical header/profile и явный UTF-8/BOM или Windows-1251; угадывания encoding нет.
Сумма разбирается сразу в integer minor units без `float`. Все строки одного batch обязаны иметь
один normalized bank account reference и валюту выбранного local account; любое нарушение отклоняет
batch целиком. Telegram document ingress использует только UTF-8 и server-authoritative default active account;
другой счёт или encoding выбираются в Mini App.

Загрузка создаёт owner/account-scoped batch и normalized staged rows, но не transactions. Для каждой
строки показывается не более пяти exact owner/account/type/amount/currency candidates в окне ±3 дня со
стабильным рангом. Владелец явно выбирает: создать restricted shared review draft, связать одну
существующую transaction или пропустить. Auto-match, auto-create и bulk confirm запрещены. В import review
разрешены только категория, комментарий, confirm/cancel/back; type, amount, account, date, OCR/rule
и hidden-intent paths fail closed. Отмена batch не откатывает уже confirmed/linked rows.

«Повторить сегодня» копирует тип, сумму, категорию, счёт и комментарий существующей операции, заменяет дату текущим
локальным временем владельца и создаёт новый review draft. Исходная операция не изменяется.

### Регулярные операции

Расписание задаёт daily/weekly/monthly cadence с bounded interval, локальные дату/время, необязательную дату
окончания и неизменяемый snapshot timezone владельца. Месячный recurrence clamp-ит день к концу месяца; ambiguous
DST выбирает первый fold, gap сдвигается к первому существующему локальному времени. Каждая due-точка имеет
уникальный `(schedule, occurrence_index)` instance. DB-only runner работает двумя короткими транзакционными фазами,
использует advisory locks и bounded batches, берёт owner lock до staging и допускает максимум 32 unstaged pending
instances на владельца.

Runner никогда не создаёт transaction: он создаёт обычный `review` draft с `flow=recurring`. Если любой active draft
уже существует, instance остаётся pending и получает bounded backoff — hidden intent, suspend, replace и auto-confirm
запрещены. Confirm связывает новую transaction с instance и `source=recurring`; cancel удаляет draft, а instance
показывается как dismissed. Telegram даёт bounded список/detail и owner-only Mini App link; Mini App предоставляет
CRUD, pause/resume/delete/restore и bounded instance history с skip/retry.

### Опциональное AI-предложение

`/ai <text>` — единственный local-AI ingress. При `LOCAL_AI_ENABLED=false` команда отвечает фиксированным отказом и
не выполняет network call; обычный text input и OCR не меняют поведение. Enabled adapter принимает только exact
local Ollama endpoint, bounded prompt/response и strict structured output. Amount обязан быть decimal string и
разбирается в integer minor units без `float`; дополнительные/неверные поля отклоняют предложение целиком.

Provider вызывается до DB mutation UoW. После валидации suggestion проходит общий account/category resolution и
создаёт `flow=local_ai` draft в `review`, `category_required` или `account_required`. Transaction напрямую не
создаётся. Любой active draft даёт durable fixed conflict receipt без изменения draft payload/revision/suspended
state и без hidden `pending_intent`. V1 не интерпретирует transaction date: date words остаются в description для
явной проверки владельцем.

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

## HTTP API backend

Versioned `/api/v1` использует проверенный Telegram Mini App `initData` только для выдачи одночасовой opaque
cookie-session exact configured owner. HMAC проверяется до разбора identity; signed proof нельзя повторить. Protected
mutation требует host-only session cookie, double-submit CSRF и canonical owner-wide `Idempotency-Key`; один bounded
ASCII Origin, если WebView его передал, остаётся только transport metadata. Session lock, owner lock, idempotency claim,
domain write и completion принадлежат одной транзакции.

Read API содержит dashboard, today/period/comparison reports, bounded owner-local day/week/month timeseries, active
transaction keyset list, owner-scoped detail и complete-but-bounded accounts/categories. Minor units передаются
decimal strings. Multi-query reads используют один
`READ ONLY REPEATABLE READ` snapshot; collection overflow завершается fail closed, а не silent truncation.

Recurring HTTP API использует тот же auth/CSRF/idempotency UoW. Списки schedules/instances ограничены 50 элементами,
а cursors подписаны, owner/filter-bound и opaque. Schedule create/replace/lifecycle возвращает retained minimal
`recurring_schedule` receipt, instance skip/retry — `recurring_instance`; raw schedule payload не логируется.

Exchange-rate HTTP API возвращает не более 32 owner-scoped ручных источников и пагинированные неизменяемые версии.
Публикация требует optimistic `expected_source_version` и возвращает retained minimal `exchange_rate_version`
receipt. Пересчёт периода принимает точный `version_id`; выбор latest на сервере запрещён, исходные transactions не
изменяются. Поддерживаются только явно заданные прямые курсы, без inverse и triangulation.

Draft/transaction writes используют только closed typed review-first actions. HTTP не предоставляет direct transaction
create и не принимает raw draft payload. Same-key replay строится из сохранённого minimal result без повторного чтения
изменившейся domain entity. Catalog writes требуют optimistic version и сохраняют active/archive cap под owner lock,
включая cross-channel custom input.

FastAPI `app.openapi()` является единственным backend contract source. Offline exporter собирает self-contained
canonical JSON без environment, secrets, database или network access; Swagger/ReDoc CDN UI отключены.

## Telegram Mini App UI и запуск

React Mini App использует только same-origin `/api/v1`: raw Telegram `initData` передаётся ровно один раз в auth POST
и не попадает в storage, URL или telemetry. После cookie bootstrap доступны mobile-first overview,
currency-isolated analytics/timeseries, today/month summary, active transaction history/detail, trash и единый
persistent draft review/create/edit/repeat flow. Любая новая transaction
по-прежнему появляется только через явный confirm; Telegram и HTTP разделяют один draft UUID/revision.

Launch button устанавливается Bot API только для exact numeric owner private chat. Default menu не открывает Web App,
а глобальный BotFather Main Mini App запрещён и останавливает startup. Если владелец ещё не открыл bot chat, установка
лениво повторяется на `/start` или `/menu`. Telegram mobile reconnect повторяет mutation только с тем же
`Idempotency-Key`; unknown outcome не создаёт второй draft/transaction.

Production edge принимает единственный HTTPS DNS Host, отдаёт source-map-free SPA и immutable hashed assets,
проксирует `/api/*` и health к internal API без retry/forwarded identity и возвращает fixed error envelope. HTML и SPA
не кешируются. CSP/permissions policy запрещают лишние origins и capabilities; native Telegram mobile/desktop WebView
поддержан, iframe embedding и Telegram Web намеренно fail closed запрещены.

## Commands

Поддерживаются `/start`, `/help`, `/menu`, `/wizard`, `/today`, `/month`, `/last`, `/recurring`, `/rates`, `/undo`, `/export`, `/settings` и
постоянная reply keyboard. В BotFather command menu может быть пустым; ручные slash-команды продолжают работать.

## Data invariants

PostgreSQL — единственный source of truth. UUIDv7 идентифицирует domain records. Transaction amount — `BIGINT > 0`;
валюта — uppercase 3-character code; timezone-aware timestamps сохраняются в UTC. Для money запрещены `float` и
scientific notation; `Decimal` допустим только на input boundary.

Версия курса хранит положительные integer `coefficient` и `scale <= 12`, каноническую ASCII-десятичную строку и
двухзнаковую minor-unit модель. Конвертация агрегата выполняется целочисленно с HALF_EVEN; опубликованные версии и
entries не редактируются и не удаляются application flow.

Archived account/category нельзя назначить новой операции; category kind обязан совпадать с transaction kind.
Исторические foreign keys сохраняются. Optimistic versions предотвращают lost updates. Partial unique indexes
обеспечивают idempotent transaction creation по Telegram update и уникальность normalized learned rules в scope.
Финансовая mutation и её typed Telegram response outbox фиксируются атомарно; replay сначала доставляет pending
receipt и не повторяет business handler. Доставка receipt at-least-once, поскольку Telegram API не предоставляет
idempotency key.

Persistent tables: users, accounts, categories, transactions, channel-neutral drafts, Telegram presentation,
category rules, recurring schedules/instances, processed updates, audit events, typed Telegram response outbox,
bounded web sessions и HTTP
idempotency records. Raw Telegram updates после обработки не сохраняются.

## Layers

`domain` и `application` не импортируют aiogram, FastAPI/Pydantic HTTP, SQLAlchemy или `finbot.adapters`. Application
объявляет DTO/ports и политики; PostgreSQL, Telegram и HTTP реализуют adapters. Telegram/HTTP routers остаются thin и
не владеют domain invariants, SQLAlchemy queries или транзакционными правилами.

## Reliability, privacy and operations

Polling обрабатывает один update за раз, увеличивает Telegram offset только после успеха и повторяет failed update с
bounded backoff. Owner/private authorization выполняется до handlers. Idempotency не допускает повторного business
write для одного `update_id`; durable outbox закрывает crash gap между финансовым commit и receipt.

JSON logs являются allowlist API: разрешены только безопасные event codes и bounded metadata. Free-form messages,
exception text, traceback, SQL, Telegram IDs/payloads, суммы, описания, категории, account names, token, credentials и
URLs не сериализуются.

Production bot, API и web edge работают отдельными non-root containers с read-only rootfs, tmpfs, dropped
capabilities, no-new-privileges, без Docker socket и без published PostgreSQL port. API публикуется только во
внутреннюю `api-edge`; host TCP/443 принадлежит web edge. Runtime и migrations используют разные database credentials.

Integration tests всегда запускаются в отдельном Compose окружении и в реально подключённой БД с suffix `_test`;
отсутствующий `TEST_DATABASE_URL`, неверное имя или любой skipped integration test являются ошибкой.

Backup workflow использует hardened ops-container: verified `pg_dump -Fc` во временном tmpfs, encrypted Restic
snapshot, retention, freshness и repository check. Restore drill допускает только isolated `_test` database, затем
выполняет Alembic upgrade и application healthcheck. Required commands and secrets описаны в `README.md`.

## Fixed stack

CPython 3.14.7, uv 0.12.2, aiogram 3.30.0, FastAPI 0.141.1, Uvicorn 0.52.3, Pydantic 2.13.4,
pydantic-settings 2.14.2, SQLAlchemy 2.0.51, Alembic 1.19.0, Psycopg 3.3.4 и PostgreSQL 18.4. Используется один frozen
Python resolver/lockfile; pre-release dependencies и параллельные Python package managers запрещены.

Frontend foundation: Node 22.22.2, npm 10.9.7, React/React DOM 19.2.8, React Router 8.3.0, Vite 8.2.1,
TypeScript 5.9.3, Tailwind CSS 4.3.3, TanStack Query 5.101.4 и Vitest 4.1.10. Все версии exact-pinned в npm
lockfile; generated API types обязаны совпадать с canonical offline OpenAPI, production source maps запрещены.

## Non-goals

Нет external AI parser/OpenAI API, voice, прямой банковской/API-интеграции, инвестиций, семейного доступа, Redis,
Celery, Kafka, Prometheus, OpenTelemetry Collector или Sentry. Telegram Web iframe не входит в M3; OCR намеренно
является локальным и детерминированным. Optional Ollama не является fallback и доступен только через явную `/ai`.
