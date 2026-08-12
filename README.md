<div align="center">
  <img src="docs/assets/numismat-hero.png" alt="Numismat — локальный учёт финансов и OCR чеков" width="100%">

  <h1>Numismat</h1>

  <p><strong>Приватный self-hosted Telegram-бот для личного учёта денег</strong></p>
  <p>Быстрый ввод, отчёты, безопасные черновики и локальный OCR чеков — без отправки финансовых данных в AI-сервисы.</p>

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

## Зачем он нужен

| Быстрый учёт | Локальный OCR | Данные под контролем |
|---|---|---|
| Напишите `1450 ресторан` или `+250000 зарплата` — бот соберёт карточку проверки. | Фото чека превращается в одну операцию, банковский список — в очередь до 20 операций. | PostgreSQL, owner-only доступ, локальный Tesseract и никакого внешнего AI для финансовых данных. |

Каждая новая операция сначала становится persistent-черновиком. До явного нажатия **«Сохранить»** строка транзакции
не создаётся. Любое поле можно поправить прямо на экране проверки.

## Возможности

- быстрый текстовый ввод и пошаговый мастер;
- расходы, доходы, категории, несколько счетов и валют;
- фото чеков и банковские скриншоты через локальный Tesseract `rus+eng`;
- последовательная проверка, сохранение или пропуск каждой OCR-операции;
- детерминированные правила категоризации, создаваемые только после явного согласия;
- отчёты за день и месяц, сравнение периодов и расходы по категориям;
- история, полное редактирование, повтор операции на текущую дату;
- подтверждение удаления, корзина, восстановление и audit-based `/undo`;
- CSV в UTF-8 with BOM с защитой от spreadsheet formulas;
- versioned callbacks, optimistic locking, idempotent Telegram updates и durable response outbox;
- encrypted Restic backup и проверяемый restore drill.

## Как выглядит ввод

```text
1450 ресторан                       расход без лишнего синтаксиса
+250000 зарплата                    доход
вчера 3200 бензин @наличные         явный счёт
1450 #рестораны ужин                явная категория
799 кофе @"Карта Мир" #"Кафе"      многословные названия
```

```mermaid
flowchart LR
    A[Текст или изображение] --> B[Persistent draft]
    B --> C{Проверка владельцем}
    C -->|исправить| B
    C -->|сохранить| D[(PostgreSQL)]
    C -->|пропустить / отменить| E[Без записи]
```

Сумма хранится в integer minor units: `1450,50 RUB` превращается в `145050`. `float` для денег не используется.
Знак задаёт тип операции, но не входит в сумму: без знака и с `-` — расход, с `+` — доход.

## OCR: чек или список операций

Numismat принимает Telegram photo и JPEG/PNG/WebP-документы до 10 MiB, 20 мегапикселей и 5000 px по стороне.
Формат проверяется по содержимому, изображение нормализуется в памяти и передаётся локальному Tesseract через stdin.

- фискальный чек разбирается как одна операция с итоговой суммой;
- банковский список может дать до 20 строк с явным знаком и денежной дробной суммой;
- одинаковые суммы и порядок строк сохраняются;
- распознанная валюта обязана совпадать с валютой выбранного счёта;
- каждая найденная операция показывается отдельно: **сохранить**, **исправить** или **пропустить**;
- если весь список нельзя разделить надёжно, бот отклоняет пакет целиком — молчаливого частичного импорта нет.

Исходное изображение и сырой OCR-текст существуют только в памяти процесса: они не записываются в PostgreSQL,
файлы или логи и не отправляются внешним AI-провайдерам.

## Быстрый старт

### Docker Compose — рекомендуемый путь

Требуются Docker Engine, Docker Compose v2, Telegram bot token от `@BotFather` и числовой Telegram ID владельца.

```bash
git clone https://github.com/leputain/numismat.git
cd numismat
cp .env.example .env
```

Заполните `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=replace-me
OWNER_TELEGRAM_USER_ID=123456789
DATABASE_URL=postgresql+psycopg://finbot:finbot-dev-only@db:5432/finbot
```

Запустите:

```bash
make dev-up
docker compose ps
docker compose logs -f bot
```

PostgreSQL разработки публикуется только на `127.0.0.1:55432`. Остановка: `make dev-down`. Long polling допускает
ровно один работающий экземпляр `bot`.

<details>
<summary><strong>Запуск Python непосредственно на хосте</strong></summary>

Требуются CPython 3.14.x, `uv` 0.12.2, Tesseract 5 и языковые пакеты `rus+eng`.

```bash
uv sync --frozen
docker compose up -d db
export DATABASE_URL=postgresql+psycopg://finbot:finbot-dev-only@127.0.0.1:55432/finbot
uv run alembic upgrade head
PYTHONPATH=src uv run python -m finbot
```

На Debian/Ubuntu установите `tesseract-ocr`, `tesseract-ocr-rus` и `tesseract-ocr-eng`. Без них остальные функции
останутся доступны, но OCR вернёт безопасную ошибку.

</details>

## Архитектура

Проект следует ports-and-adapters: домен и application-слой не импортируют Telegram, SQLAlchemy или конкретную БД.
Framework-код остаётся на границе, а деньги, инварианты и политики — внутри приложения.

> [!NOTE]
> Границы core проверяются архитектурными тестами, но Telegram composition root пока сосредоточен в крупном
> `bootstrap.py`. Разделение его на небольшие routers/controllers и application use cases остаётся открытым
> техническим долгом и отмечено в [PLAN.md](PLAN.md).

```text
src/finbot/
├── domain/                 деньги, даты, операции, category rules
├── application/            DTO, порты, policies и interaction codec
└── adapters/
    ├── database/           PostgreSQL + SQLAlchemy repositories
    ├── ocr/                image validation + Tesseract adapter
    └── telegram/           polling, middleware, presenters и UI

migrations/                 Alembic revisions
tests/unit/                 быстрые изолированные проверки
tests/integration/          disposable PostgreSQL + Telegram/OCR E2E
```

Polling обрабатывает updates последовательно и двигает offset только после успешного handler. Бизнес-изменение,
claim update и typed Telegram response записываются атомарно; pending response доставляется при replay. Внешняя
доставка остаётся честно at-least-once в узком окне между принятием ответа Telegram и отметкой `sent_at`.

## Безопасность и приватность

- авторизация проверяет одновременно числовой `OWNER_TELEGRAM_USER_ID` и тип чата `private`;
- username не участвует в доступе;
- удаление мягкое и двухэтапное, `/undo` работает только по существующему audit event;
- старые callback-кнопки не могут изменить новый draft: используются UUID, revision и presentation reference;
- JSON-логи принимают только allowlisted event codes и не содержат Telegram ID, суммы, описания, message text, token
  или database URL;
- production bot запускается от non-root UID с read-only rootfs, `cap_drop: ALL`, `no-new-privileges` и `tmpfs`.

Telegram Bot API не является end-to-end encrypted Secret Chat. Не отправляйте номера карт, CVV, банковские пароли,
коды подтверждения и документы. Полная модель угроз и правила disclosure описаны в [SECURITY.md](SECURITY.md).

## Разработка

```text
make setup          frozen dependency sync
make format         Ruff formatter
make lint           Ruff lint
make typecheck      mypy strict
make test           unit tests
make integration    isolated PostgreSQL + Telegram/OCR E2E
make compose-config validate all Compose files
make ops-test       synthetic encrypted backup/restore
make check          полный quality gate
```

`make integration` создаёт отдельный Compose project и PostgreSQL с именем, заканчивающимся на `_test`, применяет
и проверяет миграции, запрещает skips, затем полностью удаляет окружение. Локальная и production базы не используются.

Перед отправкой изменений выполните:

```bash
uv sync --frozen
make check
docker compose config --quiet
```

Правила внесения изменений находятся в [CONTRIBUTING.md](CONTRIBUTING.md).

<details>
<summary><strong>Production Compose и least-privilege runtime role</strong></summary>

Production PostgreSQL не публикуется на host. Для запуска создайте локальный каталог `secrets/`, исключённый из Git:

- `telegram_bot_token`;
- `postgres_password`;
- `database_url_migrations` — URL migration-role/владельца объектов;
- `database_url_runtime` — URL отдельной runtime-role без DDL-прав.

В production `.env` задайте `OWNER_TELEGRAM_USER_ID`. Оба URL должны вести на один PostgreSQL host, port и database,
но использовать разные роли. Migration-role должна владеть БД/объектами и иметь `CREATEROLE`; runtime-role не должна
иметь `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION` или `BYPASSRLS`.

```bash
docker compose -f compose.production.yaml config --quiet
docker compose -f compose.production.yaml up -d db
docker compose -f compose.production.yaml stop bot
docker compose -f compose.production.yaml run --rm migrate
docker compose -f compose.production.yaml run --rm provision-runtime
docker compose -f compose.production.yaml up -d bot
docker compose -f compose.production.yaml ps
```

Для нового volume `deploy/postgres/001-runtime-boundary.sql` отзывает опасные `PUBLIC` ACL при `initdb`. На
существующем/shared кластере сначала примените hardening вместе с DBA; provisioning намеренно fail-closed и не меняет
глобальные права чужой базы автоматически. Детали и необходимые DBA-команды — в [SECURITY.md](SECURITY.md).

</details>

<details>
<summary><strong>Зашифрованные backup и restore drill</strong></summary>

Ops workflow запускается в отдельном hardened container, создаёт проверяемый `pg_dump -Fc`, сохраняет его в
зашифрованный Restic repository, применяет retention `14 daily / 8 weekly / 12 monthly` и удаляет временный dump.

Создайте в `secrets/`:

- `restic_repository` и `restic_password`;
- `backup_pgpass`;
- `restore_postgres_password`, `restore_pgpass`, `restore_database_url`.

```bash
make restic-init     # один раз
make backup
make backup-age
make restic-check
make restore-drill   # isolated finbot_restore_test + Alembic + healthcheck
```

Для end-to-end проверки без production credentials используйте `make ops-test`. Локальный named volume защищает от
сбоя БД, но не от потери Docker host; для disaster recovery храните Restic repository на независимом носителе.

</details>

## Документация

- [SPEC.md](SPEC.md) — продуктовый контракт и UX;
- [SECURITY.md](SECURITY.md) — threat model, секреты, логирование и эксплуатационные границы;
- [DECISIONS.md](DECISIONS.md) — архитектурные решения;
- [PLAN.md](PLAN.md) — фактическое состояние и дальнейшие шаги;
- [AGENTS.md](AGENTS.md) — инженерные ограничения проекта.
- [CHANGELOG.md](CHANGELOG.md) — история публичных версий.

## Ограничения

- один владелец и только private chat;
- интерфейс и parser ориентированы на русский язык;
- OCR намеренно консервативен: неоднозначное изображение лучше отклонить, чем сохранить неверную сумму;
- нет web-панели, совместного бюджета, инвестиций и банковской синхронизации;
- production readiness зависит от вашей настройки Telegram, PostgreSQL, secrets и внешнего Restic storage.

## Лицензия

Код распространяется по лицензии [Apache License 2.0](LICENSE).
