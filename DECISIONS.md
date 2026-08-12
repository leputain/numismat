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
- `TransactionParser` — application port. MVP parser детерминированный; AI остаётся вне scope.
- OCR реализован отдельным application port и локальным Tesseract 5 adapter (`rus+eng`). Pillow проверяет и
  нормализует недоверенное JPEG/PNG/WebP в памяти; Tesseract получает PNG через stdin с timeout. Изображение и сырой
  текст не сохраняются. Фискальный чек остаётся одной review-операцией с итогом. Банковский список может дать до 20
сильных строк-кандидатов с явным знаком и денежной дробной суммой. Повреждённая валюта заменяется валютой счёта; распознанная чужая или смешанная валюта
отклоняет весь batch. Строки хранятся как нормализованная draft-очередь и проходят
  review последовательно, без bulk-save. Каждый save/skip, включая последний, — явное действие владельца. Неполный или неоднозначный batch
  отвергается целиком, а cancel удаляет очередь,
  но не разворачивает уже сохранённые transactions.
- Для быстрого ввода отсутствие знака и `-` означают расход, `+` — доход. Знак не хранится в amount; amount всегда
  положительный. Многословные `@account` и `#category` требуют кавычек, чтобы границы значения были однозначны.
- Быстрое сохранение без review удалено из UX. Quick input, wizard и repeat создают persistent draft и требуют
  отдельного «Сохранить»; legacy `fast_mode` может временно оставаться только как schema compatibility detail.
- Review является единым editor для типа, суммы, категории, счёта, даты и комментария. Telegram UI редактирует одно
  сообщение, где возможно; typed answers после обработки удаляются, чтобы личный чат оставался компактным.
- Сохранённые category rules создаются только явным выбором владельца после исправления категории. Автоматическое
  обучение по истории запрещено. Account-scoped rule приоритетнее global; затем сравниваются длина фразы и версия.
- У владельца один active draft. Конфликт нового намерения разрешается явно через resume/replace/keep; revision,
  suspended flag и presentation reference нужны для отклонения stale UI без потери черновика.
- «Повторить сегодня» никогда не копирует строку напрямую в transactions: оно создаёт review draft с текущей локальной
  датой, оставляя исходную операцию неизменной.
- Удаление двухэтапное и мягкое. Корзина — отдельная read model удалённых строк; restore проверяет optimistic version.
- Undo допускается только по audit event и хранит минимальный prior state. Эвристический fallback на последнюю
  транзакцию запрещён.
- Report periods используют inclusive UTC start и exclusive UTC end. CSV отображает время в timezone пользователя,
  форматирует money целочисленно и нейтрализует spreadsheet formula prefixes в user-controlled cells.
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
