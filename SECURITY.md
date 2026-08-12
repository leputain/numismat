# Security

## Модель угроз

Finbot рассчитан на одного владельца. В scope входят: посторонние Telegram-пользователи, утечка в group chat,
повторные и переставленные updates/callbacks, устаревшие UI-кнопки, malformed input, компрометация credentials или БД,
утечка чувствительных данных в логи и непроверяемые backup/restore процедуры.

Telegram не является end-to-end encrypted Secret Chat для ботов. Не отправляйте Finbot номера карт, CVV, банковские
пароли, коды подтверждения, документы и иные секреты. Перед отправкой банковского скриншота обрежьте номера карты,
баланс и прочие поля, которые не нужны для распознавания операции.

## Доступ и обработка updates

- Каждый update принимается только при точном совпадении числового `OWNER_TELEGRAM_USER_ID` и `chat.type == private`.
  Username не является идентификатором доступа.
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
- Все user-controlled строки перед HTML-выводом экранируются.

## Безопасные изменения данных

Любой quick input, мастер и «Повторить сегодня» сначала создают черновик и требуют явного «Сохранить». При конфликте
незавершённый черновик не перезаписывается без выбора владельца. Изменение категории не создаёт learned rule
автоматически: сохранение правила требует отдельного явного действия.

Изображения считаются недоверенным вводом. Telegram metadata и фактический формат сверяются; принимаются только
JPEG/PNG/WebP до 10 MiB, 20 мегапикселей и 5000 px по стороне. Pillow полностью декодирует изображение, нормализует
ориентацию и передаёт bounded PNG локальному Tesseract через stdin с таймаутом. Исходник, нормализованное изображение
и сырой OCR-текст существуют только в памяти процесса, не сохраняются и не логируются. OCR никогда не
создаёт transaction без обычного review и явного подтверждения владельца. Для банковского списка parser принимает не более 20
сильных строк-кандидатов с явным знаком и денежной дробной суммой. Повреждённая или пропущенная OCR валюта заменяется валютой выбранного счёта; любая успешно
распознанная в строке или заголовке валюта обязана совпадать с ней. Если все строки нельзя надёжно разделить или валюта смешана/чужая, очередь не создаётся: частичный молчаливый импорт запрещён. Даже после разбора нет кнопки массового
сохранения: каждую операцию, включая последнюю, нужно отдельно сохранить
или пропустить. Отмена очищает ещё не просмотренную очередь, но не откатывает уже явно сохранённые операции.

Удаление операции мягкое и двухэтапное: сначала подтверждение, затем перенос в корзину. Восстановление использует
optimistic version. `/undo` применяет только существующий audit event; отсутствие audit event означает безопасный
отказ, а не эвристическое удаление последней записи.

## Логи и диагностика

Runtime пишет однострочный JSON, но не сериализует произвольный log message. Formatter принимает только фиксированные
event codes и allowlist полей: component, correlation ID, нормализованный event type, result, bucket длительности и
безопасный класс ошибки. `args`, текст exception/traceback, SQL, Telegram payload и неизвестные extra-поля
отбрасываются.

Запрещено логировать token, credentials, Telegram/chat/update IDs, суммы, валюты, названия счетов и категорий,
описания, message text, CSV или database URL. Добавление нового log event требует review allowlist и теста на утечку.
Healthcheck также возвращает только безопасный код состояния, без exception text и connection string.

Запрет на message text распространяется на OCR-текст и содержимое изображений. Ошибки декодирования, Tesseract и
разбора возвращаются только как фиксированные безопасные пользовательские сообщения.

## Secrets и PostgreSQL

Локально secrets приходят из environment/`.env`; production получает их из файлов в `/run/secrets`. Каталог
`secrets/`, реальные Telegram данные и production exports не входят в Git. После подозрения на утечку token нужно
отозвать через BotFather, а не только удалить из файла.

Runtime database role не должен иметь `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS` или DDL-права.
Миграции выполняются отдельным role/URL; provisioning проверяет effective privileges и запрещённые DDL/DML probes.
Production container работает non-root, с read-only rootfs, `tmpfs /tmp`,
`cap_drop: ALL`, `no-new-privileges`, без Docker socket и без опубликованного PostgreSQL port.

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
