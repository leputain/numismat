# Architecture decisions

- PostgreSQL вместо SQLite: нужны durable drafts, транзакционная идемпотентность, row locks, partial indexes и
  production parity.
- Long polling запускается ровно в одном экземпляре. Updates dispatch-ятся последовательно; offset подтверждает
  только успешно завершённый update, а failure блокирует последующие до retry или shutdown.
- Финансовый результат и typed Telegram response outbox фиксируются одной DB-транзакцией. Replay сначала доставляет
  pending response и не повторяет mutation. Семантика receipt at-least-once: Telegram API не имеет idempotency key,
  поэтому crash между успешным API-вызовом и `sent_at` может дать повторную квитанцию, но не повторную операцию.
- Redis отсутствует: persistent drafts хранятся в PostgreSQL.
- Money хранится в integer minor units. `float` запрещён; `Decimal` используется только для разбора на input boundary.
- UUIDv7 создаётся через `uuid.uuid7()` на Python 3.14.
- `domain` и `application` не зависят от aiogram, SQLAlchemy или adapters. Framework-specific queries/repositories
  находятся в adapter layer; handlers оркестрируют use cases и presentation.
- `bootstrap.py` остаётся composition root. Выделенные Telegram routers и controllers не выполняют прямые SQLAlchemy
  queries, `commit` или business persistence: router ограниченно декодирует transport input и выбирает controller,
  controller вызывает application use case через внедрённые ports/UoW, а post-commit delivery остаётся outer adapter.
- Детерминированный `TransactionParser` остаётся default и не имеет AI fallback. Optional local Ollama реализует
  отдельный async suggestion port и вызывается только через `/ai`; network call завершается до DB mutation UoW.
  Exact-schema output считается недоверенным и может создать лишь общий review draft. Active draft не заменяется и
  не получает hidden intent; provider/model/URL/prompt/output/finance values не попадают в логи. External/cloud AI
  остаётся вне financial-data path.
- Первый MCP milestone является отдельным local stdio read adapter, а не новым application API и не сетевым
  сервисом. Он переиспользует bounded finance/catalog query use cases, фиксирует configured owner внутри процесса и
  выполняет каждый tool в `READ ONLY REPEATABLE READ` UoW. Production MCP получает отдельный SELECT-only DB role;
  mutation tools, arbitrary SQL, owner/tool credentials и protocol/result logging запрещены.
- Telegram/HTTP доступ расширяется не открытой регистрацией, а полным bounded `TELEGRAM_ALLOWED_USER_IDS` максимум
  из 32 numeric ID. Пустая переменная сохраняет singleton `OWNER_TELEGRAM_USER_ID`; primary обязан входить в явный
  список и остаётся process-fixed MCP/rollback principal, но не получает прав читать чужие ledgers. Каждый разрешённый
  subject является отдельным tenant; shared household, cross-user transfers и RBAC сознательно не входят в scope.
- OCR реализован application use case `ProcessOcrImage`, отдельным port и локальным Tesseract 5 adapter (`rus+eng`).
  Telegram image router получает bounded bytes, после idempotency claim controller передаёт их в use case. Pillow
  проверяет и нормализует недоверенное JPEG/PNG/WebP в памяти; Tesseract получает PNG через stdin с timeout.
  Изображение и сырой текст не сохраняются. Фискальный чек остаётся одной review-операцией с итогом. Банковский
  список может дать до 20 сильных строк-кандидатов с явным знаком и денежной дробной суммой. Повреждённая валюта
  заменяется валютой счёта; распознанная чужая или смешанная валюта отклоняет весь batch. Строки хранятся как
  нормализованная draft-очередь и проходят review последовательно, без bulk-save. Каждый save/skip, включая
  последний, — явное действие владельца. Неполный или неоднозначный batch отвергается целиком, а cancel удаляет
  очередь, но не разворачивает уже сохранённые transactions.
- Bank CSV import — отдельный staged workflow, а не OCR-очередь и не direct bank integration. Strict
  bounded parsing и keyed digesting завершаются до mutation UoW; в PostgreSQL попадают только normalized
  finance fields и 32-byte fingerprint/reference digests. Каждая строка требует явного review-confirm,
  link или skip; deterministic reconciliation ничего не связывает автоматически. Точный transaction
  provenance `source=bank_import/import_row_id` фиксируется в одной owner-locked transaction с confirm.
- `BANK_IMPORT_SECURITY_KEY` — отдельный privacy/deduplication data-key, не HTTP master key. Сырые bank
  references намеренно не хранятся, поэтому transparent rekey невозможен. Rotation начинает новую
  dedupe epoch: будущие импорты защищены новым ключом, но cross-epoch duplicates могут не распознаться.
- Для быстрого ввода отсутствие знака и `-` означают расход, `+` — доход. Знак не хранится в amount; amount всегда
  положительный. Многословные `@account` и `#category` требуют кавычек, чтобы границы значения были однозначны.
- Typed text ingress имеет один fail-closed порядок: finance draft, transaction edit, settings draft, затем quick
  input. Переход к следующему controller разрешён только по его явному `NotApplicable`; validation/application error
  или уже обработанный update завершает dispatch и не может случайно превратиться в quick transaction intent.
- Быстрое сохранение без review удалено из UX. Quick input, wizard и repeat создают persistent draft и требуют
  отдельного «Сохранить»; legacy `fast_mode` может временно оставаться только как schema compatibility detail.
- Review является единым editor для типа, суммы, категории, счёта, даты и комментария. Telegram UI редактирует одно
  сообщение, где возможно; typed answers после обработки удаляются, чтобы личный чат оставался компактным.
- Сохранённые category rules создаются только явным выбором владельца после исправления категории. Автоматическое
  обучение по истории запрещено. Account-scoped rule приоритетнее global; затем сравниваются длина фразы и версия.
- У владельца один active draft. Конфликт нового намерения разрешается явно через resume/replace/keep; revision,
  suspended flag и presentation reference нужны для отклонения stale UI без потери черновика.
- Любая application mutation черновика принимает точный `DraftRef` — UUID и ожидаемую revision. Telegram callback
  дополнительно сверяет owner-scoped projection с исходными chat/message и rendered revision под теми же locks;
  несовпадение отклоняется до business mutation.
- Application draft не содержит Telegram chat/message identifiers. Актуальная Telegram binding хранится в отдельной
  `telegram_draft_presentations` projection и обновляется только для совпадающих draft UUID/revision. Введённые
  миграцией `0005` `history_page`/`pending_history_page` принадлежат только Telegram projection/outbox context и не
  входят в shared draft DTO/payload. Legacy binding временно сохранена как rolling-deploy/downgrade compatibility
  boundary; downgrade возвращает в неё только projection, совпадающую с текущей revision.
- Telegram mutation controllers используют одну внешнюю SQLAlchemy transaction через `TelegramMutationExecutor`:
  update claim, owner lock, application mutation, audit/draft changes и response outbox либо фиксируются вместе,
  либо полностью откатываются. Telegram network I/O не выполняется внутри этой транзакции.
- Перед любым Telegram network I/O outbox delivery проверяет, что связанный draft принадлежит записанному owner и
  его immutable private chat. Mismatch fail closed останавливает отправку до Telegram API и не ставит `sent_at`;
  presentation binder повторяет owner/chat guard. Это прикладной defense-in-depth поверх owner-scoped outbox FK.
- HTTP ingress использует собственный idempotency contract и не переиспользует Telegram `update_id`. Alembic `0007`
  хранит только keyed digests, bounded status/result references и session expiry; repositories участвуют во внешней
  транзакции вместе с business mutation и не коммитят самостоятельно. Закрытый `HttpRevisionMutationService` не
  предоставляет generic/direct transaction create: новая и повторяемая операция проходят shared review draft.
  Replay строится только из сохранённых status/result kind/UUID/revision без перечитывания изменившейся сущности;
  несовместимая семантика того же key возвращает typed `409`. Same-key confirm даёт точный `201/201` replay, а
  different-key race после consume — `201/404`. Будущая draft schema видна как `unsupported`, и старый HTTP API её
  не изменяет и не удаляет. M2 FastAPI process существует как отдельный ASGI entry point; routes используют общий
  application API.
- Telegram Mini App auth проверяет HMAC всего bounded raw `initData` до разбора доверенной identity, принимает только
  allowlisted subject и окно `auth_date` 5 минут с 30-секундным future skew. Frontend предъявляет signed `initData`
  при каждом launch до доверия к cookie и очищает protected query state перед rebinding. Активная same-subject session
  может быть сохранена без повторного consume proof; stale cross-subject cookie отзывается только после fresh proof
  claim. Replay key выводится из уже проверенного Telegram hash, поэтому перестановка параметров и эквивалентное
  percent-encoding не обходят защиту; иной replay удерживается до конца TTL + skew и не освобождается logout.
- Session и CSRF — независимые 256-bit opaque tokens. В БД попадают только domain-separated keyed digests; cookie
  host-only, Secure и SameSite=Strict, session cookie дополнительно HttpOnly. Logout и protected mutation endpoints
  авторизуются session + page binding + double-submit CSRF; один bounded ASCII Origin принимается как optional transport metadata,
  потому что native Telegram WebViews не гарантируют portable Origin. Duplicate, empty, oversized и non-ASCII Origin
  fail closed, но само значение Origin не предоставляет authority. Каждая session read/write/logout повторно
  проверяет membership сохранённого Telegram subject в process-local allowlist, поэтому отзыв ID действует и на
  незавершившиеся cookies после restart API. Shared session lock живёт в той же transaction, что mutation; logout
  использует exclusive lock. `Set-Cookie` формируется только после commit нового successful login; retained
  same-subject auth не вращает cookies. Failed auth, любой protected error и successful logout никогда не удаляют
  ambient cookies. Logout отзывает session server-side и отвечает `204` без `Set-Cookie`; frontend очищает binding/UI,
  а невалидная cookie безопасно перезаписывается следующим successful signed login.
- Cookie jar нативного WebView является общей transport-механикой, а не достаточной page/tenant binding. Каждый
  успешный auth response возвращает `X-Session-Binding = HMAC(key, domain || raw_session_token)` без tenant ID;
  страница хранит значение только в памяти и предъявляет его вместе с cookie на каждом protected GET/write/logout.
  Backend пересчитывает binding до owner lookup; mutation всё ещё требует отдельный double-submit CSRF. Если страница
  A отправляет свой binding после того, как общая cookie уже заменена сессией B, ответ — `401` без очистки cookies B.
  Монотонная auth-attempt epoch, synchronous protected-view/query-client teardown при `hidden`/любом `pagehide` и
  state-sensitive binding invalidation не позволяют позднему async result или cached DOM восстановить tenant. Resume
  уже authenticated страницы делает только `/auth/me` с сохранённой в памяти парой cookie+binding и никогда не
  повторяет stale `initData`; suspend во время in-flight auth очищает binding и требует reopen.
- HTTP finance read model не дублирует доменную модель: FastAPI schemas отображают shared query DTO, а SQL adapters
  исполняют auth и составной read в одной `READ ONLY REPEATABLE READ` transaction. Minor units сериализуются decimal
  strings из-за ограничений JavaScript Number; валюты, категории, recent/report rows и page size fail-closed bounded.
- Transaction list использует live keyset `(occurred_at DESC, id DESC)` и owner/domain-bound full HMAC cursor. Cursor
  считается opaque API token и защищён от подделки, но не зашифрован: timestamp/UUID уже были показаны клиенту.
  Гарантируется стабильный порядок и повтор на неизменном наборе, не snapshot traversal при concurrent edit; после
  любой mutation frontend сбрасывает cursor и начинает список заново.
- HTTP catalog read model является complete-but-bounded: database adapter запрашивает `cap + 1`, а application либо
  возвращает весь набор до 200 элементов, либо fail closed с `catalog_unavailable`. Silent truncation запрещён. Все
  destination-growing writers используют один owner lock и тот же capacity probe, включая custom draft input.
- FastAPI `app.openapi()` — единственный backend contract source. Offline exporter собирает полный injected surface
  без runtime settings/БД, проверяет local JSON Pointers и атомарно записывает canonical JSON; generated clients не
  должны поддерживать параллельные hand-written DTO.
- Mini App запускается только через per-chat `MenuButtonWebApp` каждого allowlisted principal после committed
  onboarding `/start`/`/menu`. Default menu принудительно остаётся commands, а обнаруженный
  `getMe.has_main_web_app` останавливает bot startup: глобальный BotFather Main Mini App несовместим с закрытым
  allowlist boundary. Отсутствующий private chat не ломает startup и повторно конфигурируется при следующей команде.
- Production web edge — отдельный pinned multi-stage image: один canonical HTTPS Host, same-origin `/api/*`, immutable
  hashed assets и extensionless BrowserRouter fallback. API состоит только в `data + api-edge`; публичная network
  принадлежит web, поэтому reverse proxy не превращает API container в egress/public boundary.
- Expired HTTP security state очищает FastAPI lifespan task, а не отдельный scheduler stack. Transaction-scoped
  PostgreSQL advisory lock выбирает одного replica, каждый tick ограничен batch/timeout и логирует только event/result.
- Recurring schedules обслуживает отдельный DB-only runner без Celery/cron service dependency. Due materialization и
  draft staging — две independently committed bounded фазы с разными transaction advisory locks; owner lock всегда
  предшествует schedule/instance/draft. Runner создаёт только обычный review draft, а существующий active draft
  оставляет due instance pending с backoff. Timezone schedule неизменяем, DST gap/fold и monthly clamp определены
  доменом; `(schedule_id, occurrence_index)` и transaction provenance уникальны на уровне PostgreSQL. Materialization
  выбирает максимум одну due-схему на owner за tick для tenant fairness. Runner не читает Telegram allowlist, поэтому
  offboarding требует предварительно pause-нуть schedules или остановить runner.
- Alembic `0012_multitenant_integrity` fail closed проверяет legacy данные и вводит private actor/chat constraint плюс
  composite owner foreign keys для default account, category parent, audit transaction, recurring/import draft и
  Telegram outbox. RLS отложен: безопасное введение требует отдельных DB roles/context contracts для pre-auth,
  cleanup, recurring, outbox и MCP, а не механического включения policy поверх общего runtime role.
- Exchange rates моделируются как owner-scoped manual source и append-only version/entries. Значение хранится
  integer `coefficient + scale`, конвертация агрегата использует integer HALF_EVEN. Отчёт обязан получить явный UUID
  версии; latest lookup, inverse и triangulation сознательно запрещены, чтобы результат оставался воспроизводимым и
  отсутствие курса завершалось fail closed. Исходная transaction никогда не конвертируется и не переписывается.
- Explicit category learning сохраняет полный deterministic precedence только внутри bounded namespace: до 512 rules
  на owner/kind читаются целиком, 513-я строка означает fail-closed corruption/legacy overflow. New upsert берёт owner
  lock; silently выбирать кандидата из усечённого набора запрещено.
- Python dependency audit строится из `uv export --frozen --no-dev --no-emit-project` с hashes и запускает
  `pip-audit --no-deps`; аудит текущего mutable environment не считается release evidence.
- «Повторить сегодня» никогда не копирует строку напрямую в transactions: оно создаёт review draft с текущей локальной
  датой, оставляя исходную операцию неизменной.
- Удаление двухэтапное и мягкое. Корзина — отдельная read model удалённых строк; restore проверяет optimistic version.
- Undo допускается только по audit event и хранит минимальный prior state. Эвристический fallback на последнюю
  транзакцию запрещён.
- Report periods используют inclusive UTC start и exclusive UTC end. CSV отображает время в timezone пользователя,
  форматирует money целочисленно и нейтрализует spreadsheet formula prefixes в user-controlled cells.
- CSV export представлен durable outbox job с единственным marker `csv_export:v1`. Generated bytes, filename и row
  count не записываются в БД, логи или временные файлы: после commit delivery повторно разрешает точную пару
  Telegram owner user/chat и формирует export из текущего состояния БД в памяти. Это сознательно не request-time
  snapshot. Export ограничен 10 000 строками и 16 MiB и сохраняет spreadsheet-formula defense. Внешняя отправка
  честно at-least-once: crash после принятия Telegram document, но до `sent_at`, может дать второй файл, не вторую
  business mutation. Downgrade `0006 -> 0005` блокируется, пока не удалены все CSV export job rows, включая уже
  доставленные.
- Accounts и categories используют reversible soft archive. Historical transactions сохраняют foreign keys; основной
  или последний usable account и последнюю категорию типа архивировать нельзя.
- Runtime и migration PostgreSQL URLs — разные production secrets: bot не получает DDL privileges.
- Structured logging — строгий allowlist API, а не redaction произвольного текста. Неизвестные messages/extras,
  traceback text и SQL отбрасываются; разрешены только bounded event metadata.
- Integration suite всегда поднимает disposable Compose PostgreSQL с именем `_test`, применяет migrations и падает
  при skip. Host/live database не является fallback для тестов.
- Backup/restore запускаются только в hardened ops-container. Dump живёт в tmpfs до encrypted Restic snapshot;
  restore drill использует disposable `_test` PostgreSQL, затем migrations и healthcheck. Копирование live volume не
  считается backup.
- Локальный Restic named volume — допустимый первый backend, но не off-host защита. Disaster recovery требует
  независимого repository и регулярно успешного restore drill.
- Pinned stack и `uv.lock` являются частью воспроизводимости; handoff требует `uv sync --frozen`, полного check и
  проверки Compose config.
