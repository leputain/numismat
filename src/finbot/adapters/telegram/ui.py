from dataclasses import dataclass
from hashlib import blake2s
from uuid import UUID

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from finbot.application.interactions import DraftAction, DraftInteraction


@dataclass(frozen=True, slots=True)
class Choice:
    id: UUID
    label: str
    emoji: str = ""
    version: int = 1


def _draft_callback(
    legacy: str,
    draft_id: UUID | None,
    revision: int | None,
    *,
    object_version: int = 1,
) -> str:
    if draft_id is None and revision is None:
        return legacy
    if draft_id is None or revision is None:
        raise ValueError("draft_id and revision must be provided together")
    action: DraftAction
    page: int | None = None
    object_id: UUID | None = None
    version: int | None = None
    direct = {
        "w:confirm": DraftAction.CONFIRM,
        "w:cancel": DraftAction.CANCEL,
        "w:back": DraftAction.BACK,
        "w:review:type": DraftAction.EDIT_TYPE,
        "w:review:amount": DraftAction.EDIT_AMOUNT,
        "w:review:category": DraftAction.EDIT_CATEGORY,
        "w:review:account": DraftAction.EDIT_ACCOUNT,
        "w:review:date": DraftAction.EDIT_DATE,
        "w:description": DraftAction.EDIT_DESCRIPTION,
        "w:description:skip": DraftAction.SKIP_DESCRIPTION,
        "d:resume": DraftAction.RESUME,
        "d:replace": DraftAction.REPLACE,
        "d:keep": DraftAction.KEEP,
        "d:discard": DraftAction.DISCARD,
        "w:rule:global": DraftAction.RULE_GLOBAL,
        "w:rule:account": DraftAction.RULE_ACCOUNT,
        "w:rule:remove": DraftAction.RULE_REMOVE,
        "w:ocr:skip": DraftAction.SKIP_OCR_ITEM,
        "e:amount": DraftAction.TX_EDIT_AMOUNT,
        "e:category": DraftAction.TX_EDIT_CATEGORY,
        "e:account": DraftAction.TX_EDIT_ACCOUNT,
        "e:date": DraftAction.TX_EDIT_DATE,
        "e:description": DraftAction.TX_EDIT_DESCRIPTION,
        "e:dateback": DraftAction.TX_DATE_BACK,
        "e:back": DraftAction.TX_BACK,
    }
    if legacy in direct:
        action = direct[legacy]
    elif legacy.startswith(("w:type:", "w:review:settype:")):
        action = DraftAction.SELECT_TYPE
        page = 1 if legacy.endswith(":income") else 0
    elif legacy.startswith("w:cat:"):
        action = DraftAction.SELECT_CATEGORY
        key = legacy.removeprefix("w:cat:")
        if key != "new":
            object_id = UUID(key)
            version = object_version
    elif legacy.startswith("w:acct:"):
        action = DraftAction.SELECT_ACCOUNT
        key = legacy.removeprefix("w:acct:")
        if key != "new":
            object_id = UUID(key)
            version = object_version
    elif legacy.startswith(("w:date:", "w:review:setdate:")):
        action = DraftAction.SELECT_DATE
        key = legacy.rsplit(":", 1)[1]
        page = {"today": 0, "yesterday": 1, "custom": 2}[key]
    elif legacy.startswith("e:cat:"):
        action = DraftAction.TX_SELECT_CATEGORY
        object_id = UUID(legacy.removeprefix("e:cat:"))
        version = object_version
    elif legacy.startswith("e:acct:"):
        action = DraftAction.TX_SELECT_ACCOUNT
        object_id = UUID(legacy.removeprefix("e:acct:"))
        version = object_version
    elif legacy.startswith("e:datepick:"):
        action = DraftAction.TX_SELECT_DATE
        page = {"today": 0, "yesterday": 1, "custom": 2}[legacy.removeprefix("e:datepick:")]
    else:
        raise ValueError(f"unsupported draft callback: {legacy}")
    return DraftInteraction(
        action=action,
        draft_id=draft_id,
        revision=revision,
        page=page,
        object_id=object_id,
        object_version=version,
    ).encode()


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить операцию")],
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📊 Месяц")],
        [KeyboardButton(text="🧾 Все операции"), KeyboardButton(text="↩️ Отменить")],
        [KeyboardButton(text="📤 CSV"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Например: 1450 ресторан",
)


def _rows(buttons: list[InlineKeyboardButton], width: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + width] for index in range(0, len(buttons), width)]


def wizard_input_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_draft_callback("w:back", draft_id, revision)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                ),
            ]
        ]
    )


def edit_input_keyboard(
    *,
    date_menu: bool = False,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    callback_data = "e:dateback" if date_menu else "e:back"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=_draft_callback(callback_data, draft_id, revision),
                )
            ]
        ]
    )


def wizard_type_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="− Расход",
                    callback_data=_draft_callback("w:type:expense", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="+ Доход",
                    callback_data=_draft_callback("w:type:income", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✖️ Закрыть",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                )
            ],
        ]
    )


def draft_conflict_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Продолжить черновик",
                    callback_data=_draft_callback("d:resume", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆕 Сбросить и начать новое",
                    callback_data=_draft_callback("d:replace", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Не начинать новое",
                    callback_data=_draft_callback("d:keep", draft_id, revision),
                )
            ],
        ]
    )


def resume_draft_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Продолжить незавершённый ввод",
                    callback_data=_draft_callback("d:resume", draft_id, revision),
                )
            ]
        ]
    )


def append_resume_button(
    keyboard: InlineKeyboardMarkup,
    *,
    has_draft: bool,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    if not has_draft:
        return keyboard
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *keyboard.inline_keyboard,
            [
                InlineKeyboardButton(
                    text="▶️ Продолжить черновик",
                    callback_data=_draft_callback("d:resume", draft_id, revision),
                )
            ],
        ]
    )


def category_keyboard(
    categories: list[Choice],
    *,
    edit: bool = False,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{choice.emoji or '▫️'} {choice.label}",
            callback_data=(
                _draft_callback(
                    f"e:cat:{choice.id}",
                    draft_id,
                    revision,
                    object_version=choice.version,
                )
                if edit
                else _draft_callback(
                    f"w:cat:{choice.id}",
                    draft_id,
                    revision,
                    object_version=choice.version,
                )
            ),
        )
        for choice in categories
    ]
    rows = _rows(buttons)
    if not edit:
        rows.append(
            [
                InlineKeyboardButton(
                    text="＋ Своя категория",
                    callback_data=_draft_callback("w:cat:new", draft_id, revision),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_draft_callback("w:back", draft_id, revision)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=_draft_callback("e:back", draft_id, revision),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_keyboard(
    accounts: list[Choice],
    *,
    edit: bool = False,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    rows = _rows(
        [
            InlineKeyboardButton(
                text=f"💳 {choice.label}",
                callback_data=(
                    _draft_callback(
                        f"e:acct:{choice.id}",
                        draft_id,
                        revision,
                        object_version=choice.version,
                    )
                    if edit
                    else _draft_callback(
                        f"w:acct:{choice.id}",
                        draft_id,
                        revision,
                        object_version=choice.version,
                    )
                ),
            )
            for choice in accounts
        ]
    )
    if not edit:
        rows.append(
            [
                InlineKeyboardButton(
                    text="＋ Свой счёт",
                    callback_data=_draft_callback("w:acct:new", draft_id, revision),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_draft_callback("w:back", draft_id, revision)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=_draft_callback("e:back", draft_id, revision),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wizard_date_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сегодня",
                    callback_data=_draft_callback("w:date:today", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="Вчера",
                    callback_data=_draft_callback("w:date:yesterday", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📆 Другая дата",
                    callback_data=_draft_callback("w:date:custom", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_draft_callback("w:back", draft_id, revision)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                ),
            ],
        ]
    )


def wizard_confirm_keyboard(
    has_description: bool,
    *,
    rule_offer: str | None = None,
    rule_scope: str | None = None,
    draft_id: UUID | None = None,
    revision: int | None = None,
    ocr_batch: bool = False,
    ocr_has_more: bool = False,
) -> InlineKeyboardMarkup:
    description = "✏️ Изменить комментарий" if has_description else "✏️ Добавить комментарий"
    rule_rows: list[list[InlineKeyboardButton]] = []
    if rule_offer is not None:
        compact_rule = rule_offer if len(rule_offer) <= 18 else rule_offer[:17] + "…"
        if rule_scope is None:
            rule_rows = [
                [
                    InlineKeyboardButton(
                        text=f"🧠 «{compact_rule}» · все счета",
                        callback_data=_draft_callback("w:rule:global", draft_id, revision),
                    ),
                    InlineKeyboardButton(
                        text=f"💳 «{compact_rule}» · этот счёт",
                        callback_data=_draft_callback("w:rule:account", draft_id, revision),
                    ),
                ]
            ]
        else:
            scope = "этого счёта" if rule_scope == "account" else "всех счетов"
            rule_rows = [
                [
                    InlineKeyboardButton(
                        text=f"🧠 Правило для {scope} · убрать",
                        callback_data=_draft_callback("w:rule:remove", draft_id, revision),
                    )
                ]
            ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Сохранить и дальше" if ocr_has_more else "✅ Сохранить",
                    callback_data=_draft_callback("w:confirm", draft_id, revision),
                )
            ],
            *(
                [
                    [
                        InlineKeyboardButton(
                            text="⏭ Пропустить эту операцию",
                            callback_data=_draft_callback("w:ocr:skip", draft_id, revision),
                        )
                    ]
                ]
                if ocr_batch
                else []
            ),
            *rule_rows,
            [
                InlineKeyboardButton(
                    text="↔️ Тип",
                    callback_data=_draft_callback("w:review:type", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="💰 Сумма",
                    callback_data=_draft_callback("w:review:amount", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏷 Категория",
                    callback_data=_draft_callback("w:review:category", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="💳 Счёт",
                    callback_data=_draft_callback("w:review:account", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📆 Дата",
                    callback_data=_draft_callback("w:review:date", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text=description,
                    callback_data=_draft_callback("w:description", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_draft_callback("w:back", draft_id, revision)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                ),
            ],
        ]
    )


def review_type_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="− Расход",
                    callback_data=_draft_callback("w:review:settype:expense", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="+ Доход",
                    callback_data=_draft_callback("w:review:settype:income", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К проверке",
                    callback_data=_draft_callback("w:back", draft_id, revision),
                )
            ],
        ]
    )


def review_date_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сегодня",
                    callback_data=_draft_callback("w:review:setdate:today", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="Вчера",
                    callback_data=_draft_callback("w:review:setdate:yesterday", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📆 Ввести дату",
                    callback_data=_draft_callback("w:review:setdate:custom", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К проверке",
                    callback_data=_draft_callback("w:back", draft_id, revision),
                )
            ],
        ]
    )


def wizard_description_keyboard(
    draft_id: UUID | None = None, revision: int | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Без комментария",
                    callback_data=_draft_callback("w:description:skip", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_draft_callback("w:back", draft_id, revision)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=_draft_callback("w:cancel", draft_id, revision),
                ),
            ],
        ]
    )


def transaction_keyboard(
    transaction_id: UUID, version: int, history_page: int = 0
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Повторить сегодня",
                    callback_data=f"tx:repeat:{transaction_id}:{version}:{history_page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить операцию",
                    callback_data=f"tx:edit:{transaction_id}:{version}:{history_page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить операцию",
                    callback_data=f"tx:del:ask:{transaction_id}:{version}:{history_page}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Все операции", callback_data=f"h:{history_page}")],
        ]
    )


def delete_confirmation_keyboard(
    transaction_id: UUID, version: int, history_page: int = 0
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=f"tx:del:do:{transaction_id}:{version}:{history_page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"tx:view:{transaction_id}:{version}:{history_page}",
                )
            ],
        ]
    )


def restore_keyboard(
    transaction_id: UUID, version: int, history_page: int = 0
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Восстановить",
                    callback_data=f"tx:restore:{transaction_id}:{version}:{history_page}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Все операции", callback_data=f"h:{history_page}")],
        ]
    )


def edit_keyboard(
    transaction_id: UUID,
    version: int,
    history_page: int = 0,
    *,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Сумма",
                    callback_data=_draft_callback("e:amount", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="🏷 Категория",
                    callback_data=_draft_callback("e:category", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Счёт",
                    callback_data=_draft_callback("e:account", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="📆 Дата",
                    callback_data=_draft_callback("e:date", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Комментарий",
                    callback_data=_draft_callback("e:description", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К операции",
                    callback_data=f"tx:view:{transaction_id}:{version}:{history_page}",
                )
            ],
        ]
    )


def edit_date_keyboard(
    transaction_id: UUID,
    version: int,
    history_page: int = 0,
    *,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сегодня",
                    callback_data=_draft_callback("e:datepick:today", draft_id, revision),
                ),
                InlineKeyboardButton(
                    text="Вчера",
                    callback_data=_draft_callback("e:datepick:yesterday", draft_id, revision),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📆 Ввести дату",
                    callback_data=_draft_callback("e:datepick:custom", draft_id, revision),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=_draft_callback("e:back", draft_id, revision),
                )
            ],
        ]
    )


def history_keyboard(
    items: list[tuple[UUID, int, str]],
    page: int,
    total_pages: int,
    deleted_count: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, (item_id, version, label) in enumerate(items):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{index + 1} · {label}",
                    callback_data=f"tx:view:{item_id}:{version}:{page}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"tx:del:ask:{item_id}:{version}:{page}",
                ),
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="‹", callback_data=f"h:{page - 1}"))
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="h:noop")
    )
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(text="›", callback_data=f"h:{page + 1}"))
    rows.append(navigation)
    if deleted_count:
        rows.append(
            [InlineKeyboardButton(text=f"🗑 Корзина · {deleted_count}", callback_data="z:list:0")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trash_keyboard(
    items: list[tuple[UUID, int, str]], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"↩️ {label}",
                callback_data=f"z:view:{item_id}:{version}:{page}",
            )
        ]
        for item_id, version, label in items
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="‹", callback_data=f"z:list:{page - 1}"))
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="z:noop")
    )
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(text="›", callback_data=f"z:list:{page + 1}"))
    rows.append(navigation)
    rows.append([InlineKeyboardButton(text="⬅️ Все операции", callback_data="h:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trash_restore_keyboard(
    transaction_id: UUID, version: int, trash_page: int = 0
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Восстановить",
                    callback_data=f"z:restore:{transaction_id}:{version}:{trash_page}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ В корзину", callback_data=f"z:list:{trash_page}")],
        ]
    )


def settings_keyboard(
    fast_mode: bool,
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    del fast_mode  # retained until the legacy database column is removed
    rows = [
        [InlineKeyboardButton(text="💳 Счета", callback_data="s:accounts")],
        [InlineKeyboardButton(text="🏷 Категории", callback_data="sc:root")],
        [InlineKeyboardButton(text="🕒 Часовой пояс", callback_data="s:timezones")],
    ]
    if draft_id is not None or revision is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 Сбросить незавершённый ввод",
                    callback_data=_draft_callback("d:discard", draft_id, revision),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="❓ Справка", callback_data="s:help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="s:back")]
        ]
    )


def settings_accounts_keyboard(
    accounts: list[Choice], selected_id: UUID | None, archived_count: int = 0
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'⭐ ' if choice.id == selected_id else '💳 '}{choice.label}",
                callback_data=f"sa:view:{choice.id}:{choice.version}",
            )
        ]
        for choice in accounts
    ]
    rows.append([InlineKeyboardButton(text="➕ Новый счёт", callback_data="sa:new")])
    if archived_count:
        rows.append(
            [InlineKeyboardButton(text=f"📦 Архив · {archived_count}", callback_data="sa:archived")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="s:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_account_keyboard(
    account_id: UUID, version: int, *, is_default: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not is_default:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⭐ Сделать основным",
                    callback_data=f"sa:default:{account_id}:{version}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Переименовать", callback_data=f"sa:rename:{account_id}:{version}"
            )
        ]
    )
    if not is_default:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📦 В архив", callback_data=f"sa:archive:ask:{account_id}:{version}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К счетам", callback_data="sa:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_account_archive_keyboard(account_id: UUID, version: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Да, архивировать",
                    callback_data=f"sa:archive:do:{account_id}:{version}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sa:view:{account_id}:{version}")],
        ]
    )


def settings_archived_accounts_keyboard(accounts: list[Choice]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"↩️ {choice.label}",
                callback_data=f"sa:restore:{choice.id}:{choice.version}",
            )
        ]
        for choice in accounts
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К счетам", callback_data="sa:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_categories_keyboard(expense_count: int, income_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💸 Расходы · {expense_count}", callback_data="sc:list:expense"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💰 Доходы · {income_count}", callback_data="sc:list:income"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="s:back")],
        ]
    )


def settings_category_list_keyboard(
    categories: list[Choice], kind: str, archived_count: int = 0
) -> InlineKeyboardMarkup:
    rows = _rows(
        [
            InlineKeyboardButton(
                text=f"{choice.emoji or '▫️'} {choice.label}",
                callback_data=f"sc:view:{choice.id}:{choice.version}",
            )
            for choice in categories
        ]
    )
    rows.append([InlineKeyboardButton(text="➕ Новая категория", callback_data=f"sc:new:{kind}")])
    if archived_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📦 Архив · {archived_count}",
                    callback_data=f"sc:archived:{kind}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="sc:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_category_keyboard(category_id: UUID, kind: str, version: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Переименовать",
                    callback_data=f"sc:rename:{category_id}:{version}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 В архив",
                    callback_data=f"sc:archive:ask:{category_id}:{version}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"sc:list:{kind}")],
        ]
    )


def settings_category_archive_keyboard(category_id: UUID, version: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Да, архивировать",
                    callback_data=f"sc:archive:do:{category_id}:{version}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=f"sc:view:{category_id}:{version}"
                )
            ],
        ]
    )


def settings_archived_categories_keyboard(
    categories: list[Choice], kind: str
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"↩️ {choice.emoji or '▫️'} {choice.label}",
                callback_data=f"sc:restore:{choice.id}:{choice.version}",
            )
        ]
        for choice in categories
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=f"sc:list:{kind}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text_input_keyboard(draft_id: UUID, revision: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Отменить ввод",
                    callback_data=_draft_callback("d:discard", draft_id, revision),
                )
            ]
        ]
    )


TIMEZONES = (
    ("Europe/Kaliningrad", "Калининград · UTC+2"),
    ("Europe/Moscow", "Москва · UTC+3"),
    ("Europe/Samara", "Самара · UTC+4"),
    ("Asia/Yekaterinburg", "Екатеринбург · UTC+5"),
    ("Asia/Omsk", "Омск · UTC+6"),
    ("Asia/Krasnoyarsk", "Красноярск · UTC+7"),
    ("Asia/Irkutsk", "Иркутск · UTC+8"),
    ("Asia/Yakutsk", "Якутск · UTC+9"),
    ("Asia/Vladivostok", "Владивосток · UTC+10"),
)


def timezone_token(value: str) -> str:
    return blake2s(value.encode("utf-8"), digest_size=8).hexdigest()


def settings_timezones_keyboard(selected: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if timezone == selected else ''}{label}",
                callback_data=f"s:timezone:{index}:{timezone_token(selected)}",
            )
        ]
        for index, (timezone, label) in enumerate(TIMEZONES)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="s:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
