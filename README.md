<div align="center">
  <img src="docs/assets/numismat-hero.png" alt="Numismat — локальный учёт финансов и OCR чеков" width="100%">
  <h1>Numismat</h1>
  <p><strong>Приватный self-hosted Telegram-бот для личного учёта денег</strong></p>
  <p>Быстрый ввод, review-first операции, отчёты и локальный OCR — без отправки финансовых данных во внешние AI-сервисы.</p>
  <p>
    <a href="https://github.com/leputain/numismat/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/leputain/numismat?color=CB7B42"></a>
    <a href="https://www.python.org/downloads/release/python-3140/"><img alt="Python 3.14" src="https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white"></a>
    <a href="https://docs.aiogram.dev/"><img alt="aiogram 3" src="https://img.shields.io/badge/aiogram-3-2CA5E0?logo=telegram&logoColor=white"></a>
    <a href="https://www.postgresql.org/"><img alt="PostgreSQL 18" src="https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white"></a>
    <a href="https://docs.docker.com/compose/"><img alt="Docker Compose" src="https://img.shields.io/badge/Docker_Compose-ready-2496ED?logo=docker&logoColor=white"></a>
    <a href="https://github.com/tesseract-ocr/tesseract"><img alt="Tesseract OCR" src="https://img.shields.io/badge/OCR-Tesseract_5-5A45FF"></a>
    <a href="LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-D22128?logo=apache"></a>
  </p>
</div>

> [!IMPORTANT]
> Numismat рассчитан на одного владельца и работает только в его личном Telegram-чате. Это не мультипользовательский
> сервис и не банковское приложение. Внутреннее имя Python-пакета и Docker-сервиса — `finbot`.

## Возможности

- быстрый текстовый ввод и пошаговый мастер с обязательной карточкой проверки;
- расходы, доходы, счета, категории, история, редактирование, корзина, restore и audit-based `/undo`;
- локальный Tesseract `rus+eng`: один чек или очередь до 20 явно проверяемых операций без bulk-save;
- детерминированные правила категоризации только после согласия владельца;
- отчёты за день и месяц, сравнение периодов и расходы по категориям;
- CSV UTF-8 with BOM с защитой от spreadsheet formulas;
- owner-only Telegram Mini App с dashboard, историей и общим review-first draft;
- бюджеты расходов с явным периодом и прогрессом только в собственной валюте, без скрытого FX;
- регулярные daily/weekly/monthly расписания, которые создают только review-черновики, а не операции;
- ручные неизменяемые версии валютных курсов и отчёты, привязанные к явно выбранной версии;
- staged CSV-импорт банка с детерминированной сверкой и явным review/link/skip для каждой строки;
- optimistic versions, exact draft revisions, idempotent updates и durable Telegram response outbox;
- hardened Docker Compose и encrypted Restic backup/restore drill.

Каждая новая операция сначала становится persistent draft. Строка транзакции появляется только после явного
нажатия **«Сохранить»**.

## Пример ввода

```text
1450 ресторан                       расход без лишнего синтаксиса
+250000 зарплата                    доход
вчера 3200 бензин @наличные         явный счёт
1450 #рестораны ужин                явная категория
799 кофе @"Карта Мир" #"Кафе"      многословные названия
```

Сумма хранится в integer minor units: `1450,50 RUB` превращается в `145050`; `float` не используется. Без знака и
с `-` создаётся расход, с `+` — доход. Изображения принимаются как Telegram photo или JPEG/PNG/WebP до 10 MiB,
20 мегапикселей и 5000 px по стороне. Исходное изображение и сырой OCR-текст остаются только в памяти процесса.

## Опциональный локальный AI

Обычный parser и OCR остаются детерминированными. Локальный Ollama вызывается только явной командой
`/ai <описание>` и по умолчанию выключен; автоматического fallback или подмены обычного ввода нет. Ответ модели
считается недоверенным, проверяется по закрытой JSON schema и преобразуется в integer minor units без `float`.
Результат создаёт только общий review draft: транзакция появляется после отдельного «Сохранить».

```dotenv
LOCAL_AI_ENABLED=true
LOCAL_AI_ENDPOINT=http://127.0.0.1:11434
LOCAL_AI_MODEL=qwen3:4b
```

Разрешены ровно два endpoint: loopback выше для запуска Python на host и `http://ollama:11434` для отдельного
контейнера в internal Compose network. Prompt ограничен 1024 символами, ответ — 16 KiB, общий timeout — 8 секунд;
redirect, environment proxy, credentials, streaming и keep-alive модели запрещены. V1 не выводит дату операции:
слова о дате остаются в description и должны быть проверены/исправлены в review.

Base Compose намеренно не скачивает модель и не добавляет непроверенный image. Для production operator должен
предоставить заранее загруженный локальный model volume и daemon с DNS-именем `ollama`, подключённый только к
internal network `local-ai`, без published ports и egress, с `OLLAMA_NO_CLOUD=1`. Bot настраивается через
`LOCAL_AI_ENDPOINT=http://ollama:11434`; произвольный URL или model name с cloud-маркером останавливает startup.

## Локальный read-only MCP

Опциональный MCP adapter работает только через локальный `stdio`: он не открывает TCP port и не принимает owner,
credentials или произвольный SQL в аргументах tools. В первом milestone доступны шесть bounded read-only tools для
dashboard, period report, transaction list/detail и account/category catalogs. Все запросы используют существующие
application use cases в одной `READ ONLY REPEATABLE READ` транзакции; mutation tools отсутствуют.

Для локального клиента процесс запускается командой `make run-mcp`. В production профиль `mcp` требует отдельный
secret `secrets/database_url_mcp`; одноразовый provisioner создаёт и проверяет dedicated `SELECT`-only PostgreSQL
role, а MCP container получает только этот DB secret и configured numeric owner. Аргументы и результаты tools,
finance values, owner ID и protocol payloads не журналируются.

## Быстрый старт

Требуются Docker Engine, Docker Compose v2, Telegram bot token от `@BotFather` и числовой Telegram ID владельца.

```bash
git clone https://github.com/leputain/numismat.git
cd numismat
cp .env.example .env
```

Заполните `.env` безопасными локальными значениями:

```dotenv
TELEGRAM_BOT_TOKEN=replace-me
OWNER_TELEGRAM_USER_ID=123456789
DATABASE_URL=postgresql+psycopg://finbot:finbot-dev-only@db:5432/finbot
MINIAPP_PUBLIC_URL=https://numismat.localhost
HTTP_SECURITY_KEY=replace-with-canonical-32-byte-base64url
BANK_IMPORT_SECURITY_KEY=replace-with-a-different-canonical-32-byte-base64url
```

Сгенерируйте два независимых key: HTTP session/idempotency и bank-import fingerprint. Они не должны
совпадать друг с другом или с bot token:

```bash
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"
```

```bash
make dev-up
docker compose ps
docker compose logs -f bot api
```

PostgreSQL разработки публикуется только на `127.0.0.1:55432`, API — на `127.0.0.1:${API_PUBLISHED_PORT:-8080}`.
Остановка: `make dev-down`. Long polling допускает ровно один работающий экземпляр `bot`.

## Архитектура и надёжность

M0/M1 application foundation завершён: framework-neutral DTO/use cases и PostgreSQL repositories используются
focused Telegram routers/controllers для menu, finance queries, settings/catalog, typed input, transaction lifecycle,
draft interactions, OCR и CSV export. `bootstrap.py` остаётся крупным composition root, но не содержит business
handler bodies, прямых ORM-запросов или ручных commit. M2 начат: добавлен отдельный FastAPI process с versioned
OpenAPI, fixed error envelope и database/Alembic readiness, а Alembic `0007` добавляет bounded web sessions и HTTP
idempotency только с keyed digests. Реализованы owner-only Telegram `initData` auth, одночасовая opaque cookie-session,
exact-Origin/CSRF защита, retained proof replay denial и logout с транзакционной блокировкой.

HTTP read API уже включает month-to-date dashboard, bounded period/comparison reports, owner-scoped transaction
detail и active keyset pagination. Все суммы передаются decimal strings, коллекции имеют жёсткие пределы, а auth и
составной read выполняются в одной `READ ONLY REPEATABLE READ` транзакции. Подписанный cursor нельзя подделать, но
он не зашифрован; после изменения операции клиент должен начать live-pagination заново. Draft/transaction mutations
реализованы owner-only и revision-safe: active/get/create/update/confirm/cancel/resume/replace работают через общий
persistent draft, а repeat/edit-draft/delete/restore требуют optimistic version. Прямого HTTP transaction-create
endpoint нет — новая и повторяемая операция появляется в `transactions` только после явного confirm. Каждая mutation
требует exact Origin, session/CSRF и canonical `Idempotency-Key`; одинаковая семантика возвращает сохранённый
минимальный result, несовместимое повторное использование ключа — typed `409`. HTTP catalog API возвращает полные,
но жёстко ограниченные списки счетов и категорий, а все create/archive/restore paths — включая Telegram draft input —
сохраняют лимит под owner lock. `/reports/today` строит границы по timezone владельца; `make openapi` атомарно создаёт
детерминированный самодостаточный контракт без доступа к БД или secrets. Exact-pinned React/Vite Mini App использует
generated OpenAPI contract, cookie/CSRF shell, общие draft/revision контракты и same-origin production edge.

Alembic `0009` добавляет owner-scoped recurring schedules и уникальные due instances. Отдельный DB-only
`recurring-runner` за один bounded tick материализует максимум 32 due-точки и ставит не более одного review-draft
на владельца; существующий draft оставляет instance pending с backoff. Часовой пояс фиксируется при создании,
месячная дата clamp-ится, DST fold выбирается как `fold=0`, а несуществующее локальное время сдвигается вперёд.
Confirm сохраняет transaction с уникальным provenance `recurring_instance_id`; cancel оставляет instance dismissed.

Alembic `0010` добавляет owner-scoped ручные источники курсов и неизменяемые версии. Курс хранится как целочисленные
`coefficient + scale`, без `float`; обратные и составные курсы не выводятся автоматически. Конвертированный отчёт
всегда принимает явный UUID версии, округляет агрегаты детерминированно по HALF_EVEN и не меняет исходную валюту или
сумму операций. Telegram показывает только опубликованные источники и owner-only ссылку на управление в Mini App.

```text
src/finbot/
├── domain/                 деньги и бизнес-инварианты
├── application/            DTO, ports, queries, policies и use cases
└── adapters/
    ├── database/           PostgreSQL repositories, queries и services
    ├── http/               FastAPI composition, routes, errors и health probes
    ├── ocr/                untrusted-image validation + local Tesseract
    └── telegram/           routers, controllers, executor, delivery и UI
```

`TelegramMutationExecutor` фиксирует update claim, owner resolution, business mutation, audit/draft state и typed
response outbox одной DB-транзакцией. Telegram delivery выполняется после commit и остаётся честно at-least-once:
crash после принятия запроса Telegram, но до `sent_at`, может повторить сообщение или файл, но не business mutation.

Alembic `0005_channel_neutral_drafts` отделяет Telegram binding и navigation context (`history_page` /
`pending_history_page`) от business draft. Compact callback несёт UUID/revision; exact chat/message проверяется по
`telegram_draft_presentations`. Legacy presentation-поля сохранены только для rolling compatibility и downgrade.

Alembic `0006_csv_export_outbox_job` сохраняет в outbox только job marker. CSV строится из текущего owner-scoped
состояния БД при доставке, целиком в памяти, с лимитами 10 000 строк и 16 MiB; bytes, filename и row count в БД не
пишутся. Это не point-in-time snapshot запроса. Downgrade до `0005` fail-closed, пока существуют любые CSV-job rows,
включая доставленные.

## Безопасность

- доступ требует точного numeric `OWNER_TELEGRAM_USER_ID` и `chat.type == private`; username не используется;
- stale callbacks проверяются по UUID/revision и актуальному Telegram projection под lock;
- JSON-логи принимают только allowlisted event codes и не содержат идентификаторы, суммы, descriptions, message/OCR
  text, tokens или database URL;
- production bot, API и web edge работают non-root с read-only rootfs, `cap_drop: ALL`, `no-new-privileges` и
  `tmpfs`; API доступен edge только через отдельную internal Compose-сеть;
- runtime и migration PostgreSQL roles разделены; OCR и CSV не отправляются внешним AI-провайдерам, а optional
  Ollama доступен только по явной `/ai` через literal loopback/internal endpoint.

Telegram Bot API не является end-to-end encrypted Secret Chat. Не отправляйте номера карт, CVV, пароли, коды
подтверждения и документы. Полная модель угроз и production/backup runbook находятся в [SECURITY.md](SECURITY.md).

## Разработка и эксплуатация

```text
make setup          frozen dependency sync
make run-api        локальный FastAPI entry point
make run-recurring-tick  один bounded tick recurring runner
make run-mcp        локальный read-only MCP stdio server
make openapi        deterministic offline OpenAPI export
make lint           Ruff lint
make typecheck      mypy strict
make test           unit tests
make integration    isolated PostgreSQL + Telegram/OCR E2E
make frontend-build production frontend build без source maps
make web-image      production Mini App edge image
make web-secrets-check  TLS key ownership/mode и SAN preflight
make compose-config validate all Compose files
make ops-test       synthetic encrypted backup/restore
make check          полный handoff gate
make release-gate   релизный gate: compose+openapi+edge+health+audit (без полного pytest)
make release-gate-ci цель: быстрый CI-совместимый релизный проход (compose/openapi/frontend-api/frontend-audit)
make release-gate-ci-timed тайминг-версия `release-gate-ci` (каждый шаг c duration)
make release-gate-timed тайминг-версия полного `release-gate` для локальной диагностики
``` 

### Быстрый runbook gate

Если падает `release-gate`, запускать по-очереди:
- `make compose-config` (конфиги/secret placeholders в compose).
- `make openapi-check` (контракт backend vs зафиксированный JSON).
- `make frontend-api-check` (совместимость frontend-схем с контрактом).
- `make web-secrets-check` (проверка secrets/mode/SAN для prod edge).
- `make web-edge-smoke` (проверка edge-прокси и privacy headers).
- `make frontend-audit` (npm audit).
- `make healthcheck` (доступ к DB и alembic head).
- `make audit` (финальный runtime dependency audit).

Типовая быстрый путь диагностики:
```bash
make release-gate-ci   # локально/CI: compose+openapi+frontend API+audit
make release-gate      # полный локальный prod-like gate
make release-gate-ci-timed
make release-gate-timed
make release-gate-ci-timed && gh workflow run release-gate-full.yml # ручной full-prod-like timed run в CI (workflow_dispatch)
```

Итоговый M0/M1 handoff gate пройден: frozen sync, Ruff, mypy, unit/integration, dependency audit, пять Compose
конфигураций, production image build и synthetic encrypted backup/restore. Production deployment не выполнялся.
Production использует `compose.production.yaml`, отдельные runtime/migration secrets, `provision-runtime` и
единственную публичную TLS-точку `web`. Recovery entrypoints: `make restic-init`, `make backup`, `make backup-age`,
`make restic-check`, `make restore-drill`.

## Production Mini App

До запуска нужны внешние prerequisites: DNS A/AAAA для одного lowercase hostname, публично доверенный TLS-сертификат
с этим hostname в SAN, доступный TCP/443 и отключённый в BotFather глобальный **Main Mini App**. Последнее обязательно:
бот проверяет `getMe.has_main_web_app` и не стартует, если Mini App мог бы стать публичным. Владелец должен хотя бы
один раз открыть private chat с ботом; после `/start` или `/menu` бот повторит установку персональной launch-кнопки.

`MINIAPP_PUBLIC_URL` задаётся только как `https://lowercase.dns.name` — без port, path, query, fragment или trailing
dot. Тот же origin используется bot, API и edge; configurable API base URL намеренно отсутствует. Разместите
сертификат и ключ так, чтобы Compose `file:` bind mounts сохранили реальные host permissions:

Production дополнительно требует `secrets/bank_import_security_key`. Храните его вместе с deployment
secrets и backup metadata: замена ключа начинает новую deduplication epoch, а исторические digests нельзя
пересчитать без исходных банковских реквизитов.

```bash
sudo install -d -m 0700 secrets
sudo install -o 101 -g 101 -m 0444 /secure/source/fullchain.pem secrets/miniapp_tls_fullchain.pem
sudo install -o 101 -g 101 -m 0400 /secure/source/private-key.pem secrets/miniapp_tls_private_key.pem
MINIAPP_PUBLIC_URL=https://numismat.example.com make web-secrets-check
docker compose -f compose.production.yaml config --quiet
docker compose -f compose.production.yaml up -d --build
```

`uid/gid/mode` в Compose не подменяют host ownership для `file:` secrets, поэтому preflight обязателен: он проверяет
фактические `101:101:0400`, читаемость и SAN без вывода hostname или certificate data. `web` публикует только TLS
`8443 -> ${MINIAPP_HTTPS_BIND:-0.0.0.0}:${MINIAPP_HTTPS_PORT:-443}`, отдаёт hashed assets с immutable cache,
`index.html`/SPA — с `no-store`, а `/api/*` и два health endpoint проксирует без retry и forwarded identity headers.
Проверка после запуска: `curl --fail --resolve <host>:443:<ip> https://<host>/health/ready`.

Access log edge содержит только `edge_request_completed`, route group, result и status; URI/query, client address,
headers, cookies, bytes и timing не пишутся. Нативные Telegram mobile/desktop WebView являются целевым контуром;
iframe-встраивание, включая Telegram Web, закрыто `frame-ancestors 'none'` и `X-Frame-Options: DENY`.

## Документация

| Документ | Назначение |
|---|---|
| [SPEC.md](SPEC.md) | Продуктовый и UX-контракт |
| [SECURITY.md](SECURITY.md) | Threat model, secrets, production и recovery runbook |
| [DECISIONS.md](DECISIONS.md) | Архитектурные и reliability-решения |
| [PLAN.md](PLAN.md) | Проверяемое текущее состояние и дальнейшие шаги |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Правила разработки и handoff |
| [AGENTS.md](AGENTS.md) | Структурная карта и обязательные ограничения |
| [CHANGELOG.md](CHANGELOG.md) | История публичных и unreleased изменений |

## Лицензия

Код распространяется по лицензии [Apache License 2.0](LICENSE).
