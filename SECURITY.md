# Security

## Модель угроз

Finbot рассчитан на закрытый operator-managed allowlist до 32 независимых владельцев ledger. В scope входят:
посторонние и удалённые из списка Telegram-пользователи, cross-tenant object/cookie confusion, утечка в group chat,
повторные и переставленные updates/callbacks, устаревшие UI-кнопки, malformed input, компрометация credentials или БД,
утечка чувствительных данных в логи и непроверяемые backup/restore процедуры. Общий семейный ledger, RBAC и
self-registration отсутствуют: один verified Telegram subject всегда соответствует одному изолированному ledger.

Telegram не является end-to-end encrypted Secret Chat для ботов. Не отправляйте Finbot номера карт, CVV, банковские
пароли, коды подтверждения, документы и иные секреты. Перед отправкой банковского скриншота обрежьте номера карты,
баланс и прочие поля, которые не нужны для распознавания операции.

## Доступ и обработка updates

- Каждый update принимается только при membership actor ID в полном `TELEGRAM_ALLOWED_USER_IDS`, условии
  `chat.type == private` и точном `actor_id == chat_id`. Пустой allowlist означает singleton
  `OWNER_TELEGRAM_USER_ID`; primary обязан входить в явно заданный список. Username не является идентификатором
  доступа.
- Middleware создаёт immutable principal из проверенной пары actor/chat. Callback message может принадлежать боту,
  поэтому downstream никогда не выводит пользователя из `message.from_user`: principal передаётся через dispatch
  scope во все mutation/query/outbox context factories.
- Первая committed onboarding-транзакция создаёт отдельного `users` owner и его seed catalog. Private chat binding
  допускает только `NULL -> actor_id`, после чего неизменяем; несовпадение отклоняет всю транзакцию.
- Updates обрабатываются последовательно. Telegram offset увеличивается только после успешного завершения handler;
  ошибка удерживает текущий update для повторной попытки с bounded backoff и не пропускает вперёд последующие.
- `processed_updates` обеспечивает идемпотентность бизнес-обработки. Повтор одного `update_id` не должен создавать
  вторую операцию.
- Финансовое подтверждение и его Telegram receipt записываются одной транзакцией. Pending outbox доставляется до
  повторного запуска handler, поэтому crash после commit не оставляет сохранённую операцию без ответа. Telegram не
  поддерживает idempotency key: crash уже после принятия API-запроса, но до фиксации `sent_at`, может повторить
  квитанцию; бизнес-операция при этом не повторяется.
- Mutating callbacks привязаны к владельцу, UUID объекта и optimistic version. Persistent draft дополнительно имеет
  ревизию и ссылку на актуальное UI-представление; устаревшая кнопка не должна изменять более новое состояние.
- Для выделенных routers/controllers application mutation получает точный `DraftRef` (UUID и ожидаемую revision), а
  Telegram guard дополнительно сверяет owner-scoped projection с исходными chat/message и rendered revision.
  Проверка, business mutation и постановка response в outbox происходят под одним owner/draft/presentation lock и
  одним commit через `TelegramMutationExecutor`. Это закрывает TOCTOU между предварительной проверкой и mutation;
  routers/controllers не выполняют прямые SQL queries или commit, network delivery выполняется только после commit.
- Telegram presentation отделена от business draft. Поздний outbox response не может привязать старую ревизию к
  новому draft. `history_page` и `pending_history_page`, добавленные миграцией `0005`, остаются adapter-only полями
  projection/outbox, а не business payload; при downgrade `0005 -> 0004` только projection текущей ревизии
  возвращается в legacy binding.
- Перед любым Telegram network I/O delivery выполняет owner/private-chat preflight для каждого outbox response со
  связанным draft. Несовпадение `draft -> owner -> immutable private chat` останавливает отправку до Telegram API и не
  устанавливает `sent_at`; presentation binder повторяет тот же guard. Это закрывает cross-tenant UI projection даже
  при forged или legacy outbox reference.
- Typed text маршрутизируется в фиксированном порядке: finance draft, transaction edit, settings draft, quick input.
  Только явный результат `NotApplicable` разрешает следующий controller; validation/application error или duplicate
  update завершает dispatch. Ошибочный ввод состояния не должен молча интерпретироваться как новая операция.
- Все user-controlled строки перед HTML-выводом экранируются.

## Безопасные изменения данных

Любой quick input, мастер и «Повторить сегодня» сначала создают черновик и требуют явного «Сохранить». При конфликте
незавершённый черновик не перезаписывается без выбора владельца. Изменение категории не создаёт learned rule
автоматически: сохранение правила требует отдельного явного действия.

Изображения считаются недоверенным вводом. Выделенный image ingress получает bounded Telegram media и вызывает
application use case `ProcessOcrImage` внутри idempotent mutation envelope. Telegram metadata и фактический формат
сверяются; принимаются только JPEG/PNG/WebP до 10 MiB, 20 мегапикселей и 5000 px по стороне. Pillow полностью
декодирует изображение, нормализует ориентацию и передаёт bounded PNG локальному Tesseract через stdin с таймаутом.
Исходник, нормализованное изображение и сырой OCR-текст существуют только в памяти процесса, не сохраняются и не
логируются. OCR никогда не создаёт transaction без обычного review и явного подтверждения владельца. Для
банковского списка parser принимает не более 20 сильных строк-кандидатов с явным знаком и денежной дробной суммой.
Повреждённая или пропущенная OCR валюта заменяется валютой выбранного счёта; любая успешно распознанная в строке или
заголовке валюта обязана совпадать с ней. Если все строки нельзя надёжно разделить или валюта смешана/чужая, очередь
не создаётся: частичный молчаливый импорт запрещён. Даже после разбора нет кнопки массового сохранения: каждую
операцию, включая последнюю, нужно отдельно сохранить или пропустить. Отмена очищает ещё не просмотренную очередь,
но не откатывает уже явно сохранённые операции.

### Опциональный локальный Ollama

`LOCAL_AI_ENABLED=false` является безопасным default. Модель вызывается только командой `/ai <text>`; обычный
детерминированный parser и локальный OCR никогда не используют AI как fallback. Вызов выполняется до открытия DB
mutation UoW, а проверенный typed suggestion затем проходит существующий `PrepareParsedDraft` и review-first flow.
Прямой transaction write отсутствует. Если active draft уже существует, update claim и фиксированный ответ
коммитятся с durable outbox, но draft не получает `pending_intent`, не suspend-ится, не заменяется и не
перепривязывается к сообщению об ошибке.

Endpoint имеет закрытый allowlist: `http://127.0.0.1:11434` для host runtime или `http://ollama:11434` в отдельной
internal Compose network. Credentials, redirect и environment proxy запрещены. Request использует `stream=false`,
`temperature=0`, fixed seed/context/output limits и `keep_alive=0`; prompt не длиннее 1024 символов, полный response
не больше 16 KiB, hard caller timeout — 8 секунд. Outer response и model content принимаются только как strict UTF-8
JSON без duplicate/non-finite values; suggestion schema запрещает дополнительные поля, numeric amount и неизвестный
transaction type.

Production daemon поставляет operator: модель должна быть preseeded, `OLLAMA_NO_CLOUD=1`, published ports и egress
отсутствуют, контейнер подключён только к `local-ai`. Base Compose ничего не скачивает и не разрешает arbitrary URL;
cloud-marked model name отклоняется при startup. Prompt, response, description, amount, model, URL и identifiers
существуют только в памяти и не логируются. Allowlisted event содержит только `provider`, `stage` и `result`.

### Локальный read-only MCP

MCP server запускается только как local stdio subprocess и не имеет HTTP/SSE listener. Owner identity фиксируется
из validated process configuration и разрешается внутри read-only UoW; tool arguments и schemas не содержат owner,
database URL, credentials или SQL. Первый milestone предоставляет только bounded query tools с declarative
`readOnlyHint=true`, `destructiveHint=false` и `openWorldHint=false`; write tools отсутствуют.

Production profile использует отдельный PostgreSQL login с `NOINHERIT`, transaction default `read_only=on` и только
`CONNECT`/`USAGE`/`SELECT` privileges. Provisioner запрещает DML, DDL, sequence access, role/schema creation и доступ к
другим databases, после чего выполняет write-denial probe. MCP transaction дополнительно устанавливает
`REPEATABLE READ` и `READ ONLY`. Stdout зарезервирован для protocol frames, privacy-safe logs идут в stderr и содержат
только allowlisted `mcp_tool_completed` + `result`, без tool name, arguments, result, finance data или identifiers.

Удаление операции мягкое и двухэтапное: сначала подтверждение, затем перенос в корзину. Восстановление использует
optimistic version. `/undo` применяет только существующий audit event; отсутствие audit event означает безопасный
отказ, а не эвристическое удаление последней записи.

## CSV export

CSV является чувствительным финансовым артефактом, а отправка ботом не получает свойств end-to-end encrypted Secret
Chat. Durable outbox хранит только typed marker `csv_export:v1`; generated bytes, filename и row count не записываются
в БД, логи или временные файлы. После commit delivery заново разрешает точную числовую пару Telegram owner user/chat
и формирует файл из текущего owner-scoped состояния БД только в памяти.

Это не snapshot на момент запроса: изменения между commit job и delivery попадут в файл. Export ограничен 10 000
строками и 16 MiB; user-controlled CSV cells сохраняют защиту от spreadsheet formula injection. Telegram не имеет
idempotency key для document upload, поэтому crash после принятия файла, но до фиксации `sent_at`, может привести к
повторной отправке того же logical export. Business mutation и update claim при этом не повторяются.

Миграция `0006` запрещает downgrade до `0005`, пока в outbox остаётся хотя бы одна CSV export job row, включая уже
доставленную. Перед осознанным rollback такие строки нужно удалить отдельной контролируемой операцией с учётом
потери их delivery-state evidence; автоматическая очистка при downgrade запрещена.

## Staged bank CSV import

Bank import принимает только strict canonical CSV: raw body до 2 MiB, 2000 строк, 32 колонок и 500
символов на поле. Encoding задаётся явно (`utf-8`/BOM или `windows-1251` в Mini App), без
эвристик. Money разбирается в integer minor units без `float`; mixed bank-account references, mixed/foreign
валюта, malformed header или любая невалидная строка отклоняют batch целиком.

HTTP route `POST /api/v1/bank-imports/upload` делает bounded Origin/cookie/CSRF/session admission до
чтения и parsing body, затем повторяет authoritative auth/idempotency в mutation UoW. Exact Nginx location
ограничен 2 MiB и `2r/m` с `burst=1`, не буферизует request до backend admission; остальной `/api/`
сохраняет 16 KiB limit. Telegram сначала делает read-only processed-update/default-account precheck, затем
bounded download и UTF-8 parse; update claim, staged rows и fixed response outbox коммитятся атомарно.

Raw CSV, filename, source row/account/reference не сохраняются и не логируются. В БД попадают только
normalized finance fields и domain-separated keyed 32-byte fingerprint/reference digests. Reconciliation
возвращает не более пяти deterministic exact candidates и никогда не auto-match/create. Transaction появляется
только после shared restricted review confirm и атомарно получает `source=bank_import/import_row_id`.

`BANK_IMPORT_SECURITY_KEY` — отдельный 32-byte data-key и не должен совпадать с `HTTP_SECURITY_KEY`. Его
хранят и восстанавливают вместе с deployment secrets, никогда не логируют. Поскольку raw references не
хранятся, transparent rekey невозможен: rotation начинает новую dedupe epoch и теряет cross-epoch
распознавание дублей. При компрометации ключ меняют, принимают эту потерю и не обещают
бесшовную rotation. Downgrade `0011 -> 0010` fail closed запрещён, пока существуют batches, rows,
transaction provenance или retained bank-import idempotency receipts.

## Логи и диагностика

Runtime пишет однострочный JSON, но не сериализует произвольный log message. Formatter принимает только фиксированные
event codes и allowlist полей: component, correlation ID, нормализованный event type, result, bucket длительности и
безопасный класс ошибки. `args`, текст exception/traceback, SQL, Telegram payload и неизвестные extra-поля
отбрасываются.

Запрещено логировать token, credentials, Telegram/chat/update IDs, суммы, валюты, названия счетов и категорий,
описания, message text, CSV bytes/filename/row count или database URL. Добавление нового log event требует review
allowlist и теста на утечку. Healthcheck также возвращает только безопасный код состояния, без exception text и
connection string.

Application/controller DTO скрывают owner/draft identifiers и финансовые payload из `repr`. Сформированный Telegram
receipt также не должен раскрывать текст/keyboard в диагностическом представлении.

Запрет на message text распространяется на OCR-текст, содержимое изображений, local-AI prompt и model response.
Ошибки декодирования, Tesseract, provider transport и schema validation возвращаются только как фиксированные
безопасные пользовательские сообщения.

## Telegram Mini App и HTTP session

Production HTTP auth не имеет bypass и запускается только при валидных `MINIAPP_PUBLIC_URL` и `HTTP_SECURITY_KEY`.
Public URL обязан быть одним canonical HTTPS origin без credentials, query или fragment. Signed login, logout и
protected mutations считают один bounded ASCII `Origin` необязательной transport metadata: native WebView не
предоставляет единый portable Origin contract. Duplicate, empty, oversized и non-ASCII Origin отклоняются. Для login
authority задают verified Telegram HMAC, membership в process-local allowlist, TTL и replay denial; для logout и
mutations — host-only session, совпадающий page binding и double-submit CSRF, а для mutations дополнительно canonical
idempotency key. Любое
одиночное bounded ASCII значение Origin принимается только как metadata и не предоставляет authority. Security key —
независимые 32 random bytes, он не совпадает с Telegram bot token и поступает через environment или
`/run/secrets/http_security_key`.

`POST /api/v1/auth/telegram` принимает не более 12 KiB JSON и 8 KiB raw `initData`, запрещает ambiguous/duplicate
fields и проверяет официальный Telegram HMAC до использования `user`/`auth_date`. Принимается только allowlisted
subject, proof младше 5 минут и не более чем на 30 секунд из будущего. Mini App предъявляет signed `initData` при
каждом запуске до доверия к общей WebView cookie jar. Если активная cookie принадлежит тому же verified subject,
backend сохраняет сессию без повторного consume proof. Для другого subject старая сессия отзывается только после
успешного fresh proof claim и заменяется новыми cookies. Replay digest строится из verified 32-byte Telegram hash,
поэтому reorder и эквивалентное query encoding не создают новый proof; replay без активной same-subject session
получает conflict. Запись удерживается до `auth_date + TTL + skew`, включая logout.

Session и CSRF генерируются независимо с 256-bit entropy и живут один час. Браузер получает host-only cookies
`__Host-numismat_session` (`HttpOnly; Secure; SameSite=Strict; Path=/`) и `__Host-numismat_csrf`
(`Secure; SameSite=Strict; Path=/`). Каждый успешный auth response также возвращает `X-Session-Binding`: privacy-safe
domain-separated HMAC от raw HttpOnly session token. Это не tenant ID и не замена session cookie; frontend хранит
binding только в памяти страницы, не пишет в storage/URL/telemetry и отправляет с каждым protected GET, write и
logout. Backend пересчитывает binding из текущей cookie и сравнивает его до owner/tenant lookup. State-changing
endpoint дополнительно требует точного совпадения CSRF cookie/header; browser Origin остаётся только optional bounded
transport metadata. Duplicate/empty/oversized/non-ASCII Origin и duplicate/oversized Cookie, binding, CSRF,
Content-Type или encoded body отклоняются fail closed. `Set-Cookie` разрешён только успешному auth, который создаёт
новую session; retained same-subject auth возвращает binding без rotation. Failed auth и любой protected error —
включая invalid/expired/revoked session, binding mismatch и CSRF failure — не отправляют `Set-Cookie` и не меняют
ambient cookies. Поэтому поздний ответ старой страницы A не может стереть валидные cookies B из общей WebView jar.

`GET /api/v1/auth/me` проверяет session cookie вместе с page binding. Любая session read/mutation/logout дополнительно
проверяет, что её сохранённый Telegram subject всё ещё входит в текущий allowlist. `POST /api/v1/auth/logout` берёт
exclusive row lock и инвалидирует её; protected mutation удерживает shared session lock в той же DB transaction до
domain/idempotency commit. Успешный logout отвечает `204` без `Set-Cookie`: frontend удаляет page-memory binding и
protected state, а оставшаяся отозванная cookie не предоставляет authority и перезаписывается новым successful signed
login. New-session cookie headers создаются только после commit. Auth logs содержат только allowlisted
outcome/reason codes: Telegram IDs, raw/canonical `initData`, cookies, tokens, session binding, CSRF, Origin, headers и
validation payload не журналируются.

Frontend связывает каждый async auth result с монотонной attempt epoch и отбрасывает результат, если более новая
попытка уже началась. `hidden` и любой `pagehide` синхронно снимают protected UI, заменяют tenant QueryClient и
инвалидируют epoch до background/bfcache. Уже authenticated страница сохраняет binding только в памяти для resume:
`visible` или persisted `pageshow` выполняет только `/auth/me` с прежней cookie+binding, не воспроизводя старый
`initData`. Если suspend застал auth незавершённой, binding очищается; failed resume переводит UI в reopen-required.
Поэтому ни cached DOM, ни поздний response, ни stale launch proof не возвращают предыдущего tenant в UI.

## HTTP mutations и idempotency

HTTP mutation surface является закрытым typed API: persistent draft payload и Telegram presentation metadata наружу
не возвращаются. Новая и повторяемая transaction создаётся только через draft confirm; existing edit начинается через
`edit-draft`, delete/restore требуют optimistic version. OCR, settings, unknown и будущие draft schemas публикуются
только как bounded `supported=false` projection без business payload. Draft-changing endpoints для неподдерживаемой
schema возвращают `409 invalid_state` и не изменяют или удаляют такой draft.

Каждая mutation требует валидную host-only session, совпадающий page-memory `X-Session-Binding`, double-submit CSRF и
один canonical 43-character `Idempotency-Key`; `Origin`, если WebView его передал, валидируется только как bounded
однозначная ASCII metadata.
JSON ограничен 12 KiB; duplicate/unknown fields, encoded body и non-canonical values отклоняются.
В PostgreSQL попадают только keyed digests семантического fingerprint и минимальный результат.

Порядок блокировок и фиксации един для всех endpoints:

```text
shared web-session lock → owner row lock → idempotency claim → domain rows → completion → commit
```

Committed `2xx` хранит только status, result kind, UUID и revision/version. Тот же key и fingerprint воспроизводит
сохранённый result без повторного чтения domain state; другая семантика получает `409 idempotency_key_conflict`.
Ошибка mutation откатывает claim, поэтому ключ не остаётся занятым.

При двух разных ключах concurrent confirm одной revision намеренно даёт один `201` и один owner-scoped `404`: первый
commit удаляет consumed draft, второй не создаёт transaction и его idempotency claim откатывается. Повтор с тем же
ключом получает точный сохранённый `201` result.

## HTTP finance reads

Dashboard, period/comparison reports, transaction detail и active transaction list доступны только по валидной
session cookie вместе с совпадающим `X-Session-Binding`. Binding проверяется до tenant lookup; session lookup и все SQL
одного ответа выполняются в одной `READ ONLY REPEATABLE READ` transaction;
ошибка setup гарантированно закрывает inner transaction/session. Ответы `/api/v1/` всегда получают `no-store,
no-cache`, `Pragma: no-cache` и `Referrer-Policy: no-referrer`.

Query parser ограничивает raw bytes/fields, запрещает unknown и duplicate keys, принимает только bounded canonical
limits, lowercase canonical UUID и UTC RFC3339 `Z` с максимум шестью дробными знаками. Период не длиннее 366 дней;
report rows, page size, currencies и per-currency categories имеют SQL/application/schema caps. Суммы сериализуются
decimal strings и не теряют точность в JavaScript.

Pagination cursor — canonical 76-character base64url token с full HMAC, привязанный к owner и active-list domain.
Он не содержит сумму, описание или owner ID и никогда не логируется. Это защита целостности, а не шифрование:
timestamp и transaction UUID можно декодировать, но они уже присутствуют в предыдущем ответе. Pagination — live
keyset, поэтому после изменения transaction клиент обязан отбросить cursor и начать список заново; при неизменном
наборе одинаковый cursor даёт детерминированный результат без offset drift.

## HTTP catalogs и OpenAPI contract

Account/category reads выполняют session authentication, owner settings и catalog query в одной
`READ ONLY REPEATABLE READ` транзакции. SQL получает не более 201 строки; приложение возвращает максимум 200 и при
наличии 201-й строки отвечает фиксированным `409 catalog_unavailable`, а не silently truncates справочник. Ответы не
содержат owner, slug, timestamps, balance, rule patterns или иные внутренние поля.

Catalog mutation использует тот же порядок `session -> owner -> idempotency -> domain -> completion`. Destination
capacity проверяется после owner/target/version validation и до create/restore/archive. Этот инвариант применяется ко
всем production writers, включая custom account/category из Telegram и HTTP draft flow: на cap существующий slug можно
прочитать, но новый row не добавляется, а failed idempotency claim откатывается. Names, currency, UUID, version и тела
catalog запросов не журналируются; replay строится только из сохранённого минимального account/category reference.

`/reports/today` получает локальную дату только из серверных clock и timezone владельца. Offline OpenAPI exporter
использует inert typed dependencies, не читает environment/secrets и не обращается к БД или сети; перед atomic replace
он запрещает external/dangling `$ref`, non-canonical JSON и NaN.

## Exchange-rate integrity boundary

Ручные источники курсов принадлежат exact owner и сериализуют публикацию общим owner lock перед source lock.
HTTP mutation использует общий порядок `session -> owner -> idempotency -> domain -> completion`; receipt хранит
только kind, UUID версии и номер. Rate body, значения курсов, исходные/конвертированные итоги и финансовые строки не
попадают в логи.

Каждая опубликованная версия и её entries неизменяемы. Отчёт принимает явный owner-scoped `version_id` и выполняет
auth, чтение версии и финансовых агрегатов в одной `READ ONLY REPEATABLE READ` транзакции. Автовыбор latest,
обратный курс и triangulation запрещены: отсутствие прямого курса завершает запрос fail closed. Расчёт не использует
`float`, не перезаписывает transaction и округляет только итоговые integer minor units по HALF_EVEN.

## Recurring runner и review boundary

`recurring-runner` — отдельный hardened DB-only process без Telegram token, HTTP key и внешней сети. Он получает
только runtime database secret, выполняет две bounded транзакционные фазы с `pg_try_advisory_xact_lock`, локальными
lock/statement timeouts и owner-first locking. Materialization берёт максимум одну ближайшую due-схему на owner, после
чего общий tick рассматривает максимум 32 schedules; staging рассматривает максимум 16 owners. Pending
probe читает не более 33 UUID и запрещает 33-й unstaged instance. Активный draft не изменяется, не suspend-ится и
не получает скрытый intent: due instance остаётся pending с backoff.

Schedule payload, name, amount, currency, description, owner/catalog/draft/transaction identifiers и runner counts
не входят в логи. Разрешены только фиксированные event/result codes. Transaction создаётся исключительно через
существующий confirm review-draft; уникальные owner-composite FK и `recurring_instance_id` сохраняют provenance.
Удаление draft очищает только ссылку `draft_id`, поэтому instance остаётся аудируемым как dismissed.

## Notification jobs и privacy boundary

Уведомления выключены по умолчанию и включаются каждым owner отдельно. `notification-scheduler`
получает только runtime database URL и независимый 32-byte `NOTIFICATION_SECURITY_KEY`; Telegram token,
HTTP/BANK keys и public network ему не доступны. `notification-delivery` получает runtime DB, bot token и полный
allowlist, но не получает notification/HTTP/BANK keys. Оба процесса non-root, read-only, cap-drop и ограничены
своими Compose networks.

Queue row содержит только owner UUID, фиксированный event kind, opaque owner-owned reference UUID,
доменно разделённый keyed 32-byte dedupe digest, state/attempt/lease и timestamps. Суммы, валюты, названия,
описания, текст Telegram message и сырой dedupe material не пересекают границу persistence/logging.
Доставщик выбирает текст из закрытого static mapping в коде.

Перед каждым Telegram I/O доставщик повторно проверяет current allowlist, exact private
`telegram_user_id == telegram_chat_id`, текущий opt-in для event kind, quiet hours и принадлежность
budget/recurring reference тому же owner. Для budget jobs он заново рассчитывает authoritative progress и
отправляет alert только пока его точный порог 80/100% остаётся актуальным. Revoked, disabled, stale-threshold и
cross-owner jobs завершаются без сети.
Retry ограничен пятью попытками, exponential delay ограничен 15 минутами, lease recovery не
даёт потерять crash-interrupted job. Как и любая at-least-once Telegram delivery, crash после успешного
network acceptance, но до `delivered_at`, может дать один дубликат; транзакцию БД нельзя держать во время
внешнего network call.

Terminal `delivered`/`failed` jobs удаляются отдельным advisory-singleton cleanup не ранее 400 дней и не более
500 строк за tick. Такой horizon длиннее максимального budget period и сохраняет dedupe; `pending` и leased rows
cleanup не затрагивает.

## Secrets и PostgreSQL

Локально secrets приходят из environment/`.env`; production получает их из файлов в `/run/secrets`. Каталог
`secrets/`, реальные Telegram данные и production exports не входят в Git. После подозрения на утечку token нужно
отозвать через BotFather, а не только удалить из файла.

Runtime database role не должен иметь `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS` или DDL-права.
Миграции выполняются отдельным role/URL; provisioning проверяет effective privileges и запрещённые DDL/DML probes.
Production container работает non-root, с read-only rootfs, `tmpfs /tmp`,
`cap_drop: ALL`, `no-new-privileges`, без Docker socket и без опубликованного PostgreSQL port.
Telegram polling, FastAPI и immutable web edge запускаются отдельными контейнерами. Production API не публикует host
port и разделяет с web только internal `api-edge`; публичная `public-edge` network принадлежит одному web-контейнеру.
Web слушает unprivileged `8443`, host публикует только TCP/443, а TLS private key приходит read-only через Compose
secret. Поскольку `file:` secrets являются bind mounts и Compose не применяет к ним декларативные `uid/gid/mode`,
host key обязан иметь точные `101:101:0400`; `make web-secrets-check` fail closed проверяет ownership, mode и SAN.

Edge принимает один exact lowercase DNS Host из `MINIAPP_PUBLIC_URL`, не поднимает plaintext/redirect listener и
отклоняет неизвестный Host. TLS ограничен 1.2/1.3, session tickets выключены, HSTS не захватывает sibling domains.
CSP запрещает everything-by-default, framing, objects, workers и внешние connections; разрешены same-origin assets/API
и официальный `https://telegram.org` SDK. `index.html`/BrowserRouter fallback используют `no-store, max-age=0`, hashed
`/assets/` — immutable cache, а missing asset не превращается в SPA. `/api/*` проксируется без retry, client identity
forwarding и configurable upstream; Origin, Cookie и Set-Cookie сохраняются как same-origin auth contract.

Alembic `0012_multitenant_integrity` перед DDL берёт `ACCESS EXCLUSIVE` locks и fail closed сканирует существующие
cross-owner/private-chat нарушения. Composite foreign keys связывают owner с default account, parent category, audit
transaction, recurring/import draft и Telegram outbox; `users.telegram_chat_id`, если задан, обязан равняться
`telegram_user_id`. Это database defense-in-depth поверх обязательных owner predicates. PostgreSQL RLS отложен до
отдельного проектирования ролей/контекста для pre-auth, cleanup, runner, outbox и process-fixed MCP.

Access log edge состоит только из event code, route group, result и numeric status. URI/query, client IP, user-agent,
referrer, headers, cookies, body, byte counts, upstream и timing отсутствуют; error log ограничен critical level. Нативные
Telegram mobile/desktop WebView являются поддержанным контуром, iframe embedding fail closed запрещён.

Alembic `0007_http_security_state` хранит web-session, CSRF и HTTP-idempotency значения только как отдельные
keyed 32-byte digests. Expiry ограничен 24 часами constraints и repository validation; cleanup выполняется bounded
batch с `SKIP LOCKED`. Logout берёт exclusive row lock, а state-changing HTTP transaction — shared row lock, поэтому
успешный logout сериализуется с mutation. Downgrade до `0006` берёт write-blocking table lock и fail-closed
отклоняется, пока остаются активные сессии или retained idempotency rows. Raw tokens, keys, fingerprints и request
bodies не логируются.

FastAPI lifespan запускает один bounded maintenance task: первая попытка через 60 секунд, затем раз в 15 минут, общий
timeout 10 секунд и максимум 500 web sessions плюс 500 idempotency rows за tick. PostgreSQL transaction-scoped advisory
lock выбирает одного caller при нескольких API replicas; `SKIP LOCKED`, local lock/statement timeout и rollback при
cancellation не блокируют owner mutations. Лог содержит только `http_security_cleanup_completed` и result.

Learned category resolution не загружает неограниченный набор правил: owner/kind namespace допускает максимум 512,
reader запрашивает 513 и fail closed при legacy overflow. Новый rule capacity проверяется под owner row lock;
обновление существующего normalized pattern остаётся разрешённым, а deterministic precedence не меняется.

## Отзыв доступа и rollback

`TELEGRAM_ALLOWED_USER_IDS` — полный, а не добавочный список. Он загружается при старте: удаление ID вступает в силу
только после согласованного restart `bot` и `api`; затем bot отклоняет updates, а API отклоняет даже ещё не истёкшую
session. Данные пользователя, audit trail и расписания не удаляются. `recurring-runner` не получает Telegram allowlist
и продолжает создавать review drafts для активных schedules, поэтому до отзыва доступа operator должен pause-нуть
их либо остановить runner. Notification delivery сразу fail closed отклоняет jobs удалённого из allowlist
owner, но DB-only scheduler продолжит создавать терминально отклоняемые jobs, пока opt-in не выключен.
Перед offboarding отключите notification preferences или остановите scheduler. Destructive offboarding в текущем
scope отсутствует.

Безопасный rollout/rollback начинается в singleton-режиме с пустым `TELEGRAM_ALLOWED_USER_IDS`. После проверки
основного пользователя operator задаёт полный canonical список, включающий `OWNER_TELEGRAM_USER_ID`, и одновременно
перезапускает bot/API. Возврат к пустому значению восстанавливает доступ только primary owner; primary остаётся
process-fixed MCP principal, но не получает cross-tenant прав через Telegram или HTTP. Логи rollout и отказов остаются
value-free: только фиксированные event/result/reason, без allowlist, ID, cookies и финансовых значений.

## Backup и restore

Backup обязателен через hardened ops-container. Временный `pg_dump -Fc` создаётся только в `tmpfs`, проверяется,
сохраняется в зашифрованный Restic repository и удаляется. Restic password и PostgreSQL `.pgpass` передаются через
Compose secrets. Не используйте копию live PostgreSQL volume как backup.

Required ops secrets:

- `secrets/restic_repository`;
- `secrets/restic_password`;
- `secrets/backup_pgpass`;
- `secrets/restore_postgres_password`;
- `secrets/restore_pgpass`;
- `secrets/restore_database_url`.

Перед эксплуатацией выполните `make restic-init`, затем регулярно `make backup`, `make backup-age` и
`make restic-check`. `make restore-drill` восстанавливает только в одноразовую БД `finbot_restore_test`, после чего
применяет миграции и healthcheck. И configured URL, и реально подключённая database проверяются на суффикс `_test`.
Подробная конфигурация secrets, network/volume overrides и systemd timers описана в `README.md`.

Локальный Restic named volume не является off-host копией. Для устойчивости к потере хоста используйте независимый
repository/backend и регулярно подтверждайте его работоспособность restore drill.

## Реагирование на инцидент

1. Остановите все polling-инстансы.
2. Отзовите и перевыпустите Telegram token через BotFather.
3. Смените runtime/migration PostgreSQL и Restic credentials.
4. Сохраните безопасные event logs и проверьте audit trail, не выгружая финансовые поля в тикеты или чаты.
5. При сомнении в целостности восстановите последний проверенный snapshot только в изолированную `_test` БД.
6. Выполните миграции и healthcheck, затем запустите ровно один bot instance.
7. После восстановления выполните новый backup, freshness check и restore drill.
