import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from io import BytesIO
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot import VERSION_LABEL
from finbot.adapters.database.category_rules import SqlAlchemyCategoryRuleRepository
from finbot.adapters.database.models import Account, Category, Draft, User
from finbot.adapters.database.queries.reports import (
    category_totals,
    period_transactions,
    totals_by_currency,
)
from finbot.adapters.database.queries.transactions import (
    export_transaction_details,
    get_transaction_details,
    list_deleted_transaction_details,
    list_transaction_details,
)
from finbot.adapters.database.services.catalogs import (
    archive_account,
    archive_category,
    create_account,
    create_category,
    rename_account,
    rename_category,
    restore_account,
    restore_category,
    set_default_account,
)
from finbot.adapters.database.services.onboarding import ensure_owner_user
from finbot.adapters.database.services.outbox import (
    queue_edit_message_text,
    queue_send_message,
)
from finbot.adapters.database.services.transactions import (
    clear_draft as database_clear_draft,
)
from finbot.adapters.database.services.transactions import (
    create_or_get_account,
    create_or_get_category,
    edit_transaction,
    get_draft,
    resolve_account,
    resolve_category,
    restore_transaction,
    save_transaction,
    set_draft_suspended,
    soft_delete_transaction,
    start_draft,
    undo_last_action,
)
from finbot.adapters.database.services.transactions import (
    put_draft as database_put_draft,
)
from finbot.adapters.database.services.updates import claim_update, is_update_processed
from finbot.adapters.database.session import session_factory
from finbot.adapters.ocr.tesseract import (
    MAX_IMAGE_BYTES,
    SUPPORTED_IMAGE_MIME_TYPES,
    BoundedImageBuffer,
    TesseractTextExtractor,
)
from finbot.adapters.telegram.delivery import ReliableDeliveryMiddleware
from finbot.adapters.telegram.middlewares.auth import OwnerOnlyMiddleware
from finbot.adapters.telegram.outbox import TelegramResponseOutboxMiddleware
from finbot.adapters.telegram.parser import DeterministicParser
from finbot.adapters.telegram.polling import run as run_polling
from finbot.adapters.telegram.presenters import (
    HELP_TEXT,
    MONTH_NAMES,
    history_text,
    money,
    operation_sign,
    report_text,
    transaction_card,
    trash_text,
)
from finbot.adapters.telegram.presenters import (
    wizard_summary as render_wizard_summary,
)
from finbot.adapters.telegram.ui import (
    MAIN_MENU,
    TIMEZONES,
    Choice,
    account_keyboard,
    append_resume_button,
    category_keyboard,
    delete_confirmation_keyboard,
    draft_conflict_keyboard,
    edit_date_keyboard,
    edit_input_keyboard,
    edit_keyboard,
    history_keyboard,
    restore_keyboard,
    resume_draft_keyboard,
    review_date_keyboard,
    review_type_keyboard,
    settings_account_archive_keyboard,
    settings_account_keyboard,
    settings_accounts_keyboard,
    settings_archived_accounts_keyboard,
    settings_archived_categories_keyboard,
    settings_categories_keyboard,
    settings_category_archive_keyboard,
    settings_category_keyboard,
    settings_category_list_keyboard,
    settings_help_keyboard,
    settings_keyboard,
    settings_text_input_keyboard,
    settings_timezones_keyboard,
    timezone_token,
    transaction_keyboard,
    trash_keyboard,
    trash_restore_keyboard,
    wizard_confirm_keyboard,
    wizard_date_keyboard,
    wizard_description_keyboard,
    wizard_input_keyboard,
    wizard_type_keyboard,
)
from finbot.application.interactions import (
    DraftAction,
    DraftInteraction,
    InteractionCodecError,
)
from finbot.application.ocr import (
    ImageTextExtractor,
    OcrImportError,
    parse_ocr_transactions,
)
from finbot.application.queries.transactions import TransactionDetails
from finbot.application.rules import (
    learning_candidates,
    resolve_learned_category,
    validate_staged_rule,
)
from finbot.application.services.csv_export import build_csv
from finbot.config import Settings
from finbot.domain.dates import parse_local_datetime
from finbot.domain.errors import (
    FinbotError,
    UnknownAccountError,
    UnknownCategoryError,
)
from finbot.domain.money import MoneyError, parse_minor
from finbot.domain.transactions import (
    TransactionDraft,
    TransactionType,
    month_to_date_bounds,
    period_bounds,
    previous_month_to_date_bounds,
)

PAGE_SIZE = 5


def _currency_code(value: object, fallback: str) -> str:
    code = str(value or fallback).strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError("Некорректная валюта счёта")
    return code


def wizard_summary(payload: dict[str, object], fallback_currency: str, timezone: str) -> str:
    """Render the currency of the selected account, with a pre-selection fallback."""
    return _ocr_batch_header(payload) + render_wizard_summary(
        payload, _currency_code(payload.get("currency"), fallback_currency), timezone
    )


@dataclass(frozen=True, slots=True)
class ValidatedDraftInteraction:
    draft_id: UUID
    revision: int


_validated_interaction: ContextVar[ValidatedDraftInteraction | None] = ContextVar(
    "finbot_validated_interaction", default=None
)


async def put_draft(
    session: AsyncSession,
    user_id: UUID,
    state: str,
    payload: dict[str, object],
    *,
    expected_revision: int | None = None,
    new_flow: bool = False,
) -> Draft:
    validated = _validated_interaction.get()
    return await database_put_draft(
        session,
        user_id,
        state,
        payload,
        expected_revision=(
            expected_revision
            if expected_revision is not None
            else validated.revision
            if validated is not None
            else None
        ),
        expected_draft_id=validated.draft_id if validated is not None else None,
        new_flow=new_flow,
    )


async def clear_draft(
    session: AsyncSession,
    user_id: UUID,
    *,
    expected_revision: int | None = None,
) -> None:
    validated = _validated_interaction.get()
    await database_clear_draft(
        session,
        user_id,
        expected_revision=(
            expected_revision
            if expected_revision is not None
            else validated.revision
            if validated is not None
            else None
        ),
        expected_draft_id=validated.draft_id if validated is not None else None,
    )


async def ensure_user(session: AsyncSession, settings: Settings, chat_id: int) -> User:
    return await ensure_owner_user(
        session,
        telegram_user_id=settings.owner_telegram_user_id,
        telegram_chat_id=chat_id,
        locale=settings.default_locale,
        timezone=settings.default_timezone,
        currency=settings.default_currency,
    )


async def _replace_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return message.message_id
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return message.message_id
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        return sent.message_id


def _queue_callback_receipt(
    session: AsyncSession,
    settings: Settings,
    update_id: int | None,
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
) -> bool:
    if update_id is None:
        return False
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=settings.owner_telegram_user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        draft_id=draft_id,
    )
    return True


def _queue_message_receipt(
    session: AsyncSession,
    settings: Settings,
    update_id: int | None,
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    if update_id is None:
        return False
    queue_send_message(
        session,
        update_id=update_id,
        owner_telegram_user_id=settings.owner_telegram_user_id,
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )
    return True


def _queue_input_receipt(
    session: AsyncSession,
    settings: Settings,
    update_id: int | None,
    message: Message,
    payload: dict[str, object],
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
) -> bool:
    if update_id is None:
        return False
    message_id_raw = payload.get("ui_message_id")
    try:
        message_id = int(str(message_id_raw)) if message_id_raw is not None else None
    except ValueError:
        message_id = None
    if message_id is not None and message_id > 0:
        queue_edit_message_text(
            session,
            update_id=update_id,
            owner_telegram_user_id=settings.owner_telegram_user_id,
            chat_id=message.chat.id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            draft_id=draft_id,
        )
    else:
        queue_send_message(
            session,
            update_id=update_id,
            owner_telegram_user_id=settings.owner_telegram_user_id,
            chat_id=message.chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            draft_id=draft_id,
        )
    return True


async def _render_from_input(
    message: Message,
    payload: dict[str, object],
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    message_id_raw = payload.get("ui_message_id")
    message_id = int(str(message_id_raw)) if message_id_raw is not None else None
    bot = message.bot
    if message_id is not None and bot is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
            payload["ui_message_id"] = sent.message_id
    else:
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        payload["ui_message_id"] = sent.message_id
    try:
        await message.delete()
    except TelegramBadRequest, TelegramForbiddenError:
        pass


def _callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


def _parse_transaction_callback(data: str, prefix: str) -> tuple[UUID, int, int]:
    parts = data.removeprefix(prefix).split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("invalid transaction callback")
    history_page = int(parts[2]) if len(parts) == 3 else 0
    if history_page < 0:
        raise ValueError("invalid history page")
    return UUID(parts[0]), int(parts[1]), history_page


def _parse_catalog_callback(data: str, prefix: str) -> tuple[UUID, int]:
    parts = data.removeprefix(prefix).split(":")
    if len(parts) != 2:
        raise ValueError("invalid catalog callback")
    version = int(parts[1])
    if version < 1:
        raise ValueError("invalid catalog version")
    return UUID(parts[0]), version


async def _category_choices(
    session: AsyncSession, user_id: UUID, kind: str, *, archived: bool = False
) -> list[Choice]:
    archive_filter = (
        Category.archived_at.is_not(None) if archived else Category.archived_at.is_(None)
    )
    categories = list(
        (
            await session.scalars(
                select(Category)
                .where(
                    Category.user_id == user_id,
                    Category.kind == kind,
                    archive_filter,
                )
                .order_by(Category.name)
            )
        ).all()
    )
    return [Choice(item.id, item.name, item.emoji, item.version) for item in categories]


async def _account_choices(
    session: AsyncSession, user_id: UUID, *, archived: bool = False
) -> list[Choice]:
    archive_filter = Account.archived_at.is_not(None) if archived else Account.archived_at.is_(None)
    accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(Account.user_id == user_id, archive_filter)
                .order_by(Account.name)
            )
        ).all()
    )
    return [Choice(item.id, item.name, version=item.version) for item in accounts]


async def _accounts_data(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    accounts = await _account_choices(session, user.id)
    archived_count = int(
        await session.scalar(
            select(func.count(Account.id)).where(
                Account.user_id == user.id, Account.archived_at.is_not(None)
            )
        )
        or 0
    )
    return (
        "<b>Счета</b>\n\n"
        "⭐ — основной счёт для операций без <code>@счёт</code>.\n"
        "Откройте счёт, чтобы переименовать его или изменить основной.",
        settings_accounts_keyboard(accounts, user.default_account_id, archived_count),
    )


async def _categories_data(
    session: AsyncSession, user_id: UUID
) -> tuple[str, InlineKeyboardMarkup]:
    counts = {"expense": 0, "income": 0}
    rows = await session.execute(
        select(Category.kind, func.count(Category.id))
        .where(Category.user_id == user_id, Category.archived_at.is_(None))
        .group_by(Category.kind)
    )
    for kind, count in rows:
        if kind in counts:
            counts[kind] = int(count)
    return (
        "<b>Категории</b>\n\n"
        "Категории доходов и расходов разделены. Архивные остаются в старых операциях.",
        settings_categories_keyboard(counts["expense"], counts["income"]),
    )


async def _category_list_data(
    session: AsyncSession, user_id: UUID, kind: str
) -> tuple[str, InlineKeyboardMarkup]:
    if kind not in {"expense", "income"}:
        raise ValueError("Неизвестный тип категории")
    categories = await _category_choices(session, user_id, kind)
    archived_count = int(
        await session.scalar(
            select(func.count(Category.id)).where(
                Category.user_id == user_id,
                Category.kind == kind,
                Category.archived_at.is_not(None),
            )
        )
        or 0
    )
    title = "Категории расходов" if kind == "expense" else "Категории доходов"
    return (
        f"<b>{title}</b>\n\nОткройте категорию для переименования или архивации.",
        settings_category_list_keyboard(categories, kind, archived_count),
    )


def _account_card(account: Account, is_default: bool) -> str:
    status = "⭐ Основной счёт" if is_default else "Активный счёт"
    return (
        f"<b>💳 {escape(account.name)}</b>\n\n"
        f"{status}\n"
        f"Валюта: <b>{escape(account.currency)}</b>\n\n"
        "Архивация не удаляет операции с этого счёта."
    )


def _category_card(category: Category) -> str:
    kind = "Расход" if category.kind == "expense" else "Доход"
    return (
        f"<b>{category.emoji or '▫️'} {escape(category.name)}</b>\n\n"
        f"Тип: <b>{kind}</b>\n\n"
        "Архивация скрывает категорию при новом вводе, но сохраняет её в истории."
    )


async def _has_catalog_input_draft(session: AsyncSession, user_id: UUID) -> bool:
    draft = await get_draft(session, user_id)
    return draft is not None and draft.state.startswith("settings_")


def _payload_draft(payload: dict[str, object]) -> TransactionDraft:
    return TransactionDraft(
        amount_minor=int(str(payload["amount"])),
        type=TransactionType(str(payload["type"])),
        occurred_at=datetime.fromisoformat(str(payload["occurred_at"])),
        category_hint=str(payload["category_slug"]),
        account_hint=str(payload["account_slug"]),
        description=str(payload.get("description", "")),
    )


def _review_keyboard(
    payload: dict[str, object],
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    offer = str(payload.get("rule_offer_pattern", "")).strip() or None
    pending = payload.get("pending_rule")
    scope = str(pending.get("scope")) if isinstance(pending, dict) else None
    return wizard_confirm_keyboard(
        bool(payload.get("description")),
        rule_offer=offer,
        rule_scope=scope,
        draft_id=draft_id,
        revision=revision,
        ocr_batch=_ocr_batch(payload) is not None,
        ocr_has_more=_ocr_batch_has_more(payload),
    )


def _ocr_batch(payload: dict[str, object]) -> dict[str, object] | None:
    batch = payload.get("ocr_batch")
    return batch if isinstance(batch, dict) else None


def _ocr_batch_has_more(payload: dict[str, object]) -> bool:
    batch = _ocr_batch(payload)
    return bool(_ocr_batch_remaining(batch))


def _ocr_batch_remaining(batch: dict[str, object] | None) -> list[object]:
    if batch is None:
        return []
    remaining = batch.get("remaining")
    return list(remaining) if isinstance(remaining, list) else []


def _serialize_ocr_draft(draft: TransactionDraft) -> dict[str, object]:
    return {
        "amount": draft.amount_minor,
        "type": draft.type.value,
        "occurred_at": draft.occurred_at.isoformat() if draft.occurred_at else None,
        "description": draft.description,
        "needs_confirmation": draft.needs_confirmation,
    }


def _deserialize_ocr_draft(value: object) -> TransactionDraft:
    if not isinstance(value, dict):
        raise ValueError("Очередь OCR повреждена")
    occurred = value.get("occurred_at")
    return TransactionDraft(
        amount_minor=int(str(value["amount"])),
        type=TransactionType(str(value["type"])),
        occurred_at=datetime.fromisoformat(str(occurred)) if occurred else None,
        description=str(value.get("description", "")),
        needs_confirmation=bool(value.get("needs_confirmation")),
    )


def _ocr_batch_header(payload: dict[str, object]) -> str:
    batch = _ocr_batch(payload)
    if batch is None:
        return ""
    index = int(str(batch.get("index", 1)))
    total = int(str(batch.get("total", 1)))
    return f"<b>Из изображения · операция {index} из {total}</b>\n\n"


async def _save_payload(
    session: AsyncSession,
    user: User,
    payload: dict[str, object],
    update_id: int | None,
    *,
    clear_after_save: bool = True,
) -> TransactionDetails:
    transaction = await save_transaction(
        session,
        user.id,
        _payload_draft(payload),
        user.base_currency,
        update_id=update_id,
        default_account_id=user.default_account_id,
    )
    if clear_after_save:
        await clear_draft(session, user.id)
    details = await get_transaction_details(session, user.id, transaction.id)
    if details is None:
        raise RuntimeError("saved transaction is not readable")
    return details


def _clear_rule_learning(payload: dict[str, object]) -> None:
    payload.pop("rule_offer_pattern", None)
    payload.pop("pending_rule", None)


def _pending_rule(payload: dict[str, object]) -> tuple[str, str] | None:
    """Return a still-valid staged rule, or ignore obsolete draft metadata."""
    pending = payload.get("pending_rule")
    if not isinstance(pending, dict):
        return None
    pattern = str(pending.get("pattern", "")).strip()
    scope = str(pending.get("scope", ""))
    if pattern != str(payload.get("rule_offer_pattern", "")).strip():
        return None
    if str(pending.get("account_id", "")) != str(payload.get("account_id", "")):
        return None
    if str(pending.get("category_id", "")) != str(payload.get("category_id", "")):
        return None
    return validate_staged_rule(
        pattern,
        scope,
        str(payload.get("description", "")),
        flow=str(payload.get("flow", "")),
        category_explicit=bool(payload.get("category_explicit")),
    )


async def _attach_account(
    session: AsyncSession,
    user: User,
    payload: dict[str, object],
    account: Account,
) -> None:
    if account.user_id != user.id or account.archived_at is not None:
        raise UnknownAccountError("выбранный")
    if payload.get("account_id") is not None and str(payload["account_id"]) != str(account.id):
        _clear_rule_learning(payload)
    payload["account_id"] = str(account.id)
    payload["account_name"] = account.name
    payload["account_slug"] = account.slug
    payload["currency"] = _currency_code(account.currency, user.base_currency)


async def _attach_category(
    session: AsyncSession,
    user: User,
    payload: dict[str, object],
    category: Category,
) -> None:
    if (
        category.user_id != user.id
        or category.kind != str(payload["type"])
        or category.archived_at is not None
    ):
        raise UnknownCategoryError("выбранная")
    payload["category_id"] = str(category.id)
    payload["category_name"] = category.name
    payload["category_slug"] = category.slug


def _stage_rule_offer(payload: dict[str, object], previous_category_id: object) -> None:
    """Offer learning only after an explicit correction of an automatic category."""
    if str(payload.get("flow")) != "quick" or bool(payload.get("category_explicit")):
        payload.pop("rule_offer_pattern", None)
        payload.pop("pending_rule", None)
        return
    if str(previous_category_id) == str(payload.get("category_id")):
        return
    candidates = learning_candidates(str(payload.get("description", "")))
    if candidates:
        payload["rule_offer_pattern"] = candidates[0]
    else:
        payload.pop("rule_offer_pattern", None)
    payload.pop("pending_rule", None)


def _settings_text(user: User, account_name: str) -> str:
    return (
        "<b>Настройки</b>\n\n"
        f"💳 Основной счёт: <b>{escape(account_name)}</b>\n"
        f"🕒 Часовой пояс: <b>{escape(user.timezone)}</b>\n"
        f"💱 Валюта: <b>{escape(user.base_currency)}</b>\n\n"
        "Перед записью любой операции Finbot показывает карточку проверки."
    )


async def _settings_data(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    account_name = "не выбран"
    if user.default_account_id is not None:
        account = await session.scalar(
            select(Account).where(
                Account.id == user.default_account_id,
                Account.user_id == user.id,
                Account.archived_at.is_(None),
            )
        )
        if account is not None:
            account_name = account.name
    draft = await get_draft(session, user.id)
    keyboard = settings_keyboard(
        user.fast_mode,
        draft_id=draft.id if draft is not None else None,
        revision=draft.revision if draft is not None else None,
    )
    return _settings_text(user, account_name), append_resume_button(
        keyboard,
        has_draft=draft is not None,
        draft_id=draft.id if draft is not None else None,
        revision=draft.revision if draft is not None else None,
    )


async def _draft_screen(
    session: AsyncSession,
    user: User,
    state: str,
    payload: dict[str, object],
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if state == "wizard_type":
        return (
            "<b>Новая операция · 1/6</b>\n\nЭто расход или доход?",
            wizard_type_keyboard(draft_id, revision),
        )
    if state in {"wizard_amount", "review_amount"}:
        return (
            "<b>Введите сумму</b>\n\nНапример <code>1450</code>.",
            wizard_input_keyboard(draft_id, revision),
        )
    if state in {"wizard_category", "quick_category", "review_category"}:
        choices = await _category_choices(session, user.id, str(payload["type"]))
        return "<b>Выберите категорию</b>", category_keyboard(
            choices, draft_id=draft_id, revision=revision
        )
    if state in {"wizard_account", "quick_account", "review_account"}:
        choices = await _account_choices(session, user.id)
        return "<b>Выберите счёт</b>", account_keyboard(
            choices, draft_id=draft_id, revision=revision
        )
    if state in {"wizard_date", "custom_date"}:
        return "<b>Когда произошла операция?</b>", wizard_date_keyboard(draft_id, revision)
    if state in {"review_date", "review_date_input"}:
        return "<b>Изменить дату</b>", review_date_keyboard(draft_id, revision)
    if state == "wizard_description":
        return (
            "<b>Комментарий</b>\n\nВведите текст или нажмите «Без комментария».",
            wizard_description_keyboard(draft_id, revision),
        )
    if state in {"wizard_confirm", "quick_confirm"}:
        return (
            wizard_summary(payload, user.base_currency, user.timezone),
            _review_keyboard(payload, draft_id, revision),
        )
    if state == "review_type":
        return (
            "<b>Изменить тип</b>\n\nЭто расход или доход?",
            review_type_keyboard(draft_id, revision),
        )
    if state.startswith("edit_"):
        details = await get_transaction_details(
            session, user.id, UUID(str(payload["transaction_id"]))
        )
        if details is None or details.deleted_at is not None:
            raise ValueError("Операция для редактирования больше недоступна")
        page = int(str(payload.get("history_page", 0)))
        if state == "edit_menu":
            return (
                transaction_card(details, user.timezone, title="Что изменить?"),
                edit_keyboard(
                    details.id,
                    details.version,
                    page,
                    draft_id=draft_id,
                    revision=revision,
                ),
            )
        if state == "edit_category":
            choices = await _category_choices(session, user.id, details.type)
            return (
                "<b>Изменить категорию</b>\n\nВыберите новую категорию:",
                category_keyboard(
                    choices,
                    edit=True,
                    draft_id=draft_id,
                    revision=revision,
                ),
            )
        if state == "edit_account":
            choices = await _account_choices(session, user.id)
            return (
                "<b>Изменить счёт</b>\n\nВыберите новый счёт:",
                account_keyboard(
                    choices,
                    edit=True,
                    draft_id=draft_id,
                    revision=revision,
                ),
            )
        if state == "edit_date_menu":
            return (
                "<b>Изменить дату</b>\n\nВыберите дату или введите свою:",
                edit_date_keyboard(
                    details.id,
                    details.version,
                    page,
                    draft_id=draft_id,
                    revision=revision,
                ),
            )
        if state == "edit_date":
            return (
                "<b>Изменить дату</b>\n\nВведите <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.",
                edit_input_keyboard(
                    date_menu=True,
                    draft_id=draft_id,
                    revision=revision,
                ),
            )
        if state == "edit_amount":
            return (
                "<b>Изменить сумму</b>\n\nВведите новую сумму, например <code>1750</code>.",
                edit_input_keyboard(draft_id=draft_id, revision=revision),
            )
        if state == "edit_description":
            return (
                "<b>Изменить комментарий</b>\n\nВведите новый текст. "
                "Дефис <code>-</code> очистит комментарий.",
                edit_input_keyboard(draft_id=draft_id, revision=revision),
            )
    if state.startswith("settings_account"):
        if draft_id is not None and revision is not None:
            return (
                "<b>Настройка счёта</b>\n\nПродолжите ввод.",
                settings_text_input_keyboard(draft_id, revision),
            )
    if state.startswith("settings_category"):
        if draft_id is not None and revision is not None:
            return (
                "<b>Настройка категории</b>\n\nПродолжите ввод.",
                settings_text_input_keyboard(draft_id, revision),
            )
    return "<b>Незавершённый ввод</b>\n\nОткройте нужный раздел заново.", settings_keyboard(
        user.fast_mode
    )


def _draft_message_matches(draft: Draft, message: Message) -> bool:
    payload = draft.payload
    expected = payload.get("ui_message_id")
    if expected is None:
        expected = draft.presentation_ref
    return expected is not None and int(str(expected)) == message.message_id


async def _repeat_payload(
    session: AsyncSession,
    user: User,
    details: TransactionDetails,
    history_page: int,
) -> dict[str, object]:
    account = await session.scalar(
        select(Account).where(
            Account.id == details.account_id,
            Account.user_id == user.id,
            Account.archived_at.is_(None),
        )
    )
    category = await session.scalar(
        select(Category).where(
            Category.id == details.category_id,
            Category.user_id == user.id,
            Category.archived_at.is_(None),
        )
    )
    if account is None:
        raise UnknownAccountError("архивный")
    if category is None:
        raise UnknownCategoryError("архивная")
    local_now = datetime.now(ZoneInfo(user.timezone)).replace(second=0, microsecond=0)
    return {
        "flow": "repeat",
        "type": details.type,
        "amount": details.amount_minor,
        "category_id": str(details.category_id),
        "category_name": details.category_name,
        "category_slug": category.slug,
        "account_id": str(details.account_id),
        "account_name": details.account_name,
        "account_slug": account.slug,
        "currency": _currency_code(account.currency, details.currency),
        "occurred_at": local_now.isoformat(),
        "description": details.description,
        "history_page": history_page,
    }


async def _prepare_parsed_input(
    session: AsyncSession,
    user: User,
    parsed: TransactionDraft,
    *,
    flow: str,
) -> tuple[str, dict[str, object], str, InlineKeyboardMarkup]:
    occurred = parsed.occurred_at or datetime.now(ZoneInfo(user.timezone))
    payload: dict[str, object] = {
        "flow": flow,
        "type": parsed.type.value,
        "amount": parsed.amount_minor,
        "occurred_at": occurred.isoformat(),
        "description": parsed.description,
        "account_hint": parsed.account_hint or "",
        "category_explicit": parsed.category_explicit,
        "needs_confirmation": parsed.needs_confirmation,
    }
    if not parsed.category_explicit:
        try:
            account_for_rule = await resolve_account(
                session,
                user.id,
                parsed.account_hint,
                user.default_account_id,
            )
        except UnknownAccountError:
            account_for_rule = None
        decision = await resolve_learned_category(
            SqlAlchemyCategoryRuleRepository(session),
            user_id=user.id,
            kind=parsed.type,
            account_id=account_for_rule.id if account_for_rule is not None else None,
            description=parsed.description,
        )
        if decision is not None:
            learned = await session.scalar(
                select(Category).where(
                    Category.id == decision.category_id,
                    Category.user_id == user.id,
                    Category.kind == parsed.type.value,
                    Category.archived_at.is_(None),
                )
            )
            if learned is not None:
                await _attach_category(session, user, payload, learned)
    try:
        if "category_id" not in payload:
            category = await resolve_category(
                session, user.id, parsed.type.value, parsed.category_hint
            )
            await _attach_category(session, user, payload, category)
    except UnknownCategoryError:
        categories = await _category_choices(session, user.id, parsed.type.value)
        return (
            "quick_category",
            payload,
            "<b>Уточните категорию</b>\n\nЯ не нашёл подходящую категорию:",
            category_keyboard(categories),
        )
    try:
        account = await resolve_account(
            session, user.id, parsed.account_hint, user.default_account_id
        )
    except UnknownAccountError:
        accounts = await _account_choices(session, user.id)
        return (
            "quick_account",
            payload,
            "<b>Уточните счёт</b>\n\nУказанный счёт не найден:",
            account_keyboard(accounts),
        )
    await _attach_account(session, user, payload, account)
    return (
        "quick_confirm",
        payload,
        wizard_summary(payload, user.base_currency, user.timezone),
        _review_keyboard(payload),
    )


async def _prepare_quick_input(
    session: AsyncSession,
    user: User,
    text: str,
) -> tuple[str, dict[str, object], str, InlineKeyboardMarkup]:
    return await _prepare_parsed_input(
        session,
        user,
        DeterministicParser(user.timezone).parse(text),
        flow="quick",
    )


async def _suspend_for_navigation(session: AsyncSession, user: User) -> Draft | None:
    return await set_draft_suspended(session, user.id, suspended=True)


async def _send_resume_notice(message: Message, draft: Draft | None) -> None:
    if draft is not None:
        await message.answer(
            "Незавершённый ввод сохранён и приостановлен.",
            reply_markup=resume_draft_keyboard(draft.id, draft.revision),
        )


def build_dispatcher(
    settings: Settings,
    image_text_extractor: ImageTextExtractor | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    sessions = session_factory(settings)
    ocr = image_text_extractor or TesseractTextExtractor()
    middleware = OwnerOnlyMiddleware(settings, sessions)
    outbox_middleware = TelegramResponseOutboxMiddleware(sessions)
    dp.message.outer_middleware(middleware)
    dp.message.outer_middleware(outbox_middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.callback_query.outer_middleware(outbox_middleware)

    @dp.callback_query(F.data.startswith("w:") | F.data.startswith("d:") | F.data.startswith("e:"))
    async def reject_legacy_draft_button(callback: CallbackQuery) -> None:
        await callback.answer(
            "Эта кнопка относится к старой версии формы. Откройте актуальный черновик",
            show_alert=True,
        )

    @dp.message(CommandStart())
    async def start(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            suspended_draft = await _suspend_for_navigation(session, user)
            await session.commit()
        await message.answer(
            f"<b>Numismat {VERSION_LABEL} готов</b> 👋\n\n"
            "Отправьте <code>1450 ресторан</code> или нажмите «➕ Добавить операцию».\n"
            "Все данные доступны только вам в этом личном чате.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        await _send_resume_notice(message, suspended_draft)

    @dp.message(Command("menu"))
    @dp.message(F.text.in_({"🏠 Меню"}))
    async def menu(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            suspended_draft = await _suspend_for_navigation(session, user)
            await session.commit()
        await message.answer(
            "<b>Главное меню</b>\nВыберите действие или просто напишите покупку сообщением.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        await _send_resume_notice(message, suspended_draft)

    @dp.message(Command("help"))
    @dp.message(F.text.in_({"❓ Помощь", "❓ Как пользоваться", "❓ Справка"}))
    async def help_menu(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            suspended_draft = await _suspend_for_navigation(session, user)
            await session.commit()
        await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MAIN_MENU)
        await _send_resume_notice(message, suspended_draft)

    @dp.message(Command("wizard"))
    @dp.message(F.text.in_({"➕ Добавить операцию", "➕ Новая операция", "🧙 Мастер"}))
    async def start_wizard(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            if active is not None:
                payload = dict(active.payload)
                if "pending_intent" in payload:
                    await session.commit()
                    await message.answer("Сначала выберите действие для незавершённого ввода.")
                    return
                payload["pending_intent"] = {"kind": "wizard"}
                active = await put_draft(session, user.id, active.state, payload)
                await session.commit()
                await message.answer(
                    "<b>Есть незавершённый ввод</b>\n\n"
                    "Ничего не будет перезаписано без вашего решения.",
                    parse_mode="HTML",
                    reply_markup=draft_conflict_keyboard(active.id, active.revision),
                )
                return
            draft = await start_draft(
                session,
                user.id,
                "wizard_type",
                {"flow": "wizard"},
            )
            sent = await message.answer(
                "<b>Новая операция · 1/6</b>\n\nЭто расход или доход?",
                parse_mode="HTML",
                reply_markup=wizard_type_keyboard(draft.id, draft.revision),
            )
            draft.payload = {"flow": "wizard", "ui_message_id": sent.message_id}
            draft.presentation_ref = str(sent.message_id)
            await session.commit()

    @dp.message(F.text.in_({"⚡ Быстрый ввод", "➕ Добавить"}))
    async def quick_help(message: Message, finbot_update_id: int | None = None) -> None:
        response = (
            "<b>Быстрый ввод</b>\n\nНапишите сумму и назначение одним сообщением:\n"
            "<code>1450 ресторан</code>\n<code>+250000 зарплата</code>"
        )
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            await ensure_user(session, settings, message.chat.id)
            if finbot_update_id is not None:
                queue_send_message(
                    session,
                    update_id=finbot_update_id,
                    owner_telegram_user_id=settings.owner_telegram_user_id,
                    chat_id=message.chat.id,
                    text=response,
                    parse_mode="HTML",
                )
            await session.commit()
        if finbot_update_id is None:
            await message.answer(response, parse_mode="HTML")

    @dp.callback_query(F.data.in_({"w:type:expense", "w:type:income"}))
    async def wizard_type(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state != "wizard_type":
                await session.commit()
                await callback.answer("Этот шаг уже неактуален", show_alert=True)
                return
            payload = dict(draft.payload)
            payload["type"] = callback.data.rsplit(":", 1)[1]
            message_id = await _replace_message(
                message,
                "<b>Новая операция · 2/6</b>\n\nВведите сумму, например <code>1450</code>",
                wizard_input_keyboard(draft.id, draft.revision + 1),
            )
            payload["ui_message_id"] = message_id
            await put_draft(session, user.id, "wizard_amount", payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data.startswith("w:cat:"))
    async def choose_category(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        category_key = callback.data.removeprefix("w:cat:")
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state not in {
                "wizard_category",
                "quick_category",
                "review_category",
            }:
                await session.commit()
                await callback.answer("Этот шаг уже неактуален", show_alert=True)
                return
            payload = dict(draft.payload)
            if category_key == "new":
                message_id = await _replace_message(
                    message,
                    "<b>Своя категория</b>\n\nВведите короткое название, например «Питомцы».",
                    wizard_input_keyboard(draft.id, draft.revision + 1),
                )
                payload["custom_back_state"] = draft.state
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, "custom_category", payload)
                await session.commit()
                await callback.answer()
                return
            category = await session.scalar(
                select(Category).where(
                    Category.id == UUID(category_key), Category.user_id == user.id
                )
            )
            if category is None:
                await session.commit()
                await callback.answer("Категория не найдена", show_alert=True)
                return
            previous_category_id = payload.get("category_id")
            await _attach_category(session, user, payload, category)
            if draft.state == "review_category":
                _stage_rule_offer(payload, previous_category_id)
                return_state = str(payload.pop("review_return_state", "quick_confirm"))
                message_id = await _replace_message(
                    message,
                    wizard_summary(payload, user.base_currency, user.timezone),
                    _review_keyboard(payload, draft.id, draft.revision + 1),
                )
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, return_state, payload)
            elif str(payload.get("flow")) == "wizard":
                accounts = await _account_choices(session, user.id)
                message_id = await _replace_message(
                    message,
                    "<b>Новая операция · 4/6</b>\n\nНа какой счёт записать?",
                    account_keyboard(accounts, draft_id=draft.id, revision=draft.revision + 1),
                )
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, "wizard_account", payload)
            else:
                account_hint = str(payload.get("account_hint", "")) or None
                try:
                    account = await resolve_account(
                        session, user.id, account_hint, user.default_account_id
                    )
                except UnknownAccountError:
                    accounts = await _account_choices(session, user.id)
                    message_id = await _replace_message(
                        message,
                        "<b>Уточните счёт</b>\n\nУказанный счёт не найден. Выберите существующий:",
                        account_keyboard(accounts, draft_id=draft.id, revision=draft.revision + 1),
                    )
                    payload["ui_message_id"] = message_id
                    await put_draft(session, user.id, "quick_account", payload)
                else:
                    await _attach_account(session, user, payload, account)
                    message_id = await _replace_message(
                        message,
                        wizard_summary(payload, user.base_currency, user.timezone),
                        _review_keyboard(payload, draft.id, draft.revision + 1),
                    )
                    payload["ui_message_id"] = message_id
                    await put_draft(session, user.id, "quick_confirm", payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data.startswith("w:acct:"))
    async def choose_account(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        account_key = callback.data.removeprefix("w:acct:")
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state not in {
                "wizard_account",
                "quick_account",
                "review_account",
            }:
                await session.commit()
                await callback.answer("Этот шаг уже неактуален", show_alert=True)
                return
            payload = dict(draft.payload)
            if account_key == "new":
                message_id = await _replace_message(
                    message,
                    "<b>Свой счёт</b>\n\nВведите название, например «Наличные».",
                    wizard_input_keyboard(draft.id, draft.revision + 1),
                )
                payload["custom_back_state"] = draft.state
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, "custom_account", payload)
                await session.commit()
                await callback.answer()
                return
            account = await session.scalar(
                select(Account).where(Account.id == UUID(account_key), Account.user_id == user.id)
            )
            if account is None:
                await session.commit()
                await callback.answer("Счёт не найден", show_alert=True)
                return
            await _attach_account(session, user, payload, account)
            if draft.state == "review_account":
                return_state = str(payload.pop("review_return_state", "quick_confirm"))
                message_id = await _replace_message(
                    message,
                    wizard_summary(payload, user.base_currency, user.timezone),
                    _review_keyboard(payload, draft.id, draft.revision + 1),
                )
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, return_state, payload)
            elif str(payload.get("flow")) == "wizard":
                message_id = await _replace_message(
                    message,
                    "<b>Новая операция · 5/6</b>\n\nКогда произошла операция?",
                    wizard_date_keyboard(draft.id, draft.revision + 1),
                )
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, "wizard_date", payload)
            else:
                message_id = await _replace_message(
                    message,
                    wizard_summary(payload, user.base_currency, user.timezone),
                    _review_keyboard(payload, draft.id, draft.revision + 1),
                )
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, "quick_confirm", payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data.startswith("w:date:"))
    async def choose_date(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        choice = callback.data.removeprefix("w:date:")
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state != "wizard_date":
                await session.commit()
                await callback.answer("Этот шаг уже неактуален", show_alert=True)
                return
            payload = dict(draft.payload)
            if choice == "custom":
                message_id = await _replace_message(
                    message,
                    "<b>Новая операция · 5/6</b>\n\n"
                    "Введите дату: <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>",
                    wizard_input_keyboard(draft.id, draft.revision + 1),
                )
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, "custom_date", payload)
                await session.commit()
                await callback.answer()
                return
            local_now = datetime.now(ZoneInfo(user.timezone))
            occurred = local_now if choice == "today" else local_now - timedelta(days=1)
            payload["occurred_at"] = occurred.isoformat()
            message_id = await _replace_message(
                message,
                "<b>Новая операция · 6/6</b>\n\n"
                "Введите комментарий, например «ужин с друзьями», "
                "или нажмите «Без комментария».",
                wizard_description_keyboard(draft.id, draft.revision + 1),
            )
            payload["ui_message_id"] = message_id
            payload["return_state"] = "wizard_confirm"
            payload["description_back_state"] = "wizard_date"
            await put_draft(session, user.id, "wizard_description", payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data == "w:description:skip")
    async def skip_wizard_description(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state != "wizard_description":
                await session.commit()
                await callback.answer("Этот шаг уже неактуален", show_alert=True)
                return
            payload = dict(draft.payload)
            return_state = str(payload.pop("return_state", "wizard_confirm"))
            payload.pop("description_back_state", None)
            payload["description"] = ""
            _clear_rule_learning(payload)
            message_id = await _replace_message(
                message,
                wizard_summary(payload, user.base_currency, user.timezone),
                _review_keyboard(payload, draft.id, draft.revision + 1),
            )
            payload["ui_message_id"] = message_id
            await put_draft(session, user.id, return_state, payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data == "w:description")
    async def wizard_description(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state not in {"wizard_confirm", "quick_confirm"}:
                await session.commit()
                await callback.answer("Черновик устарел", show_alert=True)
                return
            payload = dict(draft.payload)
            payload["return_state"] = draft.state
            payload["description_back_state"] = draft.state
            message_id = await _replace_message(
                message,
                "<b>Комментарий</b>\n\nВведите комментарий. "
                "Отправьте дефис <code>-</code>, чтобы очистить.",
                wizard_description_keyboard(draft.id, draft.revision + 1),
            )
            payload["ui_message_id"] = message_id
            await put_draft(session, user.id, "wizard_description", payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data.startswith("w:review:"))
    async def review_field(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        action = callback.data.removeprefix("w:review:")
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None:
                await session.commit()
                await callback.answer("Черновик уже закрыт", show_alert=True)
                return
            payload = dict(draft.payload)
            if action in {"type", "amount", "category", "account", "date"}:
                if draft.state not in {"wizard_confirm", "quick_confirm"}:
                    await session.commit()
                    await callback.answer("Этот экран устарел", show_alert=True)
                    return
                payload["review_return_state"] = draft.state
                if action == "type":
                    text = "<b>Изменить тип</b>\n\nЭто расход или доход?"
                    keyboard = review_type_keyboard(draft.id, draft.revision + 1)
                    state = "review_type"
                elif action == "amount":
                    text = "<b>Изменить сумму</b>\n\nВведите положительную сумму."
                    keyboard = wizard_input_keyboard(draft.id, draft.revision + 1)
                    state = "review_amount"
                elif action == "category":
                    choices = await _category_choices(session, user.id, str(payload["type"]))
                    text = "<b>Изменить категорию</b>\n\nВыберите категорию:"
                    keyboard = category_keyboard(
                        choices, draft_id=draft.id, revision=draft.revision + 1
                    )
                    state = "review_category"
                elif action == "account":
                    choices = await _account_choices(session, user.id)
                    text = "<b>Изменить счёт</b>\n\nВыберите счёт:"
                    keyboard = account_keyboard(
                        choices, draft_id=draft.id, revision=draft.revision + 1
                    )
                    state = "review_account"
                else:
                    text = "<b>Изменить дату</b>\n\nВыберите дату или введите свою:"
                    keyboard = review_date_keyboard(draft.id, draft.revision + 1)
                    state = "review_date"
                message_id = await _replace_message(message, text, keyboard)
                payload["ui_message_id"] = message_id
                await put_draft(session, user.id, state, payload)
                await session.commit()
                await callback.answer()
                return
            if action.startswith("settype:") and draft.state == "review_type":
                kind = action.removeprefix("settype:")
                if kind not in {"expense", "income"}:
                    await session.commit()
                    await callback.answer("Кнопка повреждена", show_alert=True)
                    return
                payload["type"] = kind
                _clear_rule_learning(payload)
                fallback = "прочие доходы" if kind == "income" else "другое"
                category = await resolve_category(session, user.id, kind, fallback)
                await _attach_category(session, user, payload, category)
            elif action.startswith("setdate:") and draft.state == "review_date":
                choice = action.removeprefix("setdate:")
                if choice == "custom":
                    message_id = await _replace_message(
                        message,
                        "<b>Изменить дату</b>\n\nВведите <code>ДД.ММ</code> или "
                        "<code>ДД.ММ.ГГГГ</code>.",
                        wizard_input_keyboard(draft.id, draft.revision + 1),
                    )
                    payload["ui_message_id"] = message_id
                    await put_draft(session, user.id, "review_date_input", payload)
                    await session.commit()
                    await callback.answer()
                    return
                if choice not in {"today", "yesterday"}:
                    await session.commit()
                    await callback.answer("Кнопка повреждена", show_alert=True)
                    return
                local_now = datetime.now(ZoneInfo(user.timezone))
                occurred = local_now if choice == "today" else local_now - timedelta(days=1)
                payload["occurred_at"] = occurred.isoformat()
            else:
                await session.commit()
                await callback.answer("Этот экран устарел", show_alert=True)
                return
            return_state = str(payload.pop("review_return_state", "quick_confirm"))
            message_id = await _replace_message(
                message,
                wizard_summary(payload, user.base_currency, user.timezone),
                _review_keyboard(payload, draft.id, draft.revision + 1),
            )
            payload["ui_message_id"] = message_id
            await put_draft(session, user.id, return_state, payload)
            await session.commit()
        await callback.answer("Изменено")

    @dp.callback_query(F.data.in_({"w:rule:global", "w:rule:account", "w:rule:remove"}))
    async def stage_category_rule(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state not in {"wizard_confirm", "quick_confirm"}:
                await session.commit()
                await callback.answer("Этот экран устарел", show_alert=True)
                return
            payload = dict(draft.payload)
            pattern = str(payload.get("rule_offer_pattern", "")).strip()
            if callback.data == "w:rule:remove":
                payload.pop("pending_rule", None)
                answer = "Правило не будет сохранено"
            elif not pattern:
                await session.commit()
                await callback.answer("Предложение уже неактуально", show_alert=True)
                return
            else:
                scope = "account" if callback.data.endswith(":account") else "global"
                account_id = str(payload.get("account_id", ""))
                category_id = str(payload.get("category_id", ""))
                if not account_id or not category_id:
                    await session.commit()
                    await callback.answer("Данные операции уже изменились", show_alert=True)
                    return
                payload["pending_rule"] = {
                    "pattern": pattern,
                    "scope": scope,
                    "account_id": account_id,
                    "category_id": category_id,
                }
                answer = "Правило будет сохранено вместе с операцией"
            message_id = await _replace_message(
                message,
                wizard_summary(payload, user.base_currency, user.timezone),
                _review_keyboard(payload, draft.id, draft.revision + 1),
            )
            payload["ui_message_id"] = message_id
            await put_draft(session, user.id, draft.state, payload)
            await session.commit()
        await callback.answer(answer)

    @dp.callback_query(F.data == "w:confirm")
    async def confirm_wizard(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        try:
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                draft = await get_draft(session, user.id)
                if draft is None or draft.state not in {"wizard_confirm", "quick_confirm"}:
                    await session.commit()
                    await callback.answer("Черновик уже обработан", show_alert=True)
                    return
                payload = dict(draft.payload)
                pending_rule = _pending_rule(payload)
                if pending_rule is not None:
                    pattern, scope = pending_rule
                    repository = SqlAlchemyCategoryRuleRepository(session)
                    await repository.upsert(
                        user.id,
                        TransactionType(str(payload["type"])),
                        UUID(str(payload["category_id"])),
                        pattern,
                        UUID(str(payload["account_id"])) if scope == "account" else None,
                    )
                batch = _ocr_batch(payload)
                remaining = _ocr_batch_remaining(batch)
                details = await _save_payload(
                    session,
                    user,
                    payload,
                    finbot_update_id,
                    clear_after_save=not remaining,
                )
                if remaining:
                    if batch is None:  # defensive narrowing for malformed payloads
                        raise ValueError("Очередь OCR повреждена")
                    next_item = _deserialize_ocr_draft(remaining.pop(0))
                    next_state, next_payload, _prompt, _keyboard = await _prepare_parsed_input(
                        session,
                        user,
                        next_item,
                        flow="ocr",
                    )
                    next_batch = {
                        "version": 1,
                        "index": int(str(batch.get("index", 1))) + 1,
                        "total": int(str(batch.get("total", 1))),
                        "saved": int(str(batch.get("saved", 0))) + 1,
                        "skipped": int(str(batch.get("skipped", 0))),
                        "remaining": remaining,
                    }
                    next_payload["ocr_batch"] = next_batch
                    updated = await put_draft(
                        session,
                        user.id,
                        next_state,
                        next_payload,
                    )
                    receipt_text, receipt_keyboard = await _draft_screen(
                        session,
                        user,
                        next_state,
                        next_payload,
                        updated.id,
                        updated.revision,
                    )
                    receipt_text = "✅ Предыдущая операция сохранена.\n\n" + receipt_text
                    if finbot_update_id is not None:
                        queue_edit_message_text(
                            session,
                            update_id=finbot_update_id,
                            owner_telegram_user_id=settings.owner_telegram_user_id,
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                            text=receipt_text,
                            parse_mode="HTML",
                            reply_markup=receipt_keyboard,
                            draft_id=updated.id,
                        )
                    else:
                        message_id = await _replace_message(
                            message,
                            receipt_text,
                            receipt_keyboard,
                        )
                        next_payload["ui_message_id"] = message_id
                        updated.payload = dict(next_payload)
                        updated.presentation_ref = str(message_id)
                else:
                    final_title = "✅ Операция сохранена"
                    if batch is not None:
                        saved = int(str(batch.get("saved", 0))) + 1
                        skipped = int(str(batch.get("skipped", 0)))
                        final_title = f"✅ Импорт завершён · сохранено {saved}"
                        if skipped:
                            final_title += f", пропущено {skipped}"
                    final_text = transaction_card(
                        details,
                        user.timezone,
                        title=final_title,
                    )
                    final_keyboard = transaction_keyboard(details.id, details.version)
                    if finbot_update_id is not None:
                        queue_edit_message_text(
                            session,
                            update_id=finbot_update_id,
                            owner_telegram_user_id=settings.owner_telegram_user_id,
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                            text=final_text,
                            parse_mode="HTML",
                            reply_markup=final_keyboard,
                        )
                    else:
                        await _replace_message(message, final_text, final_keyboard)
                await session.commit()
        except (FinbotError, ValueError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer("Сохранено" if not remaining else "Сохранено, проверьте следующую")

    @dp.callback_query(F.data == "w:ocr:skip")
    async def skip_ocr_item(
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None or draft.state not in {"wizard_confirm", "quick_confirm"}:
                await session.commit()
                await callback.answer("Черновик уже изменился", show_alert=True)
                return
            payload = dict(draft.payload)
            batch = _ocr_batch(payload)
            remaining = _ocr_batch_remaining(batch)
            if batch is None:
                await session.commit()
                await callback.answer("Это не пакетный импорт", show_alert=True)
                return
            if not remaining:
                saved = int(str(batch.get("saved", 0)))
                skipped = int(str(batch.get("skipped", 0))) + 1
                await clear_draft(session, user.id)
                receipt_text = (
                    "<b>Импорт завершён</b>\n\n"
                    f"Сохранено: <b>{saved}</b>. Пропущено: <b>{skipped}</b>."
                )
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                )
                if finbot_update_id is None:
                    await _replace_message(message, receipt_text)
                await session.commit()
                await callback.answer("Пропущено")
                return
            next_item = _deserialize_ocr_draft(remaining.pop(0))
            next_state, next_payload, _prompt, _keyboard = await _prepare_parsed_input(
                session,
                user,
                next_item,
                flow="ocr",
            )
            next_payload["ocr_batch"] = {
                "version": 1,
                "index": int(str(batch.get("index", 1))) + 1,
                "total": int(str(batch.get("total", 1))),
                "saved": int(str(batch.get("saved", 0))),
                "skipped": int(str(batch.get("skipped", 0))) + 1,
                "remaining": remaining,
            }
            updated = await put_draft(session, user.id, next_state, next_payload)
            receipt_text, receipt_keyboard = await _draft_screen(
                session,
                user,
                next_state,
                next_payload,
                updated.id,
                updated.revision,
            )
            receipt_text = "⏭ Операция пропущена.\n\n" + receipt_text
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
                draft_id=updated.id,
            )
            if finbot_update_id is None:
                message_id = await _replace_message(message, receipt_text, receipt_keyboard)
                next_payload["ui_message_id"] = message_id
                updated.payload = dict(next_payload)
                updated.presentation_ref = str(message_id)
            await session.commit()
        await callback.answer("Пропущено")

    @dp.callback_query(F.data == "w:back")
    async def wizard_back(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None:
                await session.commit()
                await callback.answer("Черновик уже закрыт", show_alert=True)
                return
            state = draft.state
            payload = dict(draft.payload)
            target_state: str
            if state in {
                "review_type",
                "review_amount",
                "review_category",
                "review_account",
                "review_date",
                "review_date_input",
            }:
                target_state = str(payload.pop("review_return_state", "quick_confirm"))
                text = wizard_summary(payload, user.base_currency, user.timezone)
                keyboard = _review_keyboard(payload, draft.id, draft.revision + 1)
            elif state == "wizard_amount":
                payload.pop("type", None)
                target_state = "wizard_type"
                text = "<b>Новая операция · 1/6</b>\n\nЭто расход или доход?"
                keyboard = wizard_type_keyboard(draft.id, draft.revision + 1)
            elif state == "wizard_category":
                payload.pop("amount", None)
                target_state = "wizard_amount"
                text = (
                    "<b>Новая операция · 2/6</b>\n\n"
                    "Введите сумму заново, например <code>1450</code>."
                )
                keyboard = wizard_input_keyboard(draft.id, draft.revision + 1)
            elif state == "custom_category":
                target_state = str(
                    payload.pop(
                        "custom_back_state",
                        "wizard_category" if payload.get("flow") == "wizard" else "quick_category",
                    )
                )
                categories = await _category_choices(session, user.id, str(payload["type"]))
                text = (
                    "<b>Новая операция · 3/6</b>\n\nВыберите категорию:"
                    if target_state == "wizard_category"
                    else "<b>Уточните категорию</b>\n\nВыберите существующую категорию:"
                )
                keyboard = category_keyboard(
                    categories, draft_id=draft.id, revision=draft.revision + 1
                )
            elif state in {"wizard_account", "quick_account"}:
                if state == "quick_account":
                    target_state = "quick_category"
                    text = "<b>Уточните категорию</b>\n\nВыберите категорию:"
                else:
                    target_state = "wizard_category"
                    text = "<b>Новая операция · 3/6</b>\n\nВыберите категорию:"
                for key in ("category_id", "category_name", "category_slug"):
                    payload.pop(key, None)
                categories = await _category_choices(session, user.id, str(payload["type"]))
                keyboard = category_keyboard(
                    categories, draft_id=draft.id, revision=draft.revision + 1
                )
            elif state == "custom_account":
                target_state = str(
                    payload.pop(
                        "custom_back_state",
                        "wizard_account" if payload.get("flow") == "wizard" else "quick_account",
                    )
                )
                accounts = await _account_choices(session, user.id)
                text = (
                    "<b>Новая операция · 4/6</b>\n\nВыберите счёт:"
                    if target_state == "wizard_account"
                    else "<b>Уточните счёт</b>\n\nВыберите существующий счёт:"
                )
                keyboard = account_keyboard(
                    accounts, draft_id=draft.id, revision=draft.revision + 1
                )
            elif state == "custom_date":
                target_state = "wizard_date"
                text = "<b>Новая операция · 5/6</b>\n\nКогда произошла операция?"
                keyboard = wizard_date_keyboard(draft.id, draft.revision + 1)
            elif state == "wizard_date":
                for key in (
                    "account_id",
                    "account_name",
                    "account_slug",
                    "currency",
                    "occurred_at",
                ):
                    payload.pop(key, None)
                target_state = "wizard_account"
                accounts = await _account_choices(session, user.id)
                text = "<b>Новая операция · 4/6</b>\n\nНа какой счёт записать?"
                keyboard = account_keyboard(
                    accounts, draft_id=draft.id, revision=draft.revision + 1
                )
            elif state == "wizard_description":
                back_state = str(payload.pop("description_back_state", ""))
                payload.pop("return_state", None)
                if back_state in {"wizard_confirm", "quick_confirm"}:
                    target_state = back_state
                    text = wizard_summary(payload, user.base_currency, user.timezone)
                    keyboard = _review_keyboard(payload, draft.id, draft.revision + 1)
                else:
                    target_state = "wizard_date"
                    payload.pop("occurred_at", None)
                    text = "<b>Новая операция · 5/6</b>\n\nКогда произошла операция?"
                    keyboard = wizard_date_keyboard(draft.id, draft.revision + 1)
            elif state == "wizard_confirm":
                target_state = "wizard_description"
                payload["return_state"] = "wizard_confirm"
                payload["description_back_state"] = "wizard_date"
                current = str(payload.get("description", "")).strip()
                current_text = f"\n\nСейчас: <i>{escape(current)}</i>" if current else ""
                text = (
                    "<b>Новая операция · 6/6</b>\n\n"
                    "Введите комментарий или нажмите «Без комментария»."
                    f"{current_text}"
                )
                keyboard = wizard_description_keyboard(draft.id, draft.revision + 1)
            elif state == "quick_confirm":
                target_state = "quick_account"
                _clear_rule_learning(payload)
                for key in ("account_id", "account_name", "account_slug", "currency"):
                    payload.pop(key, None)
                accounts = await _account_choices(session, user.id)
                text = "<b>Уточните счёт</b>\n\nВыберите счёт:"
                keyboard = account_keyboard(
                    accounts, draft_id=draft.id, revision=draft.revision + 1
                )
            elif state == "quick_category":
                await clear_draft(session, user.id)
                receipt_text = (
                    "<b>Быстрый ввод</b>\n\n"
                    "Напишите операцию заново, например <code>1450 ресторан</code>."
                )
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                )
                await session.commit()
                if finbot_update_id is None:
                    await _replace_message(message, receipt_text)
                await callback.answer()
                return
            else:
                await session.commit()
                await callback.answer("Для этого экрана возврат недоступен", show_alert=True)
                return
            message_id = await _replace_message(message, text, keyboard)
            payload["ui_message_id"] = message_id
            await put_draft(session, user.id, target_state, payload)
            await session.commit()
        await callback.answer()

    @dp.callback_query(F.data == "w:cancel")
    async def cancel_wizard(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            batch = _ocr_batch(dict(draft.payload)) if draft is not None else None
            await clear_draft(session, user.id)
            if batch is None:
                receipt_text = "<b>Ввод отменён</b>\nЧерновик удалён."
            else:
                saved = int(str(batch.get("saved", 0)))
                skipped = int(str(batch.get("skipped", 0)))
                receipt_text = (
                    "<b>Импорт завершён</b>\n"
                    f"Сохранено: {saved}. Пропущено: {skipped}. "
                    "Текущая и оставшиеся операции отменены."
                )
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text)
        await callback.answer("Отменено")

    @dp.callback_query(F.data.in_({"d:resume", "d:replace", "d:keep"}))
    async def resolve_draft_conflict(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if draft is None:
                await session.commit()
                await callback.answer("Черновик уже закрыт", show_alert=True)
                return
            payload = dict(draft.payload)
            pending = payload.pop("pending_intent", None)
            state = draft.state
            if callback.data == "d:replace":
                if not isinstance(pending, dict):
                    await session.commit()
                    await callback.answer("Новое действие уже отменено", show_alert=True)
                    return
                kind = str(pending.get("kind", ""))
                if kind == "wizard":
                    state = "wizard_type"
                    payload = {"flow": "wizard"}
                elif kind == "repeat" and isinstance(pending.get("payload"), dict):
                    state = str(pending.get("state", "quick_confirm"))
                    payload = dict(pending["payload"])
                elif kind == "quick" and isinstance(pending.get("text"), str):
                    try:
                        state, payload, text, keyboard = await _prepare_quick_input(
                            session, user, str(pending["text"])
                        )
                    except (MoneyError, ValueError, FinbotError) as exc:
                        await session.commit()
                        await callback.answer(str(exc), show_alert=True)
                        return
                elif kind == "edit":
                    state = "edit_menu"
                    payload = {
                        "transaction_id": str(pending["transaction_id"]),
                        "version": int(str(pending["version"])),
                        "history_page": int(str(pending.get("history_page", 0))),
                    }
                else:
                    await session.commit()
                    await callback.answer("Новое действие устарело", show_alert=True)
                    return
            elif callback.data == "d:keep":
                updated = await put_draft(session, user.id, state, payload)
                updated.suspended = False
                text, keyboard = await _draft_screen(
                    session, user, state, payload, updated.id, updated.revision
                )
                message_id = await _replace_message(message, text, keyboard)
                payload["ui_message_id"] = message_id
                updated.payload = dict(payload)
                updated.presentation_ref = str(message_id)
                await session.commit()
                await callback.answer("Черновик сохранён и открыт")
                return
            updated = await put_draft(
                session,
                user.id,
                state,
                payload,
                new_flow=callback.data == "d:replace",
            )
            updated.suspended = False
            text, keyboard = await _draft_screen(
                session, user, state, payload, updated.id, updated.revision
            )
            message_id = await _replace_message(message, text, keyboard)
            payload["ui_message_id"] = message_id
            updated.payload = dict(payload)
            updated.presentation_ref = str(message_id)
            await session.commit()
        await callback.answer("Черновик открыт")

    @dp.message(Command("today", "month"))
    @dp.message(F.text.in_({"📅 Сегодня", "📊 Месяц"}))
    async def report(message: Message, finbot_update_id: int | None = None) -> None:
        is_month = bool(
            message.text and (message.text.startswith("/month") or message.text == "📊 Месяц")
        )
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            suspended_draft = await _suspend_for_navigation(session, user)
            local_now = datetime.now(ZoneInfo(user.timezone))
            if is_month:
                start, end = month_to_date_bounds(local_now, user.timezone)
                previous_start, previous_end = previous_month_to_date_bounds(
                    local_now, user.timezone
                )
                previous_totals = await totals_by_currency(
                    session, user.id, previous_start, previous_end
                )
                previous_expense = {
                    currency: values.get("expense", 0)
                    for currency, values in previous_totals.items()
                }
                title = f"📊 {MONTH_NAMES[local_now.month].capitalize()} {local_now.year}"
            else:
                start, end = period_bounds(local_now.date(), user.timezone)
                previous_expense = None
                title = f"📅 Сегодня · {local_now:%d.%m.%Y}"
            summary = await totals_by_currency(session, user.id, start, end)
            categories = await category_totals(session, user.id, start, end)
            operations = await period_transactions(session, user.id, start, end, limit=8)
            await session.commit()
        await message.answer(
            report_text(
                title,
                summary,
                categories,
                operations,
                user.timezone,
                previous_expense=previous_expense,
            ),
            parse_mode="HTML",
            reply_markup=(
                resume_draft_keyboard(suspended_draft.id, suspended_draft.revision)
                if suspended_draft is not None
                else None
            ),
        )

    async def _history_payload(
        session: AsyncSession, user: User, page: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        items, total = await list_transaction_details(
            session, user.id, page=max(page, 0), page_size=PAGE_SIZE
        )
        _, deleted_count = await list_deleted_transaction_details(
            session, user.id, page=0, page_size=1
        )
        total_pages = max(ceil(total / PAGE_SIZE), 1)
        safe_page = min(max(page, 0), total_pages - 1)
        if safe_page != page:
            items, total = await list_transaction_details(
                session, user.id, page=safe_page, page_size=PAGE_SIZE
            )
        text = history_text(items, safe_page, total, user.timezone)
        keyboard = history_keyboard(
            [
                (
                    item.id,
                    item.version,
                    f"{operation_sign(item.type)}{money(item.amount_minor, item.currency)} "
                    f"· {item.category_name[:18]}",
                )
                for item in items
            ],
            safe_page,
            total_pages,
            deleted_count,
        )
        return text, keyboard

    @dp.message(Command("last"))
    @dp.message(F.text.in_({"🧾 Все операции", "🧾 История", "🧾 Последние"}))
    async def history(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            suspended_draft = await _suspend_for_navigation(session, user)
            text, keyboard = await _history_payload(session, user, 0)
            await session.commit()
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=append_resume_button(
                keyboard,
                has_draft=suspended_draft is not None,
                draft_id=suspended_draft.id if suspended_draft is not None else None,
                revision=suspended_draft.revision if suspended_draft is not None else None,
            ),
        )

    @dp.callback_query(F.data.startswith("h:"))
    async def history_page(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        if callback.data == "h:noop":
            await callback.answer()
            return
        try:
            page = int(callback.data.removeprefix("h:"))
        except ValueError:
            await callback.answer("Кнопка устарела")
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            text, keyboard = await _history_payload(session, user, page)
            await session.commit()
        await _replace_message(message, text, keyboard)
        await callback.answer()

    @dp.callback_query(F.data.startswith("tx:view:"))
    async def view_transaction(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, _, history_page = _parse_transaction_callback(callback.data, "tx:view:")
        except ValueError, TypeError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            details = await get_transaction_details(session, user.id, transaction_id)
            await session.commit()
        if details is None:
            await callback.answer("Операция не найдена", show_alert=True)
            return
        keyboard = (
            restore_keyboard(details.id, details.version, history_page)
            if details.deleted_at
            else transaction_keyboard(details.id, details.version, history_page)
        )
        await _replace_message(message, transaction_card(details, user.timezone), keyboard)
        await callback.answer()

    @dp.callback_query(F.data.startswith("tx:del:ask:"))
    async def ask_delete_transaction(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = _parse_transaction_callback(
                callback.data, "tx:del:ask:"
            )
        except ValueError, TypeError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            details = await get_transaction_details(session, user.id, transaction_id)
            await session.commit()
        if details is None or details.deleted_at is not None or details.version != version:
            await callback.answer("Операция изменилась. Откройте её заново", show_alert=True)
            return
        await _replace_message(
            message,
            transaction_card(details, user.timezone, title="Удалить операцию?")
            + "\n\nОна исчезнет из отчётов и CSV, но останется в Корзине.",
            delete_confirmation_keyboard(details.id, details.version, history_page),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("tx:del:do:"))
    async def delete_transaction(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = _parse_transaction_callback(
                callback.data, "tx:del:do:"
            )
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                transaction = await soft_delete_transaction(
                    session, user.id, transaction_id, version
                )
                details = await get_transaction_details(session, user.id, transaction.id)
                if details is None:
                    raise RuntimeError("deleted transaction is not readable")
                receipt_text = transaction_card(details, user.timezone, title="🗑 Операция удалена")
                receipt_keyboard = restore_keyboard(details.id, details.version, history_page)
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                    receipt_keyboard,
                )
                await session.commit()
        except (ValueError, FinbotError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if finbot_update_id is None:
            await _replace_message(
                message,
                receipt_text,
                receipt_keyboard,
            )
        await callback.answer("Удалено")

    @dp.callback_query(F.data.regexp(r"^tx:del:[0-9a-fA-F-]{36}:\d+(?::\d+)?$"))
    async def legacy_delete_requires_confirmation(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        translated = callback.model_copy(
            update={"data": "tx:del:ask:" + callback.data.removeprefix("tx:del:")}
        )
        await ask_delete_transaction(translated, finbot_update_id)

    @dp.callback_query(F.data.startswith("tx:restore:"))
    async def restore_deleted(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = _parse_transaction_callback(
                callback.data, "tx:restore:"
            )
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                transaction = await restore_transaction(session, user.id, transaction_id, version)
                details = await get_transaction_details(session, user.id, transaction.id)
                if details is None:
                    raise RuntimeError("restored transaction is not readable")
                receipt_text = transaction_card(
                    details, user.timezone, title="✅ Операция восстановлена"
                )
                receipt_keyboard = transaction_keyboard(details.id, details.version, history_page)
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                    receipt_keyboard,
                )
                await session.commit()
        except (ValueError, FinbotError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if finbot_update_id is None:
            await _replace_message(
                message,
                receipt_text,
                receipt_keyboard,
            )
        await callback.answer("Восстановлено")

    @dp.callback_query(F.data.startswith("tx:repeat:"))
    async def repeat_transaction(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = _parse_transaction_callback(
                callback.data, "tx:repeat:"
            )
        except ValueError, TypeError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            details = await get_transaction_details(session, user.id, transaction_id)
            if details is None or details.deleted_at is not None or details.version != version:
                await session.commit()
                await callback.answer("Операция изменилась. Откройте её заново", show_alert=True)
                return
            try:
                repeated_payload = await _repeat_payload(session, user, details, history_page)
            except UnknownAccountError, UnknownCategoryError:
                await session.commit()
                await callback.answer(
                    "Счёт или категория уже в архиве. Сначала восстановите их",
                    show_alert=True,
                )
                return
            active = await get_draft(session, user.id)
            if active is not None:
                payload = dict(active.payload)
                payload["pending_intent"] = {
                    "kind": "repeat",
                    "state": "quick_confirm",
                    "payload": repeated_payload,
                }
                active = await put_draft(session, user.id, active.state, payload)
                await _replace_message(
                    message,
                    "<b>Есть незавершённый ввод</b>\n\n"
                    "Продолжите его или сбросьте, чтобы повторить операцию.",
                    draft_conflict_keyboard(active.id, active.revision),
                )
                await session.commit()
                await callback.answer()
                return
            payload = repeated_payload
            draft = await start_draft(session, user.id, "quick_confirm", payload)
            message_id = await _replace_message(
                message,
                wizard_summary(payload, details.currency, user.timezone),
                _review_keyboard(payload, draft.id, draft.revision),
            )
            payload["ui_message_id"] = message_id
            draft.payload = dict(payload)
            draft.presentation_ref = str(message_id)
            await session.commit()
        await callback.answer("Проверьте копию")

    @dp.callback_query(F.data.startswith("z:list:"))
    async def trash_page(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            page = int(callback.data.removeprefix("z:list:"))
            if page < 0:
                raise ValueError
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            items, total = await list_deleted_transaction_details(
                session, user.id, page=page, page_size=PAGE_SIZE
            )
            total_pages = max(ceil(total / PAGE_SIZE), 1)
            safe_page = min(page, total_pages - 1)
            if safe_page != page:
                items, total = await list_deleted_transaction_details(
                    session, user.id, page=safe_page, page_size=PAGE_SIZE
                )
            await session.commit()
        keyboard = trash_keyboard(
            [
                (
                    item.id,
                    item.version,
                    f"{operation_sign(item.type)}{money(item.amount_minor, item.currency)} "
                    f"· {item.category_name[:18]}",
                )
                for item in items
            ],
            safe_page,
            total_pages,
        )
        await _replace_message(
            message, trash_text(items, safe_page, total, user.timezone), keyboard
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("z:view:"))
    async def trash_view(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, page = _parse_transaction_callback(callback.data, "z:view:")
        except ValueError, TypeError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            details = await get_transaction_details(session, user.id, transaction_id)
            await session.commit()
        if details is None or details.deleted_at is None or details.version != version:
            await callback.answer("Операция изменилась", show_alert=True)
            return
        await _replace_message(
            message,
            transaction_card(details, user.timezone, title="Операция в Корзине"),
            trash_restore_keyboard(details.id, details.version, page),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("z:restore:"))
    async def trash_restore(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, _ = _parse_transaction_callback(callback.data, "z:restore:")
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                transaction = await restore_transaction(session, user.id, transaction_id, version)
                details = await get_transaction_details(session, user.id, transaction.id)
                if details is None:
                    raise RuntimeError("restored transaction is not readable")
                receipt_text = transaction_card(
                    details, user.timezone, title="✅ Операция восстановлена"
                )
                receipt_keyboard = transaction_keyboard(details.id, details.version)
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                    receipt_keyboard,
                )
                await session.commit()
        except (ValueError, FinbotError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if finbot_update_id is None:
            await _replace_message(
                message,
                receipt_text,
                receipt_keyboard,
            )
        await callback.answer("Восстановлено")

    @dp.callback_query(F.data == "z:noop")
    async def trash_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @dp.callback_query(F.data.startswith("tx:edit:"))
    async def edit_menu(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = _parse_transaction_callback(
                callback.data, "tx:edit:"
            )
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            details = await get_transaction_details(session, user.id, transaction_id)
            if details is None or details.deleted_at is not None:
                await session.commit()
                await callback.answer("Операция недоступна", show_alert=True)
                return
            if details.version != version:
                await session.commit()
                await callback.answer("Карточка устарела. Откройте историю", show_alert=True)
                return
            active = await get_draft(session, user.id)
            edit_payload: dict[str, object] = {
                "transaction_id": str(details.id),
                "version": details.version,
                "history_page": history_page,
                "ui_message_id": message.message_id,
            }
            if active is not None:
                payload = dict(active.payload)
                payload["pending_intent"] = {"kind": "edit", **edit_payload}
                active = await put_draft(session, user.id, active.state, payload)
                await _replace_message(
                    message,
                    "<b>Есть незавершённый ввод</b>\n\n"
                    "Редактирование не заменит его без вашего решения.",
                    draft_conflict_keyboard(active.id, active.revision),
                )
                await session.commit()
                await callback.answer()
                return
            active = await start_draft(
                session,
                user.id,
                "edit_menu",
                edit_payload,
            )
            await session.commit()
        await _replace_message(
            message,
            transaction_card(details, user.timezone, title="Что изменить?"),
            edit_keyboard(
                details.id,
                details.version,
                history_page,
                draft_id=active.id,
                revision=active.revision,
            ),
        )
        await callback.answer()

    @dp.callback_query(F.data.regexp(r"^e:(amount|category|account|date|description):"))
    async def edit_option(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        parts = callback.data.split(":")
        try:
            if len(parts) not in {4, 5}:
                raise ValueError("invalid edit callback")
            action = parts[1]
            transaction_id = UUID(parts[2])
            version = int(parts[3])
            history_page = int(parts[4]) if len(parts) == 5 else 0
            if history_page < 0:
                raise ValueError("invalid history page")
        except ValueError, IndexError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            details = await get_transaction_details(session, user.id, transaction_id)
            if details is None or details.version != version or details.deleted_at is not None:
                await session.commit()
                await callback.answer("Карточка устарела. Откройте историю", show_alert=True)
                return
            active = await get_draft(session, user.id)
            if (
                active is None
                or active.state != "edit_menu"
                or active.suspended
                or not _draft_message_matches(active, message)
                or str(active.payload.get("transaction_id")) != str(transaction_id)
                or int(str(active.payload.get("version", 0))) != version
            ):
                await session.commit()
                await callback.answer("Экран редактирования устарел", show_alert=True)
                return
            payload: dict[str, object] = {
                "transaction_id": str(transaction_id),
                "version": version,
                "history_page": history_page,
                "ui_message_id": message.message_id,
            }
            if action == "category":
                choices = await _category_choices(session, user.id, details.type)
                updated = await put_draft(session, user.id, "edit_category", payload)
                text = "<b>Изменить категорию</b>\n\nВыберите новую категорию:"
                keyboard = category_keyboard(
                    choices,
                    edit=True,
                    draft_id=updated.id,
                    revision=updated.revision,
                )
            elif action == "account":
                choices = await _account_choices(session, user.id)
                updated = await put_draft(session, user.id, "edit_account", payload)
                text = "<b>Изменить счёт</b>\n\nВыберите новый счёт:"
                keyboard = account_keyboard(
                    choices,
                    edit=True,
                    draft_id=updated.id,
                    revision=updated.revision,
                )
            elif action == "date":
                updated = await put_draft(session, user.id, "edit_date_menu", payload)
                text = "<b>Изменить дату</b>\n\nВыберите дату или введите свою:"
                keyboard = edit_date_keyboard(
                    details.id,
                    details.version,
                    history_page,
                    draft_id=updated.id,
                    revision=updated.revision,
                )
            else:
                state = "edit_amount" if action == "amount" else "edit_description"
                updated = await put_draft(session, user.id, state, payload)
                text = (
                    "<b>Изменить сумму</b>\n\nВведите новую сумму, например <code>1750</code>."
                    if action == "amount"
                    else "<b>Изменить комментарий</b>\n\nВведите новый текст. "
                    "Дефис <code>-</code> очистит комментарий."
                )
                keyboard = edit_input_keyboard(
                    draft_id=updated.id,
                    revision=updated.revision,
                )
            await session.commit()
        await _replace_message(message, text, keyboard)
        await callback.answer()

    @dp.callback_query(F.data.startswith("e:cat:"))
    async def edit_category_choice(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            category_id = UUID(callback.data.removeprefix("e:cat:"))
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                draft = await get_draft(session, user.id)
                if (
                    draft is None
                    or draft.state != "edit_category"
                    or draft.suspended
                    or not _draft_message_matches(draft, message)
                ):
                    await session.commit()
                    await callback.answer("Выбор устарел", show_alert=True)
                    return
                history_page = int(str(draft.payload.get("history_page", 0)))
                transaction = await edit_transaction(
                    session,
                    user.id,
                    UUID(str(draft.payload["transaction_id"])),
                    int(str(draft.payload["version"])),
                    category_id=category_id,
                )
                await clear_draft(session, user.id)
                details = await get_transaction_details(session, user.id, transaction.id)
                if details is None:
                    raise RuntimeError("updated transaction is not readable")
                receipt_text = transaction_card(
                    details, user.timezone, title="✅ Категория изменена"
                )
                receipt_keyboard = transaction_keyboard(details.id, details.version, history_page)
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                    receipt_keyboard,
                )
                await session.commit()
        except (ValueError, FinbotError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if finbot_update_id is None:
            await _replace_message(
                message,
                receipt_text,
                receipt_keyboard,
            )
        await callback.answer("Изменено")

    @dp.callback_query(F.data.startswith("e:acct:"))
    async def edit_account_choice(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id = UUID(callback.data.removeprefix("e:acct:"))
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                draft = await get_draft(session, user.id)
                if (
                    draft is None
                    or draft.state != "edit_account"
                    or draft.suspended
                    or not _draft_message_matches(draft, message)
                ):
                    await session.commit()
                    await callback.answer("Выбор устарел", show_alert=True)
                    return
                history_page = int(str(draft.payload.get("history_page", 0)))
                transaction = await edit_transaction(
                    session,
                    user.id,
                    UUID(str(draft.payload["transaction_id"])),
                    int(str(draft.payload["version"])),
                    account_id=account_id,
                )
                await clear_draft(session, user.id)
                details = await get_transaction_details(session, user.id, transaction.id)
                if details is None:
                    raise RuntimeError("updated transaction is not readable")
                receipt_text = transaction_card(details, user.timezone, title="✅ Счёт изменён")
                receipt_keyboard = transaction_keyboard(details.id, details.version, history_page)
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                    receipt_keyboard,
                )
                await session.commit()
        except (ValueError, FinbotError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if finbot_update_id is None:
            await _replace_message(
                message,
                receipt_text,
                receipt_keyboard,
            )
        await callback.answer("Изменено")

    @dp.callback_query(F.data.startswith("e:datepick:"))
    async def edit_date_choice(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        choice = callback.data.removeprefix("e:datepick:")
        try:
            async with sessions() as session:
                if not await claim_update(session, finbot_update_id):
                    await callback.answer("Уже обработано")
                    return
                user = await ensure_user(session, settings, message.chat.id)
                draft = await get_draft(session, user.id)
                if (
                    draft is None
                    or draft.state != "edit_date_menu"
                    or draft.suspended
                    or not _draft_message_matches(draft, message)
                ):
                    await session.commit()
                    await callback.answer("Выбор устарел", show_alert=True)
                    return
                payload = dict(draft.payload)
                history_page = int(str(payload.get("history_page", 0)))
                if choice == "custom":
                    updated = await put_draft(session, user.id, "edit_date", payload)
                    await session.commit()
                    await _replace_message(
                        message,
                        "<b>Изменить дату</b>\n\nВведите <code>ДД.ММ</code> "
                        "или <code>ДД.ММ.ГГГГ</code>.",
                        edit_input_keyboard(
                            date_menu=True,
                            draft_id=updated.id,
                            revision=updated.revision,
                        ),
                    )
                    await callback.answer()
                    return
                occurred = parse_local_datetime(
                    "сегодня" if choice == "today" else "вчера", user.timezone
                )
                transaction = await edit_transaction(
                    session,
                    user.id,
                    UUID(str(payload["transaction_id"])),
                    int(str(payload["version"])),
                    occurred_at=occurred,
                )
                await clear_draft(session, user.id)
                details = await get_transaction_details(session, user.id, transaction.id)
                if details is None:
                    raise RuntimeError("updated transaction is not readable")
                receipt_text = transaction_card(details, user.timezone, title="✅ Дата изменена")
                receipt_keyboard = transaction_keyboard(details.id, details.version, history_page)
                _queue_callback_receipt(
                    session,
                    settings,
                    finbot_update_id,
                    message,
                    receipt_text,
                    receipt_keyboard,
                )
                await session.commit()
        except (ValueError, FinbotError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if finbot_update_id is None:
            await _replace_message(
                message,
                receipt_text,
                receipt_keyboard,
            )
        await callback.answer("Изменено")

    @dp.callback_query(F.data == "e:dateback")
    async def edit_date_back(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if (
                draft is None
                or draft.state != "edit_date"
                or draft.suspended
                or not _draft_message_matches(draft, message)
            ):
                await session.commit()
                await callback.answer("Карточка устарела", show_alert=True)
                return
            payload = dict(draft.payload)
            details = await get_transaction_details(
                session, user.id, UUID(str(payload["transaction_id"]))
            )
            if details is None or details.deleted_at is not None:
                await session.commit()
                await callback.answer("Операция недоступна", show_alert=True)
                return
            history_page = int(str(payload.get("history_page", 0)))
            payload["version"] = details.version
            updated = await put_draft(session, user.id, "edit_date_menu", payload)
            await session.commit()
        await _replace_message(
            message,
            "<b>Изменить дату</b>\n\nВыберите дату или введите свою:",
            edit_date_keyboard(
                details.id,
                details.version,
                history_page,
                draft_id=updated.id,
                revision=updated.revision,
            ),
        )
        await callback.answer()

    @dp.callback_query(F.data == "e:back")
    async def edit_back(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            draft = await get_draft(session, user.id)
            if (
                draft is None
                or "transaction_id" not in draft.payload
                or draft.suspended
                or not _draft_message_matches(draft, message)
            ):
                await session.commit()
                await callback.answer("Карточка устарела", show_alert=True)
                return
            payload = dict(draft.payload)
            details = await get_transaction_details(
                session, user.id, UUID(str(payload["transaction_id"]))
            )
            if details is None or details.deleted_at is not None:
                await session.commit()
                await callback.answer("Операция недоступна", show_alert=True)
                return
            history_page = int(str(payload.get("history_page", 0)))
            payload["version"] = details.version
            updated = await put_draft(session, user.id, "edit_menu", payload)
            await session.commit()
        await _replace_message(
            message,
            transaction_card(details, user.timezone, title="Что изменить?"),
            edit_keyboard(
                details.id,
                details.version,
                history_page,
                draft_id=updated.id,
                revision=updated.revision,
            ),
        )
        await callback.answer()

    @dp.message(Command("undo"))
    @dp.message(F.text.in_({"↩️ Отменить действие", "↩️ Отменить"}))
    async def undo(message: Message, finbot_update_id: int | None = None) -> None:
        labels = {
            "create": "Создание операции отменено",
            "delete": "Удаление отменено",
            "restore": "Восстановление отменено",
            "update": "Последнее изменение отменено",
        }
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            result = await undo_last_action(session, user.id)
            details = (
                await get_transaction_details(session, user.id, result.transaction.id)
                if result is not None and result.transaction is not None
                else None
            )
            receipt_keyboard: InlineKeyboardMarkup | None = None
            if result is None:
                receipt_text = "Отменять пока нечего."
            elif details is None:
                receipt_text = labels.get(result.action, "Последнее действие отменено")
            else:
                receipt_keyboard = (
                    restore_keyboard(details.id, details.version)
                    if details.deleted_at
                    else transaction_keyboard(details.id, details.version)
                )
                receipt_text = transaction_card(
                    details,
                    user.timezone,
                    title=f"↩️ {labels.get(result.action, 'Действие отменено')}",
                )
            _queue_message_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await message.answer(
                receipt_text,
                parse_mode="HTML",
                reply_markup=receipt_keyboard,
            )

    @dp.message(Command("export"))
    @dp.message(F.text.in_({"📤 CSV", "📤 Экспорт"}))
    async def export_csv(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            suspended_draft = await _suspend_for_navigation(session, user)
            rows = await export_transaction_details(session, user.id)
            await session.commit()
        if not rows:
            await message.answer("Экспортировать пока нечего.")
            await _send_resume_notice(message, suspended_draft)
            return
        generated = datetime.now(ZoneInfo(user.timezone)).date().isoformat()
        filename = f"finbot-all-{generated}.csv"
        await message.answer_document(
            BufferedInputFile(build_csv(rows, user.timezone), filename=filename),
            caption=f"Готово: {len(rows)} операций · UTF-8 · разделитель «;»",
        )
        await _send_resume_notice(message, suspended_draft)

    @dp.message(Command("settings"))
    @dp.message(F.text == "⚙️ Настройки")
    async def show_settings(message: Message, finbot_update_id: int | None = None) -> None:
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            await _suspend_for_navigation(session, user)
            text, keyboard = await _settings_data(session, user)
            await session.commit()
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    @dp.callback_query(F.data == "s:back")
    async def settings_main(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            text, keyboard = await _settings_data(session, user)
            await session.commit()
        await _replace_message(message, text, keyboard)
        await callback.answer()

    @dp.callback_query(F.data.in_({"s:accounts", "sa:list"}))
    async def settings_accounts(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if await _has_catalog_input_draft(session, user.id):
                await session.commit()
                await callback.answer(
                    "Сначала отмените текущий ввод кнопкой под формой",
                    show_alert=True,
                )
                return
            text, keyboard = await _accounts_data(session, user)
            await session.commit()
        await _replace_message(message, text, keyboard)
        await callback.answer()

    @dp.callback_query(F.data.startswith("s:account:"))
    async def settings_account(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        del finbot_update_id
        await callback.answer(
            "Эта кнопка устарела. Откройте список счетов заново",
            show_alert=True,
        )

    @dp.callback_query(F.data.startswith("sa:view:"))
    async def settings_account_view(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id, expected_version = _parse_catalog_callback(callback.data, "sa:view:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if await _has_catalog_input_draft(session, user.id):
                await session.commit()
                await callback.answer(
                    "Сначала отмените текущий ввод кнопкой под формой",
                    show_alert=True,
                )
                return
            account = await session.scalar(
                select(Account).where(
                    Account.id == account_id,
                    Account.user_id == user.id,
                    Account.archived_at.is_(None),
                )
            )
            if account is None or account.version != expected_version:
                await session.commit()
                await callback.answer("Счёт изменился. Обновите список", show_alert=True)
                return
            await session.commit()
        is_default = account.id == user.default_account_id
        await _replace_message(
            message,
            _account_card(account, is_default),
            settings_account_keyboard(account.id, account.version, is_default=is_default),
        )
        await callback.answer()

    @dp.callback_query(F.data == "sa:new")
    async def settings_account_new(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            if active is not None:
                await session.commit()
                await callback.answer(
                    "Сначала отмените или завершите текущий ввод",
                    show_alert=True,
                )
                return
            draft = await put_draft(
                session,
                user.id,
                "settings_account_create",
                {"ui_message_id": message.message_id},
            )
            await session.commit()
        await _replace_message(
            message,
            "<b>Новый счёт</b>\n\nВведите название, например «Наличные» или «Карта Мир».",
            settings_text_input_keyboard(draft.id, draft.revision),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sa:rename:"))
    async def settings_account_rename(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id, expected_version = _parse_catalog_callback(callback.data, "sa:rename:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            if active is not None:
                await session.commit()
                await callback.answer(
                    "Сначала отмените или завершите текущий ввод",
                    show_alert=True,
                )
                return
            account = await session.scalar(
                select(Account).where(
                    Account.id == account_id,
                    Account.user_id == user.id,
                    Account.archived_at.is_(None),
                )
            )
            if account is None or account.version != expected_version:
                await session.commit()
                await callback.answer("Счёт изменился. Обновите список", show_alert=True)
                return
            draft = await put_draft(
                session,
                user.id,
                "settings_account_rename",
                {
                    "account_id": str(account.id),
                    "object_version": account.version,
                    "ui_message_id": message.message_id,
                },
            )
            await session.commit()
        await _replace_message(
            message,
            f"<b>Переименовать счёт</b>\n\nСейчас: <b>{escape(account.name)}</b>\n"
            "Введите новое название.",
            settings_text_input_keyboard(draft.id, draft.revision),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sa:default:"))
    async def settings_account_default(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id, expected_version = _parse_catalog_callback(callback.data, "sa:default:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            try:
                account = await set_default_account(session, user.id, account_id, expected_version)
            except FinbotError as exc:
                await session.commit()
                await callback.answer(str(exc), show_alert=True)
                return
            receipt_text = _account_card(account, True)
            receipt_keyboard = settings_account_keyboard(
                account.id, account.version, is_default=True
            )
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Основной счёт изменён")

    @dp.callback_query(F.data.startswith("sa:archive:ask:"))
    async def settings_account_archive_ask(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id, expected_version = _parse_catalog_callback(callback.data, "sa:archive:ask:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            account = await session.scalar(
                select(Account).where(
                    Account.id == account_id,
                    Account.user_id == user.id,
                    Account.archived_at.is_(None),
                )
            )
            if account is None or account.version != expected_version:
                await session.commit()
                await callback.answer("Счёт изменился. Обновите список", show_alert=True)
                return
            if account.id == user.default_account_id:
                await session.commit()
                await callback.answer("Сначала выберите другой основной счёт", show_alert=True)
                return
            await session.commit()
        await _replace_message(
            message,
            f"<b>Архивировать «{escape(account.name)}»?</b>\n\n"
            "Счёт исчезнет из нового ввода, но останется в истории и отчётах.",
            settings_account_archive_keyboard(account.id, account.version),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sa:archive:do:"))
    async def settings_account_archive_do(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id, expected_version = _parse_catalog_callback(callback.data, "sa:archive:do:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            try:
                await archive_account(
                    session,
                    user.id,
                    account_id,
                    user.default_account_id,
                    expected_version,
                )
            except (ValueError, FinbotError) as exc:
                await session.commit()
                await callback.answer(str(exc), show_alert=True)
                return
            receipt_text, receipt_keyboard = await _accounts_data(session, user)
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Счёт перенесён в архив")

    @dp.callback_query(F.data == "sa:archived")
    async def settings_accounts_archived(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            accounts = await _account_choices(session, user.id, archived=True)
            await session.commit()
        await _replace_message(
            message,
            "<b>Архив счетов</b>\n\nНажмите на счёт, чтобы восстановить его.",
            settings_archived_accounts_keyboard(accounts),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sa:restore:"))
    async def settings_account_restore(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            account_id, expected_version = _parse_catalog_callback(callback.data, "sa:restore:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            try:
                account = await restore_account(session, user.id, account_id, expected_version)
            except FinbotError as exc:
                await session.commit()
                await callback.answer(str(exc), show_alert=True)
                return
            is_default = account.id == user.default_account_id
            receipt_text = _account_card(account, is_default)
            receipt_keyboard = settings_account_keyboard(
                account.id,
                account.version,
                is_default=is_default,
            )
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Счёт восстановлен")

    @dp.callback_query(F.data == "sc:root")
    async def settings_categories(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if await _has_catalog_input_draft(session, user.id):
                await session.commit()
                await callback.answer(
                    "Сначала отмените текущий ввод кнопкой под формой",
                    show_alert=True,
                )
                return
            text, keyboard = await _categories_data(session, user.id)
            await session.commit()
        await _replace_message(message, text, keyboard)
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:list:"))
    async def settings_category_list(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        kind = callback.data.removeprefix("sc:list:")
        if kind not in {"expense", "income"}:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if await _has_catalog_input_draft(session, user.id):
                await session.commit()
                await callback.answer(
                    "Сначала отмените текущий ввод кнопкой под формой",
                    show_alert=True,
                )
                return
            text, keyboard = await _category_list_data(session, user.id, kind)
            await session.commit()
        await _replace_message(message, text, keyboard)
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:view:"))
    async def settings_category_view(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            category_id, expected_version = _parse_catalog_callback(callback.data, "sc:view:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if await _has_catalog_input_draft(session, user.id):
                await session.commit()
                await callback.answer(
                    "Сначала отмените текущий ввод кнопкой под формой",
                    show_alert=True,
                )
                return
            category = await session.scalar(
                select(Category).where(
                    Category.id == category_id,
                    Category.user_id == user.id,
                    Category.archived_at.is_(None),
                )
            )
            if category is None or category.version != expected_version:
                await session.commit()
                await callback.answer("Категория изменилась. Обновите список", show_alert=True)
                return
            await session.commit()
        await _replace_message(
            message,
            _category_card(category),
            settings_category_keyboard(category.id, category.kind, category.version),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:new:"))
    async def settings_category_new(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        kind = callback.data.removeprefix("sc:new:")
        if kind not in {"expense", "income"}:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            if active is not None:
                await session.commit()
                await callback.answer(
                    "Сначала отмените или завершите текущий ввод",
                    show_alert=True,
                )
                return
            draft = await put_draft(
                session,
                user.id,
                "settings_category_create",
                {"kind": kind, "ui_message_id": message.message_id},
            )
            await session.commit()
        title = "расходов" if kind == "expense" else "доходов"
        await _replace_message(
            message,
            f"<b>Новая категория {title}</b>\n\nВведите короткое понятное название.",
            settings_text_input_keyboard(draft.id, draft.revision),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:rename:"))
    async def settings_category_rename(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            category_id, expected_version = _parse_catalog_callback(callback.data, "sc:rename:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            if active is not None:
                await session.commit()
                await callback.answer(
                    "Сначала отмените или завершите текущий ввод",
                    show_alert=True,
                )
                return
            category = await session.scalar(
                select(Category).where(
                    Category.id == category_id,
                    Category.user_id == user.id,
                    Category.archived_at.is_(None),
                )
            )
            if category is None or category.version != expected_version:
                await session.commit()
                await callback.answer("Категория изменилась. Обновите список", show_alert=True)
                return
            draft = await put_draft(
                session,
                user.id,
                "settings_category_rename",
                {
                    "category_id": str(category.id),
                    "kind": category.kind,
                    "object_version": category.version,
                    "ui_message_id": message.message_id,
                },
            )
            await session.commit()
        await _replace_message(
            message,
            f"<b>Переименовать категорию</b>\n\nСейчас: "
            f"<b>{escape(category.name)}</b>\nВведите новое название.",
            settings_text_input_keyboard(draft.id, draft.revision),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:archive:ask:"))
    async def settings_category_archive_ask(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            category_id, expected_version = _parse_catalog_callback(
                callback.data, "sc:archive:ask:"
            )
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            category = await session.scalar(
                select(Category).where(
                    Category.id == category_id,
                    Category.user_id == user.id,
                    Category.archived_at.is_(None),
                )
            )
            if category is None or category.version != expected_version:
                await session.commit()
                await callback.answer("Категория изменилась. Обновите список", show_alert=True)
                return
            await session.commit()
        await _replace_message(
            message,
            f"<b>Архивировать «{escape(category.name)}»?</b>\n\n"
            "Она исчезнет из нового ввода, но останется в истории и отчётах.",
            settings_category_archive_keyboard(category.id, category.version),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:archive:do:"))
    async def settings_category_archive_do(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            category_id, expected_version = _parse_catalog_callback(callback.data, "sc:archive:do:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            try:
                category = await archive_category(session, user.id, category_id, expected_version)
            except (ValueError, FinbotError) as exc:
                await session.commit()
                await callback.answer(str(exc), show_alert=True)
                return
            receipt_text, receipt_keyboard = await _category_list_data(
                session, user.id, category.kind
            )
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Категория перенесена в архив")

    @dp.callback_query(F.data.startswith("sc:archived:"))
    async def settings_categories_archived(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        kind = callback.data.removeprefix("sc:archived:")
        if kind not in {"expense", "income"}:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            categories = await _category_choices(session, user.id, kind, archived=True)
            await session.commit()
        await _replace_message(
            message,
            "<b>Архив категорий</b>\n\nНажмите на категорию, чтобы восстановить её.",
            settings_archived_categories_keyboard(categories, kind),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sc:restore:"))
    async def settings_category_restore(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            category_id, expected_version = _parse_catalog_callback(callback.data, "sc:restore:")
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            try:
                category = await restore_category(session, user.id, category_id, expected_version)
            except FinbotError as exc:
                await session.commit()
                await callback.answer(str(exc), show_alert=True)
                return
            receipt_text = _category_card(category)
            receipt_keyboard = settings_category_keyboard(
                category.id, category.kind, category.version
            )
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Категория восстановлена")

    @dp.callback_query(F.data == "s:timezones")
    async def settings_timezones(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if await _has_catalog_input_draft(session, user.id):
                await session.commit()
                await callback.answer(
                    "Сначала отмените текущий ввод кнопкой под формой",
                    show_alert=True,
                )
                return
            await session.commit()
        await _replace_message(
            message,
            "<b>Часовой пояс</b>\n\nОт него зависят «сегодня», отчёты и даты операций.",
            settings_timezones_keyboard(user.timezone),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("s:timezone:"))
    async def settings_timezone(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target_index, expected_timezone_token = callback.data.removeprefix("s:timezone:").split(
                ":", 1
            )
            timezone = TIMEZONES[int(target_index)][0]
        except ValueError, IndexError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            if timezone_token(user.timezone) != expected_timezone_token:
                await session.commit()
                await callback.answer(
                    "Часовой пояс уже изменился. Обновите настройки",
                    show_alert=True,
                )
                return
            user.timezone = timezone
            receipt_text, receipt_keyboard = await _settings_data(session, user)
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Часовой пояс изменён")

    async def settings_reset(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            user = await ensure_user(session, settings, message.chat.id)
            await clear_draft(session, user.id)
            receipt_text, receipt_keyboard = await _settings_data(session, user)
            _queue_callback_receipt(
                session,
                settings,
                finbot_update_id,
                message,
                receipt_text,
                receipt_keyboard,
            )
            await session.commit()
        if finbot_update_id is None:
            await _replace_message(message, receipt_text, receipt_keyboard)
        await callback.answer("Незавершённый ввод сброшен")

    @dp.callback_query(F.data == "s:help")
    async def settings_help(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        message = _callback_message(callback)
        if message is None:
            await callback.answer()
            return
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                await callback.answer("Уже обработано")
                return
            await ensure_user(session, settings, message.chat.id)
            await session.commit()
        await _replace_message(message, HELP_TEXT, settings_help_keyboard())
        await callback.answer()

    @dp.message(F.photo | F.document)
    async def image_input(
        message: Message,
        bot: Bot,
        finbot_update_id: int | None = None,
    ) -> None:
        if finbot_update_id is not None:
            async with sessions() as replay_session:
                if await is_update_processed(replay_session, finbot_update_id):
                    return
        file_id = ""
        mime_type = ""
        file_size: int | None = None
        if message.photo:
            image = message.photo[-1]
            file_id = image.file_id
            file_size = image.file_size
            mime_type = "image/jpeg"
        elif message.document is not None:
            file_id = message.document.file_id
            file_size = message.document.file_size
            mime_type = (message.document.mime_type or "").lower()

        recognized_text: str | None = None
        recognition_error: str | None = None
        try:
            if not file_id or mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
                raise OcrImportError("Отправьте изображение JPEG, PNG или WebP")
            if file_size is not None and file_size > MAX_IMAGE_BYTES:
                raise OcrImportError("Изображение слишком большое — максимум 10 МБ")
            downloaded = await bot.download(
                file_id,
                destination=BoundedImageBuffer(),
                timeout=20,
            )
            if downloaded is None:
                raise OcrImportError("Не удалось загрузить изображение из Telegram")
            if isinstance(downloaded, BytesIO):
                content = downloaded.getvalue()
            else:
                raw_content = downloaded.read()
                if not isinstance(raw_content, bytes):
                    raise OcrImportError("Не удалось прочитать изображение")
                content = raw_content
            recognized_text = await ocr.extract_text(content, mime_type)
        except OcrImportError as exc:
            recognition_error = str(exc)
        except TelegramBadRequest:
            recognition_error = "Telegram не смог загрузить это изображение"

        response_text = ""
        response_keyboard: InlineKeyboardMarkup | None = None
        queued = False
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            draft: Draft | None = None
            payload: dict[str, object] | None = None
            if recognition_error is not None:
                response_text = (
                    "<b>Не получилось распознать операцию</b>\n\n"
                    f"{escape(recognition_error)}.\n\n"
                    "Попробуйте более чёткий скриншот или отправьте операцию текстом."
                )
            elif active is not None:
                response_text = (
                    "<b>Есть незавершённый ввод</b>\n\n"
                    "Завершите или отмените текущий черновик, затем отправьте изображение ещё раз."
                )
                response_keyboard = resume_draft_keyboard(active.id, active.revision)
            else:
                if recognized_text is None:
                    raise RuntimeError("OCR completed without text or a bounded error")
                try:
                    default_account = await resolve_account(
                        session,
                        user.id,
                        None,
                        user.default_account_id,
                    )
                    parsed_items = parse_ocr_transactions(
                        recognized_text,
                        user.timezone,
                        _currency_code(default_account.currency, user.base_currency),
                    )
                    parsed = parsed_items[0]
                    state, payload, _prompt, _keyboard = await _prepare_parsed_input(
                        session,
                        user,
                        parsed,
                        flow="ocr",
                    )
                    if len(parsed_items) > 1:
                        payload["ocr_batch"] = {
                            "version": 1,
                            "index": 1,
                            "total": len(parsed_items),
                            "saved": 0,
                            "skipped": 0,
                            "remaining": [_serialize_ocr_draft(item) for item in parsed_items[1:]],
                        }
                    draft = await start_draft(session, user.id, state, payload)
                    response_text, response_keyboard = await _draft_screen(
                        session,
                        user,
                        state,
                        payload,
                        draft.id,
                        draft.revision,
                    )
                except OcrImportError as exc:
                    response_text = (
                        "<b>Нужно уточнение</b>\n\n"
                        f"{escape(str(exc))}.\n\n"
                        "Отправьте сумму и назначение текстом, например <code>1450 продукты</code>."
                    )

            if finbot_update_id is not None:
                queue_send_message(
                    session,
                    update_id=finbot_update_id,
                    owner_telegram_user_id=settings.owner_telegram_user_id,
                    chat_id=message.chat.id,
                    text=response_text,
                    parse_mode="HTML",
                    reply_markup=response_keyboard,
                    draft_id=draft.id if draft is not None else None,
                )
                queued = True
            else:
                sent = await message.answer(
                    response_text,
                    parse_mode="HTML",
                    reply_markup=response_keyboard,
                )
                if draft is not None and payload is not None:
                    payload["ui_message_id"] = sent.message_id
                    draft.payload = dict(payload)
                    draft.presentation_ref = str(sent.message_id)
            await session.commit()
        if not queued:
            return

    @dp.message()
    async def text_input(message: Message, finbot_update_id: int | None = None) -> None:
        if not message.text:
            return
        details: TransactionDetails | None = None
        response_error: str | None = None
        async with sessions() as session:
            if not await claim_update(session, finbot_update_id):
                return
            user = await ensure_user(session, settings, message.chat.id)
            active = await get_draft(session, user.id)
            try:
                if active is not None and active.suspended:
                    payload = dict(active.payload)
                    payload["pending_intent"] = {"kind": "quick", "text": message.text}
                    active = await put_draft(session, user.id, active.state, payload)
                    await message.answer(
                        "<b>Есть приостановленный черновик</b>\n\n"
                        "Выберите, продолжить его или заменить новым быстрым вводом.",
                        parse_mode="HTML",
                        reply_markup=draft_conflict_keyboard(active.id, active.revision),
                    )
                    await session.commit()
                    return
                if active is not None and active.state == "settings_account_create":
                    payload = dict(active.payload)
                    try:
                        account = await create_account(
                            session, user.id, message.text, user.base_currency
                        )
                    except (ValueError, FinbotError) as exc:
                        await _render_from_input(
                            message,
                            payload,
                            f"<b>Новый счёт</b>\n\n{escape(str(exc))}. Введите другое название.",
                            settings_text_input_keyboard(active.id, active.revision + 1),
                        )
                        await put_draft(session, user.id, "settings_account_create", payload)
                        await session.commit()
                        return
                    if user.default_account_id is None:
                        account = await set_default_account(
                            session, user.id, account.id, account.version
                        )
                    await clear_draft(session, user.id)
                    is_default = account.id == user.default_account_id
                    receipt_text = _account_card(account, is_default)
                    receipt_keyboard = settings_account_keyboard(
                        account.id,
                        account.version,
                        is_default=is_default,
                    )
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None and active.state == "settings_account_rename":
                    payload = dict(active.payload)
                    try:
                        account = await rename_account(
                            session,
                            user.id,
                            UUID(str(payload["account_id"])),
                            message.text,
                            int(str(payload.get("object_version", 0))),
                        )
                    except (ValueError, FinbotError) as exc:
                        await _render_from_input(
                            message,
                            payload,
                            "<b>Переименовать счёт</b>\n\n"
                            f"{escape(str(exc))}. Введите другое название.",
                            settings_text_input_keyboard(active.id, active.revision + 1),
                        )
                        await put_draft(session, user.id, "settings_account_rename", payload)
                        await session.commit()
                        return
                    await clear_draft(session, user.id)
                    is_default = account.id == user.default_account_id
                    receipt_text = _account_card(account, is_default)
                    receipt_keyboard = settings_account_keyboard(
                        account.id,
                        account.version,
                        is_default=is_default,
                    )
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None and active.state == "settings_category_create":
                    payload = dict(active.payload)
                    kind = str(payload["kind"])
                    try:
                        category = await create_category(session, user.id, message.text, kind)
                    except (ValueError, FinbotError) as exc:
                        await _render_from_input(
                            message,
                            payload,
                            "<b>Новая категория</b>\n\n"
                            f"{escape(str(exc))}. Введите другое название.",
                            settings_text_input_keyboard(active.id, active.revision + 1),
                        )
                        await put_draft(session, user.id, "settings_category_create", payload)
                        await session.commit()
                        return
                    await clear_draft(session, user.id)
                    receipt_text = _category_card(category)
                    receipt_keyboard = settings_category_keyboard(
                        category.id, category.kind, category.version
                    )
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None and active.state == "settings_category_rename":
                    payload = dict(active.payload)
                    try:
                        category = await rename_category(
                            session,
                            user.id,
                            UUID(str(payload["category_id"])),
                            message.text,
                            int(str(payload.get("object_version", 0))),
                        )
                    except (ValueError, FinbotError) as exc:
                        await _render_from_input(
                            message,
                            payload,
                            "<b>Переименовать категорию</b>\n\n"
                            f"{escape(str(exc))}. Введите другое название.",
                            settings_text_input_keyboard(active.id, active.revision + 1),
                        )
                        await put_draft(session, user.id, "settings_category_rename", payload)
                        await session.commit()
                        return
                    await clear_draft(session, user.id)
                    receipt_text = _category_card(category)
                    receipt_keyboard = settings_category_keyboard(
                        category.id, category.kind, category.version
                    )
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None and active.state == "wizard_amount":
                    payload = dict(active.payload)
                    try:
                        payload["amount"] = parse_minor(message.text)
                    except MoneyError as exc:
                        await _render_from_input(
                            message,
                            payload,
                            f"<b>Новая операция · 2/6</b>\n\n{escape(str(exc))}. "
                            "Попробуйте ещё раз, например <code>1450</code>.",
                            wizard_input_keyboard(active.id, active.revision + 1),
                        )
                        await put_draft(session, user.id, "wizard_amount", payload)
                        await session.commit()
                        return
                    categories = await _category_choices(session, user.id, str(payload["type"]))
                    await _render_from_input(
                        message,
                        payload,
                        "<b>Новая операция · 3/6</b>\n\nВыберите категорию:",
                        category_keyboard(
                            categories,
                            draft_id=active.id,
                            revision=active.revision + 1,
                        ),
                    )
                    await put_draft(session, user.id, "wizard_category", payload)
                    await session.commit()
                    return
                if active is not None and active.state == "custom_category":
                    payload = dict(active.payload)
                    previous_category_id = payload.get("category_id")
                    custom_back_state = str(payload.get("custom_back_state", ""))
                    name = message.text.strip()
                    if len(name) > 60:
                        raise ValueError("Название категории должно быть короче 60 символов")
                    category = await create_or_get_category(
                        session, user.id, name, str(payload["type"])
                    )
                    payload.pop("custom_back_state", None)
                    await _attach_category(session, user, payload, category)
                    if custom_back_state == "review_category":
                        _stage_rule_offer(payload, previous_category_id)
                        state = str(payload.pop("review_return_state", "quick_confirm"))
                        receipt_text = wizard_summary(payload, user.base_currency, user.timezone)
                        receipt_keyboard = _review_keyboard(payload, active.id, active.revision + 1)
                    else:
                        accounts = await _account_choices(session, user.id)
                        receipt_text = (
                            "<b>Новая операция · 4/6</b>\n\n"
                            f"Категория «{escape(category.name)}» создана. Выберите счёт:"
                        )
                        receipt_keyboard = account_keyboard(
                            accounts,
                            draft_id=active.id,
                            revision=active.revision + 1,
                        )
                        state = (
                            "wizard_account"
                            if str(payload.get("flow")) == "wizard"
                            else "quick_account"
                        )
                    updated = await put_draft(session, user.id, state, payload)
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                        updated.id,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None and active.state == "custom_account":
                    payload = dict(active.payload)
                    custom_back_state = str(payload.get("custom_back_state", ""))
                    name = message.text.strip()
                    if len(name) > 60:
                        raise ValueError("Название счёта должно быть короче 60 символов")
                    account = await create_or_get_account(
                        session, user.id, name, user.base_currency
                    )
                    payload.pop("custom_back_state", None)
                    await _attach_account(session, user, payload, account)
                    if custom_back_state == "review_account":
                        receipt_text = wizard_summary(payload, user.base_currency, user.timezone)
                        receipt_keyboard = _review_keyboard(payload, active.id, active.revision + 1)
                        updated = await put_draft(
                            session,
                            user.id,
                            str(payload.pop("review_return_state", "quick_confirm")),
                            payload,
                        )
                    elif str(payload.get("flow")) == "wizard":
                        receipt_text = "<b>Новая операция · 5/6</b>\n\nКогда произошла операция?"
                        receipt_keyboard = wizard_date_keyboard(active.id, active.revision + 1)
                        updated = await put_draft(session, user.id, "wizard_date", payload)
                    else:
                        receipt_text = wizard_summary(payload, user.base_currency, user.timezone)
                        receipt_keyboard = _review_keyboard(payload, active.id, active.revision + 1)
                        updated = await put_draft(session, user.id, "quick_confirm", payload)
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                        updated.id,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None and active.state == "custom_date":
                    payload = dict(active.payload)
                    occurred = parse_local_datetime(message.text, user.timezone)
                    payload["occurred_at"] = occurred.isoformat()
                    payload["return_state"] = "wizard_confirm"
                    payload["description_back_state"] = "wizard_date"
                    await _render_from_input(
                        message,
                        payload,
                        "<b>Новая операция · 6/6</b>\n\n"
                        "Введите комментарий или нажмите «Без комментария».",
                        wizard_description_keyboard(active.id, active.revision + 1),
                    )
                    await put_draft(session, user.id, "wizard_description", payload)
                    await session.commit()
                    return
                if active is not None and active.state == "wizard_description":
                    payload = dict(active.payload)
                    description = message.text.strip()
                    if len(description) > 500:
                        raise ValueError("Комментарий должен быть короче 500 символов")
                    payload["description"] = "" if description == "-" else description
                    _clear_rule_learning(payload)
                    return_state = str(payload.pop("return_state", "wizard_confirm"))
                    payload.pop("description_back_state", None)
                    await _render_from_input(
                        message,
                        payload,
                        wizard_summary(payload, user.base_currency, user.timezone),
                        _review_keyboard(payload, active.id, active.revision + 1),
                    )
                    await put_draft(session, user.id, return_state, payload)
                    await session.commit()
                    return
                if active is not None and active.state in {
                    "review_amount",
                    "review_date_input",
                }:
                    payload = dict(active.payload)
                    try:
                        if active.state == "review_amount":
                            payload["amount"] = parse_minor(message.text)
                        else:
                            payload["occurred_at"] = parse_local_datetime(
                                message.text, user.timezone
                            ).isoformat()
                    except (MoneyError, ValueError) as exc:
                        field = "сумму" if active.state == "review_amount" else "дату"
                        await _render_from_input(
                            message,
                            payload,
                            f"<b>Изменить {field}</b>\n\n{escape(str(exc))}. Попробуйте ещё раз.",
                            wizard_input_keyboard(active.id, active.revision + 1),
                        )
                        await put_draft(session, user.id, active.state, payload)
                        await session.commit()
                        return
                    return_state = str(payload.pop("review_return_state", "quick_confirm"))
                    await _render_from_input(
                        message,
                        payload,
                        wizard_summary(payload, user.base_currency, user.timezone),
                        _review_keyboard(payload, active.id, active.revision + 1),
                    )
                    await put_draft(session, user.id, return_state, payload)
                    await session.commit()
                    return
                if active is not None and active.state in {
                    "edit_amount",
                    "edit_date",
                    "edit_description",
                }:
                    payload = dict(active.payload)
                    kwargs: dict[str, object] = {}
                    title = "✅ Операция изменена"
                    if active.state == "edit_amount":
                        kwargs["amount_minor"] = parse_minor(message.text)
                        title = "✅ Сумма изменена"
                    elif active.state == "edit_date":
                        kwargs["occurred_at"] = parse_local_datetime(message.text, user.timezone)
                        title = "✅ Дата изменена"
                    else:
                        description = message.text.strip()
                        if len(description) > 500:
                            raise ValueError("Комментарий должен быть короче 500 символов")
                        kwargs["description"] = "" if description == "-" else description
                        title = "✅ Комментарий изменён"
                    transaction = await edit_transaction(
                        session,
                        user.id,
                        UUID(str(payload["transaction_id"])),
                        int(str(payload["version"])),
                        **kwargs,  # type: ignore[arg-type]
                    )
                    await clear_draft(session, user.id)
                    details = await get_transaction_details(session, user.id, transaction.id)
                    if details is None:
                        raise RuntimeError("updated transaction is not readable")
                    receipt_text = transaction_card(details, user.timezone, title=title)
                    receipt_keyboard = transaction_keyboard(
                        details.id,
                        details.version,
                        int(str(payload.get("history_page", 0))),
                    )
                    if not _queue_input_receipt(
                        session,
                        settings,
                        finbot_update_id,
                        message,
                        payload,
                        receipt_text,
                        receipt_keyboard,
                    ):
                        await _render_from_input(
                            message,
                            payload,
                            receipt_text,
                            receipt_keyboard,
                        )
                    await session.commit()
                    return
                if active is not None:
                    payload = dict(active.payload)
                    payload["pending_intent"] = {"kind": "quick", "text": message.text}
                    active = await put_draft(session, user.id, active.state, payload)
                    await message.answer(
                        "<b>Есть незавершённый ввод</b>\n\n"
                        "Новый текст не изменил черновик. Выберите нужное действие.",
                        parse_mode="HTML",
                        reply_markup=draft_conflict_keyboard(active.id, active.revision),
                    )
                    await session.commit()
                    return

                state, payload, prompt, keyboard = await _prepare_quick_input(
                    session, user, message.text
                )
                draft = await start_draft(session, user.id, state, payload)
                prompt, keyboard = await _draft_screen(
                    session, user, state, payload, draft.id, draft.revision
                )
                sent = await message.answer(
                    prompt,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                payload["ui_message_id"] = sent.message_id
                draft.payload = dict(payload)
                draft.presentation_ref = str(sent.message_id)
                await session.commit()
            except (MoneyError, ValueError, FinbotError) as exc:
                response_error = str(exc)
                await session.commit()
        if response_error is not None:
            await message.answer(
                f"Не получилось распознать операцию: {escape(response_error)}\n\n"
                "Пример: <code>1450 ресторан</code> или используйте «➕ Добавить операцию».",
                parse_mode="HTML",
            )
            return
        if details is not None:
            await message.answer(
                transaction_card(details, user.timezone, title="✅ Операция сохранена"),
                parse_mode="HTML",
                reply_markup=transaction_keyboard(details.id, details.version),
            )

    @dp.callback_query(F.data.startswith("d"))
    async def versioned_draft_button(
        callback: CallbackQuery, finbot_update_id: int | None = None
    ) -> None:
        message = _callback_message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            interaction = DraftInteraction.decode(callback.data)
        except InteractionCodecError:
            await callback.answer("Кнопка повреждена или устарела", show_alert=True)
            return
        conflict_actions = {
            DraftAction.RESUME,
            DraftAction.REPLACE,
            DraftAction.KEEP,
            DraftAction.DISCARD,
        }
        async with sessions() as session:
            user = await session.scalar(
                select(User).where(User.telegram_user_id == settings.owner_telegram_user_id)
            )
            if user is None:
                await callback.answer("Черновик не найден", show_alert=True)
                return
            draft = await get_draft(session, user.id)
            if (
                draft is None
                or not interaction.is_current(draft.id, draft.revision)
                or (draft.suspended and interaction.action not in conflict_actions)
                or (
                    interaction.action not in conflict_actions
                    and not _draft_message_matches(draft, message)
                )
            ):
                await callback.answer(
                    "Форма уже изменилась. Откройте актуальный черновик",
                    show_alert=True,
                )
                return
            edit_state_by_action = {
                DraftAction.TX_EDIT_AMOUNT: {"edit_menu"},
                DraftAction.TX_EDIT_CATEGORY: {"edit_menu"},
                DraftAction.TX_EDIT_ACCOUNT: {"edit_menu"},
                DraftAction.TX_EDIT_DATE: {"edit_menu"},
                DraftAction.TX_EDIT_DESCRIPTION: {"edit_menu"},
                DraftAction.TX_SELECT_CATEGORY: {"edit_category"},
                DraftAction.TX_SELECT_ACCOUNT: {"edit_account"},
                DraftAction.TX_SELECT_DATE: {"edit_date_menu"},
                DraftAction.TX_DATE_BACK: {"edit_date"},
                DraftAction.TX_BACK: {
                    "edit_amount",
                    "edit_category",
                    "edit_account",
                    "edit_date_menu",
                    "edit_description",
                },
            }
            allowed_states = edit_state_by_action.get(interaction.action)
            if allowed_states is not None and draft.state not in allowed_states:
                await callback.answer("Экран редактирования устарел", show_alert=True)
                return
            category_actions = {
                DraftAction.SELECT_CATEGORY,
                DraftAction.TX_SELECT_CATEGORY,
            }
            if (
                interaction.action == DraftAction.TX_SELECT_CATEGORY
                and interaction.object_id is None
            ):
                await callback.answer("Кнопка категории повреждена", show_alert=True)
                return
            if interaction.action in category_actions and interaction.object_id:
                category = await session.scalar(
                    select(Category).where(
                        Category.id == interaction.object_id,
                        Category.user_id == user.id,
                    )
                )
                if (
                    category is None
                    or category.version != interaction.object_version
                    or category.archived_at is not None
                ):
                    await callback.answer("Категория изменилась", show_alert=True)
                    return
            account_actions = {
                DraftAction.SELECT_ACCOUNT,
                DraftAction.TX_SELECT_ACCOUNT,
            }
            if (
                interaction.action == DraftAction.TX_SELECT_ACCOUNT
                and interaction.object_id is None
            ):
                await callback.answer("Кнопка счёта повреждена", show_alert=True)
                return
            if interaction.action in account_actions and interaction.object_id:
                account = await session.scalar(
                    select(Account).where(
                        Account.id == interaction.object_id,
                        Account.user_id == user.id,
                    )
                )
                if (
                    account is None
                    or account.version != interaction.object_version
                    or account.archived_at is not None
                ):
                    await callback.answer("Счёт изменился", show_alert=True)
                    return
            state = draft.state
            edit_payload = dict(draft.payload) if allowed_states is not None else None

        direct: dict[DraftAction, tuple[str, object]] = {
            DraftAction.CONFIRM: ("w:confirm", confirm_wizard),
            DraftAction.CANCEL: ("w:cancel", cancel_wizard),
            DraftAction.BACK: ("w:back", wizard_back),
            DraftAction.EDIT_TYPE: ("w:review:type", review_field),
            DraftAction.EDIT_AMOUNT: ("w:review:amount", review_field),
            DraftAction.EDIT_CATEGORY: ("w:review:category", review_field),
            DraftAction.EDIT_ACCOUNT: ("w:review:account", review_field),
            DraftAction.EDIT_DATE: ("w:review:date", review_field),
            DraftAction.EDIT_DESCRIPTION: ("w:description", wizard_description),
            DraftAction.SKIP_DESCRIPTION: ("w:description:skip", skip_wizard_description),
            DraftAction.RESUME: ("d:resume", resolve_draft_conflict),
            DraftAction.REPLACE: ("d:replace", resolve_draft_conflict),
            DraftAction.KEEP: ("d:keep", resolve_draft_conflict),
            DraftAction.RULE_GLOBAL: ("w:rule:global", stage_category_rule),
            DraftAction.RULE_ACCOUNT: ("w:rule:account", stage_category_rule),
            DraftAction.RULE_REMOVE: ("w:rule:remove", stage_category_rule),
            DraftAction.SKIP_OCR_ITEM: ("w:ocr:skip", skip_ocr_item),
            DraftAction.DISCARD: ("_s:reset", settings_reset),
            DraftAction.TX_DATE_BACK: ("e:dateback", edit_date_back),
            DraftAction.TX_BACK: ("e:back", edit_back),
        }
        if interaction.action in direct:
            legacy, handler = direct[interaction.action]
        elif interaction.action == DraftAction.SELECT_TYPE:
            suffix = "income" if interaction.page == 1 else "expense"
            legacy = f"w:review:settype:{suffix}" if state == "review_type" else f"w:type:{suffix}"
            handler = review_field if state == "review_type" else wizard_type
        elif interaction.action == DraftAction.SELECT_CATEGORY:
            legacy = f"w:cat:{interaction.object_id or 'new'}"
            handler = choose_category
        elif interaction.action == DraftAction.SELECT_ACCOUNT:
            legacy = f"w:acct:{interaction.object_id or 'new'}"
            handler = choose_account
        elif interaction.action == DraftAction.SELECT_DATE:
            date_key = (
                {0: "today", 1: "yesterday", 2: "custom"}.get(interaction.page)
                if interaction.page is not None
                else None
            )
            if date_key is None:
                await callback.answer("Кнопка повреждена", show_alert=True)
                return
            legacy = (
                f"w:review:setdate:{date_key}" if state == "review_date" else f"w:date:{date_key}"
            )
            handler = review_field if state == "review_date" else choose_date
        elif interaction.action in {
            DraftAction.TX_EDIT_AMOUNT,
            DraftAction.TX_EDIT_CATEGORY,
            DraftAction.TX_EDIT_ACCOUNT,
            DraftAction.TX_EDIT_DATE,
            DraftAction.TX_EDIT_DESCRIPTION,
        }:
            if edit_payload is None:
                await callback.answer("Экран редактирования устарел", show_alert=True)
                return
            try:
                transaction_id = UUID(str(edit_payload["transaction_id"]))
                transaction_version = int(str(edit_payload["version"]))
                history_page = int(str(edit_payload.get("history_page", 0)))
            except KeyError, ValueError:
                await callback.answer("Черновик редактирования повреждён", show_alert=True)
                return
            edit_field = {
                DraftAction.TX_EDIT_AMOUNT: "amount",
                DraftAction.TX_EDIT_CATEGORY: "category",
                DraftAction.TX_EDIT_ACCOUNT: "account",
                DraftAction.TX_EDIT_DATE: "date",
                DraftAction.TX_EDIT_DESCRIPTION: "description",
            }[interaction.action]
            legacy = f"e:{edit_field}:{transaction_id}:{transaction_version}:{history_page}"
            handler = edit_option
        elif interaction.action == DraftAction.TX_SELECT_CATEGORY:
            legacy = f"e:cat:{interaction.object_id}"
            handler = edit_category_choice
        elif interaction.action == DraftAction.TX_SELECT_ACCOUNT:
            legacy = f"e:acct:{interaction.object_id}"
            handler = edit_account_choice
        elif interaction.action == DraftAction.TX_SELECT_DATE:
            date_key = (
                {0: "today", 1: "yesterday", 2: "custom"}.get(interaction.page)
                if interaction.page is not None
                else None
            )
            if date_key is None:
                await callback.answer("Кнопка даты повреждена", show_alert=True)
                return
            legacy = f"e:datepick:{date_key}"
            handler = edit_date_choice
        else:
            await callback.answer("Действие больше не поддерживается", show_alert=True)
            return
        legacy_callback = callback.model_copy(update={"data": legacy})
        validation_token = _validated_interaction.set(
            ValidatedDraftInteraction(interaction.draft_id, interaction.revision)
        )
        try:
            await handler(legacy_callback, finbot_update_id)  # type: ignore[operator]
        finally:
            _validated_interaction.reset(validation_token)

    @dp.callback_query()
    async def stale_callback(callback: CallbackQuery) -> None:
        await callback.answer("Эта кнопка устарела. Откройте меню или историю", show_alert=True)

    return dp


async def run(settings: Settings) -> None:
    bot = Bot(settings.telegram_bot_token)
    bot.session.middleware(ReliableDeliveryMiddleware())
    dispatcher = build_dispatcher(settings)
    # Keep one visible interface: the persistent reply keyboard. Slash commands still work manually.
    await bot.delete_my_commands()
    logger = logging.getLogger("finbot.lifecycle")
    logger.info("polling_started")
    try:
        await run_polling(bot, dispatcher)
    finally:
        logger.info("polling_stopped")
        await bot.session.close()
