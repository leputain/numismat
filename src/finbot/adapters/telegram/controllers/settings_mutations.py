from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.controllers.settings_queries import (
    SettingsCatalogTarget,
    SettingsInputDraftConflictError,
    SettingsMainSnapshot,
    SettingsQueryDraftLifecycle,
    SettingsQueryError,
    SettingsQueryReader,
    TelegramSettingsQueryReceipt,
    render_settings_receipt,
)
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.ui import TIMEZONES, settings_text_input_keyboard
from finbot.application.settings_mutations import (
    MAX_SETTINGS_VERSION,
    BeginAccountCreateCommand,
    BeginAccountRenameCommand,
    BeginCategoryCreateCommand,
    BeginCategoryRenameCommand,
    ChangeTimezoneCommand,
    SettingsInputIngressOperation,
    SettingsInputIngressResult,
)
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts
from finbot.application.use_cases.settings_mutations import (
    BeginSettingsInput,
    ChangeSettingsTimezone,
)
from finbot.domain.transactions import TransactionType


class InvalidSettingsMutationCallback(SettingsQueryError):
    """A settings mutation callback is malformed."""


class StaleSettingsTimezoneCallback(SettingsQueryError):
    """A timezone callback was rendered for another owner snapshot."""


@dataclass(frozen=True, slots=True)
class SettingsTimezoneTarget:
    timezone: str = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if self.timezone not in {item[0] for item in TIMEZONES}:
            raise ValueError("Settings timezone target is not supported")
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or not 1 <= self.expected_version <= MAX_SETTINGS_VERSION
        ):
            raise ValueError("Settings version is invalid")


def parse_settings_timezone_callback(data: str) -> SettingsTimezoneTarget:
    prefix = "s:timezone:"
    if type(data) is not str or not data.startswith(prefix):
        raise InvalidSettingsMutationCallback("Кнопка повреждена")
    try:
        encoded = data.encode("ascii")
        index_token, version_token = data[len(prefix) :].split(":", 1)
        if (
            len(encoded) > 64
            or not index_token.isascii()
            or not index_token.isdecimal()
            or str(int(index_token)) != index_token
            or not version_token.isascii()
            or not version_token.isdecimal()
            or str(int(version_token)) != version_token
        ):
            raise ValueError
        timezone = TIMEZONES[int(index_token)][0]
        return SettingsTimezoneTarget(timezone, int(version_token))
    except (UnicodeEncodeError, ValueError, IndexError) as error:
        raise InvalidSettingsMutationCallback("Кнопка повреждена") from error


@dataclass(frozen=True, slots=True)
class TelegramSettingsMutationContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class SettingsMutationSessionUseCases:
    begin_input: BeginSettingsInput = field(repr=False)
    change_timezone: ChangeSettingsTimezone = field(repr=False)


class SettingsMutationUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> SettingsMutationSessionUseCases: ...


type SettingsMutationReaderFactory = Callable[[TelegramMutationSession], SettingsQueryReader]
type SettingsMutationDraftFactory = Callable[[TelegramMutationSession], SettingsQueryDraftLifecycle]
type SettingsMutationReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        TelegramSettingsQueryReceipt,
    ],
    Awaitable[None],
]


def _render_input_receipt(
    result: SettingsInputIngressResult,
    *,
    message_id: int,
) -> TelegramSettingsQueryReceipt:
    draft = result.draft
    if result.operation is SettingsInputIngressOperation.ACCOUNT_CREATE:
        text = "<b>Новый счёт</b>\n\nВведите название, например «Наличные» или «Карта Мир»."
    elif result.operation is SettingsInputIngressOperation.ACCOUNT_RENAME:
        if result.account is None:  # pragma: no cover - result invariant
            raise RuntimeError("Settings account rename target is missing")
        text = (
            "<b>Переименовать счёт</b>\n\n"
            f"Сейчас: <b>{escape(result.account.name)}</b>\nВведите новое название."
        )
    elif result.operation is SettingsInputIngressOperation.CATEGORY_CREATE:
        title = "расходов" if draft.payload["kind"] == "expense" else "доходов"
        text = f"<b>Новая категория {title}</b>\n\nВведите короткое понятное название."
    else:
        if result.category is None:  # pragma: no cover - result invariant
            raise RuntimeError("Settings category rename target is missing")
        text = (
            "<b>Переименовать категорию</b>\n\n"
            f"Сейчас: <b>{escape(result.category.name)}</b>\nВведите новое название."
        )
    return TelegramSettingsQueryReceipt(
        text=text,
        reply_markup=settings_text_input_keyboard(draft.draft_id, draft.revision),
        message_id=message_id,
        draft_ref=draft.ref,
    )


class SettingsMutationController:
    """Run settings ingress/timezone mutations and queue receipts before commit."""

    __slots__ = (
        "_drafts",
        "_enqueue_receipt",
        "_executor",
        "_readers",
        "_use_cases",
    )

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: SettingsMutationUseCaseFactory,
        reader_factory: SettingsMutationReaderFactory,
        draft_factory: SettingsMutationDraftFactory,
        enqueue_receipt: SettingsMutationReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._readers = reader_factory
        self._drafts = draft_factory
        self._enqueue_receipt = enqueue_receipt

    async def _input(
        self,
        context: TelegramSettingsMutationContext,
        mutate: Callable[
            [TelegramMutationSession, UUID],
            Awaitable[SettingsInputIngressResult],
        ],
    ) -> TelegramSettingsQueryReceipt | None:
        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=lambda result: _render_input_receipt(
                result,
                message_id=context.message_id,
            ),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def account_create(
        self,
        context: TelegramSettingsMutationContext,
    ) -> TelegramSettingsQueryReceipt | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> SettingsInputIngressResult:
            return await self._use_cases(session).begin_input.account_create(
                BeginAccountCreateCommand(owner_id)
            )

        return await self._input(context, mutate)

    async def account_rename(
        self,
        context: TelegramSettingsMutationContext,
        target: SettingsCatalogTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> SettingsInputIngressResult:
            return await self._use_cases(session).begin_input.account_rename(
                BeginAccountRenameCommand(
                    owner_id,
                    target.entity_id,
                    target.expected_version,
                )
            )

        return await self._input(context, mutate)

    async def category_create(
        self,
        context: TelegramSettingsMutationContext,
        kind: TransactionType,
    ) -> TelegramSettingsQueryReceipt | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> SettingsInputIngressResult:
            return await self._use_cases(session).begin_input.category_create(
                BeginCategoryCreateCommand(owner_id, kind)
            )

        return await self._input(context, mutate)

    async def category_rename(
        self,
        context: TelegramSettingsMutationContext,
        target: SettingsCatalogTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> SettingsInputIngressResult:
            return await self._use_cases(session).begin_input.category_rename(
                BeginCategoryRenameCommand(
                    owner_id,
                    target.entity_id,
                    target.expected_version,
                )
            )

        return await self._input(context, mutate)

    async def timezone(
        self,
        context: TelegramSettingsMutationContext,
        target: SettingsTimezoneTarget,
    ) -> TelegramSettingsQueryReceipt | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> SettingsMainSnapshot:
            reader = self._readers(session)
            current = await GetOwnerSettings(reader)(owner_id)
            if current.settings_version != target.expected_version:
                raise StaleSettingsTimezoneCallback(
                    "Часовой пояс уже изменился. Обновите настройки"
                )
            owner = await self._use_cases(session).change_timezone.execute(
                ChangeTimezoneCommand(owner_id, target.expected_version, target.timezone)
            )
            active = await self._drafts(session).get_active(owner_id)
            if active is not None and active.state.startswith("settings_"):
                raise SettingsInputDraftConflictError(
                    "Сначала отмените текущий ввод кнопкой под формой"
                )
            return SettingsMainSnapshot(
                owner=owner,
                active_accounts=await ListAccounts(reader)(owner_id),
                active_draft=active,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=lambda result: render_settings_receipt(
                result,
                message_id=context.message_id,
            ),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt


__all__ = [
    "InvalidSettingsMutationCallback",
    "SettingsMutationController",
    "SettingsMutationSessionUseCases",
    "SettingsTimezoneTarget",
    "StaleSettingsTimezoneCallback",
    "TelegramSettingsMutationContext",
    "parse_settings_timezone_callback",
]
