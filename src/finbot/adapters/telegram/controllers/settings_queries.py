from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from typing import Protocol
from uuid import UUID

from aiogram.types import InlineKeyboardMarkup

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.presenters import HELP_TEXT
from finbot.adapters.telegram.ui import (
    Choice,
    append_resume_button,
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
    settings_timezones_keyboard,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.interactions import MAX_CALLBACK_BYTES, MAX_OBJECT_VERSION
from finbot.application.ports import CatalogReader, OwnerReader
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    ListAccounts,
    ListCategories,
)
from finbot.domain.transactions import TransactionType

MAX_TELEGRAM_TEXT_LENGTH = 4096


class SettingsQueryError(ValueError):
    """Safe user-facing failure raised by settings navigation."""


class InvalidSettingsQueryCallback(SettingsQueryError):
    """A settings callback is malformed or outside the supported bounds."""


class StaleSettingsQueryCallback(SettingsQueryError):
    """A settings callback targets an obsolete catalog snapshot."""


class SettingsInputDraftConflictError(SettingsQueryError):
    """Catalog navigation cannot replace an active settings text-input form."""


class DefaultAccountArchiveError(SettingsQueryError):
    """The current default account cannot be offered for archival."""


class SettingsQueryReader(CatalogReader, OwnerReader, Protocol):
    """Owner-scoped settings read capabilities."""


type SettingsQueryReaderFactory = Callable[[TelegramMutationSession], SettingsQueryReader]


class SettingsQueryDraftLifecycle(Protocol):
    """Minimum draft lifecycle needed by settings navigation."""

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None: ...

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot: ...


type SettingsQueryDraftFactory = Callable[[TelegramMutationSession], SettingsQueryDraftLifecycle]


@dataclass(frozen=True, slots=True)
class TelegramSettingsQueryContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class SettingsCatalogTarget:
    entity_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, UUID):
            raise TypeError("Catalog entity id must be a UUID")
        if isinstance(self.expected_version, bool) or not isinstance(self.expected_version, int):
            raise TypeError("Catalog version must be an integer")
        if not 1 <= self.expected_version <= MAX_OBJECT_VERSION:
            raise ValueError("Catalog version is outside callback bounds")


@dataclass(frozen=True, slots=True)
class TelegramSettingsQueryReceipt:
    """Render-complete settings response produced before the atomic commit."""

    text: str = field(repr=False)
    reply_markup: InlineKeyboardMarkup = field(repr=False)
    message_id: int | None = field(default=None, repr=False)
    parse_mode: str = field(default="HTML", repr=False)
    draft_ref: DraftRef | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise TypeError("Telegram settings receipt text must be a string")
        if not 1 <= len(self.text) <= MAX_TELEGRAM_TEXT_LENGTH:
            raise ValueError("Telegram settings receipt has an invalid text length")
        if not isinstance(self.reply_markup, InlineKeyboardMarkup):
            raise TypeError("Telegram settings receipt requires an inline keyboard")
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")
        if self.parse_mode != "HTML":
            raise ValueError("Telegram settings receipts require HTML parse mode")
        if self.draft_ref is not None and not isinstance(self.draft_ref, DraftRef):
            raise TypeError("Presented draft must be a DraftRef")


type SettingsQueryReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        TelegramSettingsQueryReceipt,
    ],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class SettingsMainSnapshot:
    """Render-complete, immutable settings-main projection shared by mutations."""

    owner: OwnerSnapshot = field(repr=False)
    active_accounts: tuple[AccountSnapshot, ...] = field(repr=False)
    active_draft: DraftSnapshot | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _AccountsPresentation:
    owner: OwnerSnapshot = field(repr=False)
    active: tuple[AccountSnapshot, ...] = field(repr=False)
    archived: tuple[AccountSnapshot, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AccountPresentation:
    owner: OwnerSnapshot = field(repr=False)
    account: AccountSnapshot = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CategoriesPresentation:
    active: tuple[CategorySnapshot, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CategoryListPresentation:
    kind: TransactionType
    active: tuple[CategorySnapshot, ...] = field(repr=False)
    archived: tuple[CategorySnapshot, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CategoryPresentation:
    category: CategorySnapshot = field(repr=False)


def _invalid_callback() -> InvalidSettingsQueryCallback:
    return InvalidSettingsQueryCallback("Кнопка повреждена")


def _callback_payload(data: str, prefix: str) -> str:
    if type(data) is not str or type(prefix) is not str or not prefix:
        raise _invalid_callback()
    try:
        encoded = data.encode("ascii")
    except UnicodeEncodeError as error:
        raise _invalid_callback() from error
    if len(encoded) > MAX_CALLBACK_BYTES or not data.startswith(prefix):
        raise _invalid_callback()
    payload = data[len(prefix) :]
    if not payload:
        raise _invalid_callback()
    return payload


def parse_settings_catalog_callback(data: str, *, prefix: str) -> SettingsCatalogTarget:
    """Decode an exact UUID/version target without leaking callback data in errors."""

    parts = _callback_payload(data, prefix).split(":")
    if len(parts) != 2:
        raise _invalid_callback()
    entity_token, version_token = parts
    if len(entity_token) != 36:
        raise _invalid_callback()
    try:
        entity_id = UUID(entity_token)
    except (ValueError, AttributeError) as error:
        raise _invalid_callback() from error
    if str(entity_id) != entity_token.lower():
        raise _invalid_callback()
    if not version_token or not version_token.isascii() or not version_token.isdecimal():
        raise _invalid_callback()
    version = int(version_token)
    if not 1 <= version <= MAX_OBJECT_VERSION:
        raise _invalid_callback()
    return SettingsCatalogTarget(entity_id, version)


def parse_settings_category_kind(data: str, *, prefix: str) -> TransactionType:
    """Decode the only two category kinds exposed by settings navigation."""

    try:
        return TransactionType(_callback_payload(data, prefix))
    except ValueError as error:
        raise _invalid_callback() from error


def _account_choice(account: AccountSnapshot) -> Choice:
    return Choice(account.account_id, account.name, version=account.version)


def _category_choice(category: CategorySnapshot) -> Choice:
    return Choice(
        category.category_id,
        category.name,
        category.emoji,
        category.version,
    )


def _settings_text(owner: OwnerSnapshot, account_name: str) -> str:
    return (
        "<b>Настройки</b>\n\n"
        f"💳 Основной счёт: <b>{escape(account_name)}</b>\n"
        f"🕒 Часовой пояс: <b>{escape(owner.timezone)}</b>\n"
        f"💱 Валюта: <b>{escape(owner.base_currency)}</b>\n\n"
        "Перед записью любой операции Finbot показывает карточку проверки."
    )


def render_settings_receipt(
    value: SettingsMainSnapshot,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    default_account = next(
        (
            account
            for account in value.active_accounts
            if account.account_id == value.owner.default_account_id
        ),
        None,
    )
    account_name = default_account.name if default_account is not None else "не выбран"
    draft = value.active_draft
    keyboard = settings_keyboard(
        value.owner.fast_mode,
        draft_id=draft.draft_id if draft is not None else None,
        revision=draft.revision if draft is not None else None,
    )
    keyboard = append_resume_button(
        keyboard,
        has_draft=draft is not None,
        draft_id=draft.draft_id if draft is not None else None,
        revision=draft.revision if draft is not None else None,
    )
    return TelegramSettingsQueryReceipt(
        text=_settings_text(value.owner, account_name),
        reply_markup=keyboard,
        message_id=message_id,
        draft_ref=draft.ref if draft is not None else None,
    )


def render_accounts_receipt(
    value: _AccountsPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    return TelegramSettingsQueryReceipt(
        text=(
            "<b>Счета</b>\n\n"
            "⭐ — основной счёт для операций без <code>@счёт</code>.\n"
            "Откройте счёт, чтобы переименовать его или изменить основной."
        ),
        reply_markup=settings_accounts_keyboard(
            [_account_choice(account) for account in value.active],
            value.owner.default_account_id,
            len(value.archived),
        ),
        message_id=message_id,
    )


def render_account_receipt(
    value: _AccountPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    account = value.account
    is_default = account.account_id == value.owner.default_account_id
    status = "⭐ Основной счёт" if is_default else "Активный счёт"
    return TelegramSettingsQueryReceipt(
        text=(
            f"<b>💳 {escape(account.name)}</b>\n\n"
            f"{status}\n"
            f"Валюта: <b>{escape(account.currency)}</b>\n\n"
            "Архивация не удаляет операции с этого счёта."
        ),
        reply_markup=settings_account_keyboard(
            account.account_id,
            account.version,
            is_default=is_default,
        ),
        message_id=message_id,
    )


def render_archived_accounts_receipt(
    accounts: tuple[AccountSnapshot, ...],
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    return TelegramSettingsQueryReceipt(
        text="<b>Архив счетов</b>\n\nНажмите на счёт, чтобы восстановить его.",
        reply_markup=settings_archived_accounts_keyboard(
            [_account_choice(account) for account in accounts]
        ),
        message_id=message_id,
    )


def render_account_archive_confirmation_receipt(
    value: _AccountPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    account = value.account
    return TelegramSettingsQueryReceipt(
        text=(
            f"<b>Архивировать «{escape(account.name)}»?</b>\n\n"
            "Счёт исчезнет из нового ввода, но останется в истории и отчётах."
        ),
        reply_markup=settings_account_archive_keyboard(account.account_id, account.version),
        message_id=message_id,
    )


def render_categories_receipt(
    value: _CategoriesPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    expense_count = sum(category.kind is TransactionType.EXPENSE for category in value.active)
    income_count = sum(category.kind is TransactionType.INCOME for category in value.active)
    return TelegramSettingsQueryReceipt(
        text=(
            "<b>Категории</b>\n\n"
            "Категории доходов и расходов разделены. "
            "Архивные остаются в старых операциях."
        ),
        reply_markup=settings_categories_keyboard(expense_count, income_count),
        message_id=message_id,
    )


def render_category_list_receipt(
    value: _CategoryListPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    kind = value.kind.value
    title = "Категории расходов" if value.kind is TransactionType.EXPENSE else "Категории доходов"
    return TelegramSettingsQueryReceipt(
        text=f"<b>{title}</b>\n\nОткройте категорию для переименования или архивации.",
        reply_markup=settings_category_list_keyboard(
            [_category_choice(category) for category in value.active],
            kind,
            len(value.archived),
        ),
        message_id=message_id,
    )


def render_category_receipt(
    value: _CategoryPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    category = value.category
    kind_label = "Расход" if category.kind is TransactionType.EXPENSE else "Доход"
    emoji = escape(category.emoji or "▫️")
    return TelegramSettingsQueryReceipt(
        text=(
            f"<b>{emoji} {escape(category.name)}</b>\n\n"
            f"Тип: <b>{kind_label}</b>\n\n"
            "Архивация скрывает категорию при новом вводе, "
            "но сохраняет её в истории."
        ),
        reply_markup=settings_category_keyboard(
            category.category_id,
            category.kind.value,
            category.version,
        ),
        message_id=message_id,
    )


def render_archived_categories_receipt(
    value: _CategoryListPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    return TelegramSettingsQueryReceipt(
        text=("<b>Архив категорий</b>\n\nНажмите на категорию, чтобы восстановить её."),
        reply_markup=settings_archived_categories_keyboard(
            [_category_choice(category) for category in value.archived],
            value.kind.value,
        ),
        message_id=message_id,
    )


def render_category_archive_confirmation_receipt(
    value: _CategoryPresentation,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    category = value.category
    return TelegramSettingsQueryReceipt(
        text=(
            f"<b>Архивировать «{escape(category.name)}»?</b>\n\n"
            "Она исчезнет из нового ввода, но останется в истории и отчётах."
        ),
        reply_markup=settings_category_archive_keyboard(
            category.category_id,
            category.version,
        ),
        message_id=message_id,
    )


def render_timezones_receipt(
    owner: OwnerSnapshot,
    *,
    message_id: int | None,
) -> TelegramSettingsQueryReceipt:
    return TelegramSettingsQueryReceipt(
        text=("<b>Часовой пояс</b>\n\nОт него зависят «сегодня», отчёты и даты операций."),
        reply_markup=settings_timezones_keyboard(owner.timezone, owner.settings_version),
        message_id=message_id,
    )


class SettingsQueryController:
    """Run settings reads, draft suspension and receipt enqueue atomically."""

    __slots__ = (
        "_draft_factory",
        "_enqueue_receipt",
        "_executor",
        "_reader_factory",
    )

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        reader_factory: SettingsQueryReaderFactory,
        draft_factory: SettingsQueryDraftFactory,
        enqueue_receipt: SettingsQueryReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._reader_factory = reader_factory
        self._draft_factory = draft_factory
        self._enqueue_receipt = enqueue_receipt

    async def _execute[QueryValue](
        self,
        context: TelegramSettingsQueryContext,
        *,
        query: Callable[[TelegramMutationSession, UUID], Awaitable[QueryValue]],
        render: Callable[[QueryValue], TelegramSettingsQueryReceipt],
    ) -> TelegramSettingsQueryReceipt | None:
        execution = await self._executor.execute(
            context.request,
            mutate=query,
            build_receipt=render,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def _reject_settings_input_draft(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
    ) -> None:
        active = await self._draft_factory(session).get_active(owner_id)
        if active is not None and active.state.startswith("settings_"):
            raise SettingsInputDraftConflictError(
                "Сначала отмените текущий ввод кнопкой под формой"
            )

    async def settings_main(
        self,
        context: TelegramSettingsQueryContext,
        *,
        suspend_active_draft: bool,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> SettingsMainSnapshot:
            reader = self._reader_factory(session)
            owner = await GetOwnerSettings(reader)(owner_id)
            accounts = await ListAccounts(reader)(owner_id)
            drafts = self._draft_factory(session)
            active = await drafts.get_active(owner_id)
            if suspend_active_draft and active is not None and not active.suspended:
                active = await drafts.suspend(owner_id, active.ref)
            return SettingsMainSnapshot(owner, accounts, active)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_settings_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def accounts(
        self,
        context: TelegramSettingsQueryContext,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _AccountsPresentation:
            await self._reject_settings_input_draft(session, owner_id)
            reader = self._reader_factory(session)
            return _AccountsPresentation(
                owner=await GetOwnerSettings(reader)(owner_id),
                active=await ListAccounts(reader)(owner_id, archived=False),
                archived=await ListAccounts(reader)(owner_id, archived=True),
            )

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_accounts_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def account(
        self,
        context: TelegramSettingsQueryContext,
        target: SettingsCatalogTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _AccountPresentation:
            await self._reject_settings_input_draft(session, owner_id)
            reader = self._reader_factory(session)
            owner = await GetOwnerSettings(reader)(owner_id)
            accounts = await ListAccounts(reader)(owner_id, archived=False)
            account = next(
                (
                    item
                    for item in accounts
                    if item.account_id == target.entity_id
                    and item.version == target.expected_version
                ),
                None,
            )
            if account is None:
                raise StaleSettingsQueryCallback("Счёт изменился. Обновите список")
            return _AccountPresentation(owner, account)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_account_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def archived_accounts(
        self,
        context: TelegramSettingsQueryContext,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> tuple[AccountSnapshot, ...]:
            return await ListAccounts(self._reader_factory(session))(
                owner_id,
                archived=True,
            )

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_archived_accounts_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def account_archive_confirmation(
        self,
        context: TelegramSettingsQueryContext,
        target: SettingsCatalogTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _AccountPresentation:
            reader = self._reader_factory(session)
            owner = await GetOwnerSettings(reader)(owner_id)
            accounts = await ListAccounts(reader)(owner_id, archived=False)
            account = next(
                (
                    item
                    for item in accounts
                    if item.account_id == target.entity_id
                    and item.version == target.expected_version
                ),
                None,
            )
            if account is None:
                raise StaleSettingsQueryCallback("Счёт изменился. Обновите список")
            if account.account_id == owner.default_account_id:
                raise DefaultAccountArchiveError("Сначала выберите другой основной счёт")
            return _AccountPresentation(owner, account)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_account_archive_confirmation_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def categories(
        self,
        context: TelegramSettingsQueryContext,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _CategoriesPresentation:
            await self._reject_settings_input_draft(session, owner_id)
            return _CategoriesPresentation(
                await ListCategories(self._reader_factory(session))(
                    owner_id,
                    archived=False,
                )
            )

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_categories_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def category_list(
        self,
        context: TelegramSettingsQueryContext,
        kind: TransactionType,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _CategoryListPresentation:
            await self._reject_settings_input_draft(session, owner_id)
            reader = self._reader_factory(session)
            return _CategoryListPresentation(
                kind=kind,
                active=await ListCategories(reader)(
                    owner_id,
                    kind=kind,
                    archived=False,
                ),
                archived=await ListCategories(reader)(
                    owner_id,
                    kind=kind,
                    archived=True,
                ),
            )

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_category_list_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def category(
        self,
        context: TelegramSettingsQueryContext,
        target: SettingsCatalogTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _CategoryPresentation:
            await self._reject_settings_input_draft(session, owner_id)
            categories = await ListCategories(self._reader_factory(session))(
                owner_id,
                archived=False,
            )
            category = next(
                (
                    item
                    for item in categories
                    if item.category_id == target.entity_id
                    and item.version == target.expected_version
                ),
                None,
            )
            if category is None:
                raise StaleSettingsQueryCallback("Категория изменилась. Обновите список")
            return _CategoryPresentation(category)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_category_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def archived_categories(
        self,
        context: TelegramSettingsQueryContext,
        kind: TransactionType,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _CategoryListPresentation:
            archived = await ListCategories(self._reader_factory(session))(
                owner_id,
                kind=kind,
                archived=True,
            )
            return _CategoryListPresentation(kind=kind, active=(), archived=archived)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_archived_categories_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def category_archive_confirmation(
        self,
        context: TelegramSettingsQueryContext,
        target: SettingsCatalogTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _CategoryPresentation:
            categories = await ListCategories(self._reader_factory(session))(
                owner_id,
                archived=False,
            )
            category = next(
                (
                    item
                    for item in categories
                    if item.category_id == target.entity_id
                    and item.version == target.expected_version
                ),
                None,
            )
            if category is None:
                raise StaleSettingsQueryCallback("Категория изменилась. Обновите список")
            return _CategoryPresentation(category)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_category_archive_confirmation_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def timezones(
        self,
        context: TelegramSettingsQueryContext,
    ) -> TelegramSettingsQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> OwnerSnapshot:
            await self._reject_settings_input_draft(session, owner_id)
            return await GetOwnerSettings(self._reader_factory(session))(owner_id)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_timezones_receipt(
                value,
                message_id=context.message_id,
            ),
        )

    async def help(
        self,
        context: TelegramSettingsQueryContext,
    ) -> TelegramSettingsQueryReceipt | None:
        """Render static help through the deduplicated durable receipt envelope."""

        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> None:
            del session, owner_id

        return await self._execute(
            context,
            query=query,
            render=lambda _value: TelegramSettingsQueryReceipt(
                text=HELP_TEXT,
                reply_markup=settings_help_keyboard(),
                message_id=context.message_id,
            ),
        )
